from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime
from enum import IntEnum
from ipaddress import ip_address
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QFont

from residential_ip_manager.domain.models import (
    ComponentCheck,
    EnvironmentReport,
    NodeStatus,
    PurityGrade,
    VpnNode,
)
from residential_ip_manager.ui.country_names import localized_country_name
from residential_ip_manager.ui.theme import COLORS

SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
NODE_ROLE = int(Qt.ItemDataRole.UserRole) + 2
TONE_ROLE = int(Qt.ItemDataRole.UserRole) + 3
INVALID_INDEX = QModelIndex()
ModelIndex = QModelIndex | QPersistentModelIndex


class NodeColumn(IntEnum):
    COUNTRY = 0
    IP = 1
    CITY = 2
    ISP = 3
    ASN = 4
    PURITY = 5
    LATENCY = 6
    PROTOCOL = 7
    STATUS = 8
    LAST_CHECKED = 9


NODE_HEADERS: dict[NodeColumn, str] = {
    NodeColumn.COUNTRY: "国家/地区",
    NodeColumn.IP: "IP 地址",
    NodeColumn.CITY: "城市",
    NodeColumn.ISP: "运营商 / ISP",
    NodeColumn.ASN: "ASN",
    NodeColumn.PURITY: "家宽等级",
    NodeColumn.LATENCY: "延迟",
    NodeColumn.PROTOCOL: "协议",
    NodeColumn.STATUS: "状态",
    NodeColumn.LAST_CHECKED: "最后检测",
}

PURITY_LABELS: dict[PurityGrade, str] = {
    PurityGrade.REJECTED: "已拒绝",
    PurityGrade.CANDIDATE: "候选",
    PurityGrade.STRICT_HOME: "严格家宽",
}

STATUS_LABELS: dict[NodeStatus, str] = {
    NodeStatus.UNKNOWN: "未检测",
    NodeStatus.CHECKING: "检测中",
    NodeStatus.AVAILABLE: "可用",
    NodeStatus.UNAVAILABLE: "不可用",
    NodeStatus.COOLDOWN: "冷却中",
}


def _local_time(value: datetime | None) -> str:
    if value is None:
        return "未检测"
    with suppress(ValueError):
        value = value.astimezone()
    return value.strftime("%m-%d %H:%M:%S")


def _status_tone(status: NodeStatus) -> str:
    return {
        NodeStatus.AVAILABLE: "success",
        NodeStatus.CHECKING: "info",
        NodeStatus.COOLDOWN: "warning",
        NodeStatus.UNAVAILABLE: "error",
    }.get(status, "neutral")


def _purity_tone(grade: PurityGrade) -> str:
    return {
        PurityGrade.STRICT_HOME: "success",
        PurityGrade.CANDIDATE: "warning",
        PurityGrade.REJECTED: "error",
    }[grade]


