from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from residential_ip_manager.platform import windows_environment
from residential_ip_manager.platform.windows_environment import (
    REPAIR_ORPHANED_OPENVPN_ROUTES,
    AsyncSubprocessRunner,
    CommandResult,
    ManagedProcess,
    ProxySettings,
    WindowsEnvironmentDetector,
    load_clash_runtime_config,
)


class CompletedSubprocess:
    pid = 4321
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok", b""


@pytest.mark.asyncio
async def test_async_runner_hides_background_processes_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def create_subprocess(*args: object, **kwargs: Any) -> CompletedSubprocess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return CompletedSubprocess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    result = await AsyncSubprocessRunner().run(["powershell.exe", "-Command", "exit 0"])

    assert result.returncode == 0
    options = captured["kwargs"]
    if sys.platform == "win32":
        assert options["creationflags"] & 0x08000000
        assert options["startupinfo"].wShowWindow == 0
    else:
        assert "creationflags" not in options

    captured.clear()
    process = await AsyncSubprocessRunner().start(["openvpn.exe"], capture_output=True)
    assert process.pid == 4321
    start_options = captured["kwargs"]
    if sys.platform == "win32":
        assert start_options["creationflags"] & 0x08000000
        assert start_options["startupinfo"].wShowWindow == 0
    else:
        assert "creationflags" not in start_options


class NoStartRunner:
    def __init__(
        self,
        process_json: str = "[]",
        adapter_json: str = "[]",
        route_json: str = "[]",
    ) -> None:
        self.process_json = process_json
        self.adapter_json = adapter_json
        self.route_json = route_json
        self.commands: list[list[str]] = []

    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        del timeout
        command = list(args)
        self.commands.append(command)
        if "Get-NetRoute" in command[-1]:
            return CommandResult(0, self.route_json)
        if "Get-NetAdapter" in command[-1]:
            return CommandResult(0, self.adapter_json)
        return CommandResult(0, self.process_json)

    async def start(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = True,
    ) -> ManagedProcess:
        del args, cwd, env, capture_output
        raise AssertionError("start must not be called by environment detection")


class MemoryProxyStore:
    def __init__(self, settings: ProxySettings) -> None:
        self.settings = settings

    def read(self) -> ProxySettings:
        return self.settings

    def write(self, settings: ProxySettings) -> None:
        self.settings = settings


class RouteRepairRunner:
    def __init__(
        self,
        routes: list[dict[str, object]],
        *,
        process_json: str = "[]",
        remove_failure: str = "",
    ) -> None:
        self.routes = routes
        self.process_json = process_json
        self.remove_failure = remove_failure
        self.commands: list[list[str]] = []

    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        del timeout
        command = list(args)
        self.commands.append(command)
        script = command[-1]
        if "Get-CimInstance" in script:
            return CommandResult(0, self.process_json)
        if "Get-NetRoute" in script:
            return CommandResult(0, json.dumps(self.routes))
        if "Remove-NetRoute" in script:
            if self.remove_failure:
                return CommandResult(1, stderr=self.remove_failure)
            self.routes = [
                route
                for route in self.routes
                if not (
                    f"'{route['DestinationPrefix']}'" in script
                    and f"-InterfaceIndex {route['InterfaceIndex']}" in script
                )
            ]
            return CommandResult(0)
        raise AssertionError(f"unexpected command: {script}")

    async def start(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = True,
    ) -> ManagedProcess:
        del args, cwd, env, capture_output
        raise AssertionError("start must not be called by route repair")


def _route(
    destination: str,
    *,
    interface: str = "OpenVPN TAP-Windows6",
    interface_index: int = 5,
) -> dict[str, object]:
    return {
        "DestinationPrefix": destination,
        "NextHop": "10.211.1.42",
        "InterfaceAlias": interface,
        "RouteMetric": 0,
        "InterfaceIndex": interface_index,
    }


