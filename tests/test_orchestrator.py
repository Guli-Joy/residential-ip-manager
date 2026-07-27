import asyncio
from pathlib import Path

import pytest

from residential_ip_manager.application.orchestrator import ConnectionOrchestrator
from residential_ip_manager.config import AppSettings
from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import (
    ComponentCheck,
    ConnectionState,
    EnvironmentReport,
    NodeStatus,
    PurityGrade,
    VpnNode,
)


def make_node(node_id: str, ip: str, country: str, *, score: int = 50) -> VpnNode:
    return VpnNode(
        id=node_id,
        ip=ip,
        remote_host=ip,
        remote_port=443,
        protocol="tcp",
        country_code=country,
        country=country,
        purity_grade=PurityGrade.STRICT_HOME,
        purity_score=95,
        score=score,
        status=NodeStatus.AVAILABLE,
        city="Test City",
        isp=f"{country} Home ISP",
        asn="AS64500",
        latency_ms=88,
    )


class Source:
    def __init__(self, nodes: list[VpnNode]) -> None:
        self.nodes = nodes
        self.calls = 0

    async def fetch(self) -> list[VpnNode]:
        self.calls += 1
        return list(self.nodes)


class Classifier:
    def __init__(self, profiles: dict[str, VpnNode] | None = None) -> None:
        self.profiles = profiles or {}
        self.calls: list[list[str]] = []

    async def classify(self, nodes: list[VpnNode]) -> list[VpnNode]:
        self.calls.append([node.ip for node in nodes])
        for node in nodes:
            profile = self.profiles.get(node.ip)
            if profile is None:
                continue
            node.country_code = profile.country_code
            node.country = profile.country
            node.city = profile.city
            node.isp = profile.isp
            node.asn = profile.asn
            node.purity_grade = profile.purity_grade
            node.purity_score = profile.purity_score
        return nodes


class Probe:
    async def probe(self, nodes: list[VpnNode]) -> list[VpnNode]:
        return nodes


def ready_report() -> EnvironmentReport:
    return EnvironmentReport([ComponentCheck("component", "component", True, "ok")])


class Clash:
    ensure_calls = 0

    async def check_environment(self) -> EnvironmentReport:
        return ready_report()

    async def ensure_running(self) -> None:
        self.ensure_calls += 1

    async def proxy_exit_ip(self) -> str:
        return "203.0.113.5"


class Tunnel:
    def __init__(self) -> None:
        self.active_node: VpnNode | None = None
        self.connect_calls = 0
        self.connect_node_ids: list[str] = []
        self.disconnect_calls = 0
        self.connect_started: asyncio.Event | None = None
        self.connect_release: asyncio.Event | None = None
        self.environment_ready = True
        self.repair_calls = 0
        self.fail_node_ids: set[str] = set()

    async def check_environment(self) -> EnvironmentReport:
        if self.environment_ready:
            return ready_report()
        return EnvironmentReport(
            [ComponentCheck("split_routes", "残留路由", False, "检测到残留路由")]
        )

    async def repair_environment(self) -> bool:
        self.repair_calls += 1
        if self.environment_ready:
            return False
        self.environment_ready = True
        return True

    async def connect(self, node: VpnNode, config_path: Path) -> None:
        assert config_path.suffix == ".ovpn"
        self.connect_calls += 1
        self.connect_node_ids.append(node.id)
        if self.connect_started is not None:
            self.connect_started.set()
        if self.connect_release is not None:
            await self.connect_release.wait()
        if node.id in self.fail_node_ids:
            raise AppError(ErrorCode.OPENVPN_CONNECT_FAILED, "模拟节点连接失败")
        self.active_node = node

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.active_node = None

    async def is_connected(self) -> bool:
        return self.active_node is not None


class Network:
    def __init__(self, tunnel: Tunnel) -> None:
        self.tunnel = tunnel
        self.prepared = 0
        self.restored = 0
        self.break_node_id: str | None = None
        self.exit_ips: dict[str, str] = {}

    async def check_environment(self) -> EnvironmentReport:
        return ready_report()

    async def prepare(self) -> None:
        self.prepared += 1

    async def restore(self) -> None:
        self.restored += 1

    async def public_ip(self) -> str:
        node = self.tunnel.active_node
        if node is None:
            return ""
        if node.id in self.exit_ips:
            return self.exit_ips[node.id]
        if node.id == self.break_node_id:
            return "192.0.2.250"
        return node.ip


