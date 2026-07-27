from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import UTC, datetime  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from residential_ip_manager.domain.models import (  # noqa: E402
    ConnectionSnapshot,
    ConnectionState,
    NodeStatus,
    PurityGrade,
    VpnNode,
)
from residential_ip_manager.ui.main_window import MainWindow  # noqa: E402
from residential_ip_manager.ui.models import NodeColumn  # noqa: E402


def _application() -> QApplication:
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


def test_cached_nodes_do_not_cancel_active_loading_state() -> None:
    _application()
    window = MainWindow()
    node = VpnNode(
        id="jp-1",
        ip="203.0.113.10",
        remote_host="203.0.113.10",
        remote_port=443,
        protocol="tcp",
        country_code="JP",
        country="Japan",
        purity_grade=PurityGrade.STRICT_HOME,
        status=NodeStatus.AVAILABLE,
    )

    window.set_loading(True, "正在刷新实时节点…")
    window.set_nodes([node])

    assert window.content_pages.currentIndex() == window.PAGE_LOADING
    assert window.country_combo.itemText(1) == "日本 (JP)"
    assert window.node_table.horizontalHeader().sortIndicatorSection() == NodeColumn.COUNTRY
    assert window.progress_bar.maximum() == 0
    assert window.progress_label.text() == "正在刷新实时节点…"
    assert not window.connect_button.isEnabled()

    window.set_loading(False)

    assert window.content_pages.currentIndex() == window.PAGE_TABLE
    assert window.progress_bar.maximum() == 100
    assert window.progress_bar.value() == 0
    assert window.progress_label.text() == "空闲"
    window.close()


def test_disconnect_button_and_connected_summary_follow_connection_context() -> None:
    _application()
    window = MainWindow()

    assert not window.disconnect_button.isEnabled()

    window.set_snapshot(
        ConnectionSnapshot(
            state=ConnectionState.CONNECTED,
            active_node_id="jp-1",
            exit_ip="198.51.100.10",
            connected_since=datetime.now(UTC),
            metadata={
                "country": "Japan",
                "country_code": "JP",
                "isp": "NTT Communications",
                "latency_ms": 163,
                "clash_running": True,
                "tunnel_connected": True,
                "exit_verified": True,
            },
        )
    )

    assert window.disconnect_button.isEnabled()
    assert not window.connect_button.isEnabled()
    assert window.exit_ip_metric.value_label.text() == "198.51.100.10"
    assert window.country_metric.value_label.text() == "日本 (JP)"
    assert window.isp_metric.value_label.text() == "NTT Communications"
    assert window.latency_metric.value_label.text() == "163 ms"

    window.set_snapshot(ConnectionSnapshot(state=ConnectionState.IDLE, message="已断开"))

    assert not window.disconnect_button.isEnabled()
    assert window.connect_button.isEnabled()
    assert window.exit_ip_metric.value_label.text() == "-"
    assert window.country_metric.value_label.text() == "-"
    assert window.duration_metric.value_label.text() == "-"
    window.close()


def test_error_with_connection_context_keeps_only_disconnect_available() -> None:
    _application()
    window = MainWindow()
    window.set_snapshot(
        ConnectionSnapshot(
            state=ConnectionState.ERROR,
            active_node_id="jp-1",
            exit_ip="198.51.100.10",
            connected_since=datetime.now(UTC),
            metadata={"tunnel_connected": True},
        )
    )

    assert window.disconnect_button.isEnabled()
    assert not window.connect_button.isEnabled()
    window.close()
