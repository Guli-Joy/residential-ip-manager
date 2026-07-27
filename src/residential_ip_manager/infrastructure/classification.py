from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from residential_ip_manager.config import default_data_dir
from residential_ip_manager.domain.models import (
    PurityGrade,
    ResidentialEvidence,
    VpnNode,
)

IP_API_BATCH_URL = (
    "http://ip-api.com/batch?fields="
    "status,message,query,country,countryCode,regionName,city,isp,org,as,asname,"
    "proxy,hosting,mobile"
)
IPAPI_IS_URL = "https://api.ipapi.is/"
_USER_AGENT = "residential-ip-manager/0.1"
_CACHE_VERSION = 2
_LEGACY_CACHE_VERSION = 1
_DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60
_DEFAULT_NEGATIVE_CACHE_TTL_SECONDS = 5 * 60
_DEFAULT_CACHE_PATH = default_data_dir() / "classification-cache.json"
_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_LOCKS: dict[Path, threading.RLock] = {}

HOME_RDNS_KEYWORDS = (
    "commufa.jp",
    "bbtec.net",
    "mesh.ad.jp",
    "so-net.ne.jp",
    "ocn.ne.jp",
    "bbiq.jp",
    "gctv.ne.jp",
    "dti.ne.jp",
    "myaisfibre.com",
    "eonet.ne.jp",
    "au-net.ne.jp",
    "plala.or.jp",
    "home.ne.jp",
    "hikari",
    "ftth",
    "flets",
)

STRICT_HOME_ALLOW_KEYWORDS = (
    "ais fibre",
    "ais-fibre",
    "biglobe",
    "cable one",
    "chubu telecommunications",
    "cnci",
    "comcast cable",
    "community network center",
    "k-opticom",
    "kddi",
    "korea telecom",
    "kornet",
    "lg powercomm",
    "ntt communications",
    "ntt docomo",
    "open computer network",
    "optage",
    "qtnet",
    "rostelecom",
    "sk broadband",
    "softbank",
    "sony network",
    "stnet",
    "tokai",
    "xpeed",
)

STRICT_HOME_DENY_KEYWORDS = (
    "arteria",
    "backbone",
    "cloud",
    "colo",
    "colocation",
    "data center",
    "datacenter",
    "dacom-pubnetplus",
    "digital services",
    "hosting",
    "internet initiative japan",
    "marubeni access",
    "proxy",
    "pubnetplus",
    "retelit",
    "rikei",
    "server",
    "servers",
    "sheremet",
    "transtel",
    "transit",
    "vectant",
    "vps",
)

RdnsResolver = Callable[[str], Awaitable[str]]


@dataclass(slots=True)
class _Lookup:
    succeeded: bool
    data: dict[str, Any]
    error: str = ""
    cached_at: float | None = None


def _cache_lock(path: Path) -> threading.RLock:
    normalized = path.resolve()
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(normalized, threading.RLock())


def _read_cache_unlocked(
    path: Path,
    *,
    ttl_seconds: float,
    negative_ttl_seconds: float,
    now: float,
) -> dict[str, dict[str, _Lookup]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    cache_version = payload.get("version")
    if (
        not isinstance(cache_version, int)
        or isinstance(cache_version, bool)
        or cache_version not in {_LEGACY_CACHE_VERSION, _CACHE_VERSION}
    ):
        return {}
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, dict):
        return {}

    entries: dict[str, dict[str, _Lookup]] = {}
    for ip, raw_entry in raw_entries.items():
        if not isinstance(ip, str) or not isinstance(raw_entry, dict):
            continue
        providers: dict[str, _Lookup] = {}
        providers_to_read = (
            ("ipapi_is", "reverse_dns")
            if cache_version == _LEGACY_CACHE_VERSION
            else ("ip_api", "ipapi_is", "reverse_dns")
        )
        for provider in providers_to_read:
            record = raw_entry.get(provider)
            if not isinstance(record, dict) or not isinstance(record.get("succeeded"), bool):
                continue
            succeeded = record["succeeded"]
            cached_at = record.get("cached_at")
            data = record.get("data")
            if (
                not isinstance(cached_at, (int, float))
                or isinstance(cached_at, bool)
                or not isinstance(data, dict)
            ):
                continue
            age = now - float(cached_at)
            effective_ttl = ttl_seconds if succeeded else negative_ttl_seconds
            if age < -300 or age > effective_ttl:
                continue
            if succeeded and provider == "ip_api" and (
                data.get("status") != "success" or str(data.get("query") or "") != ip
            ):
                continue
            if (
                succeeded
                and provider == "reverse_dns"
                and not isinstance(data.get("hostname"), str)
            ):
                continue
            providers[provider] = _Lookup(
                succeeded,
                data,
                str(record.get("error") or ""),
                cached_at=float(cached_at),
            )
        if providers:
            entries[ip] = providers
    return entries


