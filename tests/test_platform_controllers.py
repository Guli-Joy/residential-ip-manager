from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import VpnNode
from residential_ip_manager.platform.clash import ClashVergeController, load_clash_proxy_hosts
from residential_ip_manager.platform.network import WindowsNetworkController
from residential_ip_manager.platform.openvpn import OpenVpnController
from residential_ip_manager.platform.windows_environment import (
    CommandResult,
    ManagedProcess,
    NetworkAdapterInfo,
    ProcessInfo,
    ProxySettings,
    RouteInfo,
)


class FakeProcess:
    def __init__(self, lines: Sequence[str] = (), *, pid: int = 4000) -> None:
        self.lines = list(lines)
        self._pid = pid
        self._returncode: int | None = None
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def readline(self) -> str:
        if self.lines:
            return self.lines.pop(0)
        return ""

    async def wait(self) -> int:
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


class FakeRunner:
    def __init__(
        self,
        *,
        result: CommandResult | None = None,
        process: FakeProcess | None = None,
    ) -> None:
        self.result = result or CommandResult(0)
        self.process = process or FakeProcess()
        self.run_calls: list[list[str]] = []
        self.start_calls: list[tuple[list[str], Path | None, bool]] = []

    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        del timeout
        self.run_calls.append(list(args))
        return self.result

    async def start(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = True,
    ) -> ManagedProcess:
        del env
        self.start_calls.append((list(args), cwd, capture_output))
        return self.process


class SequentialResultRunner(FakeRunner):
    def __init__(self, results: Sequence[CommandResult]) -> None:
        super().__init__()
        self.results = list(results)

    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        del timeout
        self.run_calls.append(list(args))
        return self.results.pop(0)


class StubDetector:
    def __init__(self, clash_processes: Sequence[ProcessInfo] = ()) -> None:
        self._clash_processes = list(clash_processes)

    async def clash_processes(self) -> list[ProcessInfo]:
        return list(self._clash_processes)

    def clash_config(self) -> None:
        return None


class OpenVpnConflictDetector:
    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.repair_calls = 0

    def resolve_openvpn(self) -> Path:
        return self.executable

    async def tap_adapters(self) -> list[NetworkAdapterInfo]:
        return [NetworkAdapterInfo("OpenVPN TAP", "TAP-Windows Adapter V9", "Up")]

    async def active_openvpn_processes(self) -> list[ProcessInfo]:
        return [ProcessInfo("openvpn.exe", 10820)]

    async def split_default_routes(self) -> list[RouteInfo]:
        return [
            RouteInfo("0.0.0.0/1", "10.211.1.130", "OpenVPN TAP-Windows6", 256),
            RouteInfo("128.0.0.0/1", "10.211.1.130", "OpenVPN TAP-Windows6", 256),
        ]

    async def repair_orphaned_openvpn_routes(self) -> list[RouteInfo]:
        self.repair_calls += 1
        return []

    async def flush_dns_cache(self) -> None:
        return None


class OwnedTunnelDetector:
    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.repair_calls = 0
        self.flush_dns_calls = 0

    def resolve_openvpn(self) -> Path:
        return self.executable

    async def tap_adapters(self) -> list[NetworkAdapterInfo]:
        return [NetworkAdapterInfo("OpenVPN TAP", "TAP-Windows Adapter V9", "Up")]

    async def active_openvpn_processes(self) -> list[ProcessInfo]:
        return []

    async def split_default_routes(self) -> list[RouteInfo]:
        return []

    async def repair_orphaned_openvpn_routes(self) -> list[RouteInfo]:
        self.repair_calls += 1
        return [
            RouteInfo("0.0.0.0/1", "10.211.1.130", "OpenVPN TAP-Windows6", 0, 5),
            RouteInfo("128.0.0.0/1", "10.211.1.130", "OpenVPN TAP-Windows6", 0, 5),
        ]

    async def flush_dns_cache(self) -> None:
        self.flush_dns_calls += 1