@pytest.mark.asyncio
async def test_process_and_tap_detection_parses_powershell_json() -> None:
    runner = NoStartRunner(
        process_json=(
            '[{"Name":"clash-verge.exe","ProcessId":7212,'
            '"ExecutablePath":"C:\\\\Clash\\\\clash-verge.exe","CommandLine":""},'
            '{"Name":"unrelated.exe","ProcessId":99}]'
        ),
        adapter_json=(
            '{"Name":"OpenVPN TAP-Windows6",'
            '"InterfaceDescription":"TAP-Windows Adapter V9","Status":"Up"}'
        ),
    )
    detector = WindowsEnvironmentDetector(runner=runner)

    processes = await detector.clash_processes()
    adapters = await detector.tap_adapters()

    assert [(item.name, item.pid) for item in processes] == [("clash-verge.exe", 7212)]
    assert adapters[0].name == "OpenVPN TAP-Windows6"
    expected_prefix = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert all(command[:4] == expected_prefix for command in runner.commands)


@pytest.mark.asyncio
async def test_environment_report_covers_admin_proxy_openvpn_tap_clash_and_port(
    tmp_path: Path,
) -> None:
    openvpn = tmp_path / "openvpn.exe"
    openvpn.touch()
    runner = NoStartRunner(
        process_json='{"Name":"verge-mihomo.exe","ProcessId":13672}',
        adapter_json=(
            '{"Name":"OpenVPN Data Channel Offload",'
            '"InterfaceDescription":"OpenVPN Data Channel Offload","Status":"Disconnected"}'
        ),
    )

    checked_ports: list[tuple[str, int]] = []

    async def port_open(host: str, port: int, timeout: float) -> bool:
        checked_ports.append((host, port))
        assert timeout > 0
        return True

    detector = WindowsEnvironmentDetector(
        runner=runner,
        proxy_store=MemoryProxyStore(ProxySettings(True, "127.0.0.1:7890")),
        admin_checker=lambda: True,
        port_checker=port_open,
        openvpn_path=openvpn,
        clash_config_path=tmp_path / "missing-clash-config.yaml",
    )

    report = await detector.check_environment()
    checks = {check.key: check for check in report.checks}

    assert report.ready
    assert checks["administrator"].ok
    assert checks["system_proxy"].detail == "enabled=True, server=127.0.0.1:7890"
    assert checks["openvpn"].ok
    assert checks["tap_adapter"].ok
    assert checks["clash_process"].ok
    assert checks["clash_port"].ok
    assert checked_ports == [
        ("127.0.0.1", 7890),
        ("127.0.0.1", 7890),
        ("127.0.0.1", 9090),
    ]


@pytest.mark.asyncio
async def test_environment_marks_orphaned_openvpn_routes_as_repairable(
    tmp_path: Path,
) -> None:
    openvpn = tmp_path / "openvpn.exe"
    openvpn.touch()
    runner = NoStartRunner(
        adapter_json=(
            '{"Name":"OpenVPN TAP-Windows6",'
            '"InterfaceDescription":"TAP-Windows Adapter V9","Status":"Disconnected"}'
        ),
        route_json=json.dumps([_route("0.0.0.0/1"), _route("128.0.0.0/1")]),
    )

    async def port_closed(_host: str, _port: int, _timeout: float) -> bool:
        return False

    detector = WindowsEnvironmentDetector(
        runner=runner,
        proxy_store=MemoryProxyStore(ProxySettings(False)),
        admin_checker=lambda: True,
        port_checker=port_closed,
        openvpn_path=openvpn,
        clash_config_path=tmp_path / "missing.yaml",
    )

    report = await detector.check_environment()
    route_check = next(
        check for check in report.checks if check.key == "split_default_route_conflict"
    )

    assert not route_check.ok
    assert route_check.repair_action == REPAIR_ORPHANED_OPENVPN_ROUTES
    assert route_check.repair_targets == (
        "0.0.0.0/1 -> 10.211.1.42 | OpenVPN TAP-Windows6 (接口 5)",
        "128.0.0.0/1 -> 10.211.1.42 | OpenVPN TAP-Windows6 (接口 5)",
    )