def _read_cache(
    path: Path,
    ttl_seconds: float,
    negative_ttl_seconds: float,
) -> dict[str, dict[str, _Lookup]]:
    with _cache_lock(path):
        return _read_cache_unlocked(
            path,
            ttl_seconds=ttl_seconds,
            negative_ttl_seconds=negative_ttl_seconds,
            now=time.time(),
        )


def _write_cache(
    path: Path,
    ttl_seconds: float,
    negative_ttl_seconds: float,
    updates: dict[str, dict[str, _Lookup]],
) -> None:
    if not updates:
        return
    lock = _cache_lock(path)
    with lock:
        now = time.time()
        entries = _read_cache_unlocked(
            path,
            ttl_seconds=ttl_seconds,
            negative_ttl_seconds=negative_ttl_seconds,
            now=now,
        )
        for ip, providers in updates.items():
            target = entries.setdefault(ip, {})
            for provider, lookup in providers.items():
                target[provider] = lookup

        serialized_entries: dict[str, dict[str, dict[str, Any]]] = {}
        for ip, providers in entries.items():
            serialized_entries[ip] = {
                provider: {
                    "succeeded": lookup.succeeded,
                    "cached_at": lookup.cached_at if lookup.cached_at is not None else now,
                    "data": lookup.data,
                    "error": lookup.error,
                }
                for provider, lookup in providers.items()
            }
        payload = {
            "version": _CACHE_VERSION,
            "updated_at": now,
            "entries": serialized_entries,
        }
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        except (OSError, TypeError, ValueError):
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _ip_api_is_clean(lookup: _Lookup) -> bool:
    required_flags = {"proxy", "hosting", "mobile"}
    return (
        lookup.succeeded
        and required_flags.issubset(lookup.data)
        and not any(bool(lookup.data.get(flag)) for flag in required_flags)
    )


def _ip_api_identity(lookup: _Lookup) -> str:
    return " ".join(
        str(lookup.data.get(field) or "") for field in ("isp", "org", "as", "asname")
    ).lower()


def _passes_secondary_prefilter(lookup: _Lookup) -> bool:
    if not _ip_api_is_clean(lookup):
        return False
    identity = _ip_api_identity(lookup)
    denied = any(keyword in identity for keyword in STRICT_HOME_DENY_KEYWORDS)
    allowed = any(keyword in identity for keyword in STRICT_HOME_ALLOW_KEYWORDS)
    return allowed and not denied


