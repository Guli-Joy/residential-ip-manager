from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path

import yaml

from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import ComponentCheck, EnvironmentReport
from residential_ip_manager.platform.windows_environment import (
    AsyncSubprocessRunner,
    CommandRunner,
    ManagedProcess,
    PortChecker,
    WindowsEnvironmentDetector,
    check_tcp_port,
    find_available_executable,
    is_windows,
    local_app_data,
)

Sleep = Callable[[float], Awaitable[None]]
_MAX_CONFIG_BYTES = 8 * 1024 * 1024
_MIHOMO_PROCESS_NAMES = {"clash.exe", "mihomo.exe", "verge-mihomo.exe"}


def load_clash_proxy_hosts(path: Path) -> tuple[str, ...]:
    """Read only proxy server hosts from Clash's generated YAML configuration."""

    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            return ()
        payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return ()
    if not isinstance(payload, dict):
        return ()
    proxies = payload.get("proxies")
    if not isinstance(proxies, list):
        return ()

    hosts: list[str] = []
    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue
        raw_host = proxy.get("server")
        if not isinstance(raw_host, str):
            continue
        host = raw_host.strip().strip("[]").rstrip(".")
        if not host or len(host) > 253 or any(character.isspace() for character in host):
            continue
        try:
            normalized = str(ipaddress.ip_address(host))
        except ValueError:
            try:
                host.encode("idna")
            except UnicodeError:
                continue
            normalized = host
        if normalized not in hosts:
            hosts.append(normalized)
    return tuple(hosts)


def default_clash_candidates() -> tuple[Path, ...]:
    local = local_app_data()
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    return (
        program_files / "Clash Verge" / "clash-verge.exe",
        program_files / "Clash Verge Rev" / "clash-verge.exe",
        local / "Programs" / "clash-verge" / "clash-verge.exe",
        local / "Clash Verge" / "clash-verge.exe",
    )


