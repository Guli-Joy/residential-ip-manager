from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from residential_ip_manager.domain.models import ComponentCheck, EnvironmentReport

if sys.platform == "win32":
    import winreg
else:  # pragma: no cover - exercised by import checks on non-Windows CI
    winreg = None  # type: ignore[assignment]


DEFAULT_OPENVPN_PATH = Path(r"C:\Program Files\OpenVPN\bin\openvpn.exe")
CLASH_PROCESS_NAMES = (
    "clash-verge.exe",
    "verge-mihomo.exe",
    "clash-meta.exe",
    "mihomo.exe",
)
SPLIT_DEFAULT_DESTINATIONS = frozenset({"0.0.0.0/1", "128.0.0.0/1"})
OPENVPN_ADAPTER_MARKERS = ("openvpn", "tap", "wintun", "data channel offload")
REPAIR_ORPHANED_OPENVPN_ROUTES = "repair_orphaned_openvpn_routes"
REPAIR_SPLIT_ROUTES_MANUALLY = "repair_split_routes_manually"
POWERSHELL_UTF8_PREFIX = (
    "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
)


def _powershell_utf8(script: str) -> str:
    return POWERSHELL_UTF8_PREFIX + script


def _background_process_options() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    startup_info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startup_info,
    }


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class ManagedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    async def readline(self) -> str: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class CommandRunner(Protocol):
    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult: ...

    async def start(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = True,
    ) -> ManagedProcess: ...


class AsyncioManagedProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def readline(self) -> str:
        if self._process.stdout is None:
            return ""
        raw_line = await self._process.stdout.readline()
        return raw_line.decode("utf-8", errors="replace")

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process.returncode is None:
            self._process.kill()