def build_orchestrator(
    nodes: list[VpnNode],
    *,
    settings: AppSettings | None = None,
    classifier: Classifier | None = None,
) -> tuple[ConnectionOrchestrator, Tunnel, Network, list[str]]:
    tunnel = Tunnel()
    network = Network(tunnel)
    built: list[str] = []

    def config_builder(node: VpnNode) -> Path:
        built.append(node.id)
        return Path(f"{node.id}.ovpn")

    orchestrator = ConnectionOrchestrator(
        source=Source(nodes),
        classifier=classifier or Classifier(),
        probe=Probe(),
        clash=Clash(),
        tunnel=tunnel,
        network=network,
        config_builder=config_builder,
        settings=settings,
    )
    return orchestrator, tunnel, network, built


@pytest.mark.asyncio
async def test_connect_uses_one_state_machine_and_verifies_exit_ip() -> None:
    node = make_node("jp-1", "198.51.100.10", "JP")
    orchestrator, tunnel, _, built = build_orchestrator([node])
    states: list[ConnectionState] = []
    orchestrator.add_snapshot_listener(lambda snapshot: states.append(snapshot.state))
    orchestrator.add_snapshot_listener(lambda _snapshot: 1 / 0)

    snapshot = await orchestrator.connect()

    assert snapshot.state is ConnectionState.CONNECTED
    assert snapshot.exit_ip == node.ip
    assert snapshot.metadata["vpn_endpoint_ip"] == node.ip
    assert snapshot.metadata["verified_exit_ip"] == node.ip
    assert snapshot.metadata["exit_verification"] == "exact"
    assert built == [node.id]
    assert tunnel.connect_calls == 1
    assert states == [
        ConnectionState.CHECKING_ENVIRONMENT,
        ConnectionState.STARTING_CLASH,
        ConnectionState.FETCHING_NODES,
        ConnectionState.PROBING_NODES,
        ConnectionState.STARTING_CLASH,
        ConnectionState.CONNECTING,
        ConnectionState.VERIFYING,
        ConnectionState.CONNECTED,
    ]


@pytest.mark.asyncio
async def test_operation_lock_prevents_duplicate_connects() -> None:
    node = make_node("jp-1", "198.51.100.10", "JP")
    orchestrator, tunnel, _, _ = build_orchestrator([node])
    tunnel.connect_started = asyncio.Event()
    tunnel.connect_release = asyncio.Event()

    first = asyncio.create_task(orchestrator.connect())
    await tunnel.connect_started.wait()
    second = asyncio.create_task(orchestrator.connect())
    await asyncio.sleep(0)
    tunnel.connect_release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.state is ConnectionState.CONNECTED
    assert second_result.state is ConnectionState.CONNECTED
    assert tunnel.connect_calls == 1


@pytest.mark.asyncio
async def test_connect_revalidates_a_stale_connected_snapshot() -> None:
    node = make_node("jp-1", "198.51.100.10", "JP")
    orchestrator, tunnel, _, _ = build_orchestrator([node])
    await orchestrator.connect()
    await tunnel.disconnect()

    snapshot = await orchestrator.connect()

    assert snapshot.state is ConnectionState.CONNECTED
    assert tunnel.connect_calls == 2


@pytest.mark.asyncio
async def test_connect_automatically_repairs_environment_before_starting_tunnel() -> None:
    node = make_node("jp-1", "198.51.100.10", "JP")
    orchestrator, tunnel, _, _ = build_orchestrator([node])
    tunnel.environment_ready = False

    snapshot = await orchestrator.connect()

    assert snapshot.state is ConnectionState.CONNECTED
    assert tunnel.repair_calls == 1
    assert tunnel.connect_node_ids == [node.id]


@pytest.mark.asyncio
async def test_connect_retries_next_available_node_after_node_failure() -> None:
    first = make_node("jp-1", "198.51.100.10", "JP", score=100)
    second = make_node("jp-2", "198.51.100.11", "JP", score=90)
    third = make_node("us-1", "198.51.100.12", "US", score=99)
    settings = AppSettings(connection_attempts=3)
    orchestrator, tunnel, _, _ = build_orchestrator(
        [first, second, third],
        settings=settings,
    )
    tunnel.fail_node_ids.add(first.id)

    snapshot = await orchestrator.connect(first.id)

    assert snapshot.state is ConnectionState.CONNECTED
    assert snapshot.active_node_id == second.id
    assert tunnel.connect_node_ids == [first.id, second.id]
    assert first.status is NodeStatus.COOLDOWN
    assert orchestrator.node_pool.cooldown_until(first.id) is not None


