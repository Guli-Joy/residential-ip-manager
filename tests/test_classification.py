from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from residential_ip_manager.domain.models import PurityGrade, VpnNode
from residential_ip_manager.infrastructure.classification import StrictResidentialClassifier


def _node(ip: str = "8.8.8.8") -> VpnNode:
    return VpnNode(
        id=f"JP_{ip}_443_tcp",
        ip=ip,
        remote_host=ip,
        remote_port=443,
        protocol="tcp",
        country_code="JP",
        country="Japan",
    )


def _ip_api_item(*, ip: str = "8.8.8.8", hosting: bool = False) -> dict[str, object]:
    return {
        "status": "success",
        "query": ip,
        "country": "Japan",
        "countryCode": "JP",
        "regionName": "Tokyo",
        "city": "Tokyo",
        "isp": "NTT Communications",
        "org": "Open Computer Network",
        "as": "AS4713 NTT",
        "asname": "OCN",
        "proxy": False,
        "hosting": hosting,
        "mobile": False,
    }


def _ipapi_item(*, datacenter: bool = False) -> dict[str, object]:
    return {
        "is_datacenter": datacenter,
        "is_proxy": False,
        "is_vpn": False,
        "is_abuser": False,
        "is_tor": False,
        "is_mobile": False,
        "company": {"type": "isp", "name": "NTT Communications", "netname": "OCN"},
        "asn": {"type": "isp", "org": "NTT Communications"},
    }


async def _home_rdns(_ip: str) -> str:
    return "p1234-ipngn.ocn.ne.jp"


@pytest.mark.asyncio
async def test_classifier_requires_all_strict_home_evidence(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ip-api.com":
            return httpx.Response(200, json=[_ip_api_item()])
        return httpx.Response(200, json=_ipapi_item())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=_home_rdns,
            cache_path=tmp_path / "classification.json",
        )
        [node] = await classifier.classify([_node()])

    assert node.purity_grade is PurityGrade.STRICT_HOME
    assert node.purity_score == 100
    assert node.isp == "NTT Communications"
    assert node.city == "Tokyo"
    assert node.country == "Japan"
    assert node.country_code == "JP"
    assert node.reverse_dns == "p1234-ipngn.ocn.ne.jp"
    assert all(evidence.passed for evidence in node.evidence)


@pytest.mark.asyncio
async def test_classifier_never_promotes_when_external_provider_fails(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ip-api.com":
            return httpx.Response(200, json=[_ip_api_item()])
        return httpx.Response(503, text="rate limited")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=_home_rdns,
            cache_path=tmp_path / "classification.json",
        )
        [node] = await classifier.classify([_node()])

    assert node.purity_grade is PurityGrade.CANDIDATE
    assert node.purity_score < 100
    assert not next(item for item in node.evidence if item.provider == "ipapi.is").passed


@pytest.mark.asyncio
async def test_classifier_accepts_clean_allowlisted_isp_without_reverse_dns(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ip-api.com":
            return httpx.Response(200, json=[_ip_api_item()])
        return httpx.Response(200, json=_ipapi_item())

    async def missing_rdns(_ip: str) -> str:
        raise OSError("PTR record not configured")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=missing_rdns,
            cache_path=tmp_path / "classification.json",
        )
        [node] = await classifier.classify([_node()])

    assert node.purity_grade is PurityGrade.STRICT_HOME
    assert node.purity_score == 75
    assert not next(item for item in node.evidence if item.provider == "reverse-dns").passed


