from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import ComponentCheck, EnvironmentReport, VpnNode
from residential_ip_manager.platform.windows_environment import (
    DEFAULT_OPENVPN_PATH,
    AsyncSubprocessRunner,
    CommandRunner,
    ManagedProcess,
    WindowsEnvironmentDetector,
    is_user_admin,
    is_windows,
)

LogListener = Callable[[str, str], None]


class OpenVpnController:
    SUCCESS_MARKER = "Initialization Sequence Completed"
    AUTH_FAILURE_MARKER = "AUTH_FAILED"

    def __init__(
        self,
        *,
        executable: Path = DEFAULT_OPENVPN_PATH,
        runner: CommandRunner | None = None,
        detector: WindowsEnvironmentDetector | None = None,
        admin_checker: Callable[[], bool] = is_user_admin,
        connect_timeout: float = 45.0,
        shutdown_timeout: float = 5.0,
        log_listener: LogListener | None = None,
    ) -> None:
        self.executable = executable
        self.runner = runner or AsyncSubprocessRunner()
        self.detector = detector or WindowsEnvironmentDetector(
            runner=self.runner,
            admin_checker=admin_checker,
            openvpn_path=executable,
        )
        self.admin_checker = admin_checker
        self.connect_timeout = connect_timeout
        self.shutdown_timeout = shutdown_timeout
        self.log_listener = log_listener
        self._owned_process: ManagedProcess | None = None
        self._connected = False
        self._operation_lock = asyncio.Lock()

    @property
    def owned_pid(self) -> int | None:
        process = self._owned_process
        return process.pid if process is not None and process.returncode is None else None

    async def check_environment(self) -> EnvironmentReport:
        if not is_windows():
            return EnvironmentReport(
                [ComponentCheck("windows", "Windows", False, "Windows is required")]
            )
        executable = self.detector.resolve_openvpn()
        adapters, processes, split_routes = await asyncio.gather(
            self.detector.tap_adapters(),
            self.detector.active_openvpn_processes(),
            self.detector.split_default_routes(),
        )
        is_admin = self.admin_checker()
        return EnvironmentReport(
            checks=[
                ComponentCheck(
                    "administrator",
                    "Administrator",
                    is_admin,
                    "Elevated" if is_admin else "Run as administrator",
                ),
                ComponentCheck(
                    "openvpn",
                    "OpenVPN",
                    executable is not None,
                    str(executable) if executable else "openvpn.exe not found",
                ),
                ComponentCheck(
                    "tap_adapter",
                    "OpenVPN adapter",
                    bool(adapters),
                    ", ".join(adapter.name for adapter in adapters) or "No TAP/Wintun adapter",
                ),
                ComponentCheck(
                    "external_openvpn",
                    "External OpenVPN conflict",
                    not processes,
                    (
                        "No external OpenVPN process"
                        if not processes
                        else "Existing process will not be modified: "
                        + ", ".join(str(item.pid) for item in processes)
                    ),
                ),
                ComponentCheck(
                    "split_default_routes",
                    "Split default route conflict",
                    not split_routes,
                    (
                        "No existing split-default routes"
                        if not split_routes
                        else "Detected without modifying: "
                        + ", ".join(
                            f"{item.destination} via {item.next_hop} ({item.interface})"
                            for item in split_routes
                        )
                    ),
                ),
            ]
        )

    async def repair_environment(self) -> bool:
        """Repair only orphaned split routes when no OpenVPN process owns them."""
        async with self._operation_lock:
            if self._owned_process is not None and self._owned_process.returncode is None:
                return False
            processes, routes = await asyncio.gather(
                self.detector.active_openvpn_processes(),
                self.detector.split_default_routes(),
            )
            if processes or not routes:
                return False
            removed = await self.detector.repair_orphaned_openvpn_routes()
            if removed:
                await self.detector.flush_dns_cache()
            if removed and self.log_listener is not None:
                destinations = ", ".join(route.destination for route in removed)
                self.log_listener("openvpn", f"已自动修复 OpenVPN 残留路由：{destinations}")
            return bool(removed)

    async def connect(self, node: VpnNode, config_path: Path) -> None:
        del node  # Connection identity is retained by the application state machine.
        async with self._operation_lock:
            if not is_windows():
                raise AppError(ErrorCode.ENVIRONMENT_NOT_READY, "OpenVPN requires Windows")
            if not self.admin_checker():
                raise AppError(
                    ErrorCode.ENVIRONMENT_NOT_READY,
                    "OpenVPN must be started with administrator privileges",
                )
            if not self.executable.is_file():
                raise AppError(ErrorCode.OPENVPN_NOT_FOUND, "openvpn.exe was not found")
            if not config_path.is_file():
                raise AppError(
                    ErrorCode.OPENVPN_CONNECT_FAILED,
                    "OpenVPN configuration file was not found",
                    detail=str(config_path),
                )

            await self._stop_owned_process()
            external_processes, split_routes = await asyncio.gather(
                self.detector.active_openvpn_processes(),
                self.detector.split_default_routes(),
            )
            if external_processes or split_routes:
                details: list[str] = []
                if external_processes:
                    details.append(
                        "external openvpn.exe PID="
                        + ",".join(str(item.pid) for item in external_processes)
                    )
                if split_routes:
                    details.append("routes=" + ",".join(item.destination for item in split_routes))
                raise AppError(
                    ErrorCode.ENVIRONMENT_NOT_READY,
                    "An existing OpenVPN connection conflicts with a new tunnel",
                    detail="; ".join(details),
                )
            process = await self.runner.start(
                [str(self.executable), "--config", str(config_path), "--verb", "3"],
                cwd=config_path.parent,
                capture_output=True,
            )
            self._owned_process = process
            self._connected = False
            try:
                await self._wait_until_connected(process)
            except BaseException:
                try:
                    await self._stop_owned_process()
                except Exception as cleanup_error:
                    if self.log_listener is not None:
                        self.log_listener(
                            "openvpn",
                            f"连接失败后的残留路由清理未完成：{cleanup_error}",
                        )
                raise

    async def _wait_until_connected(self, process: ManagedProcess) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        recent_lines: list[str] = []
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AppError(
                    ErrorCode.OPENVPN_CONNECT_FAILED,
                    "OpenVPN connection timed out",
                    detail="\n".join(recent_lines[-10:]),
                )
            try:
                line = await asyncio.wait_for(process.readline(), timeout=remaining)
            except TimeoutError as exc:
                raise AppError(
                    ErrorCode.OPENVPN_CONNECT_FAILED,
                    "OpenVPN connection timed out",
                    detail="\n".join(recent_lines[-10:]),
                ) from exc
            stripped = line.strip()
            if stripped:
                recent_lines.append(stripped)
                if self.log_listener is not None:
                    self.log_listener("openvpn", stripped)
                if self.AUTH_FAILURE_MARKER in stripped:
                    raise AppError(
                        ErrorCode.OPENVPN_AUTH_FAILED,
                        "OpenVPN authentication failed",
                        detail=stripped,
                    )
                if self.SUCCESS_MARKER in stripped:
                    self._connected = True
                    return
            elif process.returncode is not None:
                raise AppError(
                    ErrorCode.OPENVPN_CONNECT_FAILED,
                    f"OpenVPN exited with code {process.returncode}",
                    detail="\n".join(recent_lines[-10:]),
                )
            else:
                await asyncio.sleep(0.05)

    async def disconnect(self) -> None:
        async with self._operation_lock:
            await self._stop_owned_process()

    async def _stop_owned_process(self) -> None:
        process = self._owned_process
        self._connected = False
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
            except TimeoutError:
                process.kill()
                await process.wait()

        removed = await self.detector.repair_orphaned_openvpn_routes()
        self._owned_process = None
        if removed and self.log_listener is not None:
            destinations = ", ".join(route.destination for route in removed)
            self.log_listener("openvpn", f"已自动清理 OpenVPN 残留路由：{destinations}")
        try:
            await self.detector.flush_dns_cache()
        except Exception as error:
            if self.log_listener is not None:
                self.log_listener("openvpn", f"DNS 缓存刷新失败：{error}")

    async def is_connected(self) -> bool:
        process = self._owned_process
        if process is not None and process.returncode is not None:
            self._connected = False
        return self._connected and self._owned_process is not None


OpenVPNController = OpenVpnController