class NodeTableModel(QAbstractTableModel):
    """Read-only presentation model for VPN nodes."""

    def __init__(
        self,
        nodes: Sequence[VpnNode] | None = None,
        parent: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self._nodes = list(nodes or ())

    @property
    def nodes(self) -> tuple[VpnNode, ...]:
        return tuple(self._nodes)

    def set_nodes(self, nodes: Sequence[VpnNode]) -> None:
        self.beginResetModel()
        self._nodes = list(nodes)
        self.endResetModel()

    def node_at(self, row: int) -> VpnNode | None:
        if 0 <= row < len(self._nodes):
            return self._nodes[row]
        return None

    def rowCount(self, parent: ModelIndex = INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._nodes)

    def columnCount(self, parent: ModelIndex = INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(NodeColumn)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole:
            return None
        try:
            return NODE_HEADERS[NodeColumn(section)]
        except (ValueError, KeyError):
            return None

    def data(
        self,
        index: ModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._nodes):
            return None
        node = self._nodes[index.row()]
        column = NodeColumn(index.column())

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(node, column)
        if role == SORT_ROLE:
            return self._sort_value(node, column)
        if role == NODE_ROLE:
            return node
        if role == TONE_ROLE:
            if column == NodeColumn.STATUS:
                return _status_tone(node.status)
            if column == NodeColumn.PURITY:
                return _purity_tone(node.purity_grade)
            return "neutral"
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(node, column)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column in {
                NodeColumn.COUNTRY,
                NodeColumn.PURITY,
                NodeColumn.LATENCY,
                NodeColumn.PROTOCOL,
                NodeColumn.STATUS,
                NodeColumn.LAST_CHECKED,
            }:
                return int(Qt.AlignmentFlag.AlignCenter)
            return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if role == Qt.ItemDataRole.FontRole and column in {
            NodeColumn.IP,
            NodeColumn.ASN,
            NodeColumn.LATENCY,
            NodeColumn.PROTOCOL,
        }:
            return QFont("Cascadia Mono", 9)
        if role == Qt.ItemDataRole.ForegroundRole:
            tone = "neutral"
            if column == NodeColumn.STATUS:
                tone = _status_tone(node.status)
            elif column == NodeColumn.PURITY:
                tone = _purity_tone(node.purity_grade)
            color = {
                "success": COLORS.success,
                "warning": COLORS.warning,
                "error": COLORS.error,
                "info": COLORS.primary,
                "neutral": COLORS.text,
            }[tone]
            return QBrush(QColor(color))
        return None

    @staticmethod
    def _display_value(node: VpnNode, column: NodeColumn) -> str:
        values = {
            NodeColumn.COUNTRY: localized_country_name(node.country_code, node.country) or "未知",
            NodeColumn.IP: node.ip or "-",
            NodeColumn.CITY: node.city or "-",
            NodeColumn.ISP: node.isp or "-",
            NodeColumn.ASN: node.asn or "-",
            NodeColumn.PURITY: PURITY_LABELS[node.purity_grade],
            NodeColumn.LATENCY: "-" if node.latency_ms is None else f"{node.latency_ms} ms",
            NodeColumn.PROTOCOL: node.protocol.upper() or "-",
            NodeColumn.STATUS: STATUS_LABELS[node.status],
            NodeColumn.LAST_CHECKED: _local_time(node.last_checked_at),
        }
        return values[column]

    @staticmethod
    def _sort_value(node: VpnNode, column: NodeColumn) -> str | int | float:
        if column == NodeColumn.IP:
            try:
                return int(ip_address(node.ip))
            except ValueError:
                return 0
        if column == NodeColumn.PURITY:
            return {
                PurityGrade.REJECTED: 0,
                PurityGrade.CANDIDATE: 1,
                PurityGrade.STRICT_HOME: 2,
            }[node.purity_grade]
        if column == NodeColumn.LATENCY:
            return node.latency_ms if node.latency_ms is not None else 1_000_000_000
        if column == NodeColumn.STATUS:
            return {
                NodeStatus.AVAILABLE: 0,
                NodeStatus.CHECKING: 1,
                NodeStatus.COOLDOWN: 2,
                NodeStatus.UNKNOWN: 3,
                NodeStatus.UNAVAILABLE: 4,
            }[node.status]
        if column == NodeColumn.LAST_CHECKED:
            return node.last_checked_at.timestamp() if node.last_checked_at else 0.0
        return NodeTableModel._display_value(node, column).casefold()

    @staticmethod
    def _tooltip(node: VpnNode, column: NodeColumn) -> str:
        if column == NodeColumn.STATUS and node.last_error:
            return f"{STATUS_LABELS[node.status]}\n{node.last_error}"
        if column == NodeColumn.PURITY and node.evidence:
            evidence = "\n".join(
                f"{'通过' if item.passed else '未通过'} · {item.provider}: {item.summary}"
                for item in node.evidence
            )
            return f"纯净度 {node.purity_score} 分\n{evidence}"
        return NodeTableModel._display_value(node, column)


class NodeFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent: Any | None = None) -> None:
        super().__init__(parent)
        self._country_code = ""
        self._strict_only = True
        self.setDynamicSortFilter(True)
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    @staticmethod
    def _ip_sort_value(node: VpnNode) -> int:
        try:
            return int(ip_address(node.ip))
        except ValueError:
            return 0

    @classmethod
    def _stable_sort_key(cls, node: VpnNode, column: NodeColumn) -> tuple[Any, ...]:
        country = node.country_code.strip().upper() or localized_country_name(
            node.country_code,
            node.country,
        ).casefold()
        latency = node.latency_ms if node.latency_ms is not None else 1_000_000_000
        ip_value = cls._ip_sort_value(node)
        stable_tail = (country, latency, ip_value, node.id)
        if column == NodeColumn.COUNTRY:
            return stable_tail
        if column == NodeColumn.LATENCY:
            return latency, country, ip_value, node.id
        return NodeTableModel._sort_value(node, column), *stable_tail

    def lessThan(self, left: ModelIndex, right: ModelIndex) -> bool:  # noqa: N802
        source = self.sourceModel()
        if not isinstance(source, NodeTableModel):
            return super().lessThan(left, right)
        left_node = source.node_at(left.row())
        right_node = source.node_at(right.row())
        if left_node is None or right_node is None:
            return super().lessThan(left, right)
        column = NodeColumn(left.column())
        return self._stable_sort_key(left_node, column) < self._stable_sort_key(
            right_node,
            column,
        )

    @property
    def country_code(self) -> str:
        return self._country_code

    @property
    def strict_only(self) -> bool:
        return self._strict_only

    def set_country_code(self, country_code: str) -> None:
        normalized = country_code.strip().upper()
        if normalized == self._country_code:
            return
        self._country_code = normalized
        self._refilter_rows()

    def set_strict_only(self, enabled: bool) -> None:
        if enabled == self._strict_only:
            return
        self._strict_only = enabled
        self._refilter_rows()

    def _refilter_rows(self) -> None:
        begin_change = getattr(self, "beginFilterChange", None)
        end_change = getattr(self, "endFilterChange", None)
        if callable(begin_change) and callable(end_change):
            begin_change()
            end_change(QSortFilterProxyModel.Direction.Rows)
            return

        # Qt 6.8/6.9 compatibility; newer Qt versions take the branch above.
        legacy_invalidate = self.invalidateFilter
        legacy_invalidate()

    def filterAcceptsRow(  # noqa: N802
        self,
        source_row: int,
        source_parent: ModelIndex,
    ) -> bool:
        source = self.sourceModel()
        if not isinstance(source, NodeTableModel):
            return False
        node = source.node_at(source_row)
        if node is None:
            return False
        if node.status is not NodeStatus.AVAILABLE:
            return False
        if self._country_code and node.country_code.upper() != self._country_code:
            return False
        return not self._strict_only or node.purity_grade == PurityGrade.STRICT_HOME

    def node_at(self, proxy_row: int) -> VpnNode | None:
        if proxy_row < 0:
            return None
        source_index = self.mapToSource(self.index(proxy_row, 0))
        source = self.sourceModel()
        if not source_index.isValid() or not isinstance(source, NodeTableModel):
            return None
        return source.node_at(source_index.row())


class EnvironmentColumn(IntEnum):
    COMPONENT = 0
    REQUIREMENT = 1
    STATUS = 2
    DETAIL = 3


ENVIRONMENT_HEADERS: dict[EnvironmentColumn, str] = {
    EnvironmentColumn.COMPONENT: "组件",
    EnvironmentColumn.REQUIREMENT: "要求",
    EnvironmentColumn.STATUS: "状态",
    EnvironmentColumn.DETAIL: "检测详情",
}


class EnvironmentCheckTableModel(QAbstractTableModel):
    def __init__(
        self,
        checks: Sequence[ComponentCheck] | None = None,
        parent: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self._checks = list(checks or ())

    @property
    def checks(self) -> tuple[ComponentCheck, ...]:
        return tuple(self._checks)

    @property
    def ready(self) -> bool:
        return EnvironmentReport(list(self._checks)).ready

    def set_report(self, report: EnvironmentReport) -> None:
        self.beginResetModel()
        self._checks = list(report.checks)
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._checks)

    def columnCount(self, parent: ModelIndex = INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(EnvironmentColumn)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole:
            return None
        try:
            return ENVIRONMENT_HEADERS[EnvironmentColumn(section)]
        except (ValueError, KeyError):
            return None

    def data(
        self,
        index: ModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._checks):
            return None
        check = self._checks[index.row()]
        column = EnvironmentColumn(index.column())
        display = {
            EnvironmentColumn.COMPONENT: check.label,
            EnvironmentColumn.REQUIREMENT: "必需" if check.required else "可选",
            EnvironmentColumn.STATUS: "通过" if check.ok else "失败",
            EnvironmentColumn.DETAIL: check.detail or "-",
        }[column]

        if role == Qt.ItemDataRole.DisplayRole:
            return display
        if role == Qt.ItemDataRole.ToolTipRole:
            return check.detail or display
        if role == TONE_ROLE:
            if column != EnvironmentColumn.STATUS:
                return "neutral"
            if check.ok:
                return "success"
            return "error" if check.required else "warning"
        if role == Qt.ItemDataRole.ForegroundRole and column == EnvironmentColumn.STATUS:
            if check.ok:
                color = COLORS.success
            else:
                color = COLORS.error if check.required else COLORS.warning
            return QBrush(QColor(color))
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {
            EnvironmentColumn.REQUIREMENT,
            EnvironmentColumn.STATUS,
        }:
            return int(Qt.AlignmentFlag.AlignCenter)
        return None