@pytest.mark.asyncio
async def test_failed_provider_results_use_short_negative_cache(tmp_path: Path) -> None:
    batch_requests = 0
    secondary_requests = 0
    rdns_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_requests, secondary_requests
        if request.url.host == "ip-api.com":
            batch_requests += 1
            return httpx.Response(200, json=[_ip_api_item()])
        secondary_requests += 1
        return httpx.Response(503, text="temporarily unavailable")

    async def failed_rdns(_ip: str) -> str:
        nonlocal rdns_requests
        rdns_requests += 1
        raise OSError("temporary DNS failure")

    cache_path = tmp_path / "classification.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=failed_rdns,
            cache_path=cache_path,
        )
        [first] = await classifier.classify([_node()])
        [second] = await classifier.classify([_node()])

    assert first.purity_grade is PurityGrade.CANDIDATE
    assert second.purity_grade is PurityGrade.CANDIDATE
    assert batch_requests == 1
    assert secondary_requests == 1
    assert rdns_requests == 1

    cached = json.loads(cache_path.read_text(encoding="utf-8"))["entries"]["8.8.8.8"]
    assert cached["ipapi_is"]["succeeded"] is False
    assert cached["reverse_dns"]["succeeded"] is False


@pytest.mark.asyncio
async def test_classifier_rejects_idc_without_secondary_queries(tmp_path: Path) -> None:
    secondary_requests = 0
    rdns_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal secondary_requests
        if request.url.host == "ip-api.com":
            return httpx.Response(200, json=[_ip_api_item(hosting=True)])
        secondary_requests += 1
        return httpx.Response(200, json=_ipapi_item(datacenter=True))

    async def rdns(_ip: str) -> str:
        nonlocal rdns_requests
        rdns_requests += 1
        return "p1234-ipngn.ocn.ne.jp"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=rdns,
            cache_path=tmp_path / "classification.json",
        )
        [node] = await classifier.classify([_node()])

    assert node.purity_grade is PurityGrade.REJECTED
    assert node.purity_score < 100
    assert secondary_requests == 0
    assert rdns_requests == 0


@pytest.mark.asyncio
async def test_classifier_deduplicates_provider_queries_for_same_ip(tmp_path: Path) -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request.url.host == "ip-api.com":
            return httpx.Response(200, json=[_ip_api_item()])
        return httpx.Response(200, json=_ipapi_item())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=_home_rdns,
            cache_path=tmp_path / "classification.json",
        )
        nodes = await classifier.classify([_node(), _node()])

    assert request_count == 2
    assert all(node.purity_grade is PurityGrade.STRICT_HOME for node in nodes)


@pytest.mark.asyncio
async def test_persistent_cache_avoids_all_repeated_external_queries(tmp_path: Path) -> None:
    batch_requests = 0
    secondary_requests = 0
    rdns_requests = 0
    cache_path = tmp_path / "classification.json"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_requests, secondary_requests
        if request.url.host == "ip-api.com":
            batch_requests += 1
            return httpx.Response(200, json=[_ip_api_item()])
        secondary_requests += 1
        return httpx.Response(200, json=_ipapi_item())

    async def rdns(_ip: str) -> str:
        nonlocal rdns_requests
        rdns_requests += 1
        return "p1234-ipngn.ocn.ne.jp"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = StrictResidentialClassifier(
            client=client,
            rdns_resolver=rdns,
            cache_path=cache_path,
        )
        second = StrictResidentialClassifier(
            client=client,
            rdns_resolver=rdns,
            cache_path=cache_path,
        )
        [first_node] = await first.classify([_node()])
        [second_node] = await second.classify([_node()])

    assert first_node.purity_grade is PurityGrade.STRICT_HOME
    assert second_node.purity_grade is PurityGrade.STRICT_HOME
    assert batch_requests == 1
    assert secondary_requests == 1
    assert rdns_requests == 1