class RepairableEnvironmentDetector(OwnedTunnelDetector):
    async def split_default_routes(self) -> list[RouteInfo]:
        if self.repair_calls:
            return []
        return [
            RouteInfo("0.0.0.0/1", "10.211.1.130", "OpenVPN TAP-Windows6", 0, 5),
            RouteInfo("128.0.0.0/1", "10.211.1.130", "OpenVPN TAP-Windows6", 0, 5),
        ]


class SequencePortChecker:
    def __init__(self, values: Sequence[bool]) -> None:
        self.values = list(values)

    async def __call__(self, host: str, port: int, timeout: float) -> bool:
        del host, port, timeout
        return self.values.pop(0) if self.values else False


class MemoryProxyStore:
    def __init__(self, settings: ProxySettings, *, fail_first_write: bool = False) -> None:
        self.settings = settings
        self.writes: list[ProxySettings] = []
        self.fail_first_write = fail_first_write

    def read(self) -> ProxySettings:
        return self.settings

    def write(self, settings: ProxySettings) -> None:
        self.writes.append(settings)
        if self.fail_first_write:
            self.fail_first_write = False
            raise OSError("registry write failed")
        self.settings = settings


def make_node() -> VpnNode:
    return VpnNode(
        id="jp-1",
        ip="153.232.76.220",
        remote_host="153.232.76.220",
        remote_port=1423,
        protocol="tcp",
        country_code="JP",
        country="Japan",
    )


@pytest.mark.asyncio
async def test_clash_reuses_existing_process_without_starting_another(tmp_path: Path) -> None:
    runner = FakeRunner()
    detector = StubDetector([ProcessInfo("clash-verge.exe", 7212)])
    controller = ClashVergeController(
        executable=tmp_path / "missing.exe",
        runner=runner,
        detector=detector,  # type: ignore[arg-type]
        port_checker=SequencePortChecker([False, True]),
        sleep=lambda _delay: _completed_sleep(),
    )

    await controller.ensure_running()

    assert runner.start_calls == []
    assert controller.owned_pid is None


@pytest.mark.asyncio
async def test_clash_starts_and_stops_only_its_owned_process(tmp_path: Path) -> None:
    executable = tmp_path / "clash-verge.exe"
    executable.touch()
    process = FakeProcess(pid=5555)
    runner = FakeRunner(process=process)
    controller = ClashVergeController(
        executable=executable,
        runner=runner,
        detector=StubDetector(),  # type: ignore[arg-type]
        port_checker=SequencePortChecker([False, True]),
        sleep=lambda _delay: _completed_sleep(),
    )

    await controller.ensure_running()
    assert controller.owned_pid == 5555
    assert runner.start_calls[0][2] is False

    await controller.stop()
    assert process.terminated
    assert controller.owned_pid is None


@pytest.mark.asyncio
async def test_clash_proxy_exit_uses_socks5h() -> None:
    runner = FakeRunner(result=CommandResult(0, "66.90.98.146\n"))
    controller = ClashVergeController(
        runner=runner,
        detector=StubDetector(),  # type: ignore[arg-type]
    )

    exit_ip = await controller.proxy_exit_ip()

    assert exit_ip == "66.90.98.146"
    command = runner.run_calls[0]
    assert "--proxy" in command
    assert "socks5h://127.0.0.1:7890" in command


@pytest.mark.asyncio
async def test_clash_proxy_exit_falls_back_from_socks_port_to_mixed_port() -> None:
    runner = SequentialResultRunner(
        [
            CommandResult(7, stderr="port closed"),
            CommandResult(0, "66.90.98.146\n"),
        ]
    )
    controller = ClashVergeController(
        port=7890,
        socks_port=7898,
        runner=runner,
        detector=StubDetector(),  # type: ignore[arg-type]
    )

    assert await controller.proxy_exit_ip() == "66.90.98.146"
    assert "socks5h://127.0.0.1:7898" in runner.run_calls[0]
    assert "socks5h://127.0.0.1:7890" in runner.run_calls[1]