async def _system_rdns_resolver(ip: str) -> str:
    loop = asyncio.get_running_loop()
    sockaddr: tuple[Any, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    host, _service = await loop.getnameinfo(sockaddr, socket.NI_NAMEREQD)
    return host.rstrip(".").lower()


class StrictResidentialClassifier:
    """Conservative classifier: unknown provider results never become strict home."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        proxy_url: str | None = None,
        timeout_seconds: float = 10.0,
        max_concurrency: int = 8,
        rdns_resolver: RdnsResolver | None = None,
        cache_path: Path | None = None,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        negative_cache_ttl_seconds: float = _DEFAULT_NEGATIVE_CACHE_TTL_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        if negative_cache_ttl_seconds <= 0:
            raise ValueError("negative_cache_ttl_seconds must be positive")
        self._client = client
        self._proxy_url = proxy_url
        self._timeout = timeout_seconds
        self._max_concurrency = max_concurrency
        self._rdns_resolver = rdns_resolver or _system_rdns_resolver
        self._cache_path = Path(cache_path or _DEFAULT_CACHE_PATH).expanduser().resolve()
        self._cache_ttl = cache_ttl_seconds
        self._negative_cache_ttl = negative_cache_ttl_seconds

    async def classify(self, nodes: Sequence[VpnNode]) -> list[VpnNode]:
        result = list(nodes)
        if not result:
            return result

        ips = list(dict.fromkeys(node.ip for node in result))
        if self._client is None:
            async with AsyncExitStack() as stack:
                clients: list[httpx.AsyncClient] = []
                if self._proxy_url:
                    clients.append(
                        await stack.enter_async_context(
                            httpx.AsyncClient(
                                headers={"User-Agent": _USER_AGENT},
                                timeout=self._timeout,
                                proxy=self._proxy_url,
                                trust_env=False,
                            )
                        )
                    )
                clients.append(
                    await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers={"User-Agent": _USER_AGENT},
                            timeout=self._timeout,
                            trust_env=False,
                        )
                    )
                )
                lookups = await self._collect_lookups(clients, ips)
        else:
            lookups = await self._collect_lookups([self._client], ips)

        for node in result:
            ip_api, ipapi_is, rdns = lookups[node.ip]
            self._apply_result(node, ip_api, ipapi_is, rdns)
        return result

    async def _collect_lookups(
        self,
        clients: Sequence[httpx.AsyncClient],
        ips: list[str],
    ) -> dict[str, tuple[_Lookup, _Lookup, _Lookup]]:
        cache = await asyncio.to_thread(
            _read_cache,
            self._cache_path,
            self._cache_ttl,
            self._negative_cache_ttl,
        )
        if self._proxy_url:
            for providers in cache.values():
                for provider in ("ip_api", "ipapi_is"):
                    lookup = providers.get(provider)
                    if lookup is not None and not lookup.succeeded:
                        providers.pop(provider)
        for providers in cache.values():
            ip_api = providers.get("ip_api")
            if ip_api is not None and ip_api.succeeded and not ip_api.data.get("countryCode"):
                providers.pop("ip_api")
        missing_ip_api = [ip for ip in ips if cache.get(ip, {}).get("ip_api") is None]
        queried_ip_api = await self._query_ip_api(clients, missing_ip_api)
        ip_api_results: dict[str, _Lookup] = {}
        cache_updates: dict[str, dict[str, _Lookup]] = {}
        for ip in ips:
            cached_ip_api = cache.get(ip, {}).get("ip_api")
            result = cached_ip_api if cached_ip_api is not None else queried_ip_api[ip]
            ip_api_results[ip] = result
            if cached_ip_api is None:
                cache_updates[ip] = {"ip_api": result}

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def secondary(
            ip: str,
        ) -> tuple[str, _Lookup, _Lookup, dict[str, _Lookup]]:
            cached = cache.get(ip, {})
            cached_ipapi = cached.get("ipapi_is")
            cached_rdns = cached.get("reverse_dns")
            async with semaphore:
                ipapi_result, rdns_result = await asyncio.gather(
                    self._cached_or_query(
                        cached_ipapi,
                        lambda: self._query_ipapi_is(clients, ip),
                    ),
                    self._cached_or_query(cached_rdns, lambda: self._query_rdns(ip)),
                )
            updates: dict[str, _Lookup] = {}
            if cached_ipapi is None:
                updates["ipapi_is"] = ipapi_result
            if cached_rdns is None:
                updates["reverse_dns"] = rdns_result
            return ip, ipapi_result, rdns_result, updates

        lookups: dict[str, tuple[_Lookup, _Lookup, _Lookup]] = {}
        candidates: list[str] = []
        for ip in ips:
            ip_api = ip_api_results[ip]
            if _passes_secondary_prefilter(ip_api):
                candidates.append(ip)
                continue
            skipped = _Lookup(False, {}, "skipped by ip-api residential prefilter")
            lookups[ip] = (ip_api, skipped, skipped)

        secondary_results = await asyncio.gather(*(secondary(ip) for ip in candidates))
        for ip, ipapi_result, rdns_result, updates in secondary_results:
            lookups[ip] = (ip_api_results[ip], ipapi_result, rdns_result)
            if updates:
                cache_updates.setdefault(ip, {}).update(updates)
        await asyncio.to_thread(
            _write_cache,
            self._cache_path,
            self._cache_ttl,
            self._negative_cache_ttl,
            cache_updates,
        )
        return lookups

    @staticmethod
    async def _cached_or_query(
        cached: _Lookup | None,
        query: Callable[[], Awaitable[_Lookup]],
    ) -> _Lookup:
        if cached is not None:
            return cached
        return await query()

    async def _query_ip_api(
        self,
        clients: Sequence[httpx.AsyncClient],
        ips: list[str],
    ) -> dict[str, _Lookup]:
        results = {
            ip: _Lookup(False, {}, "ip-api did not return this address") for ip in ips
        }
        for offset in range(0, len(ips), 100):
            chunk = ips[offset : offset + 100]
            payload: Any = None
            errors: list[str] = []
            for client in clients:
                try:
                    response = await client.post(
                        IP_API_BATCH_URL,
                        json=chunk,
                        timeout=self._timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        raise ValueError("ip-api returned a non-list response")
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(str(exc))
                    continue
                break
            if not isinstance(payload, list):
                error = "ip-api request failed: " + "; ".join(errors)
                for ip in chunk:
                    results[ip] = _Lookup(False, {}, error)
                continue

            for item in payload:
                if not isinstance(item, dict):
                    continue
                ip = str(item.get("query") or "")
                if ip not in results:
                    continue
                if item.get("status") != "success":
                    results[ip] = _Lookup(False, item, str(item.get("message") or "failed"))
                else:
                    results[ip] = _Lookup(True, item)
        return results

    async def _query_ipapi_is(
        self,
        clients: Sequence[httpx.AsyncClient],
        ip: str,
    ) -> _Lookup:
        errors: list[str] = []
        for client in clients:
            try:
                response = await client.get(
                    IPAPI_IS_URL,
                    params={"q": ip},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("ipapi.is returned a non-object response")
                return _Lookup(True, payload)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(str(exc))
        return _Lookup(False, {}, "ipapi.is request failed: " + "; ".join(errors))

    async def _query_rdns(self, ip: str) -> _Lookup:
        try:
            hostname = await asyncio.wait_for(self._rdns_resolver(ip), timeout=self._timeout)
        except (TimeoutError, OSError, socket.gaierror) as exc:
            return _Lookup(False, {}, f"reverse DNS lookup failed: {exc}")
        if not hostname:
            return _Lookup(False, {}, "reverse DNS returned an empty hostname")
        return _Lookup(True, {"hostname": hostname.lower().rstrip(".")})

    @staticmethod
    def _apply_result(
        node: VpnNode,
        ip_api: _Lookup,
        ipapi_is: _Lookup,
        rdns: _Lookup,
    ) -> None:
        provider_names = {"ip-api", "ipapi.is", "reverse-dns", "strict-home-policy"}
        node.evidence = [item for item in node.evidence if item.provider not in provider_names]

        ip_api_clean = _ip_api_is_clean(ip_api)
        if ip_api.succeeded:
            country = str(ip_api.data.get("country") or "").strip()
            country_code = str(ip_api.data.get("countryCode") or "").strip().upper()
            if country:
                node.country = country
            if country_code:
                node.country_code = country_code
            node.isp = str(ip_api.data.get("isp") or ip_api.data.get("org") or "")
            node.city = str(ip_api.data.get("city") or "")
            node.asn = str(ip_api.data.get("as") or ip_api.data.get("asname") or "")
        node.evidence.append(
            ResidentialEvidence(
                provider="ip-api",
                passed=ip_api_clean,
                summary=(
                    "非机房、非代理、非移动网络"
                    if ip_api_clean
                    else ip_api.error or "ip-api 标记为机房/代理/移动网络"
                ),
            )
        )

        company = ipapi_is.data.get("company")
        asn_data = ipapi_is.data.get("asn")
        company_data = company if isinstance(company, dict) else {}
        asn = asn_data if isinstance(asn_data, dict) else {}
        ipapi_flags = {
            "is_datacenter",
            "is_proxy",
            "is_vpn",
            "is_abuser",
            "is_tor",
            "is_mobile",
        }
        isp_type = str(company_data.get("type") or asn.get("type") or "").lower() == "isp"
        ipapi_clean = (
            ipapi_is.succeeded
            and ipapi_flags.issubset(ipapi_is.data)
            and not any(bool(ipapi_is.data.get(flag)) for flag in ipapi_flags)
            and isp_type
        )
        node.evidence.append(
            ResidentialEvidence(
                provider="ipapi.is",
                passed=ipapi_clean,
                summary=(
                    "ISP 类型且无机房/VPN/代理/滥用标记"
                    if ipapi_clean
                    else ipapi_is.error or "ipapi.is 不满足严格 ISP 条件"
                ),
            )
        )

        hostname = str(rdns.data.get("hostname") or "") if rdns.succeeded else ""
        node.reverse_dns = hostname
        rdns_home = bool(hostname) and any(key in hostname for key in HOME_RDNS_KEYWORDS)
        node.evidence.append(
            ResidentialEvidence(
                provider="reverse-dns",
                passed=rdns_home,
                summary=(
                    f"家宽 rDNS: {hostname}"
                    if rdns_home
                    else rdns.error or f"rDNS 无家宽特征: {hostname or '-'}"
                ),
            )
        )

        identity = " ".join(
            str(value or "")
            for value in (
                ip_api.data.get("isp"),
                ip_api.data.get("org"),
                ip_api.data.get("as"),
                ip_api.data.get("asname"),
                company_data.get("name"),
                company_data.get("netname"),
                asn.get("org"),
                hostname,
            )
        ).lower()
        denied = any(keyword in identity for keyword in STRICT_HOME_DENY_KEYWORDS)
        allowed = any(keyword in identity for keyword in STRICT_HOME_ALLOW_KEYWORDS)
        policy_passed = allowed and not denied
        node.evidence.append(
            ResidentialEvidence(
                provider="strict-home-policy",
                passed=policy_passed,
                summary=(
                    "命中家庭宽带运营商且无 IDC 关键词"
                    if policy_passed
                    else "命中 IDC 关键词" if denied else "未命中可确认的家宽运营商"
                ),
            )
        )

        score = 0
        score += 25 if ip_api_clean else 0
        score += 30 if ipapi_clean else 0
        score += 25 if rdns_home else 0
        score += 20 if policy_passed else 0
        node.purity_score = score

        positively_rejected = denied or (
            (ip_api.succeeded and not ip_api_clean)
            or (ipapi_is.succeeded and not ipapi_clean)
        )
        if ip_api_clean and ipapi_clean and policy_passed:
            node.purity_grade = PurityGrade.STRICT_HOME
        elif positively_rejected:
            node.purity_grade = PurityGrade.REJECTED
        else:
            node.purity_grade = PurityGrade.CANDIDATE
