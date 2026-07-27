from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QCoreApplication, Qt

from residential_ip_manager.domain.models import (
    ComponentCheck,
    EnvironmentReport,
    NodeStatus,
    PurityGrade,
    VpnNode,
)
from residential_ip_manager.ui.models import (
    SORT_ROLE,
    EnvironmentCheckTableModel,
    EnvironmentColumn,
    NodeColumn,
    NodeFilterProxyModel,
    NodeTableModel,
)


def _app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _node(
    node_id: str,
    ip: str,
    country_code: str,
    country: str,
    *,
    purity: PurityGrade = PurityGrade.STRICT_HOME,
    latency_ms: int | None = None,
    status: NodeStatus = NodeStatus.AVAILABLE,
) -> VpnNode:
    return VpnNode(
        id=node_id,
        ip=ip,
        remote_host=ip,
        remote_port=443,
        protocol="tcp",
        country_code=country_code,
        country=country,
        city="Tokyo",
        isp="Example Fiber",
        asn="AS64500",
        purity_grade=purity,
        purity_score=96,
        latency_ms=latency_ms,
        status=status,
        last_checked_at=datetime(2026, 7, 27, 1, 2, 3, tzinfo=UTC),
    )


def test_node_model_formats_dense_operational_columns() -> None:
    _app()
    node = _node("jp-1", "153.232.76.220", "JP", "Japan", latency_ms=82)
    model = NodeTableModel([node])

    assert model.rowCount() == 1
    assert model.columnCount() == len(NodeColumn)
    assert model.data(model.index(0, NodeColumn.COUNTRY)) == "日本"
    assert model.data(model.index(0, NodeColumn.IP)) == "153.232.76.220"
    assert model.data(model.index(0, NodeColumn.PURITY)) == "严格家宽"
    assert model.data(model.index(0, NodeColumn.LATENCY)) == "82 ms"
    assert model.data(model.index(0, NodeColumn.STATUS)) == "可用"
    assert model.data(model.index(0, NodeColumn.PROTOCOL)) == "TCP"


def test_node_model_localizes_provider_country_variants_by_code() -> None:
    _app()
    model = NodeTableModel(
        [
            _node("kr-1", "121.175.252.174", "KR", "Korea Republic of"),
            _node("kr-2", "221.144.146.230", "KR", "South Korea"),
            _node("us-1", "68.84.214.76", "US", "United States"),
        ]
    )

    assert model.data(model.index(0, NodeColumn.COUNTRY)) == "韩国"
    assert model.data(model.index(1, NodeColumn.COUNTRY)) == "韩国"
    assert model.data(model.index(2, NodeColumn.COUNTRY)) == "美国"


def test_node_model_exposes_numeric_sort_values() -> None:
    _app()
    model = NodeTableModel(
        [
            _node("slow", "10.0.0.20", "US", "美国", latency_ms=180),
            _node("fast", "10.0.0.3", "JP", "日本", latency_ms=35),
            _node("unknown", "10.0.0.100", "TH", "泰国", latency_ms=None),
        ]
    )

    assert model.data(model.index(0, NodeColumn.LATENCY), SORT_ROLE) == 180
    assert model.data(model.index(2, NodeColumn.LATENCY), SORT_ROLE) > 180
    assert model.data(model.index(1, NodeColumn.IP), SORT_ROLE) < model.data(
        model.index(0, NodeColumn.IP), SORT_ROLE
    )


def test_proxy_filters_country_and_strict_home_nodes() -> None:
    _app()
    source = NodeTableModel(
        [
            _node("jp-home", "10.0.0.1", "JP", "日本"),
            _node(
                "jp-candidate",
                "10.0.0.2",
                "JP",
                "日本",
                purity=PurityGrade.CANDIDATE,
            ),
            _node("us-home", "10.0.0.3", "US", "美国"),
        ]
    )
    proxy = NodeFilterProxyModel()
    proxy.setSourceModel(source)

    assert proxy.rowCount() == 2
    proxy.set_country_code("jp")
    assert proxy.rowCount() == 1
    assert proxy.node_at(0).id == "jp-home"  # type: ignore[union-attr]

    proxy.set_strict_only(False)
    assert proxy.rowCount() == 2


def test_proxy_hides_unavailable_and_cooldown_nodes() -> None:
    _app()
    source = NodeTableModel(
        [
            _node("available", "10.0.0.1", "JP", "日本"),
            _node(
                "unavailable",
                "10.0.0.2",
                "JP",
                "日本",
                status=NodeStatus.UNAVAILABLE,
            ),
            _node(
                "cooldown",
                "10.0.0.3",
                "US",
                "美国",
                status=NodeStatus.COOLDOWN,
            ),
        ]
    )
    proxy = NodeFilterProxyModel()
    proxy.setSourceModel(source)

    assert proxy.rowCount() == 1
    assert proxy.node_at(0).id == "available"  # type: ignore[union-attr]


def test_proxy_sorts_latency_as_numbers() -> None:
    _app()
    source = NodeTableModel(
        [
            _node("slow", "10.0.0.1", "JP", "日本", latency_ms=120),
            _node("fast", "10.0.0.2", "US", "美国", latency_ms=9),
        ]
    )
    proxy = NodeFilterProxyModel()
    proxy.setSourceModel(source)
    proxy.sort(NodeColumn.LATENCY, Qt.SortOrder.AscendingOrder)

    assert proxy.node_at(0).id == "fast"  # type: ignore[union-attr]


def test_proxy_country_sort_groups_nodes_then_orders_latency_and_ip() -> None:
    _app()
    source = NodeTableModel(
        [
            _node("kr-slow", "10.0.0.20", "KR", "South Korea", latency_ms=120),
            _node("jp-slow", "10.0.0.30", "JP", "Japan", latency_ms=90),
            _node("kr-fast", "10.0.0.10", "KR", "South Korea", latency_ms=40),
            _node("jp-fast-b", "10.0.0.2", "JP", "Japan", latency_ms=20),
            _node("jp-fast-a", "10.0.0.1", "JP", "Japan", latency_ms=20),
        ]
    )
    proxy = NodeFilterProxyModel()
    proxy.setSourceModel(source)
    proxy.sort(NodeColumn.COUNTRY, Qt.SortOrder.AscendingOrder)

    assert [proxy.node_at(row).id for row in range(proxy.rowCount())] == [  # type: ignore[union-attr]
        "jp-fast-a",
        "jp-fast-b",
        "jp-slow",
        "kr-fast",
        "kr-slow",
    ]


def test_environment_model_distinguishes_required_and_optional_failures() -> None:
    _app()
    model = EnvironmentCheckTableModel()
    model.set_report(
        EnvironmentReport(
            checks=[
                ComponentCheck("openvpn", "OpenVPN", True, "2.7.5"),
                ComponentCheck("clash_api", "Clash API", False, "未配置", required=False),
            ]
        )
    )

    assert model.ready
    assert model.data(model.index(0, EnvironmentColumn.STATUS)) == "通过"
    assert model.data(model.index(1, EnvironmentColumn.REQUIREMENT)) == "可选"
    assert model.data(model.index(1, EnvironmentColumn.STATUS)) == "失败"