def test_clash_proxy_hosts_are_parsed_from_structured_yaml(tmp_path: Path) -> None:
    config = tmp_path / "clash-verge.yaml"
    config.write_text(
        """\
proxies:
  - name: first
    type: vmess
    server: edge.example.com
  - name: second
    type: socks5
    server: 1.1.1.1
  - name: duplicate
    server: edge.example.com
proxy-groups:
  - name: selection
    type: select
    proxies: [first, second]
""",
        encoding="utf-8",
    )

    assert load_clash_proxy_hosts(config) == ("edge.example.com", "1.1.1.1")


@pytest.mark.asyncio
async def test_clash_bypass_ips_merge_active_and_configured_upstreams(tmp_path: Path) -> None:
    config = tmp_path / "clash-verge.yaml"
    config.write_text("proxies:\n  - name: cloudflare\n    server: 1.1.1.1\n", encoding="utf-8")
    runner = FakeRunner(result=CommandResult(0, '"8.8.8.8"'))
    detector = StubDetector([ProcessInfo("verge-mihomo.exe", 13672)])
    controller = ClashVergeController(
        runner=runner,
        detector=detector,  # type: ignore[arg-type]
        active_config_path=config,
    )

    assert await controller.discover_bypass_ips() == ("8.8.8.8", "1.1.1.1")
    assert "Get-NetTCPConnection" in runner.run_calls[0][-1]


@pytest.mark.asyncio
async def test_openvpn_waits_for_success_and_disconnects_owned_process(tmp_path: Path) -> None:
    executable = tmp_path / "openvpn.exe"
    config = tmp_path / "node.ovpn"
    executable.touch()
    config.touch()
    process = FakeProcess(
        [
            "TCP connection established\n",
            "Initialization Sequence Completed\n",
        ],
        pid=7001,
    )
    runner = FakeRunner(process=process)
    controller = OpenVpnController(
        executable=executable,
        runner=runner,
        admin_checker=lambda: True,
    )

    await controller.connect(make_node(), config)
    assert await controller.is_connected()
    assert runner.start_calls[0][0] == [
        str(executable),
        "--config",
        str(config),
        "--verb",
        "3",
    ]

    await controller.disconnect()
    assert process.terminated
    assert not await controller.is_connected()


@pytest.mark.asyncio
async def test_openvpn_cleans_routes_after_stopping_owned_process(tmp_path: Path) -> None:
    executable = tmp_path / "openvpn.exe"
    config = tmp_path / "node.ovpn"
    executable.touch()
    config.touch()
    process = FakeProcess(["Initialization Sequence Completed\n"])
    detector = OwnedTunnelDetector(executable)
    logs: list[tuple[str, str]] = []
    controller = OpenVpnController(
        executable=executable,
        runner=FakeRunner(process=process),
        detector=detector,  # type: ignore[arg-type]
        admin_checker=lambda: True,
        log_listener=lambda source, message: logs.append((source, message)),
    )

    await controller.connect(make_node(), config)
    await controller.disconnect()

    assert detector.repair_calls == 1
    assert detector.flush_dns_calls == 1
    assert any("0.0.0.0/1" in message for _source, message in logs)


@pytest.mark.asyncio
async def test_openvpn_repairs_orphaned_routes_before_connection(tmp_path: Path) -> None:
    executable = tmp_path / "openvpn.exe"
    executable.touch()
    detector = RepairableEnvironmentDetector(executable)
    controller = OpenVpnController(
        executable=executable,
        runner=FakeRunner(),
        detector=detector,  # type: ignore[arg-type]
        admin_checker=lambda: True,
    )

    assert await controller.repair_environment()
    assert detector.repair_calls == 1
    assert detector.flush_dns_calls == 1
    assert not await controller.repair_environment()
    assert detector.repair_calls == 1