@pytest.mark.asyncio
async def test_node_pool_growth_queries_only_the_new_ip(tmp_path: Path) -> None:
    cache_path = tmp_path / "classification.json"
    batch_payloads: list[list[str]] = []
    secondary_ips: list[str] = []
    rdns_ips: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ip-api.com":
            payload = json.loads(request.content)
            assert isinstance(payload, list)
            ips = [str(ip) for ip in payload]
            batch_payloads.append(ips)
            return httpx.Response(200, json=[_ip_api_item(ip=ip) for ip in ips])
        secondary_ips.append(str(request.url.params["q"]))
        return httpx.Response(200, json=_ipapi_item())

    async def rdns(ip: str) -> str:
        rdns_ips.append(ip)
        return "p1234-ipngn.ocn.ne.jp"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=rdns,
            cache_path=cache_path,
        )
        await classifier.classify([_node("8.8.8.8")])
        nodes = await classifier.classify([_node("8.8.8.8"), _node("1.1.1.1")])

    assert batch_payloads == [["8.8.8.8"], ["1.1.1.1"]]
    assert secondary_ips == ["8.8.8.8", "1.1.1.1"]
    assert rdns_ips == ["8.8.8.8", "1.1.1.1"]
    assert all(node.purity_grade is PurityGrade.STRICT_HOME for node in nodes)


@pytest.mark.asyncio
async def test_v1_cache_reuses_secondary_data_and_upgrades_safely(tmp_path: Path) -> None:
    cache_path = tmp_path / "classification.json"
    cached_at = time.time()
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "8.8.8.8": {
                        "ipapi_is": {
                            "succeeded": True,
                            "cached_at": cached_at,
                            "data": _ipapi_item(),
                        },
                        "reverse_dns": {
                            "succeeded": True,
                            "cached_at": cached_at,
                            "data": {"hostname": "p1234-ipngn.ocn.ne.jp"},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    secondary_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal secondary_requests
        if request.url.host == "ip-api.com":
            return httpx.Response(200, json=[_ip_api_item()])
        secondary_requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(client=client, cache_path=cache_path)
        [node] = await classifier.classify([_node()])

    upgraded = json.loads(cache_path.read_text(encoding="utf-8"))
    assert node.purity_grade is PurityGrade.STRICT_HOME
    assert secondary_requests == 0
    assert upgraded["version"] == 2


@pytest.mark.asyncio
async def test_cache_without_country_code_refreshes_primary_provider(tmp_path: Path) -> None:
    cache_path = tmp_path / "classification.json"
    cached_at = time.time()
    old_ip_api = _ip_api_item()
    old_ip_api.pop("countryCode")
    cache_path.write_text(
        json.dumps(
            {
                "version": 2,
                "entries": {
                    "8.8.8.8": {
                        "ip_api": {
                            "succeeded": True,
                            "cached_at": cached_at,
                            "data": old_ip_api,
                        },
                        "ipapi_is": {
                            "succeeded": True,
                            "cached_at": cached_at,
                            "data": _ipapi_item(),
                        },
                        "reverse_dns": {
                            "succeeded": True,
                            "cached_at": cached_at,
                            "data": {"hostname": "p1234-ipngn.ocn.ne.jp"},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    primary_requests = 0
    secondary_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_requests, secondary_requests
        if request.url.host == "ip-api.com":
            primary_requests += 1
            return httpx.Response(200, json=[_ip_api_item()])
        secondary_requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(client=client, cache_path=cache_path)
        [node] = await classifier.classify([_node()])

    assert primary_requests == 1
    assert secondary_requests == 0
    assert node.country_code == "JP"
    assert node.purity_grade is PurityGrade.STRICT_HOME


@pytest.mark.asyncio
async def test_corrupt_cache_is_replaced_with_valid_data(tmp_path: Path) -> None:
    cache_path = tmp_path / "classification.json"
    cache_path.write_text("{broken", encoding="utf-8")
    secondary_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal secondary_requests
        if request.url.host == "ip-api.com":
            return httpx.Response(200, json=[_ip_api_item()])
        secondary_requests += 1
        return httpx.Response(200, json=_ipapi_item())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = StrictResidentialClassifier(
            client=client,
            rdns_resolver=_home_rdns,
            cache_path=cache_path,
        )
        [node] = await classifier.classify([_node()])

    recovered = json.loads(cache_path.read_text(encoding="utf-8"))
    assert node.purity_grade is PurityGrade.STRICT_HOME
    assert secondary_requests == 1
    assert recovered["version"] == 2
    assert "8.8.8.8" in recovered["entries"]