@pytest.mark.asyncio
async def test_repair_removes_only_exact_openvpn_split_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_environment, "is_windows", lambda: True)
    runner = RouteRepairRunner([_route("0.0.0.0/1"), _route("128.0.0.0/1")])
    detector = WindowsEnvironmentDetector(runner=runner, admin_checker=lambda: True)

    removed = await detector.repair_orphaned_openvpn_routes()

    assert [(route.destination, route.interface_index) for route in removed] == [
        ("0.0.0.0/1", 5),
        ("128.0.0.0/1", 5),
    ]
    remove_scripts = [
        command[-1] for command in runner.commands if "Remove-NetRoute" in command[-1]
    ]
    assert len(remove_scripts) == 2
    assert all("-InterfaceIndex 5" in script for script in remove_scripts)
    assert runner.routes == []


@pytest.mark.asyncio
async def test_repair_refuses_routes_while_openvpn_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_environment, "is_windows", lambda: True)
    runner = RouteRepairRunner(
        [_route("0.0.0.0/1")],
        process_json='{"Name":"openvpn.exe","ProcessId":4321}',
    )
    detector = WindowsEnvironmentDetector(runner=runner, admin_checker=lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        await detector.repair_orphaned_openvpn_routes()

    assert not any("Remove-NetRoute" in command[-1] for command in runner.commands)


@pytest.mark.asyncio
async def test_repair_refuses_non_openvpn_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_environment, "is_windows", lambda: True)
    runner = RouteRepairRunner(
        [_route("0.0.0.0/1", interface="以太网", interface_index=7)]
    )
    detector = WindowsEnvironmentDetector(runner=runner, admin_checker=lambda: True)

    with pytest.raises(RuntimeError, match="不属于 OpenVPN"):
        await detector.repair_orphaned_openvpn_routes()

    assert not any("Remove-NetRoute" in command[-1] for command in runner.commands)


@pytest.mark.asyncio
async def test_repair_surfaces_powershell_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_environment, "is_windows", lambda: True)
    runner = RouteRepairRunner(
        [_route("0.0.0.0/1")],
        remove_failure="Access is denied",
    )
    detector = WindowsEnvironmentDetector(runner=runner, admin_checker=lambda: True)

    with pytest.raises(RuntimeError, match="Access is denied"):
        await detector.repair_orphaned_openvpn_routes()


@pytest.mark.asyncio
async def test_clash_yaml_is_discovered_without_exposing_secret(tmp_path: Path) -> None:
    secret = "unit-test-controller-secret"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "mixed-port: 7890",
                "socks-port: 7898",
                "external-controller: 127.0.0.1:9097",
                f"secret: '{secret}'",
                "tun:",
                "  enable: false",
            ]
        ),
        encoding="utf-8",
    )
    runtime_config = load_clash_runtime_config(config_path)

    assert runtime_config is not None
    assert runtime_config.mixed_port == 7890
    assert runtime_config.socks_port == 7898
    assert runtime_config.external_controller_port == 9097
    assert not runtime_config.tun_enabled
    assert runtime_config.secret == secret
    assert secret not in repr(runtime_config)

    runner = NoStartRunner()

    async def port_open(_host: str, _port: int, _timeout: float) -> bool:
        return True

    detector = WindowsEnvironmentDetector(
        runner=runner,
        proxy_store=MemoryProxyStore(ProxySettings(False)),
        admin_checker=lambda: True,
        port_checker=port_open,
        openvpn_path=tmp_path / "missing-openvpn.exe",
        clash_config_path=config_path,
    )
    report = await detector.check_environment()
    report_text = "\n".join(check.detail for check in report.checks)

    assert secret not in report_text
    assert "secret=configured" in report_text


@pytest.mark.asyncio
async def test_non_windows_environment_returns_report_without_running_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = NoStartRunner()
    monkeypatch.setattr(windows_environment, "is_windows", lambda: False)
    detector = WindowsEnvironmentDetector(runner=runner)

    report = await detector.check_environment()

    assert not report.ready
    assert report.checks[0].key == "windows"
    assert runner.commands == []