@pytest.mark.asyncio
async def test_openvpn_distinguishes_auth_failed_and_cleans_up(tmp_path: Path) -> None:
    executable = tmp_path / "openvpn.exe"
    config = tmp_path / "node.ovpn"
    executable.touch()
    config.touch()
    process = FakeProcess(["AUTH: Received control message: AUTH_FAILED\n"])
    controller = OpenVpnController(
        executable=executable,
        runner=FakeRunner(process=process),
        admin_checker=lambda: True,
    )

    with pytest.raises(AppError) as captured:
        await controller.connect(make_node(), config)

    assert captured.value.code is ErrorCode.OPENVPN_AUTH_FAILED
    assert process.terminated


@pytest.mark.asyncio
async def test_openvpn_reports_external_tunnel_conflict_without_terminating_it(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "openvpn.exe"
    config = tmp_path / "node.ovpn"
    executable.touch()
    config.touch()
    runner = FakeRunner()
    detector = OpenVpnConflictDetector(executable)
    controller = OpenVpnController(
        executable=executable,
        runner=runner,
        detector=detector,  # type: ignore[arg-type]
        admin_checker=lambda: True,
    )

    report = await controller.check_environment()
    checks = {check.key: check for check in report.checks}
    assert not report.ready
    assert not checks["external_openvpn"].ok
    assert "10820" in checks["external_openvpn"].detail
    assert not checks["split_default_routes"].ok

    with pytest.raises(AppError) as captured:
        await controller.connect(make_node(), config)

    assert captured.value.code is ErrorCode.ENVIRONMENT_NOT_READY
    assert runner.start_calls == []
    await controller.disconnect()
    assert detector.repair_calls == 0


@pytest.mark.asyncio
async def test_network_proxy_snapshot_is_persisted_and_restored_atomically(tmp_path: Path) -> None:
    original = ProxySettings(
        True,
        "127.0.0.1:7890",
        "<local>",
        "http://localhost/proxy.pac",
    )
    store = MemoryProxyStore(original)
    snapshot_path = tmp_path / "proxy-snapshot.json"
    controller = WindowsNetworkController(proxy_store=store, snapshot_path=snapshot_path)

    await controller.prepare()
    assert store.settings == ProxySettings(
        False,
        "127.0.0.1:7890",
        "<local>",
        "http://localhost/proxy.pac",
    )
    assert snapshot_path.is_file()

    await controller.restore()
    assert store.settings == original
    assert not snapshot_path.exists()


@pytest.mark.asyncio
async def test_network_prepare_rolls_back_when_proxy_write_fails(tmp_path: Path) -> None:
    original = ProxySettings(True, "127.0.0.1:7890")
    store = MemoryProxyStore(original, fail_first_write=True)
    snapshot_path = tmp_path / "proxy-snapshot.json"
    controller = WindowsNetworkController(proxy_store=store, snapshot_path=snapshot_path)

    with pytest.raises(AppError) as captured:
        await controller.prepare()

    assert captured.value.code is ErrorCode.ENVIRONMENT_NOT_READY
    assert store.settings == original
    assert store.writes[-1] == original
    assert not snapshot_path.exists()


@pytest.mark.asyncio
async def test_network_prepare_preserves_snapshot_left_by_previous_run(tmp_path: Path) -> None:
    original = ProxySettings(True, "127.0.0.1:7890")
    snapshot_path = tmp_path / "proxy-snapshot.json"
    first_store = MemoryProxyStore(original)
    first_controller = WindowsNetworkController(
        proxy_store=first_store,
        snapshot_path=snapshot_path,
    )
    await first_controller.prepare()

    restarted_store = MemoryProxyStore(first_store.settings)
    restarted_controller = WindowsNetworkController(
        proxy_store=restarted_store,
        snapshot_path=snapshot_path,
    )
    await restarted_controller.prepare()
    await restarted_controller.restore()

    assert restarted_store.settings == original
    assert not snapshot_path.exists()


async def _completed_sleep() -> None:
    return None