class AsyncSubprocessRunner:
    """Executes argument vectors directly; no command is passed through a shell."""

    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *map(str, args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_background_process_options(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        return CommandResult(
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def start(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = True,
    ) -> ManagedProcess:
        stdout = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
        stderr = asyncio.subprocess.STDOUT if capture_output else asyncio.subprocess.DEVNULL
        process = await asyncio.create_subprocess_exec(
            *map(str, args),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env else None,
            stdout=stdout,
            stderr=stderr,
            **_background_process_options(),
        )
        return AsyncioManagedProcess(process)


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    name: str
    pid: int
    executable_path: str = ""
    command_line: str = ""


@dataclass(frozen=True, slots=True)
class NetworkAdapterInfo:
    name: str
    description: str
    status: str


@dataclass(frozen=True, slots=True)
class RouteInfo:
    destination: str
    next_hop: str
    interface: str
    metric: int
    interface_index: int = 0


@dataclass(frozen=True, slots=True)
class ClashRuntimeConfig:
    path: Path
    mixed_port: int = 7890
    socks_port: int = 7890
    external_controller_host: str = "127.0.0.1"
    external_controller_port: int = 9090
    tun_enabled: bool = False
    secret: str = field(default="", repr=False)

    @property
    def has_secret(self) -> bool:
        return bool(self.secret)


@dataclass(frozen=True, slots=True)
class ProxySettings:
    enabled: bool
    server: str | None = None
    override: str | None = None
    auto_config_url: str | None = None


class ProxyStore(Protocol):
    def read(self) -> ProxySettings: ...

    def write(self, settings: ProxySettings) -> None: ...


def _read_optional_registry_value(key: Any, name: str) -> str | None:
    assert winreg is not None
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    return str(value)


class WindowsRegistryProxyStore:
    KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    def read(self) -> ProxySettings:
        if winreg is None:
            raise OSError("Windows registry is unavailable on this platform")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY_PATH) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except FileNotFoundError:
                enabled = 0
            return ProxySettings(
                enabled=bool(enabled),
                server=_read_optional_registry_value(key, "ProxyServer"),
                override=_read_optional_registry_value(key, "ProxyOverride"),
                auto_config_url=_read_optional_registry_value(key, "AutoConfigURL"),
            )

    def write(self, settings: ProxySettings) -> None:
        if winreg is None:
            raise OSError("Windows registry is unavailable on this platform")
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            self.KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(settings.enabled))
            self._set_optional_string(key, "ProxyServer", settings.server)
            self._set_optional_string(key, "ProxyOverride", settings.override)
            self._set_optional_string(key, "AutoConfigURL", settings.auto_config_url)
        self._broadcast_change()

    @staticmethod
    def _set_optional_string(key: Any, name: str, value: str | None) -> None:
        assert winreg is not None
        if value is None:
            with suppress(FileNotFoundError):
                winreg.DeleteValue(key, name)
            return
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    @staticmethod
    def _broadcast_change() -> None:
        if sys.platform != "win32":
            return
        internet_set_option = ctypes.windll.Wininet.InternetSetOptionW
        internet_set_option(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        internet_set_option(None, 37, None, 0)  # INTERNET_OPTION_REFRESH


def is_windows() -> bool:
    return sys.platform == "win32"


def is_user_admin() -> bool:
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


async def check_tcp_port(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


PortChecker = Callable[[str, int, float], Awaitable[bool]]


def default_clash_config_path() -> Path:
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return roaming / "io.github.clash-verge-rev.clash-verge-rev" / "config.yaml"


def _yaml_scalar(raw_value: str) -> str:
    value = re.split(r"\s+#", raw_value.strip(), maxsplit=1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        if value[0] == '"':
            try:
                decoded = json.loads(value)
                return str(decoded)
            except json.JSONDecodeError:
                pass
        return value[1:-1]
    return value


def load_clash_runtime_config(path: Path) -> ClashRuntimeConfig | None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None

    top_level: dict[str, str] = {}
    tun_enabled = False
    tun_indent: int | None = None
    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        match = re.match(r"^\s*([\w-]+)\s*:\s*(.*?)\s*$", raw_line)
        if match is None:
            continue
        key, raw_value = match.groups()
        if indent == 0:
            tun_indent = indent if key == "tun" else None
            if raw_value:
                top_level[key] = _yaml_scalar(raw_value)
            continue
        if tun_indent is not None and indent > tun_indent and key == "enable":
            tun_enabled = _yaml_scalar(raw_value).casefold() in {"true", "yes", "on", "1"}

    controller = top_level.get("external-controller", "127.0.0.1:9090")
    controller_host, separator, controller_port_text = controller.rpartition(":")
    if not separator:
        controller_host, controller_port_text = "127.0.0.1", "9090"
    try:
        mixed_port = int(top_level.get("mixed-port", "7890"))
        socks_port = int(top_level.get("socks-port", str(mixed_port)))
        controller_port = int(controller_port_text)
    except ValueError:
        return None
    return ClashRuntimeConfig(
        path=path,
        mixed_port=mixed_port,
        socks_port=socks_port,
        external_controller_host=controller_host.strip("[]") or "127.0.0.1",
        external_controller_port=controller_port,
        tun_enabled=tun_enabled,
        secret=top_level.get("secret", ""),
    )


class WindowsEnvironmentDetector:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        proxy_store: ProxyStore | None = None,
        admin_checker: Callable[[], bool] = is_user_admin,
        port_checker: PortChecker = check_tcp_port,
        openvpn_path: Path = DEFAULT_OPENVPN_PATH,
        clash_config_path: Path | None = None,
    ) -> None:
        self.runner = runner or AsyncSubprocessRunner()
        self.proxy_store = proxy_store or WindowsRegistryProxyStore()
        self.admin_checker = admin_checker
        self.port_checker = port_checker
        self.openvpn_path = openvpn_path
        self.clash_config_path = clash_config_path or default_clash_config_path()

    def resolve_openvpn(self) -> Path | None:
        if self.openvpn_path.is_file():
            return self.openvpn_path
        discovered = shutil.which("openvpn.exe" if is_windows() else "openvpn")
        return Path(discovered) if discovered else None

    async def processes(self, names: Sequence[str]) -> list[ProcessInfo]:
        if not is_windows():
            return []
        normalized = {name.casefold() for name in names}
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object Name,ProcessId,ExecutablePath,CommandLine | "
            "ConvertTo-Json -Compress"
        )
        result = await self.runner.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _powershell_utf8(script),
            ],
            timeout=8,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        found: list[ProcessInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "")
            if name.casefold() not in normalized:
                continue
            found.append(
                ProcessInfo(
                    name=name,
                    pid=int(row.get("ProcessId") or 0),
                    executable_path=str(row.get("ExecutablePath") or ""),
                    command_line=str(row.get("CommandLine") or ""),
                )
            )
        return found

    async def clash_processes(self) -> list[ProcessInfo]:
        return await self.processes(CLASH_PROCESS_NAMES)

    async def openvpn_processes(self) -> list[ProcessInfo]:
        return await self.processes(("openvpn.exe", "openvpn-gui.exe"))

    async def active_openvpn_processes(self) -> list[ProcessInfo]:
        return await self.processes(("openvpn.exe",))

    def clash_config(self) -> ClashRuntimeConfig | None:
        return load_clash_runtime_config(self.clash_config_path)

    async def tap_adapters(self) -> list[NetworkAdapterInfo]:
        if not is_windows():
            return []
        script = (
            "Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | "
            "Where-Object { $_.InterfaceDescription -match 'TAP|Wintun|OpenVPN' "
            "-or $_.Name -match 'TAP|OpenVPN' } | "
            "Select-Object Name,InterfaceDescription,Status | ConvertTo-Json -Compress"
        )
        result = await self.runner.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _powershell_utf8(script),
            ],
            timeout=8,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return [
            NetworkAdapterInfo(
                name=str(row.get("Name") or ""),
                description=str(row.get("InterfaceDescription") or ""),
                status=str(row.get("Status") or ""),
            )
            for row in rows
            if isinstance(row, dict)
        ]

    async def split_default_routes(self) -> list[RouteInfo]:
        if not is_windows():
            return []
        script = (
            "Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Where-Object { $_.DestinationPrefix -in "
            "@('0.0.0.0/1','128.0.0.0/1') } | "
            "Select-Object DestinationPrefix,NextHop,InterfaceAlias,RouteMetric,InterfaceIndex | "
            "ConvertTo-Json -Compress"
        )
        result = await self.runner.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _powershell_utf8(script),
            ],
            timeout=8,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return [
            RouteInfo(
                destination=str(row.get("DestinationPrefix") or ""),
                next_hop=str(row.get("NextHop") or ""),
                interface=str(row.get("InterfaceAlias") or ""),
                metric=int(row.get("RouteMetric") or 0),
                interface_index=int(row.get("InterfaceIndex") or 0),
            )
            for row in rows
            if isinstance(row, dict)
        ]

    @staticmethod
    def _is_openvpn_split_route(route: RouteInfo) -> bool:
        interface = route.interface.casefold()
        return (
            route.destination in SPLIT_DEFAULT_DESTINATIONS
            and route.interface_index > 0
            and any(marker in interface for marker in OPENVPN_ADAPTER_MARKERS)
        )

    async def repair_orphaned_openvpn_routes(self) -> list[RouteInfo]:
        return await self._repair_split_default_routes(allow_unverified=False)

    async def repair_split_routes_manually(self) -> list[RouteInfo]:
        return await self._repair_split_default_routes(allow_unverified=True)

    async def _repair_split_default_routes(
        self,
        *,
        allow_unverified: bool,
    ) -> list[RouteInfo]:
        if not is_windows():
            raise RuntimeError("仅 Windows 支持修复 OpenVPN 残留路由")
        if not self.admin_checker():
            raise PermissionError("修复系统路由需要以管理员身份运行程序")

        active_openvpn = await self.active_openvpn_processes()
        if active_openvpn:
            raise RuntimeError("检测到 openvpn.exe 正在运行，请先断开 VPN 后再修复")

        routes = await self.split_default_routes()
        active_openvpn = await self.active_openvpn_processes()
        if active_openvpn:
            raise RuntimeError("修复前检测到 openvpn.exe 已启动，已取消路由修改")

        candidates = [
            route
            for route in routes
            if route.interface_index > 0
            and (allow_unverified or self._is_openvpn_split_route(route))
        ]
        if not candidates:
            if routes:
                raise RuntimeError("分流路由不属于 OpenVPN/TAP/Wintun 网卡，程序不会删除")
            return []

        unique_candidates = list(
            {
                (route.destination, route.next_hop, route.interface_index): route
                for route in candidates
            }.values()
        )
        for route in unique_candidates:
            next_hop = (
                f"-NextHop '{route.next_hop}' "
                if route.next_hop
                else ""
            )
            script = (
                "Remove-NetRoute -AddressFamily IPv4 "
                f"-DestinationPrefix '{route.destination}' "
                f"{next_hop}"
                f"-InterfaceIndex {route.interface_index} "
                "-Confirm:$false -ErrorAction Stop"
            )
            result = await self.runner.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _powershell_utf8(script),
                ],
                timeout=8,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip() or "PowerShell 返回失败"
                raise RuntimeError(f"删除路由 {route.destination} 失败：{detail}")

        remaining = await self.split_default_routes()
        removed_keys = {
            (route.destination, route.next_hop, route.interface_index)
            for route in unique_candidates
        }
        still_present = [
            route
            for route in remaining
            if (route.destination, route.next_hop, route.interface_index) in removed_keys
        ]
        if still_present:
            targets = ", ".join(route.destination for route in still_present)
            raise RuntimeError(f"路由删除后仍然存在：{targets}")
        return unique_candidates

    async def flush_dns_cache(self) -> None:
        if not is_windows():
            return
        result = await self.runner.run(["ipconfig.exe", "/flushdns"], timeout=8)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Windows DNS 缓存刷新失败")

    def proxy_settings(self) -> ProxySettings:
        return self.proxy_store.read()

    async def check_environment(
        self,
        *,
        clash_host: str | None = None,
        clash_port: int | None = None,
    ) -> EnvironmentReport:
        platform_ok = is_windows()
        if not platform_ok:
            return EnvironmentReport(
                [ComponentCheck("windows", "Windows", False, "Windows is required")]
            )

        openvpn_path = self.resolve_openvpn()
        clash_config = self.clash_config()
        effective_host = clash_host or "127.0.0.1"
        effective_mixed_port = clash_port or (
            clash_config.mixed_port if clash_config is not None else 7890
        )
        effective_socks_port = (
            clash_config.socks_port if clash_config is not None else effective_mixed_port
        )
        controller_host = (
            clash_config.external_controller_host if clash_config is not None else effective_host
        )
        controller_port = (
            clash_config.external_controller_port if clash_config is not None else 9090
        )
        adapters, clash_processes, active_openvpn, split_routes = await asyncio.gather(
            self.tap_adapters(),
            self.clash_processes(),
            self.active_openvpn_processes(),
            self.split_default_routes(),
        )
        proxy_error = ""
        try:
            proxy = self.proxy_settings()
        except OSError as exc:
            proxy = None
            proxy_error = str(exc)
        mixed_open, socks_open, controller_open = await asyncio.gather(
            self.port_checker(effective_host, effective_mixed_port, 1.5),
            self.port_checker(effective_host, effective_socks_port, 1.5),
            self.port_checker(controller_host, controller_port, 1.5),
        )
        is_admin = self.admin_checker()
        windows_version = ".".join(map(str, sys.getwindowsversion().platform_version))
        repairable_routes = [
            route
            for route in split_routes
            if not active_openvpn and self._is_openvpn_split_route(route)
        ]
        route_targets = tuple(
            f"{route.destination} -> {route.next_hop or 'on-link'} | "
            f"{route.interface} (接口 {route.interface_index})"
            for route in repairable_routes
        )
        manual_route_targets = tuple(
            f"{route.destination} -> {route.next_hop or 'on-link'} | "
            f"{route.interface} (接口 {route.interface_index})"
            for route in split_routes
            if route.interface_index > 0
        )
        if not split_routes:
            split_route_detail = "未发现 OpenVPN 分流路由残留"
        elif repairable_routes:
            split_route_detail = "OpenVPN 已退出，可点击“修复环境”：" + ", ".join(
                route_targets
            )
        elif active_openvpn:
            split_route_detail = "检测到分流路由，但 openvpn.exe 正在运行，不能自动修复"
        else:
            split_route_detail = "检测到分流路由，但接口不属于 OpenVPN，程序不会删除"
        return EnvironmentReport(
            checks=[
                ComponentCheck("windows", "Windows", True, windows_version),
                ComponentCheck(
                    "administrator",
                    "Administrator",
                    is_admin,
                    "Elevated" if is_admin else "Run as administrator",
                ),
                ComponentCheck(
                    "system_proxy",
                    "System proxy",
                    proxy is not None,
                    (
                        f"enabled={proxy.enabled}, server={proxy.server or '-'}"
                        if proxy is not None
                        else proxy_error
                    ),
                ),
                ComponentCheck(
                    "openvpn",
                    "OpenVPN",
                    openvpn_path is not None,
                    str(openvpn_path) if openvpn_path else "openvpn.exe not found",
                ),
                ComponentCheck(
                    "tap_adapter",
                    "OpenVPN adapter",
                    bool(adapters),
                    ", ".join(adapter.name for adapter in adapters) or "No TAP/Wintun adapter",
                ),
                ComponentCheck(
                    "clash_config",
                    "Clash configuration",
                    clash_config is not None,
                    (
                        f"mixed={effective_mixed_port}, socks={effective_socks_port}, "
                        f"controller={controller_host}:{controller_port}, "
                        f"tun={clash_config.tun_enabled}, "
                        f"secret={'configured' if clash_config.has_secret else 'empty'}"
                        if clash_config is not None
                        else "Clash Verge Rev config.yaml not found"
                    ),
                    required=False,
                ),
                ComponentCheck(
                    "clash_process",
                    "Clash process",
                    bool(clash_processes),
                    ", ".join(f"{item.name} ({item.pid})" for item in clash_processes)
                    or "Not running",
                    required=False,
                ),
                ComponentCheck(
                    "clash_port",
                    "Clash mixed port",
                    mixed_open,
                    (
                        f"{effective_host}:{effective_mixed_port} "
                        f"{'open' if mixed_open else 'closed'}"
                    ),
                    required=False,
                ),
                ComponentCheck(
                    "clash_socks_port",
                    "Clash SOCKS port",
                    socks_open,
                    (
                        f"{effective_host}:{effective_socks_port} "
                        f"{'open' if socks_open else 'closed'}"
                    ),
                    required=False,
                ),
                ComponentCheck(
                    "clash_controller",
                    "Clash controller",
                    controller_open,
                    (
                        f"{controller_host}:{controller_port} "
                        f"{'open' if controller_open else 'closed'}"
                    ),
                    required=False,
                ),
                ComponentCheck(
                    "external_openvpn_conflict",
                    "External OpenVPN conflict",
                    not active_openvpn,
                    (
                        "No active external openvpn.exe"
                        if not active_openvpn
                        else "Detected without modifying: "
                        + ", ".join(str(item.pid) for item in active_openvpn)
                    ),
                ),
                ComponentCheck(
                    "split_default_route_conflict",
                    "OpenVPN 残留路由",
                    not split_routes,
                    split_route_detail,
                    repair_action=(
                        REPAIR_ORPHANED_OPENVPN_ROUTES if repairable_routes else ""
                    ),
                    repair_targets=route_targets,
                    manual_repair_action=(
                        REPAIR_SPLIT_ROUTES_MANUALLY
                        if manual_route_targets and not active_openvpn
                        else ""
                    ),
                    manual_repair_targets=manual_route_targets,
                ),
            ]
        )


def find_available_executable(candidates: Sequence[Path], command: str) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    discovered = shutil.which(command)
    return Path(discovered) if discovered else None


def local_hostname() -> str:
    return socket.gethostname()


def local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