@pytest.mark.asyncio
async def test_exit_mismatch_fails_and_cools_node() -> None:
    node = make_node("jp-1", "198.51.100.10", "JP")
    orchestrator, _, network, _ = build_orchestrator([node])
    network.break_node_id = node.id

    with pytest.raises(AppError) as captured:
        await orchestrator.connect()

    assert captured.value.code is ErrorCode.EXIT_IP_MISMATCH
    assert orchestrator.snapshot.state is ConnectionState.ERROR
    assert orchestrator.node_pool.cooldown_until(node.id) is not None


@pytest.mark.asyncio
async def test_clash_exit_is_rejected_before_residential_reclassification() -> None:
    node = make_node("kr-1", "121.191.12.107", "KR")
    classifier = Classifier()
    orchestrator, _, network, _ = build_orchestrator([node], classifier=classifier)
    network.exit_ips[node.id] = "203.0.113.5"

    with pytest.raises(AppError) as captured:
        await orchestrator.connect()

    assert captured.value.code is ErrorCode.EXIT_IP_MISMATCH
    assert "Clash 中转 IP" in str(captured.value)
    assert classifier.calls == [[node.ip]]


@pytest.mark.asyncio
async def test_same_country_asn_strict_home_nat_exit_is_accepted() -> None:
    node = make_node("kr-1", "121.191.12.107", "KR")
    node.asn = "AS4766 Korea Telecom"
    nat_ip = "121.188.68.164"
    nat_exit = make_node("nat-exit", nat_ip, "KR")
    nat_exit.country = "South Korea"
    nat_exit.isp = "Korea Telecom"
    nat_exit.asn = "AS4766 Korea Telecom"
    classifier = Classifier({nat_ip: nat_exit})
    orchestrator, _, network, _ = build_orchestrator([node], classifier=classifier)
    network.exit_ips[node.id] = nat_ip

    snapshot = await orchestrator.connect()

    assert snapshot.state is ConnectionState.CONNECTED
    assert snapshot.exit_ip == nat_ip
    assert snapshot.metadata["vpn_endpoint_ip"] == node.ip
    assert snapshot.metadata["verified_exit_ip"] == nat_ip
    assert snapshot.metadata["exit_verification"] == "same_asn_nat"
    assert snapshot.metadata["country_code"] == "KR"
    assert snapshot.metadata["isp"] == "Korea Telecom"
    assert snapshot.metadata["latency_ms"] == node.latency_ms
    assert await orchestrator.check_health()
    assert orchestrator.snapshot.exit_ip == nat_ip


@pytest.mark.asyncio
async def test_nat_exit_with_different_asn_is_rejected() -> None:
    node = make_node("kr-1", "121.191.12.107", "KR")
    node.asn = "AS4766 Korea Telecom"
    nat_ip = "121.188.68.164"
    nat_exit = make_node("nat-exit", nat_ip, "KR")
    nat_exit.asn = "AS9318 SK Broadband"
    classifier = Classifier({nat_ip: nat_exit})
    orchestrator, _, network, _ = build_orchestrator([node], classifier=classifier)
    network.exit_ips[node.id] = nat_ip

    with pytest.raises(AppError) as captured:
        await orchestrator.connect()

    assert captured.value.code is ErrorCode.EXIT_IP_MISMATCH
    assert "ASN" in str(captured.value)


@pytest.mark.asyncio
async def test_nat_exit_with_different_country_is_rejected() -> None:
    node = make_node("kr-1", "121.191.12.107", "KR")
    node.asn = "AS4766 Korea Telecom"
    nat_ip = "198.51.100.30"
    nat_exit = make_node("nat-exit", nat_ip, "US")
    nat_exit.asn = node.asn
    classifier = Classifier({nat_ip: nat_exit})
    orchestrator, _, network, _ = build_orchestrator([node], classifier=classifier)
    network.exit_ips[node.id] = nat_ip

    with pytest.raises(AppError) as captured:
        await orchestrator.connect()

    assert captured.value.code is ErrorCode.EXIT_IP_MISMATCH
    assert "国家" in str(captured.value)


