from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QWidget


@dataclass(frozen=True, slots=True)
class ThemeColors:
    background: str = "#F3F5F7"
    surface: str = "#FFFFFF"
    surface_subtle: str = "#E9EDF1"
    surface_hover: str = "#E2E8EE"
    text: str = "#1F2933"
    text_muted: str = "#52606D"
    text_faint: str = "#6B7785"
    border: str = "#C8D0D8"
    border_strong: str = "#9EABB7"
    primary: str = "#2D607D"
    primary_hover: str = "#234D65"
    success: str = "#16734A"
    success_surface: str = "#E5F3EC"
    warning: str = "#955B0A"
    warning_surface: str = "#FFF1D6"
    error: str = "#B42318"
    error_surface: str = "#FDEAE8"
    focus: str = "#0B6FA4"
    selection: str = "#D9EAF4"
    disabled: str = "#A4ADB7"


COLORS = ThemeColors()

TONE_COLORS: dict[str, str] = {
    "neutral": COLORS.text_muted,
    "info": COLORS.primary,
    "success": COLORS.success,
    "warning": COLORS.warning,
    "error": COLORS.error,
}


def tone_color(tone: str) -> str:
    return TONE_COLORS.get(tone, COLORS.text_muted)


def set_widget_property(widget: QWidget, name: str, value: object) -> None:
    """Set a QSS property and refresh only the affected widget."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def application_stylesheet() -> str:
    c = COLORS
    return f"""
    QWidget {{
        color: {c.text};
        font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
        font-size: 13px;
        letter-spacing: 0px;
    }}
    QMainWindow, QDialog {{
        background: {c.background};
    }}
    QToolTip {{
        color: {c.text};
        background: {c.surface};
        border: 1px solid {c.border_strong};
        padding: 5px 7px;
    }}
    QFrame#appHeader,
    QFrame#linkStatusBar,
    QFrame#exitSummary,
    QFrame#commandBar,
    QFrame#progressStrip,
    QFrame#logDrawer,
    QFrame#dialogSummary {{
        background: {c.surface};
        border: 0;
        border-bottom: 1px solid {c.border};
    }}
    QFrame#linkStatusBar,
    QFrame#progressStrip {{
        background: {c.surface_subtle};
    }}
    QFrame#logDrawer {{
        border-top: 1px solid {c.border};
        border-bottom: 0;
    }}
    QLabel[role="title"] {{
        color: {c.text};
        font-size: 18px;
        font-weight: 600;
    }}
    QLabel[role="section"] {{
        color: {c.text};
        font-size: 14px;
        font-weight: 600;
    }}
    QLabel[role="metricLabel"], QLabel[role="muted"] {{
        color: {c.text_muted};
    }}
    QLabel[role="metricValue"] {{
        color: {c.text};
        font-family: "Cascadia Mono", "Consolas", monospace;
        font-size: 14px;
        font-weight: 600;
    }}
    QLabel[tone="neutral"] {{ color: {c.text_muted}; }}
    QLabel[tone="info"] {{ color: {c.primary}; }}
    QLabel[tone="success"] {{ color: {c.success}; }}
    QLabel[tone="warning"] {{ color: {c.warning}; }}
    QLabel[tone="error"] {{ color: {c.error}; }}
    QPushButton, QToolButton {{
        min-height: 30px;
        padding: 2px 10px;
        color: {c.text};
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 4px;
    }}
    QPushButton:hover, QToolButton:hover {{
        background: {c.surface_hover};
        border-color: {c.primary};
    }}
    QPushButton:pressed, QToolButton:pressed {{
        background: {c.surface_subtle};
    }}
    QPushButton:focus, QToolButton:focus,
    QComboBox:focus, QCheckBox:focus {{
        border: 2px solid {c.focus};
    }}
    QPushButton:disabled, QToolButton:disabled {{
        color: {c.disabled};
        background: {c.surface_subtle};
        border-color: {c.border};
    }}
    QPushButton[role="primary"] {{
        color: #FFFFFF;
        background: {c.success};
        border-color: {c.success};
        font-weight: 600;
    }}
    QPushButton[role="primary"]:hover {{
        background: #115D3B;
        border-color: #115D3B;
    }}
    QPushButton[role="danger"] {{
        color: {c.error};
        background: {c.surface};
        border-color: {c.error};
    }}
    QPushButton[role="danger"]:hover {{
        background: {c.error_surface};
    }}
    QPushButton[role="danger"]:disabled {{
        color: {c.disabled};
        background: {c.surface_subtle};
        border-color: {c.border};
    }}
    QComboBox {{
        min-height: 30px;
        min-width: 142px;
        padding: 1px 28px 1px 8px;
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 4px;
    }}
    QComboBox:hover {{ border-color: {c.primary}; }}
    QComboBox::drop-down {{
        width: 24px;
        border: 0;
        border-left: 1px solid {c.border};
    }}
    QComboBox QAbstractItemView {{
        background: {c.surface};
        border: 1px solid {c.border_strong};
        selection-background-color: {c.selection};
        selection-color: {c.text};
        outline: 0;
    }}
    QCheckBox {{
        spacing: 7px;
        min-height: 30px;
        padding: 0 2px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 3px;
    }}
    QCheckBox::indicator:hover {{ border-color: {c.primary}; }}
    QCheckBox::indicator:checked {{
        background: {c.success};
        border-color: {c.success};
    }}
    QCheckBox:disabled {{ color: {c.disabled}; }}
    QTableView {{
        background: {c.surface};
        alternate-background-color: #F7F9FA;
        border: 1px solid {c.border};
        border-radius: 4px;
        gridline-color: {c.surface_subtle};
        selection-background-color: {c.selection};
        selection-color: {c.text};
        outline: 0;
    }}
    QTableView::item {{
        min-height: 28px;
        padding: 3px 6px;
        border-bottom: 1px solid {c.surface_subtle};
    }}
    QTableView::item:hover {{ background: #EEF4F7; }}
    QTableView::item:focus {{ border: 1px solid {c.focus}; }}
    QHeaderView::section {{
        color: {c.text_muted};
        background: {c.surface_subtle};
        border: 0;
        border-right: 1px solid {c.border};
        border-bottom: 1px solid {c.border};
        padding: 6px;
        font-weight: 600;
    }}
    QProgressBar {{
        min-height: 14px;
        max-height: 14px;
        color: {c.text};
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 4px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {c.primary};
        border-radius: 3px;
    }}
    QPlainTextEdit {{
        color: {c.text};
        background: #FAFBFC;
        border: 1px solid {c.border};
        border-radius: 4px;
        padding: 6px;
        font-family: "Cascadia Mono", "Consolas", monospace;
        font-size: 12px;
        selection-background-color: {c.selection};
    }}
    QScrollBar:vertical {{
        width: 12px;
        background: {c.surface_subtle};
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        min-height: 28px;
        background: {c.border_strong};
        border-radius: 4px;
        margin: 2px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QSplitter::handle {{ background: {c.border}; }}
    """
