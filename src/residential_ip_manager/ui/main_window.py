from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from residential_ip_manager.domain.models import (
    ConnectionSnapshot,
    ConnectionState,
    EnvironmentReport,
    NodeStatus,
    PurityGrade,
    VpnNode,
)
from residential_ip_manager.ui.country_names import localized_country_name
from residential_ip_manager.ui.environment_dialog import EnvironmentDialog
from residential_ip_manager.ui.models import NodeColumn, NodeFilterProxyModel, NodeTableModel
from residential_ip_manager.ui.theme import application_stylesheet, set_widget_property

STATE_LABELS: dict[ConnectionState, str] = {
    ConnectionState.IDLE: "准备就绪",
    ConnectionState.CHECKING_ENVIRONMENT: "正在检查环境",
    ConnectionState.STARTING_CLASH: "正在启动 Clash",
    ConnectionState.FETCHING_NODES: "正在获取节点",
    ConnectionState.PROBING_NODES: "正在检测节点",
    ConnectionState.CONNECTING: "正在连接 OpenVPN",
    ConnectionState.VERIFYING: "正在验证出口",
    ConnectionState.CONNECTED: "连接正常",
    ConnectionState.DEGRADED: "连接质量下降",
    ConnectionState.FAILING_OVER: "正在自动切换",
    ConnectionState.DISCONNECTING: "正在断开连接",
    ConnectionState.ERROR: "连接错误",
}

BUSY_STATES = {
    ConnectionState.CHECKING_ENVIRONMENT,
    ConnectionState.STARTING_CLASH,
    ConnectionState.FETCHING_NODES,
    ConnectionState.PROBING_NODES,
    ConnectionState.CONNECTING,
    ConnectionState.VERIFYING,
    ConnectionState.FAILING_OVER,
    ConnectionState.DISCONNECTING,
}


def _standard_icon(widget: QWidget, name: QStyle.StandardPixmap, size: int = 18):
    icon = widget.style().standardIcon(name)
    return icon, QSize(size, size)


class LinkStage(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(8)
        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(20, 20)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label)

        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(0)
        self.title_label = QLabel(title, self)
        self.title_label.setStyleSheet("font-weight: 600;")
        self.detail_label = QLabel("未检测", self)
        self.detail_label.setProperty("role", "muted")
        self.detail_label.setMinimumWidth(0)
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        labels.addWidget(self.title_label)
        labels.addWidget(self.detail_label)
        layout.addLayout(labels, 1)
        self.setAccessibleName(title)
        self.set_status("neutral", "未检测")

    def set_status(self, tone: str, detail: str) -> None:
        self.detail_label.setText(detail)
        self.detail_label.setToolTip(detail)
        set_widget_property(self.detail_label, "tone", tone)
        pixmap = {
            "success": QStyle.StandardPixmap.SP_DialogApplyButton,
            "warning": QStyle.StandardPixmap.SP_MessageBoxWarning,
            "error": QStyle.StandardPixmap.SP_MessageBoxCritical,
            "info": QStyle.StandardPixmap.SP_MessageBoxInformation,
            "neutral": QStyle.StandardPixmap.SP_DialogResetButton,
        }[tone]
        self.icon_label.setPixmap(self.style().standardIcon(pixmap).pixmap(18, 18))
        self.setAccessibleDescription(detail)


