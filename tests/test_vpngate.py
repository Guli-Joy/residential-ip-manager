from __future__ import annotations

import asyncio
import base64
import csv
import io

import httpx
import pytest

from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import NodeStatus, VpnNode
from residential_ip_manager.infrastructure import vpngate
from residential_ip_manager.infrastructure.probe import TcpNodeProbe
from residential_ip_manager.infrastructure.vpngate import VpnGateNodeSource, parse_vpngate_csv


def _encoded_config(ip: str, port: int = 443, protocol: str = "tcp") -> str:
    config = f"client\nproto {protocol}\nremote {ip} {port}\n"
    return base64.b64encode(config.encode()).decode()


def _csv_text(rows: list[list[str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "HostName",
            "IP",
            "Score",
            "Ping",
            "Speed",
            "CountryLong",
            "CountryShort",
            "NumVpnSessions",
            "OpenVPN_ConfigData_Base64",
        ]
    )
    writer.writerows(rows)
    return "\ufeffVPNGate API\n#" + output.getvalue() + "*\n"


def _valid_row(ip: str = "8.8.8.8") -> list[str]:
    return ["host", ip, "123", "42", "1000000", "Japan, East", "jp", "2", _encoded_config(ip)]


def _node(*, protocol: str = "tcp") -> VpnNode:
    return VpnNode(
        id=f"JP_8.8.8.8_443_{protocol}",
        ip="8.8.8.8",
        remote_host="8.8.8.8",
        remote_port=443,
        protocol=protocol,
        country_code="JP",
        country="Japan",
        config_text="client\nproto tcp\nremote 8.8.8.8 443\n",
    )


def test_parse_vpngate_csv_skips_duplicates_malformed_and_redirects() -> None:
    redirect = _valid_row("1.1.1.1")
    redirect[-1] = _encoded_config("8.8.4.4")
    private = _valid_row("127.0.0.1")
    malformed = _valid_row("9.9.9.9")
    malformed[-1] = "not-base64!"
    text = _csv_text([_valid_row(), _valid_row(), redirect, private, malformed])

    nodes = parse_vpngate_csv(text)

    assert len(nodes) == 1
    assert nodes[0].id == "JP_8.8.8.8_443_tcp"
    assert nodes[0].country == "Japan, East"
    assert nodes[0].advertised_ping_ms == 42
    assert nodes[0].speed_bps == 1_000_000


def test_parse_vpngate_csv_requires_real_header() -> None:
    assert parse_vpngate_csv("temporary maintenance page") == []


@pytest.mark.asyncio
async def test_vpngate_source_fetches_with_async_httpx() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://unit.test/vpngate")
        return httpx.Response(200, content=_csv_text([_valid_row()]).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = VpnGateNodeSource(api_url="https://unit.test/vpngate", client=client)
        nodes = await source.fetch()

    assert [node.ip for node in nodes] == ["8.8.8.8"]


@pytest.mark.asyncio
async def test_vpngate_source_maps_http_error_to_domain_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = VpnGateNodeSource(api_url="https://unit.test/vpngate", client=client)
        with pytest.raises(AppError) as error:
            await source.fetch()

    assert error.value.code is ErrorCode.VPNGATE_UNAVAILABLE


@pytest.mark.asyncio
async def test_vpngate_source_prefers_configured_clash_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str | None] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            self.proxy = kwargs.get("proxy")
            attempts.append(str(self.proxy) if self.proxy else None)

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                request=request,
                content=_csv_text([_valid_row()]).encode(),
            )

    monkeypatch.setattr(vpngate.httpx, "AsyncClient", Client)
    source = VpnGateNodeSource(proxy_url="http://127.0.0.1:7890")

    nodes = await source.fetch()

    assert [node.ip for node in nodes] == ["8.8.8.8"]
    assert attempts == ["http://127.0.0.1:7890"]
    assert source.last_transport == "Clash 代理"


@pytest.mark.asyncio
async def test_vpngate_source_falls_back_to_direct_when_clash_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str | None] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            self.proxy = kwargs.get("proxy")
            attempts.append(str(self.proxy) if self.proxy else None)

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            if self.proxy:
                raise httpx.ConnectError("Clash port closed", request=request)
            return httpx.Response(
                200,
                request=request,
                content=_csv_text([_valid_row()]).encode(),
            )

    monkeypatch.setattr(vpngate.httpx, "AsyncClient", Client)
    source = VpnGateNodeSource(proxy_url="http://127.0.0.1:7890")

    nodes = await source.fetch()

    assert [node.ip for node in nodes] == ["8.8.8.8"]
    assert attempts == ["http://127.0.0.1:7890", None]
    assert source.last_transport == "国内直连"


@pytest.mark.asyncio
async def test_tcp_probe_marks_tcp_available_and_udp_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, Writer]:
        assert (host, port) == ("8.8.8.8", 443)
        return asyncio.StreamReader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    tcp_node = _node()
    udp_node = _node(protocol="udp")

    nodes = await TcpNodeProbe(timeout_seconds=1, max_concurrency=1).probe(
        [tcp_node, udp_node]
    )

    assert nodes[0].status is NodeStatus.AVAILABLE
    assert nodes[0].latency_ms is not None
    assert nodes[1].status is NodeStatus.UNAVAILABLE
    assert "UDP" in nodes[1].last_error
