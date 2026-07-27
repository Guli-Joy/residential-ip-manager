from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from residential_ip_manager.application.node_pool import NodePool  # noqa: E402
from residential_ip_manager.config import AppSettings  # noqa: E402
from residential_ip_manager.domain.models import (  # noqa: E402
    ComponentCheck,
    EnvironmentReport,
    NodeStatus,
    PurityGrade,
    VpnNode,
)
from residential_ip_manager.platform.windows_environment import (  # noqa: E402
    REPAIR_ORPHANED_OPENVPN_ROUTES,
    RouteInfo,
)
from residential_ip_manager.runtime import AsyncRuntime, DesktopBridge  # noqa: E402
from residential_ip_manager.ui.main_window import MainWindow  # noqa: E402


def _application() -> QApplication:
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


class ListenerOnlyOrchestrator:
    def add_snapshot_listener(self, _listener: object) -> None:
        return None

    def add_node_listener(self, _listener: object) -> None:
        return None

    def add_log_listener(self, _listener: object) -> None:
        return None


class InertRuntime:
    pass


class StartupOrchestrator(ListenerOnlyOrchestrator):
    def __init__(self, nodes: list[VpnNode]) -> None:
        self.node_pool = NodePool()
        self.node_pool.replace(nodes)
        self.initialize_calls = 0
        self.refresh_calls = 0
        self.background_started = False

    async def initialize(self) -> None:
        self.initialize_calls += 1

    def start_background_tasks(self) -> None:
        self.background_started = True

    async def refresh_nodes(self) -> list[VpnNode]:
        self.refresh_calls += 1
        return []


def _cached_node() -> VpnNode:
    return VpnNode(
        id="jp-1",
        ip="198.51.100.10",
        remote_host="198.51.100.10",
        remote_port=443,
        protocol="tcp",
        country_code="JP",
        country="Japan",
        purity_grade=PurityGrade.STRICT_HOME,
        status=NodeStatus.AVAILABLE,
    )


class RepairingDetector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def repair_orphaned_openvpn_routes(self) -> list[RouteInfo]:
        self.calls.append("repair")
        return [RouteInfo("0.0.0.0/1", "10.211.1.42", "OpenVPN TAP-Windows6", 0, 5)]

    async def check_environment(
        self,
        *,
        clash_host: str | None = None,
        clash_port: int | None = None,
    ) -> EnvironmentReport:
        assert clash_host == "127.0.0.1"
        assert clash_port == 7890
        self.calls.append("check")
        return EnvironmentReport([ComponentCheck("routes", "残留路由", True, "已清理")])


def test_async_runtime_executes_and_stops() -> None:
    runtime = AsyncRuntime()
    try:
        future = runtime.submit(asyncio.sleep(0, result="ok"))
        assert future.result(timeout=2) == "ok"
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_startup_uses_available_cache_without_waiting_for_refresh(tmp_path: Path) -> None:
    _application()
    window = MainWindow()
    orchestrator = StartupOrchestrator([_cached_node()])
    detector = RepairingDetector()
    bridge = DesktopBridge(
        window=window,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        settings=AppSettings(),
        settings_path=tmp_path / "settings.json",
        detector=detector,  # type: ignore[arg-type]
        runtime=InertRuntime(),  # type: ignore[arg-type]
    )
    loading_events: list[bool] = []
    bridge.loading_received.connect(lambda loading, _message: loading_events.append(loading))

    await bridge._initialize()

    assert orchestrator.initialize_calls == 1
    assert orchestrator.background_started
    assert orchestrator.refresh_calls == 0
    assert loading_events == [False]
    assert detector.calls == ["check"]
    window.close()


@pytest.mark.asyncio
async def test_first_start_refreshes_when_no_available_cache(tmp_path: Path) -> None:
    _application()
    window = MainWindow()
    orchestrator = StartupOrchestrator([])
    detector = RepairingDetector()
    bridge = DesktopBridge(
        window=window,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        settings=AppSettings(),
        settings_path=tmp_path / "settings.json",
        detector=detector,  # type: ignore[arg-type]
        runtime=InertRuntime(),  # type: ignore[arg-type]
    )

    await bridge._initialize()

    assert orchestrator.refresh_calls == 1
    assert detector.calls == ["check"]
    window.close()


@pytest.mark.asyncio
async def test_environment_repair_automatically_rechecks_and_refreshes_report(
    tmp_path: Path,
) -> None:
    _application()
    window = MainWindow()
    detector = RepairingDetector()
    bridge = DesktopBridge(
        window=window,
        orchestrator=ListenerOnlyOrchestrator(),  # type: ignore[arg-type]
        settings=AppSettings(),
        settings_path=tmp_path / "settings.json",
        detector=detector,  # type: ignore[arg-type]
        runtime=InertRuntime(),  # type: ignore[arg-type]
    )
    reports: list[EnvironmentReport] = []
    bridge.environment_received.connect(reports.append)

    await bridge._repair_environment(REPAIR_ORPHANED_OPENVPN_ROUTES)

    assert detector.calls == ["repair", "check"]
    assert reports and reports[-1].ready
    assert window._environment_dialog is not None
    assert window._environment_dialog.summary_title.text() == "环境已就绪"
    window.close()
