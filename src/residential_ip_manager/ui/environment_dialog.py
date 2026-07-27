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
        self._repair_action = ""
        self._repair_targets: tuple[str, ...] = ()
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

        self.repair_button = QPushButton("修复环境 (&F)", content)
        self.repair_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
        )
        self.repair_button.setToolTip("安全清理 OpenVPN 退出后残留的分流路由 (Alt+F)")
        self.repair_button.setAccessibleDescription("确认后精确删除 OpenVPN 残留路由")
        self.repair_button.hide()
        footer.addWidget(self.repair_button)

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
        self.repair_button.clicked.connect(self._request_repair)
        self.check_button.clicked.connect(self._request_check)
        self.close_button.clicked.connect(self.close)

    @Slot()
    def _request_check(self) -> None:
        self.set_checking()
        self.check_requested.emit()

    @Slot()
    def _request_repair(self) -> None:
        if not self._repair_action:
            return
        target_text = "\n".join(f"- {target}" for target in self._repair_targets)
        answer = QMessageBox.warning(
            self,
            "确认修复 OpenVPN 残留路由",
            "OpenVPN 已退出，但以下分流路由仍在系统中：\n\n"
            f"{target_text}\n\n"
            "程序只会按目标网段和接口编号精确删除以上路由，"
            "不会关闭 OpenVPN GUI，也不会修改普通默认网关。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        action = self._repair_action
        self.set_repairing()
        self.repair_requested.emit(action)

    @Slot(object)
    def set_report(self, report: EnvironmentReport) -> None:
        self._model.set_report(report)
        self.pages.setCurrentIndex(self.PAGE_RESULTS if report.checks else self.PAGE_EMPTY)
        self.progress.hide()
        self.check_button.setEnabled(True)

        repairable = next(
            (
                check
                for check in report.checks
                if not check.ok and check.repair_action and check.repair_targets
            ),
            None,
        )
        self._repair_action = repairable.repair_action if repairable else ""
        self._repair_targets = repairable.repair_targets if repairable else ()
        self.repair_button.setVisible(repairable is not None)
        self.repair_button.setEnabled(repairable is not None)

        failed_required = sum(1 for check in report.checks if check.required and not check.ok)
        failed_optional = sum(1 for check in report.checks if not check.required and not check.ok)
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
        self.repair_button.setEnabled(False)
        self._set_summary("info", "正在检测", "正在检查本地组件和网络配置")

    @Slot()
    def set_repairing(self) -> None:
        self.pages.setCurrentIndex(self.PAGE_RESULTS)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.check_button.setEnabled(False)
        self.repair_button.setEnabled(False)
        self._set_summary("info", "正在修复", "正在安全清理 OpenVPN 残留分流路由")

    @Slot()
    def set_empty(self) -> None:
        self.pages.setCurrentIndex(self.PAGE_EMPTY)
        self.progress.hide()
        self.check_button.setEnabled(True)
        self.repair_button.hide()
        self._set_summary("neutral", "尚未检测", "检测 OpenVPN、Clash 和 Windows 网络组件")

    @Slot(str)
    def set_error(self, message: str) -> None:
        self.error_label.setText(message or "环境检测失败，请重试。")
        self.pages.setCurrentIndex(self.PAGE_ERROR)
        self.progress.hide()
        self.check_button.setEnabled(True)
        self.repair_button.setEnabled(bool(self._repair_action))
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