@pytest.mark.asyncio
async def test_nat_exit_marked_as_idc_is_rejected() -> None:
    node = make_node("kr-1", "121.191.12.107", "KR")
    node.asn = "AS4766 Korea Telecom"
    nat_ip = "121.188.68.164"
    nat_exit = make_node("nat-exit", nat_ip, "KR")
    nat_exit.asn = node.asn
    nat_exit.purity_grade = PurityGrade.REJECTED
    classifier = Classifier({nat_ip: nat_exit})
    orchestrator, _, network, _ = build_orchestrator([node], classifier=classifier)
    network.exit_ips[node.id] = nat_ip

    with pytest.raises(AppError) as captured:
        await orchestrator.connect()

    assert captured.value.code is ErrorCode.EXIT_IP_MISMATCH
    assert "未通过严格家庭宽带复核" in str(captured.value)


@pytest.mark.asyncio
async def test_health_check_compares_against_verified_nat_exit() -> None:
    node = make_node("kr-1", "39.122.7.147", "KR")
    node.asn = "AS9318 SK Broadband"
    nat_ip = "39.122.7.119"
    nat_exit = make_node("nat-exit", nat_ip, "KR")
    nat_exit.asn = "AS9318 SK Broadband"
    classifier = Classifier({nat_ip: nat_exit})
    orchestrator, _, network, _ = build_orchestrator([node], classifier=classifier)
    network.exit_ips[node.id] = nat_ip
    await orchestrator.connect()

    assert await orchestrator.check_health()
    network.exit_ips[node.id] = "39.122.7.120"
    assert not await orchestrator.check_health()
    assert nat_ip in orchestrator.snapshot.message


@pytest.mark.asyncio
async def test_three_health_failures_switch_to_same_country_first() -> None:
    old = make_node("jp-1", "198.51.100.10", "JP", score=100)
    same_country = make_node("jp-2", "198.51.100.11", "JP", score=80)
    other_country = make_node("us-3", "198.51.100.12", "US", score=99)
    settings = AppSettings(failure_threshold=3, auto_failover=True)
    orchestrator, tunnel, network, built = build_orchestrator(
        [old, same_country, other_country], settings=settings
    )
    await orchestrator.connect()
    network.break_node_id = old.id

    assert not await orchestrator.check_health()
    assert not await orchestrator.check_health()
    assert await orchestrator.check_health()

    assert orchestrator.snapshot.state is ConnectionState.CONNECTED
    assert orchestrator.snapshot.active_node_id == same_country.id
    assert tunnel.active_node is same_country
    assert built == [old.id, same_country.id]
    assert orchestrator.node_pool.cooldown_until(old.id) is not None


@pytest.mark.asyncio
async def test_cancelled_connect_restores_network() -> None:
    node = make_node("jp-1", "198.51.100.10", "JP")
    orchestrator, tunnel, network, _ = build_orchestrator([node])
    tunnel.connect_started = asyncio.Event()
    tunnel.connect_release = asyncio.Event()

    task = asyncio.create_task(orchestrator.connect())
    await tunnel.connect_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert orchestrator.snapshot.state is ConnectionState.IDLE
    assert network.restored >= 1
    assert tunnel.active_node is None


@pytest.mark.asyncio
async def test_connected_snapshot_contains_summary_and_disconnect_clears_it() -> None:
    node = make_node("jp-1", "198.51.100.10", "JP")
    orchestrator, tunnel, network, _ = build_orchestrator([node])

    connected = await orchestrator.connect()

    assert connected.metadata["country"] == "JP"
    assert connected.metadata["country_code"] == "JP"
    assert connected.metadata["isp"] == "JP Home ISP"
    assert connected.metadata["latency_ms"] == 88
    assert connected.metadata["tunnel_connected"] is True
    assert connected.metadata["exit_verified"] is True

    disconnected = await orchestrator.disconnect()

    assert disconnected.state is ConnectionState.IDLE
    assert disconnected.active_node_id is None
    assert disconnected.exit_ip == ""
    assert disconnected.connected_since is None
    assert "country" not in disconnected.metadata
    assert "tunnel_connected" not in disconnected.metadata
    assert tunnel.active_node is None
    assert network.restored >= 1


@pytest.mark.asyncio
async def test_refresh_while_connected_preserves_active_node_missing_from_new_feed() -> None:
    active = make_node("jp-1", "198.51.100.10", "JP", score=100)
    replacement = make_node("us-1", "198.51.100.20", "US", score=90)
    orchestrator, tunnel, _, _ = build_orchestrator([active])
    await orchestrator.connect()
    source = orchestrator.source
    assert isinstance(source, Source)
    source.nodes = [replacement]

    nodes = await orchestrator.refresh_nodes()

    assert {node.id for node in nodes} == {active.id, replacement.id}
    assert orchestrator.snapshot.active_node_id == active.id
    assert tunnel.active_node is active
    assert await orchestrator.check_health()
