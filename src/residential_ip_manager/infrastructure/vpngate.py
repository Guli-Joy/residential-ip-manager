from __future__ import annotations

import base64
import binascii
import csv
import io
import ipaddress
import re
from dataclasses import dataclass, field

import httpx

from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import VpnNode

DEFAULT_VPNGATE_API_URL = "https://www.vpngate.net/api/iphone/"
DEFAULT_USER_AGENT = "residential-ip-manager/0.1"
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_DIRECTIVE = re.compile(r"^\s*(?:--)?(?P<name>[A-Za-z0-9_-]+)(?:\s+(?P<value>.*))?$")


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_protocol(value: str) -> str:
    normalized = value.strip().lower()
    if "tcp" in normalized:
        return "tcp"
    if "udp" in normalized:
        return "udp"
    return "unknown"


def _decode_config(encoded: str) -> str:
    compact = "".join(encoded.split())
    if not compact:
        raise ValueError("empty OpenVPN config")
    compact += "=" * (-len(compact) % 4)
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 OpenVPN config") from exc
    return raw.decode("utf-8-sig", errors="replace")


def _parse_remote(config_text: str) -> tuple[str, int, str]:
    protocol = "unknown"
    remotes: list[tuple[str, int, str]] = []

    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        match = _DIRECTIVE.match(line)
        if match is None:
            continue
        name = match.group("name").lower()
        values = (match.group("value") or "").split()
        if name == "proto" and values:
            protocol = _normalize_protocol(values[0])
        elif name == "remote" and len(values) >= 2:
            host = values[0].strip("\"'")
            port = _safe_int(values[1])
            remote_protocol = _normalize_protocol(values[2]) if len(values) >= 3 else ""
            remotes.append((host, port, remote_protocol))

    if not remotes:
        return "", 0, protocol
    host, port, remote_protocol = remotes[0]
    return host, port, remote_protocol or protocol


def _normalize_row(row: dict[str | None, str | list[str] | None]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        clean_key = key.lstrip("\ufeff#").strip()
        if isinstance(value, list):
            normalized[clean_key] = ",".join(value).strip()
        else:
            normalized[clean_key] = (value or "").strip()
    return normalized


def _csv_payload(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lstrip("\ufeff").startswith(("#HostName,", "HostName,"))
        ),
        None,
    )
    if header_index is None:
        return ""

    header = lines[header_index].lstrip("\ufeff")
    if header.startswith("#"):
        header = header[1:]
    data_lines = [header]
    for line in lines[header_index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", "#")):
            continue
        data_lines.append(line)
    return "\n".join(data_lines)


def _row_to_node(row: dict[str, str]) -> VpnNode | None:
    ip_text = row.get("IP", "")
    encoded_config = row.get("OpenVPN_ConfigData_Base64", "")
    if not ip_text or not encoded_config:
        return None

    try:
        public_ip = ipaddress.ip_address(ip_text)
        if not public_ip.is_global:
            return None
        config_text = _decode_config(encoded_config)
        remote_host, remote_port, protocol = _parse_remote(config_text)
        remote_ip = ipaddress.ip_address(remote_host)
    except ValueError:
        return None

    # The public API row is the trust anchor. Reject configs that redirect elsewhere.
    if remote_ip != public_ip or not remote_ip.is_global:
        return None
    if not 1 <= remote_port <= 65535 or protocol not in {"tcp", "udp"}:
        return None

    country_code = row.get("CountryShort", "").upper() or "XX"
    raw_id = f"{country_code}_{public_ip}_{remote_port}_{protocol}"
    node_id = _ID_UNSAFE.sub("_", raw_id).strip("_")
    return VpnNode(
        id=node_id,
        ip=str(public_ip),
        remote_host=str(remote_ip),
        remote_port=remote_port,
        protocol=protocol,
        country_code=country_code,
        country=row.get("CountryLong", ""),
        config_text=config_text,
        score=_safe_int(row.get("Score")),
        advertised_ping_ms=_safe_int(row.get("Ping")),
        speed_bps=_safe_int(row.get("Speed")),
        sessions=_safe_int(row.get("NumVpnSessions")),
    )


def parse_vpngate_csv(text: str) -> list[VpnNode]:
    """Parse VPNGate's comment-wrapped CSV and discard unsafe or malformed rows."""

    payload = _csv_payload(text)
    if not payload:
        return []
    reader = csv.DictReader(io.StringIO(payload, newline=""))
    if reader.fieldnames is None:
        return []
    fields = {field.lstrip("\ufeff#").strip() for field in reader.fieldnames if field}
    required = {"IP", "OpenVPN_ConfigData_Base64"}
    if not required.issubset(fields):
        return []

    nodes: list[VpnNode] = []
    seen_ids: set[str] = set()
    for raw_row in reader:
        node = _row_to_node(_normalize_row(raw_row))
        if node is None or node.id in seen_ids:
            continue
        seen_ids.add(node.id)
        nodes.append(node)
    return nodes


@dataclass(slots=True)
class VpnGateNodeSource:
    api_url: str = DEFAULT_VPNGATE_API_URL
    timeout_seconds: float = 15.0
    max_nodes: int | None = None
    client: httpx.AsyncClient | None = None
    proxy_url: str | None = None
    last_transport: str = field(default="", init=False)

    async def fetch(self) -> list[VpnNode]:
        self.last_transport = ""
        if self.client is not None:
            try:
                response = await self.client.get(self.api_url, timeout=self.timeout_seconds)
                response.raise_for_status()
                nodes = self._parse_nodes(response)
            except (httpx.HTTPError, ValueError) as exc:
                raise self._unavailable_error(str(exc)) from exc
            self.last_transport = "自定义 HTTP 客户端"
            return self._limit(nodes)

        attempts = (
            (("Clash 代理", self.proxy_url), ("国内直连", None))
            if self.proxy_url
            else (("国内直连", None),)
        )
        errors: list[str] = []
        for label, proxy_url in attempts:
            try:
                async with httpx.AsyncClient(
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                    follow_redirects=True,
                    timeout=self.timeout_seconds,
                    proxy=proxy_url,
                    trust_env=False,
                ) as client:
                    response = await client.get(self.api_url)
                    response.raise_for_status()
                    nodes = self._parse_nodes(response)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{label}: {exc}")
                continue
            self.last_transport = label
            return self._limit(nodes)

        raise self._unavailable_error("; ".join(errors))

    @staticmethod
    def _parse_nodes(response: httpx.Response) -> list[VpnNode]:
        text = response.content.decode("utf-8-sig", errors="replace")
        nodes = parse_vpngate_csv(text)
        if not nodes:
            raise ValueError("CSV 为空、格式错误或没有安全的公网 OpenVPN 节点")
        return nodes

    def _limit(self, nodes: list[VpnNode]) -> list[VpnNode]:
        if self.max_nodes is not None:
            return nodes[: max(0, self.max_nodes)]
        return nodes

    @staticmethod
    def _unavailable_error(detail: str) -> AppError:
        return AppError(
            ErrorCode.VPNGATE_UNAVAILABLE,
            "VPNGate 节点列表获取失败",
            detail=detail,
        )
