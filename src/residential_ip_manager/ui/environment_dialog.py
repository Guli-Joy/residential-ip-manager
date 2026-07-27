from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from residential_ip_manager.domain.models import EnvironmentReport
from residential_ip_manager.ui.models import EnvironmentCheckTableModel, EnvironmentColumn
from residential_ip_manager.ui.theme import set_widget_property


class EnvironmentDialog(QDialog):
    check_requested = Signal()
    repair_requested = Signal(str)

    PAGE_RESULTS = 0
    PAGE_LOADING = 1
    PAGE_EMPTY = 2
    PAGE_ERROR = 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行环境检测")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumSize(720, 440)
        self.resize(820, 500)
        self.setModal(False)

        self._model = EnvironmentCheckTableModel(parent=self)
        self._auto_repair_action = ""
        self._manual_repair_action = ""
        self._manual_repair_targets: tuple[str, ...] = ()
        self._build_ui()
        self._wire_events()
        self.set_empty()

    @property
    def model(self) -> EnvironmentCheckTableModel:
        return self._model

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        summary = QFrame(self)
        summary.setObjectName("dialogSummary")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(16, 14, 16, 14)
        summary_layout.setSpacing(12)

        self.summary_icon = QLabel(summary)
        self.summary_icon.setFixedSize(28, 28)
        self.summary_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.summary_icon.setAccessibleName("环境状态")
        summary_layout.addWidget(self.summary_icon)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        self.summary_title = QLabel("尚未检测", summary)
        self.summary_title.setProperty("role", "section")
        self.summary_detail = QLabel("检测 OpenVPN、Clash 和 Windows 网络组件", summary)
        self.summary_detail.setProperty("role", "muted")
        self.summary_detail.setWordWrap(True)
        text_layout.addWidget(self.summary_title)
        text_layout.addWidget(self.summary_detail)
        summary_layout.addLayout(text_layout, 1)
        root.addWidget(summary)

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 14, 16, 12)
        content_layout.setSpacing(10)

        self.pages = QStackedWidget(content)
        self.pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.pages.addWidget(self._build_results_page())
        self.pages.addWidget(self._build_loading_page())
        self.pages.addWidget(self._build_empty_page())
        self.pages.addWidget(self._build_error_page())
        content_layout.addWidget(self.pages, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.progress = QProgressBar(content)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(180)
        self.progress.setAccessibleName("环境检测进度")
        self.progress.hide()
        footer.addWidget(self.progress)
        footer.addStretch(1)

        self.auto_repair_button = QPushButton("自动修复 (&A)", content)
        self.auto_repair_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
        )
        self.auto_repair_button.setToolTip("自动处理可安全识别的 OpenVPN 残留项 (Alt+A)")
        self.auto_repair_button.setAccessibleDescription("仅自动处理归属明确的 OpenVPN 残留项")
        self.auto_repair_button.hide()
        footer.addWidget(self.auto_repair_button)

        self.manual_repair_button = QPushButton("手动修复 (&F)", content)
        self.manual_repair_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        )
        self.manual_repair_button.setToolTip("查看精确目标并确认清理分流路由 (Alt+F)")
        self.manual_repair_button.setAccessibleDescription("确认目标后手动清理分流路由")
        self.manual_repair_button.hide()
        footer.addWidget(self.manual_repair_button)

        self.check_button = QPushButton("重新检测 (&R)", content)
        self.check_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.check_button.setToolTip("重新检测运行环境 (Alt+R)")
        self.check_button.setAccessibleDescription("检查程序运行所需的本地组件")
        footer.addWidget(self.check_button)

        self.close_button = QPushButton("关闭", content)
        self.close_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton)
        )
        self.close_button.setDefault(True)
        footer.addWidget(self.close_button)
        content_layout.addLayout(footer)
        root.addWidget(content, 1)

    def _build_results_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableView(page)
        self.table.setModel(self._model)
        self.table.setAccessibleName("环境检测结果")
        self.table.setAccessibleDescription("列出每个组件是否满足运行要求")
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(
            EnvironmentColumn.COMPONENT,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(EnvironmentColumn.REQUIREMENT, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(EnvironmentColumn.REQUIREMENT, 72)
        header.setSectionResizeMode(EnvironmentColumn.STATUS, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(EnvironmentColumn.STATUS, 80)
        header.setSectionResizeMode(EnvironmentColumn.DETAIL, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        return page

    def _build_loading_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel(page)
        icon.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
            .pixmap(32, 32)
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAccessibleName("正在检测")
        label = QLabel("正在检测运行环境…", page)
        label.setProperty("role", "section")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        layout.addSpacing(8)
        layout.addWidget(label)
        return page

    def _build_empty_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel(page)
        icon.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon).pixmap(36, 36)
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("运行环境尚未检测", page)
        label.setProperty("role", "section")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        layout.addSpacing(8)
        layout.addWidget(label)
        return page

    def _build_error_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel(page)
        icon.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical).pixmap(36, 36)
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label = QLabel(page)
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setWordWrap(True)
        self.error_label.setMaximumWidth(560)
        self.error_label.setProperty("tone", "error")
        self.error_label.setAccessibleName("环境检测错误")
        layout.addWidget(icon)
        layout.addSpacing(8)
        layout.addWidget(self.error_label)
        return page

    def _wire_events(self) -> None:
        self.auto_repair_button.clicked.connect(self._request_auto_repair)
        self.manual_repair_button.clicked.connect(self._request_manual_repair)
        self.check_button.clicked.connect(self._request_check)
        self.close_button.clicked.connect(self.close)

    @Slot()
    def _request_check(self) -> None:
        self.set_checking()
        self.check_requested.emit()

    @Slot()
    def _request_auto_repair(self) -> None:
        if not self._auto_repair_action:
            return
        action = self._auto_repair_action
        self.set_repairing(manual=False)
        self.repair_requested.emit(action)

    @Slot()
    def _request_manual_repair(self) -> None:
        if not self._manual_repair_action:
            return
        target_text = "\n".join(f"- {target}" for target in self._manual_repair_targets)
        answer = QMessageBox.warning(
            self,
            "确认手动清理分流路由",
            "以下分流路由的接口归属可能无法自动确认：\n\n"
            f"{target_text}\n\n"
            "请先确认其他 VPN/TUN 已断开。程序将按目标网段、下一跳和接口编号"
            "精确删除以上路由，不会关闭 Clash/OpenVPN GUI，也不会修改普通默认网关。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        action = self._manual_repair_action
        self.set_repairing(manual=True)
        self.repair_requested.emit(action)

    @Slot(object)
    def set_report(self, report: EnvironmentReport) -> None:
        self._model.set_report(report)
        self.pages.setCurrentIndex(self.PAGE_RESULTS if report.checks else self.PAGE_EMPTY)
        self.progress.hide()
        self.check_button.setEnabled(True)

        auto_repairable = next(
            (
                check
                for check in report.checks
                if not check.ok and check.repair_action and check.repair_targets
            ),
            None,
        )
        manual_repairable = next(
            (
                check
                for check in report.checks
                if not check.ok
                and check.manual_repair_action
                and check.manual_repair_targets
            ),
            None,
        )
        self._auto_repair_action = (
            auto_repairable.repair_action if auto_repairable else ""
        )
        self._manual_repair_action = (
            manual_repairable.manual_repair_action if manual_repairable else ""
        )
        self._manual_repair_targets = (
            manual_repairable.manual_repair_targets if manual_repairable else ()
        )

        failed_required = sum(1 for check in report.checks if check.required and not check.ok)
        failed_optional = sum(1 for check in report.checks if not check.required and not check.ok)
        show_repair_controls = failed_required > 0
        self.auto_repair_button.setVisible(show_repair_controls)
        self.auto_repair_button.setEnabled(auto_repairable is not None)
        self.manual_repair_button.setVisible(show_repair_controls)
        self.manual_repair_button.setEnabled(manual_repairable is not None)
        self.auto_repair_button.setToolTip(
            "自动处理可安全识别的 OpenVPN 残留项 (Alt+A)"
            if auto_repairable is not None
            else "当前问题无法安全自动修复，请查看详情或使用手动修复"
        )
        self.manual_repair_button.setToolTip(
            "查看精确目标并确认清理分流路由 (Alt+F)"
            if manual_repairable is not None
            else "当前没有可安全提供给手动修复的目标"
        )
        if report.ready:
            self._set_summary(
                "success",
                "环境已就绪",
                "所有必需组件均可用"
                if not failed_optional
                else f"必需组件均可用，{failed_optional} 个可选项不可用",
            )
        else:
            self._set_summary(
                "error",
                "环境未就绪",
                f"{failed_required} 个必需组件需要处理",
            )

    @Slot()
    def set_checking(self) -> None:
        self.pages.setCurrentIndex(self.PAGE_LOADING)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.check_button.setEnabled(False)
        self.auto_repair_button.setEnabled(False)
        self.manual_repair_button.setEnabled(False)
        self._set_summary("info", "正在检测", "正在检查本地组件和网络配置")

    def set_repairing(self, *, manual: bool) -> None:
        self.pages.setCurrentIndex(self.PAGE_RESULTS)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.check_button.setEnabled(False)
        self.auto_repair_button.setEnabled(False)
        self.manual_repair_button.setEnabled(False)
        mode = "手动" if manual else "自动"
        self._set_summary("info", f"正在{mode}修复", "正在安全清理 OpenVPN 残留分流路由")

    @Slot()
    def set_empty(self) -> None:
        self.pages.setCurrentIndex(self.PAGE_EMPTY)
        self.progress.hide()
        self.check_button.setEnabled(True)
        self.auto_repair_button.hide()
        self.manual_repair_button.hide()
        self._set_summary("neutral", "尚未检测", "检测 OpenVPN、Clash 和 Windows 网络组件")

    @Slot(str)
    def set_error(self, message: str) -> None:
        self.error_label.setText(message or "环境检测失败，请重试。")
        self.pages.setCurrentIndex(self.PAGE_ERROR)
        self.progress.hide()
        self.check_button.setEnabled(True)
        self.auto_repair_button.setEnabled(bool(self._auto_repair_action))
        self.manual_repair_button.setEnabled(bool(self._manual_repair_action))
        self._set_summary("error", "检测失败", "未能完成运行环境检测")

    @Slot(str)
    def show_repair_error(self, message: str) -> None:
        QMessageBox.critical(
            self,
            "环境修复失败",
            message or "未能修复 OpenVPN 残留路由，请重新检测后再试。",
        )

    def _set_summary(self, tone: str, title: str, detail: str) -> None:
        self.summary_title.setText(title)
        self.summary_detail.setText(detail)
        set_widget_property(self.summary_title, "tone", tone)
        icon_name = {
            "success": QStyle.StandardPixmap.SP_DialogApplyButton,
            "warning": QStyle.StandardPixmap.SP_MessageBoxWarning,
            "error": QStyle.StandardPixmap.SP_MessageBoxCritical,
            "info": QStyle.StandardPixmap.SP_MessageBoxInformation,
            "neutral": QStyle.StandardPixmap.SP_ComputerIcon,
        }[tone]
        self.summary_icon.setPixmap(self.style().standardIcon(icon_name).pixmap(24, 24))