class SummaryMetric(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        label = QLabel(title, self)
        label.setProperty("role", "metricLabel")
        self.value_label = QLabel("-", self)
        self.value_label.setProperty("role", "metricValue")
        self.value_label.setMinimumWidth(0)
        self.value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(label)
        layout.addWidget(self.value_label)
        self.setAccessibleName(title)

    def set_value(self, value: str, tooltip: str | None = None) -> None:
        display = value or "-"
        self.value_label.setText(display)
        self.value_label.setToolTip(tooltip or display)
        self.setAccessibleDescription(display)


class MainWindow(QMainWindow):
    refresh_requested = Signal()
    connect_requested = Signal(str)
    switch_ip_requested = Signal(str)
    disconnect_requested = Signal()
    environment_check_requested = Signal()
    environment_repair_requested = Signal(str)
    country_filter_changed = Signal(str)
    strict_home_changed = Signal(bool)
    auto_failover_changed = Signal(bool)

    PAGE_TABLE = 0
    PAGE_LOADING = 1
    PAGE_EMPTY = 2
    PAGE_ERROR = 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("住宅 IP 控制台")
        self.setMinimumSize(1024, 680)
        self.resize(1280, 800)

        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(application_stylesheet())

        self._node_model = NodeTableModel(parent=self)
        self._proxy_model = NodeFilterProxyModel(self)
        self._proxy_model.setSourceModel(self._node_model)
        self._snapshot = ConnectionSnapshot()
        self._view_state = "normal"
        self._presentational_busy = False
        self._environment_dialog: EnvironmentDialog | None = None
        self._log_count = 0

        self._build_ui()
        self._wire_events()
        self._install_shortcuts()

        self._uptime_timer = QTimer(self)
        self._uptime_timer.setInterval(1000)
        self._uptime_timer.timeout.connect(self._refresh_connected_duration)
        self._uptime_timer.start()

        self.set_snapshot(self._snapshot)
        self._refresh_content_state()

    @property
    def node_model(self) -> NodeTableModel:
        return self._node_model

    @property
    def proxy_model(self) -> NodeFilterProxyModel:
        return self._proxy_model

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_app_header())
        root.addWidget(self._build_link_status_bar())
        root.addWidget(self._build_exit_summary())
        root.addWidget(self._build_command_bar())
        root.addWidget(self._build_content(), 1)
        root.addWidget(self._build_progress_strip())
        self.log_drawer = self._build_log_drawer()
        self.log_drawer.hide()
        root.addWidget(self.log_drawer)
        self.setCentralWidget(central)

    def _build_app_header(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("appHeader")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)
        title = QLabel("住宅 IP 控制台", frame)
        title.setProperty("role", "title")
        layout.addWidget(title)
        self.node_count_label = QLabel("0 个节点", frame)
        self.node_count_label.setProperty("role", "muted")
        layout.addWidget(self.node_count_label)
        layout.addStretch(1)

        self.environment_button = QPushButton("环境检测 (&E)", frame)
        icon, size = _standard_icon(frame, QStyle.StandardPixmap.SP_ComputerIcon)
        self.environment_button.setIcon(icon)
        self.environment_button.setIconSize(size)
        self.environment_button.setToolTip("检查 OpenVPN、Clash 和系统网络环境 (Ctrl+E)")
        self.environment_button.setAccessibleDescription("打开运行环境检测对话框")
        layout.addWidget(self.environment_button)
        return frame

    def _build_link_status_bar(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("linkStatusBar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)
        label = QLabel("链路状态", frame)
        label.setProperty("role", "section")
        layout.addWidget(label)

        self.clash_stage = LinkStage("Clash 中转", frame)
        self.openvpn_stage = LinkStage("OpenVPN 隧道", frame)
        self.exit_stage = LinkStage("出口验证", frame)
        stages = (self.clash_stage, self.openvpn_stage, self.exit_stage)
        for position, stage in enumerate(stages):
            layout.addWidget(stage)
            if position < len(stages) - 1:
                arrow = QLabel(frame)
                arrow.setPixmap(
                    self.style()
                    .standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
                    .pixmap(14, 14)
                )
                arrow.setAccessibleName("下一链路阶段")
                layout.addWidget(arrow)
        layout.addStretch(1)
        self.connection_message = QLabel("准备就绪", frame)
        self.connection_message.setProperty("tone", "neutral")
        self.connection_message.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.connection_message.setMinimumWidth(140)
        self.connection_message.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.connection_message, 1)
        return frame

    def _build_exit_summary(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("exitSummary")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(16)
        label = QLabel("当前出口", frame)
        label.setProperty("role", "section")
        label.setMinimumWidth(72)
        layout.addWidget(label)

        self.exit_ip_metric = SummaryMetric("公网 IP", frame)
        self.country_metric = SummaryMetric("国家/地区", frame)
        self.isp_metric = SummaryMetric("运营商 / ISP", frame)
        self.latency_metric = SummaryMetric("实时延迟", frame)
        self.duration_metric = SummaryMetric("连接时长", frame)
        for metric in (
            self.exit_ip_metric,
            self.country_metric,
            self.isp_metric,
            self.latency_metric,
            self.duration_metric,
        ):
            layout.addWidget(metric, 1)
        return frame

    def _build_command_bar(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("commandBar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(8)

        country_label = QLabel("国家 (&N)", frame)
        layout.addWidget(country_label)
        self.country_combo = QComboBox(frame)
        self.country_combo.addItem("全部国家", "")
        self.country_combo.setAccessibleName("国家筛选")
        self.country_combo.setToolTip("筛选节点国家；键盘可直接输入国家首字母定位")
        country_label.setBuddy(self.country_combo)
        layout.addWidget(self.country_combo)

        self.strict_checkbox = QCheckBox("仅严格家宽", frame)
        self.strict_checkbox.setChecked(True)
        self.strict_checkbox.setAccessibleDescription("隐藏未通过严格家庭宽带判定的节点")
        layout.addWidget(self.strict_checkbox)

        self.auto_failover_checkbox = QCheckBox("自动切换", frame)
        self.auto_failover_checkbox.setChecked(True)
        self.auto_failover_checkbox.setAccessibleDescription("当前出口失效时请求自动更换节点")
        layout.addWidget(self.auto_failover_checkbox)

        self.filtered_count_label = QLabel("显示 0 / 0", frame)
        self.filtered_count_label.setProperty("role", "muted")
        layout.addWidget(self.filtered_count_label)
        layout.addStretch(1)

        self.refresh_button = QPushButton("刷新节点 (&R)", frame)
        icon, size = _standard_icon(frame, QStyle.StandardPixmap.SP_BrowserReload)
        self.refresh_button.setIcon(icon)
        self.refresh_button.setIconSize(size)
        self.refresh_button.setToolTip("获取并检测最新节点 (F5)")
        layout.addWidget(self.refresh_button)

        self.connect_button = QPushButton("一键连接 (&C)", frame)
        icon, size = _standard_icon(frame, QStyle.StandardPixmap.SP_MediaPlay)
        self.connect_button.setIcon(icon)
        self.connect_button.setIconSize(size)
        self.connect_button.setProperty("role", "primary")
        self.connect_button.setDefault(True)
        self.connect_button.setToolTip("连接选中节点；未选择时由控制器智能选择 (Ctrl+Enter)")
        layout.addWidget(self.connect_button)

        self.switch_button = QPushButton("更换 IP", frame)
        icon, size = _standard_icon(frame, QStyle.StandardPixmap.SP_ArrowForward)
        self.switch_button.setIcon(icon)
        self.switch_button.setIconSize(size)
        self.switch_button.setToolTip("在当前国家筛选范围内请求更换出口 (Ctrl+Shift+S)")
        layout.addWidget(self.switch_button)

        self.disconnect_button = QPushButton("断开", frame)
        icon, size = _standard_icon(frame, QStyle.StandardPixmap.SP_MediaStop)
        self.disconnect_button.setIcon(icon)
        self.disconnect_button.setIconSize(size)
        self.disconnect_button.setProperty("role", "danger")
        self.disconnect_button.setToolTip("断开当前 OpenVPN 隧道 (Ctrl+D)")
        layout.addWidget(self.disconnect_button)
        return frame

    def _build_content(self) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(0)
        self.content_pages = QStackedWidget(container)
        self.content_pages.addWidget(self._build_table_page())
        self.content_pages.addWidget(self._build_loading_page())
        self.content_pages.addWidget(self._build_empty_page())
        self.content_pages.addWidget(self._build_error_page())
        layout.addWidget(self.content_pages)
        return container

    def _build_table_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.node_table = QTableView(page)
        self.node_table.setModel(self._proxy_model)
        self.node_table.setAccessibleName("住宅 IP 节点列表")
        self.node_table.setAccessibleDescription("可按列排序，方向键选择节点")
        self.node_table.setAlternatingRowColors(True)
        self.node_table.setSortingEnabled(True)
        self.node_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.node_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.node_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.node_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.node_table.setWordWrap(False)
        self.node_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.node_table.verticalHeader().hide()
        self.node_table.verticalHeader().setDefaultSectionSize(31)
        header = self.node_table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setHighlightSections(False)
        for column, width in {
            NodeColumn.COUNTRY: 100,
            NodeColumn.IP: 132,
            NodeColumn.CITY: 92,
            NodeColumn.ASN: 112,
            NodeColumn.PURITY: 96,
            NodeColumn.LATENCY: 78,
            NodeColumn.PROTOCOL: 76,
            NodeColumn.STATUS: 88,
            NodeColumn.LAST_CHECKED: 128,
        }.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(column, width)
        header.setSectionResizeMode(NodeColumn.ISP, QHeaderView.ResizeMode.Stretch)
        self.node_table.sortByColumn(NodeColumn.COUNTRY, Qt.SortOrder.AscendingOrder)
        layout.addWidget(self.node_table)
        return page

    def _build_loading_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel(page)
        icon.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
            .pixmap(38, 38)
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAccessibleName("正在加载")
        self.loading_label = QLabel("正在获取住宅 IP 节点…", page)
        self.loading_label.setProperty("role", "section")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        layout.addSpacing(8)
        layout.addWidget(self.loading_label)
        return page

    def _build_empty_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel(page)
        icon.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon).pixmap(40, 40)
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("暂无可用的严格家宽节点", page)
        self.empty_title.setProperty("role", "section")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_detail = QLabel("刷新节点或调整国家筛选条件", page)
        self.empty_detail.setProperty("role", "muted")
        self.empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_refresh_button = QPushButton("刷新节点", page)
        self.empty_refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.empty_refresh_button.setAccessibleDescription("重新获取住宅 IP 节点")
        layout.addWidget(icon)
        layout.addSpacing(8)
        layout.addWidget(self.empty_title)
        layout.addWidget(self.empty_detail)
        layout.addSpacing(8)
        layout.addWidget(self.empty_refresh_button, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    def _build_error_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel(page)
        icon.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical).pixmap(40, 40)
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("节点加载失败", page)
        title.setProperty("role", "section")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_detail = QLabel(page)
        self.error_detail.setProperty("tone", "error")
        self.error_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_detail.setWordWrap(True)
        self.error_detail.setMaximumWidth(640)
        self.error_detail.setAccessibleName("错误详情")
        self.retry_button = QPushButton("重试", page)
        self.retry_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        layout.addWidget(icon)
        layout.addSpacing(8)
        layout.addWidget(title)
        layout.addWidget(self.error_detail)
        layout.addSpacing(8)
        layout.addWidget(self.retry_button, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    def _build_progress_strip(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("progressStrip")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 7, 16, 7)
        layout.setSpacing(8)
        self.progress_bar = QProgressBar(frame)
        self.progress_bar.setFixedWidth(190)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setAccessibleName("任务进度")
        layout.addWidget(self.progress_bar)
        self.progress_label = QLabel("空闲", frame)
        self.progress_label.setProperty("role", "muted")
        self.progress_label.setMinimumWidth(0)
        self.progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.progress_label, 1)
        self.log_toggle = QToolButton(frame)
        self.log_toggle.setText("日志 (0)")
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log_toggle.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp)
        )
        self.log_toggle.setToolTip("展开或收起运行日志 (Ctrl+L)")
        self.log_toggle.setAccessibleName("运行日志")
        self.log_toggle.setCheckable(True)
        layout.addWidget(self.log_toggle)
        return frame

    def _build_log_drawer(self) -> QFrame:
        frame = QFrame(self)
        frame.setObjectName("logDrawer")
        frame.setMaximumHeight(220)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 8, 16, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("运行日志", frame)
        title.setProperty("role", "section")
        header.addWidget(title)
        header.addStretch(1)
        self.clear_log_button = QToolButton(frame)
        self.clear_log_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton)
        )
        self.clear_log_button.setToolTip("清空界面日志")
        self.clear_log_button.setAccessibleName("清空运行日志")
        header.addWidget(self.clear_log_button)
        layout.addLayout(header)
        self.log_view = QPlainTextEdit(frame)
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.document().setMaximumBlockCount(500)
        self.log_view.setAccessibleName("运行日志内容")
        layout.addWidget(self.log_view, 1)
        return frame

    def _wire_events(self) -> None:
        self.refresh_button.clicked.connect(self.refresh_requested)
        self.empty_refresh_button.clicked.connect(self.refresh_requested)
        self.retry_button.clicked.connect(self.refresh_requested)
        self.connect_button.clicked.connect(self._emit_connect)
        self.switch_button.clicked.connect(self._emit_switch_ip)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.environment_button.clicked.connect(self.show_environment_dialog)
        self.country_combo.currentIndexChanged.connect(self._country_changed)
        self.strict_checkbox.toggled.connect(self._strict_changed)
        self.auto_failover_checkbox.toggled.connect(self.auto_failover_changed)
        self.log_toggle.toggled.connect(self.set_log_drawer_visible)
        self.clear_log_button.clicked.connect(self.clear_logs)
        self.node_table.customContextMenuRequested.connect(self._show_node_context_menu)
        self._proxy_model.rowsInserted.connect(self._proxy_rows_changed)
        self._proxy_model.rowsRemoved.connect(self._proxy_rows_changed)
        self._proxy_model.modelReset.connect(self._proxy_rows_changed)
        self._proxy_model.layoutChanged.connect(self._proxy_rows_changed)

    def _install_shortcuts(self) -> None:
        shortcuts = (
            (QKeySequence.StandardKey.Refresh, self.refresh_requested.emit),
            (QKeySequence("Ctrl+Return"), self._emit_connect),
            (QKeySequence("Ctrl+Shift+S"), self._emit_switch_ip),
            (QKeySequence("Ctrl+D"), self.disconnect_requested.emit),
            (QKeySequence("Ctrl+E"), self.show_environment_dialog),
            (QKeySequence("Ctrl+L"), self._toggle_logs),
        )
        self._shortcuts: list[QShortcut] = []
        for sequence, callback in shortcuts:
            shortcut = QShortcut(sequence, self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

        focus_table = QAction(self)
        focus_table.setShortcut(QKeySequence("Ctrl+T"))
        focus_table.triggered.connect(self.node_table.setFocus)
        self.addAction(focus_table)

    @Slot(object)
    def set_nodes(self, nodes: Sequence[VpnNode]) -> None:
        self._node_model.set_nodes(nodes)
        self._update_country_options()
        if not self._presentational_busy:
            self._view_state = "normal"
        self._update_node_counts()
        self._refresh_content_state()
        self._update_controls()

    @Slot(object)
    def set_snapshot(self, snapshot: ConnectionSnapshot) -> None:
        self._snapshot = snapshot
        self._update_link_status(snapshot)
        self._update_exit_summary(snapshot)
        self._update_controls()

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在获取住宅 IP 节点…") -> None:
        self._presentational_busy = loading
        if loading:
            self._view_state = "loading"
            self.loading_label.setText(message or "正在获取住宅 IP 节点…")
            self.set_progress(0, 0, message)
        else:
            self._view_state = "normal"
            self.clear_progress()
        self._refresh_content_state()
        self._update_controls()

    @Slot(str)
    def set_error(self, message: str) -> None:
        detail = message or "无法获取节点，请检查网络后重试。"
        self._view_state = "error"
        self._presentational_busy = False
        self.error_detail.setText(detail)
        self.error_detail.setToolTip(detail)
        self.set_progress(0, 100, "操作失败")
        self._refresh_content_state()
        self._update_controls()

    @Slot()
    def clear_error(self) -> None:
        if self._view_state == "error":
            self._view_state = "normal"
            self._refresh_content_state()

    @Slot(int, int, str)
    def set_progress(self, current: int, total: int, message: str = "") -> None:
        if total <= 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, max(1, total))
            self.progress_bar.setValue(max(0, min(current, total)))
        text = message or ("处理中" if total <= 0 else "空闲")
        self.progress_label.setText(text)
        self.progress_label.setToolTip(text)
        self.progress_bar.setAccessibleDescription(text)

    @Slot()
    def clear_progress(self) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label.setText("空闲")
        self.progress_label.setToolTip("")
        self.progress_bar.setAccessibleDescription("空闲")

    @Slot(str, str)
    def append_log(self, level: str, message: str) -> None:
        normalized = level.strip().upper() or "INFO"
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{timestamp}  {normalized:<7} {message}")
        self._log_count += 1
        self.log_toggle.setText(f"日志 ({self._log_count})")
        if normalized in {"ERROR", "CRITICAL"}:
            self.log_toggle.setToolTip("运行日志中有错误；按 Ctrl+L 展开")

    @Slot()
    def clear_logs(self) -> None:
        self.log_view.clear()
        self._log_count = 0
        self.log_toggle.setText("日志 (0)")
        self.log_toggle.setToolTip("展开或收起运行日志 (Ctrl+L)")

    @Slot(bool)
    def set_log_drawer_visible(self, visible: bool) -> None:
        self.log_drawer.setVisible(visible)
        self.log_toggle.setChecked(visible)
        icon = QStyle.StandardPixmap.SP_ArrowDown if visible else QStyle.StandardPixmap.SP_ArrowUp
        self.log_toggle.setIcon(self.style().standardIcon(icon))
        self.log_toggle.setAccessibleDescription("已展开" if visible else "已收起")
        if visible:
            self.log_view.setFocus()

    @Slot()
    def show_environment_dialog(self) -> None:
        dialog = self._ensure_environment_dialog()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if dialog.model.rowCount() == 0:
            dialog.set_checking()
            self.environment_check_requested.emit()

    @Slot(object)
    def set_environment_report(self, report: EnvironmentReport) -> None:
        self._ensure_environment_dialog().set_report(report)

    @Slot(str)
    def set_environment_error(self, message: str) -> None:
        self._ensure_environment_dialog().set_error(message)

    @Slot(str)
    def show_environment_repair_error(self, message: str) -> None:
        self._ensure_environment_dialog().show_repair_error(message)

    def _ensure_environment_dialog(self) -> EnvironmentDialog:
        if self._environment_dialog is None:
            dialog = EnvironmentDialog(self)
            dialog.check_requested.connect(self.environment_check_requested)
            dialog.repair_requested.connect(self.environment_repair_requested)
            self._environment_dialog = dialog
        return self._environment_dialog

    @Slot()
    def _emit_connect(self) -> None:
        index = self.node_table.currentIndex()
        node = self._proxy_model.node_at(index.row()) if index.isValid() else None
        self.connect_requested.emit(node.id if node else "")

    @Slot()
    def _emit_switch_ip(self) -> None:
        self.switch_ip_requested.emit(str(self.country_combo.currentData() or ""))

    @Slot(QPoint)
    def _show_node_context_menu(self, position: QPoint) -> None:
        index = self.node_table.indexAt(position)
        if not index.isValid():
            return

        self.node_table.selectRow(index.row())
        self.node_table.setCurrentIndex(index)
        node = self._proxy_model.node_at(index.row())
        if node is None:
            return

        state = self._snapshot.state
        busy = self._presentational_busy or state in BUSY_STATES
        connected = state in {ConnectionState.CONNECTED, ConnectionState.DEGRADED}
        active = connected and node.id == self._snapshot.active_node_id
        has_connection_context = bool(
            self._snapshot.active_node_id
            or self._snapshot.exit_ip
            or self._snapshot.connected_since
            or self._snapshot.metadata.get("tunnel_connected") is True
        )
        can_connect = not busy and (
            connected
            or (
                state in {ConnectionState.IDLE, ConnectionState.ERROR}
                and not has_connection_context
            )
        )

        menu = QMenu(self.node_table)
        if active:
            action_text = "当前正在使用此节点"
        elif connected:
            action_text = "切换到此节点"
        else:
            action_text = "连接此节点"
        connect_action = menu.addAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward),
            action_text,
        )
        connect_action.setEnabled(can_connect and not active)
        connect_action.triggered.connect(
            lambda _checked=False, node_id=node.id: self.connect_requested.emit(node_id)
        )

        menu.addSeparator()
        copy_action = menu.addAction("复制 IP 地址")
        copy_action.triggered.connect(
            lambda _checked=False, ip=node.ip: QApplication.clipboard().setText(ip)
        )
        menu.exec(self.node_table.viewport().mapToGlobal(position))

    @Slot(int)
    def _country_changed(self, _index: int) -> None:
        country_code = str(self.country_combo.currentData() or "")
        self._proxy_model.set_country_code(country_code)
        self.country_filter_changed.emit(country_code)
        self._update_node_counts()
        self._refresh_content_state()

    @Slot(bool)
    def _strict_changed(self, enabled: bool) -> None:
        self._proxy_model.set_strict_only(enabled)
        self.strict_home_changed.emit(enabled)
        self._update_country_options()
        self._update_node_counts()
        self._refresh_content_state()

    @Slot()
    def _proxy_rows_changed(self, *_args: Any) -> None:
        self._update_node_counts()
        self._refresh_content_state()

    @Slot()
    def _toggle_logs(self) -> None:
        self.set_log_drawer_visible(not self.log_drawer.isVisible())

    def _update_country_options(self) -> None:
        selected = str(self.country_combo.currentData() or "")
        countries: dict[str, str] = {}
        for node in self._node_model.nodes:
            if node.status is not NodeStatus.AVAILABLE:
                continue
            if (
                self.strict_checkbox.isChecked()
                and node.purity_grade is not PurityGrade.STRICT_HOME
            ):
                continue
            code = node.country_code.strip().upper()
            if code:
                countries.setdefault(code, localized_country_name(code, node.country))

        self.country_combo.blockSignals(True)
        self.country_combo.clear()
        self.country_combo.addItem("全部国家", "")
        for code, country in sorted(countries.items(), key=lambda item: item[1].casefold()):
            self.country_combo.addItem(f"{country} ({code})", code)
        selected_index = self.country_combo.findData(selected)
        self.country_combo.setCurrentIndex(max(0, selected_index))
        self.country_combo.blockSignals(False)
        self._proxy_model.set_country_code(
            str(self.country_combo.currentData() or "")
        )

    def _update_node_counts(self) -> None:
        total = sum(node.status is NodeStatus.AVAILABLE for node in self._node_model.nodes)
        visible = self._proxy_model.rowCount()
        self.node_count_label.setText(f"{total} 个可用节点")
        self.filtered_count_label.setText(f"显示 {visible} / {total}")
        self.node_count_label.setAccessibleDescription(f"当前共 {total} 个可用节点")

    def _refresh_content_state(self) -> None:
        if self._view_state == "loading":
            page = self.PAGE_LOADING
        elif self._view_state == "error":
            page = self.PAGE_ERROR
        elif self._proxy_model.rowCount() == 0:
            page = self.PAGE_EMPTY
            available = any(node.status is NodeStatus.AVAILABLE for node in self._node_model.nodes)
            if available:
                self.empty_title.setText("当前筛选下没有节点")
                self.empty_detail.setText("调整国家或严格家宽筛选条件")
            else:
                self.empty_title.setText("暂无可用的严格家宽节点")
                self.empty_detail.setText("刷新节点以获取最新候选")
        else:
            page = self.PAGE_TABLE
        self.content_pages.setCurrentIndex(page)

    def _update_link_status(self, snapshot: ConnectionSnapshot) -> None:
        state = snapshot.state
        metadata = snapshot.metadata
        clash_flag = metadata.get("clash_running")
        tunnel_flag = metadata.get("tunnel_connected")
        verified_flag = metadata.get("exit_verified")

        if clash_flag is True:
            self.clash_stage.set_status("success", "运行正常")
        elif clash_flag is False:
            self.clash_stage.set_status("error", "未运行")
        elif state == ConnectionState.STARTING_CLASH:
            self.clash_stage.set_status("info", "启动中")
        elif state in {
            ConnectionState.FETCHING_NODES,
            ConnectionState.PROBING_NODES,
            ConnectionState.CONNECTING,
            ConnectionState.VERIFYING,
            ConnectionState.CONNECTED,
            ConnectionState.DEGRADED,
            ConnectionState.FAILING_OVER,
        }:
            self.clash_stage.set_status("success", "已就绪")
        else:
            self.clash_stage.set_status("neutral", "未检测")

        if tunnel_flag is True or state in {
            ConnectionState.CONNECTED,
            ConnectionState.DEGRADED,
            ConnectionState.FAILING_OVER,
            ConnectionState.DISCONNECTING,
        }:
            unstable = state in {ConnectionState.DEGRADED, ConnectionState.FAILING_OVER}
            tone = "warning" if unstable else "success"
            self.openvpn_stage.set_status(tone, "已连接" if tone == "success" else "连接不稳定")
        elif tunnel_flag is False:
            self.openvpn_stage.set_status("error", "未连接")
        elif state == ConnectionState.CONNECTING:
            self.openvpn_stage.set_status("info", "连接中")
        elif state == ConnectionState.ERROR:
            self.openvpn_stage.set_status("error", "连接失败")
        else:
            self.openvpn_stage.set_status("neutral", "未连接")

        if verified_flag is True or state == ConnectionState.CONNECTED:
            self.exit_stage.set_status("success", "住宅出口有效")
        elif verified_flag is False:
            self.exit_stage.set_status("error", "验证失败")
        elif state == ConnectionState.VERIFYING:
            self.exit_stage.set_status("info", "验证中")
        elif state == ConnectionState.DEGRADED:
            self.exit_stage.set_status("warning", "需要复检")
        elif state == ConnectionState.ERROR:
            self.exit_stage.set_status("error", "不可用")
        else:
            self.exit_stage.set_status("neutral", "待验证")

        tone = {
            ConnectionState.CONNECTED: "success",
            ConnectionState.DEGRADED: "warning",
            ConnectionState.FAILING_OVER: "warning",
            ConnectionState.ERROR: "error",
        }.get(state, "info" if state in BUSY_STATES else "neutral")
        message = snapshot.message.strip() if snapshot.message else ""
        if state == ConnectionState.IDLE and not message:
            message = STATE_LABELS[state]
        self.connection_message.setText(message or STATE_LABELS[state])
        self.connection_message.setToolTip(message or STATE_LABELS[state])
        set_widget_property(self.connection_message, "tone", tone)

    def _update_exit_summary(self, snapshot: ConnectionSnapshot) -> None:
        metadata = snapshot.metadata
        self.exit_ip_metric.set_value(snapshot.exit_ip)
        country_code = str(metadata.get("country_code") or "")
        country = localized_country_name(
            country_code,
            str(metadata.get("country") or metadata.get("country_name") or ""),
        )
        country_display = (
            f"{country} ({country_code})"
            if country and country_code
            else country or country_code
        )
        self.country_metric.set_value(country_display)
        self.isp_metric.set_value(str(metadata.get("isp") or ""))
        latency = metadata.get("latency_ms")
        self.latency_metric.set_value(f"{latency} ms" if latency is not None else "")
        self._refresh_connected_duration()

    def _refresh_connected_duration(self) -> None:
        connected_since = self._snapshot.connected_since
        if connected_since is None:
            self.duration_metric.set_value("")
            return
        now = datetime.now(UTC) if connected_since.tzinfo is not None else datetime.now()
        total_seconds = max(0, int((now - connected_since).total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.duration_metric.set_value(f"{hours:02d}:{minutes:02d}:{seconds:02d}")

    def _update_controls(self) -> None:
        state = self._snapshot.state
        busy = self._presentational_busy or state in BUSY_STATES
        connected = state in {ConnectionState.CONNECTED, ConnectionState.DEGRADED}
        has_connection_context = bool(
            self._snapshot.active_node_id
            or self._snapshot.exit_ip
            or self._snapshot.connected_since
            or self._snapshot.metadata.get("tunnel_connected") is True
        )
        can_disconnect = state in {
            ConnectionState.CONNECTING,
            ConnectionState.VERIFYING,
            ConnectionState.CONNECTED,
            ConnectionState.DEGRADED,
            ConnectionState.FAILING_OVER,
        } or (state in {ConnectionState.IDLE, ConnectionState.ERROR} and has_connection_context)
        self.refresh_button.setEnabled(not busy)
        self.empty_refresh_button.setEnabled(not busy)
        self.retry_button.setEnabled(not busy)
        self.connect_button.setEnabled(not busy and not connected and not has_connection_context)
        self.switch_button.setEnabled(not busy and connected)
        self.disconnect_button.setEnabled(can_disconnect)
        self.country_combo.setEnabled(not busy)
        self.strict_checkbox.setEnabled(not busy)
        self.environment_button.setEnabled(state != ConnectionState.DISCONNECTING)


__all__ = ["MainWindow"]
