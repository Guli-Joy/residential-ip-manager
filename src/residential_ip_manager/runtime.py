from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from residential_ip_manager.application.orchestrator import ConnectionOrchestrator
from residential_ip_manager.config import AppSettings
from residential_ip_manager.platform.windows_environment import (
    REPAIR_ORPHANED_OPENVPN_ROUTES,
    WindowsEnvironmentDetector,
)
from residential_ip_manager.ui.main_window import MainWindow

LOGGER = logging.getLogger(__name__)


class AsyncRuntime:
    """Own one asyncio loop outside Qt's presentation thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="application-asyncio",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("后台任务线程启动超时")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        if self._loop is None or not self._thread.is_alive():
            coroutine.close()
            raise RuntimeError("后台任务线程未运行")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def stop(self, timeout: float = 10.0) -> None:
        loop = self._loop
        if loop is None or not self._thread.is_alive():
            return
        loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=timeout)


class DesktopBridge(QObject):
    snapshot_received = Signal(object)
    nodes_received = Signal(object)
    log_received = Signal(str, str)
    loading_received = Signal(bool, str)
    environment_received = Signal(object)
    environment_failed = Signal(str)
    environment_repair_failed = Signal(str)
    operation_finished = Signal(str, bool, str)

    def __init__(
        self,
        *,
        window: MainWindow,
        orchestrator: ConnectionOrchestrator,
        settings: AppSettings,
        settings_path: Path,
        detector: WindowsEnvironmentDetector,
        runtime: AsyncRuntime | None = None,
    ) -> None:
        super().__init__(window)
        self.window = window
        self.orchestrator = orchestrator
        self.settings = settings
        self.settings_path = settings_path
        self.detector = detector
        self.runtime = runtime or AsyncRuntime()
        self._closing = False

        self.snapshot_received.connect(window.set_snapshot)
        self.nodes_received.connect(window.set_nodes)
        self.log_received.connect(window.append_log)
        self.loading_received.connect(window.set_loading)
        self.environment_received.connect(window.set_environment_report)
        self.environment_failed.connect(window.set_environment_error)
        self.environment_repair_failed.connect(window.show_environment_repair_error)
        self.operation_finished.connect(self._operation_completed)

        orchestrator.add_snapshot_listener(self.snapshot_received.emit)
        orchestrator.add_node_listener(lambda nodes: self.nodes_received.emit(list(nodes)))
        orchestrator.add_log_listener(self.log_received.emit)

        window.refresh_requested.connect(self.refresh)
        window.connect_requested.connect(self.request_connect)
        window.switch_ip_requested.connect(self.switch_ip)
        window.disconnect_requested.connect(self.request_disconnect)
        window.environment_check_requested.connect(self.check_environment)
        window.environment_repair_requested.connect(self.repair_environment)
        window.country_filter_changed.connect(self.set_country_filter)
        window.strict_home_changed.connect(self.set_strict_home)
        window.auto_failover_changed.connect(self.set_auto_failover)

    def start(self) -> None:
        self.window.set_loading(True, "正在载入节点数据库…")
        self._submit("startup", self._initialize())

    async def _initialize(self) -> None:
        await self.orchestrator.initialize()
        self.orchestrator.start_background_tasks()
        cached_nodes = self.orchestrator.node_pool.eligible_nodes()
        if cached_nodes:
            self.loading_received.emit(False, "")
            self.log_received.emit(
                "info",
                f"已立即载入 {len(cached_nodes)} 个缓存可用节点，实时刷新将在后台进行",
            )
        else:
            self.loading_received.emit(True, "首次运行，正在准备可用节点…")

        report = await self.detector.check_environment(
            clash_host=self.settings.clash_host,
            clash_port=self.settings.clash_port,
        )
        self.environment_received.emit(report)
        if cached_nodes:
            return

        self.loading_received.emit(True, "正在获取并筛选 VPNGate 实时家宽节点…")
        await self.orchestrator.refresh_nodes()

    @Slot()
    def refresh(self) -> None:
        self.window.set_loading(True, "正在刷新严格家庭宽带节点…")
        self._submit("refresh", self.orchestrator.refresh_nodes())

    @Slot(str)
    def request_connect(self, node_id: str) -> None:
        self._submit(
            "connect",
            self.orchestrator.connect(
                node_id or None,
                country=self.settings.country_filter,
            ),
        )

    @Slot(str)
    def switch_ip(self, _country: str) -> None:
        self._submit("switch", self.orchestrator.failover(reason="用户手动更换出口 IP"))

    @Slot()
    def request_disconnect(self) -> None:
        self._submit("disconnect", self.orchestrator.disconnect())

    @Slot()
    def check_environment(self) -> None:
        self._submit("environment", self._check_environment())

    async def _check_environment(self) -> None:
        try:
            report = await self.detector.check_environment(
                clash_host=self.settings.clash_host,
                clash_port=self.settings.clash_port,
            )
        except Exception as exc:
            self.environment_failed.emit(str(exc))
            raise
        self.environment_received.emit(report)

    @Slot(str)
    def repair_environment(self, action: str) -> None:
        self._submit("environment_repair", self._repair_environment(action))

    async def _repair_environment(self, action: str) -> None:
        repair_error = ""
        try:
            if action != REPAIR_ORPHANED_OPENVPN_ROUTES:
                raise ValueError(f"不支持的环境修复操作：{action}")
            removed = await self.detector.repair_orphaned_openvpn_routes()
            if removed:
                targets = ", ".join(route.destination for route in removed)
                self.log_received.emit("info", f"已清理 OpenVPN 残留路由：{targets}")
        except Exception as exc:
            repair_error = str(exc)

        try:
            await self._check_environment()
        except Exception:
            if not repair_error:
                repair_error = "路由修复已执行，但环境复检失败"
        if repair_error:
            self.environment_repair_failed.emit(repair_error)

    @Slot(str)
    def set_country_filter(self, country: str) -> None:
        self.settings.country_filter = country
        self._save_settings()

    @Slot(bool)
    def set_strict_home(self, enabled: bool) -> None:
        self.settings.strict_home_only = enabled
        self.orchestrator.node_pool.strict_home_only = enabled
        self._save_settings()

    @Slot(bool)
    def set_auto_failover(self, enabled: bool) -> None:
        self.settings.auto_failover = enabled
        self._save_settings()

    def _save_settings(self) -> None:
        try:
            self.settings.save(self.settings_path)
        except OSError as exc:
            self.log_received.emit("warning", f"设置保存失败：{exc}")

    def _submit(self, operation: str, coroutine: Coroutine[Any, Any, Any]) -> None:
        if self._closing:
            coroutine.close()
            return
        try:
            future = self.runtime.submit(coroutine)
        except Exception as exc:
            self.operation_finished.emit(operation, False, str(exc))
            return

        def completed(result: concurrent.futures.Future[Any]) -> None:
            try:
                result.result()
            except concurrent.futures.CancelledError:
                self.operation_finished.emit(operation, False, "操作已取消")
            except Exception as exc:
                LOGGER.exception("operation %s failed", operation, exc_info=exc)
                self.operation_finished.emit(operation, False, str(exc))
            else:
                self.operation_finished.emit(operation, True, "")

        future.add_done_callback(completed)

    @Slot(str, bool, str)
    def _operation_completed(self, operation: str, succeeded: bool, message: str) -> None:
        if operation in {"startup", "refresh"}:
            self.window.set_loading(False)
            if not succeeded:
                detail = message or "节点刷新失败"
                if self.window.node_model.rowCount() > 0:
                    self.window.append_log("warning", f"{detail}，继续使用缓存节点")
                else:
                    self.window.set_error(detail)
        if not succeeded and operation not in {"startup", "refresh", "environment"}:
            self.window.append_log("error", message or f"{operation} 操作失败")

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            future = self.runtime.submit(self.orchestrator.close())
            future.result(timeout=15)
        except Exception as exc:
            LOGGER.warning("application cleanup failed: %s", exc)
        finally:
            self.runtime.stop()