class ClashVergeController:
    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        socks_port: int | None = None,
        executable: Path | None = None,
        executable_candidates: Sequence[Path] | None = None,
        runner: CommandRunner | None = None,
        detector: WindowsEnvironmentDetector | None = None,
        port_checker: PortChecker = check_tcp_port,
        sleep: Sleep = asyncio.sleep,
        startup_timeout: float = 15.0,
        poll_interval: float = 0.25,
        curl_executable: str = "curl.exe",
        probe_url: str = "https://api.ipify.org",
        active_config_path: Path | None = None,
    ) -> None:
        self.runner = runner or AsyncSubprocessRunner()
        self.detector = detector or WindowsEnvironmentDetector(runner=self.runner)
        runtime_config = self.detector.clash_config()
        self.host = host or "127.0.0.1"
        self.port = port or (runtime_config.mixed_port if runtime_config is not None else 7890)
        self.socks_port = socks_port or (
            runtime_config.socks_port if runtime_config is not None else self.port
        )
        self.controller_host = (
            runtime_config.external_controller_host if runtime_config is not None else self.host
        )
        self.controller_port = (
            runtime_config.external_controller_port if runtime_config is not None else 9090
        )
        self._controller_secret = runtime_config.secret if runtime_config is not None else ""
        self.runtime_config_path = runtime_config.path if runtime_config is not None else None
        self.active_config_path = active_config_path or (
            runtime_config.path.with_name("clash-verge.yaml")
            if runtime_config is not None
            else None
        )
        self.tun_enabled = runtime_config.tun_enabled if runtime_config is not None else None
        self.executable = executable
        self.executable_candidates = tuple(executable_candidates or default_clash_candidates())
        self.port_checker = port_checker
        self.sleep = sleep
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.curl_executable = curl_executable
        self.probe_url = probe_url
        self._owned_process: ManagedProcess | None = None
        self._operation_lock = asyncio.Lock()

    @property
    def owned_pid(self) -> int | None:
        process = self._owned_process
        return process.pid if process is not None and process.returncode is None else None

    def resolve_executable(self) -> Path | None:
        if self.executable is not None:
            return self.executable if self.executable.is_file() else None
        return find_available_executable(self.executable_candidates, "clash-verge.exe")

    async def check_environment(self) -> EnvironmentReport:
        if not is_windows():
            return EnvironmentReport(
                [ComponentCheck("windows", "Windows", False, "Windows is required")]
            )
        processes, port_open = await asyncio.gather(
            self.detector.clash_processes(),
            self.port_checker(self.host, self.port, 1.5),
        )
        executable = self.resolve_executable()
        install_ok = bool(processes) or executable is not None
        return EnvironmentReport(
            checks=[
                ComponentCheck(
                    "clash_config",
                    "Clash configuration",
                    self.runtime_config_path is not None,
                    (
                        f"mixed={self.port}, socks={self.socks_port}, "
                        f"controller={self.controller_host}:{self.controller_port}, "
                        f"tun={self.tun_enabled}, "
                        f"secret={'configured' if self._controller_secret else 'empty'}"
                        if self.runtime_config_path is not None
                        else "Using fallback port defaults"
                    ),
                    required=False,
                ),
                ComponentCheck(
                    "clash_install",
                    "Clash Verge",
                    install_ok,
                    (
                        str(executable)
                        if executable is not None
                        else ("Running" if processes else "Clash Verge executable not found")
                    ),
                ),
                ComponentCheck(
                    "clash_process",
                    "Clash process",
                    bool(processes),
                    ", ".join(f"{item.name} ({item.pid})" for item in processes) or "Not running",
                    required=False,
                ),
                ComponentCheck(
                    "clash_port",
                    "Clash mixed port",
                    port_open,
                    f"{self.host}:{self.port} {'open' if port_open else 'closed'}",
                    required=False,
                ),
            ]
        )

    async def ensure_running(self) -> None:
        async with self._operation_lock:
            if await self.port_checker(self.host, self.port, 1.0):
                return

            existing = await self.detector.clash_processes()
            if existing:
                if await self._wait_for_port():
                    return
                raise AppError(
                    ErrorCode.CLASH_PORT_UNAVAILABLE,
                    "Clash is running but its mixed port is unavailable",
                    detail=f"{self.host}:{self.port}",
                )

            executable = self.resolve_executable()
            if executable is None:
                raise AppError(ErrorCode.CLASH_NOT_FOUND, "Clash Verge executable was not found")

            process = await self.runner.start([str(executable)], capture_output=False)
            self._owned_process = process
            try:
                if await self._wait_for_port(process):
                    return
            except BaseException:
                await self._stop_owned_process()
                raise

            await self._stop_owned_process()
            raise AppError(
                ErrorCode.CLASH_PORT_UNAVAILABLE,
                "Clash started but its mixed port did not become available",
                detail=f"{self.host}:{self.port}",
            )

    async def _wait_for_port(self, process: ManagedProcess | None = None) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout
        while loop.time() < deadline:
            if await self.port_checker(self.host, self.port, min(1.0, self.poll_interval + 0.1)):
                return True
            if process is not None and process.returncode is not None:
                return False
            await self.sleep(self.poll_interval)
        return False

    async def proxy_exit_ip(self) -> str:
        errors: list[str] = []
        ports = tuple(dict.fromkeys((self.socks_port, self.port)))
        for proxy_port in ports:
            proxy_url = f"socks5h://{self.host}:{proxy_port}"
            try:
                result = await self.runner.run(
                    [
                        self.curl_executable,
                        "--silent",
                        "--show-error",
                        "--fail",
                        "--max-time",
                        "15",
                        "--proxy",
                        proxy_url,
                        self.probe_url,
                    ],
                    timeout=20,
                )
            except (OSError, TimeoutError) as exc:
                errors.append(f"{self.host}:{proxy_port}: {exc}")
                continue
            candidate = result.stdout.strip()
            if result.returncode != 0:
                errors.append(f"{self.host}:{proxy_port}: {result.stderr.strip() or 'curl failed'}")
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                errors.append(f"{self.host}:{proxy_port}: invalid response")
                continue
            return candidate
        raise AppError(
            ErrorCode.CLASH_PORT_UNAVAILABLE,
            "Unable to query the Clash proxy exit",
            detail="; ".join(errors),
        )

    async def discover_bypass_ips(self) -> tuple[str, ...]:
        """Resolve Clash upstreams that must remain on the physical gateway."""

        active_task = asyncio.create_task(self._active_remote_ips())
        config_ips: tuple[str, ...] = ()
        if self.active_config_path is not None:
            hosts = await asyncio.to_thread(load_clash_proxy_hosts, self.active_config_path)
            config_ips = await self._resolve_public_ips(hosts)
        active_ips = await active_task
        return tuple(dict.fromkeys((*active_ips, *config_ips)))

    async def _active_remote_ips(self) -> tuple[str, ...]:
        try:
            processes = await self.detector.clash_processes()
            pids = sorted(
                {
                    process.pid
                    for process in processes
                    if process.pid > 0 and process.name.casefold() in _MIHOMO_PROCESS_NAMES
                }
            )
            if not pids:
                return ()
            pid_list = ",".join(str(pid) for pid in pids)
            script = (
                f"$pids=@({pid_list}); "
                "@(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | "
                "Where-Object { $_.OwningProcess -in $pids } | "
                "Select-Object -ExpandProperty RemoteAddress -Unique) | "
                "ConvertTo-Json -Compress"
            )
            result = await self.runner.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=8,
            )
        except (OSError, TimeoutError, ValueError):
            return ()
        if result.returncode != 0 or not result.stdout.strip():
            return ()
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ()
        values = payload if isinstance(payload, list) else [payload]
        return self._public_ip_values(values)

    async def _resolve_public_ips(self, hosts: Sequence[str]) -> tuple[str, ...]:
        semaphore = asyncio.Semaphore(16)
        loop = asyncio.get_running_loop()

        async def resolve_host(host: str) -> tuple[str, ...]:
            direct = self._public_ip_values([host])
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                return direct
            async with semaphore:
                try:
                    infos = await asyncio.wait_for(
                        loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
                        timeout=4,
                    )
                except (OSError, TimeoutError, UnicodeError):
                    return ()
            return self._public_ip_values(info[4][0] for info in infos)

        resolved = await asyncio.gather(*(resolve_host(host) for host in hosts))
        return tuple(dict.fromkeys(ip for group in resolved for ip in group))

    @staticmethod
    def _public_ip_values(values: Iterable[object]) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            try:
                address = ipaddress.ip_address(str(value))
            except ValueError:
                continue
            normalized = str(address)
            if address.is_global and normalized not in result:
                result.append(normalized)
        return tuple(result)

    async def stop(self) -> None:
        """Stop Clash only when this controller started that exact process."""
        async with self._operation_lock:
            await self._stop_owned_process()

    async def _stop_owned_process(self) -> None:
        process = self._owned_process
        self._owned_process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


ClashController = ClashVergeController
