"""Palette and Qt style sheet for the blade-shape GUI.

The look is a flat, dark "engineering console" theme: deep neutral surfaces,
teal/blue accents for data series, and colour-coded status semantics.  All
colours live in :data:`PALETTE` so charts and widgets stay consistent.
"""
from __future__ import annotations

from PySide6.QtGui import QColor

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
PALETTE: dict[str, str] = {
    "bg": "#0e1319",
    "surface": "#151c24",
    "surface_alt": "#1a232e",
    "surface_hi": "#212c39",
    "border": "#28323f",
    "border_soft": "#1f2833",
    "text": "#e8eef5",
    "text_dim": "#93a1b1",
    "text_faint": "#65717f",
    "accent": "#2dd4bf",
    "accent_dim": "#1f9d8c",
    "accent_soft": "rgba(45, 212, 191, 0.14)",
    "blue": "#60a5fa",
    "violet": "#a78bfa",
    "amber": "#fbbf24",
    "orange": "#fb923c",
    "pink": "#f472b6",
    "good": "#34d399",
    "bad": "#f87171",
    "warn": "#fbbf24",
    "grid": "#222c37",
    "grid_strong": "#2c3844",
    "axis": "#3a4654",
}

#: ordered series colours used by every chart
SERIES: list[str] = [
    PALETTE["accent"],
    PALETTE["blue"],
    PALETTE["amber"],
    PALETTE["violet"],
    PALETTE["pink"],
    PALETTE["orange"],
    PALETTE["good"],
]

STATUS_COLORS: dict[str, str] = {
    "success": PALETTE["good"],
    "failed": PALETTE["bad"],
    "failure": PALETTE["bad"],
    "pending": PALETTE["amber"],
    "running": PALETTE["blue"],
    "skipped": PALETTE["text_faint"],
}


def color(name: str) -> QColor:
    """Return a :class:`QColor` for a palette key or a raw colour string."""
    return QColor(PALETTE.get(name, name))


def series_colors(count: int) -> list[QColor]:
    return [QColor(SERIES[i % len(SERIES)]) for i in range(count)]


def rgba(name: str, alpha: float) -> str:
    """``rgba(...)`` string for a palette key or raw colour (Qt-safe)."""
    value = color(name)
    return f"rgba({value.red()}, {value.green()}, {value.blue()}, {alpha})"


def status_color(status: str) -> QColor:
    return QColor(STATUS_COLORS.get((status or "").strip().lower(), PALETTE["text_faint"]))


# --------------------------------------------------------------------------
# style sheet
# --------------------------------------------------------------------------
def build_stylesheet() -> str:
    p = PALETTE
    return f"""
* {{
    font-family: "Inter", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 13px;
    outline: none;
}}
QWidget {{
    background: {p['bg']};
    color: {p['text']};
}}
QToolTip {{
    background: {p['surface_hi']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    padding: 6px 9px;
}}

/* ---------------------------------------------------------------- shell */
#Sidebar {{
    background: {p['surface']};
    border-right: 1px solid {p['border_soft']};
}}
#Brand {{
    background: transparent;
    color: {p['text']};
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.3px;
}}
#BrandSub {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 11px;
}}
#SidebarFooter {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 11px;
}}
#TopBar {{
    background: {p['surface']};
    border-bottom: 1px solid {p['border_soft']};
}}
#PageTitle {{
    background: transparent;
    font-size: 18px;
    font-weight: 700;
}}
#PageSubtitle {{
    background: transparent;
    color: {p['text_dim']};
    font-size: 12px;
}}
#StatusBar {{
    background: {p['surface']};
    border-top: 1px solid {p['border_soft']};
    color: {p['text_faint']};
    font-size: 11px;
}}

/* ------------------------------------------------------------- nav item */
QPushButton#NavItem {{
    background: transparent;
    color: {p['text_dim']};
    border: none;
    border-radius: 9px;
    padding: 9px 12px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#NavItem:hover {{
    background: {p['surface_alt']};
    color: {p['text']};
}}
QPushButton#NavItem:checked {{
    background: {p['accent_soft']};
    color: {p['accent']};
    font-weight: 600;
}}

/* ------------------------------------------------------------ containers */
#Card {{
    background: {p['surface']};
    border: 1px solid {p['border_soft']};
    border-radius: 12px;
}}
#CardTitle {{
    background: transparent;
    font-size: 13px;
    font-weight: 700;
    color: {p['text']};
}}
#CardHint {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 11px;
}}
#SectionLabel {{
    background: transparent;
    color: {p['text_dim']};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
}}
#EmptyTitle {{
    background: transparent;
    color: {p['text_dim']};
    font-size: 14px;
    font-weight: 600;
}}
#EmptyHint {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 12px;
}}

/* ------------------------------------------------------------ stat tile */
#StatTile {{
    background: {p['surface']};
    border: 1px solid {p['border_soft']};
    border-radius: 12px;
}}
#StatValue {{
    background: transparent;
    font-size: 24px;
    font-weight: 700;
}}
#StatLabel {{
    background: transparent;
    color: {p['text_dim']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
#StatFoot {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 11px;
}}

/* --------------------------------------------------------------- badges */
#Badge {{
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 700;
}}

/* -------------------------------------------------------------- buttons */
QPushButton {{
    background: {p['surface_alt']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}}
QPushButton:hover {{
    background: {p['surface_hi']};
    border-color: {p['axis']};
}}
QPushButton:pressed {{
    background: {p['surface']};
}}
QPushButton:disabled {{
    color: {p['text_faint']};
    background: {p['surface']};
    border-color: {p['border_soft']};
}}
QPushButton[variant="primary"] {{
    background: {p['accent_dim']};
    border-color: {p['accent_dim']};
    color: #04211d;
}}
QPushButton[variant="primary"]:hover {{
    background: {p['accent']};
    border-color: {p['accent']};
}}
QPushButton[variant="danger"] {{
    background: #7f2b2b;
    border-color: #a33a3a;
    color: #ffe9e9;
}}
QPushButton[variant="danger"]:hover {{
    background: #a33a3a;
}}
QPushButton[variant="ghost"] {{
    background: transparent;
    border-color: {p['border_soft']};
    color: {p['text_dim']};
}}
QPushButton[variant="ghost"]:hover {{
    color: {p['text']};
    border-color: {p['axis']};
}}

/* ---------------------------------------------------------------- input */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p['bg']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 6px 9px;
    selection-background-color: {p['accent_dim']};
    selection-color: #04211d;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {p['accent_dim']};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {p['text_faint']};
    background: {p['surface']};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: {p['surface_hi']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    selection-background-color: {p['accent_dim']};
    selection-color: #04211d;
    padding: 4px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background: {p['surface_alt']};
    border: none;
    width: 16px;
}}
QCheckBox, QRadioButton {{
    background: transparent;
    spacing: 7px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {p['axis']};
    border-radius: 4px;
    background: {p['bg']};
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p['accent']};
    border-color: {p['accent']};
}}
QCheckBox::indicator:disabled {{
    border-color: {p['border_soft']};
}}

/* ---------------------------------------------------------------- table */
QTableWidget, QTableView, QTreeWidget, QListWidget {{
    background: {p['surface']};
    alternate-background-color: {p['surface_alt']};
    border: 1px solid {p['border_soft']};
    border-radius: 10px;
    gridline-color: {p['border_soft']};
    selection-background-color: {p['accent_soft']};
    selection-color: {p['text']};
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{
    padding: 5px 6px;
    border: none;
}}
QHeaderView::section {{
    background: {p['surface_alt']};
    color: {p['text_dim']};
    border: none;
    border-bottom: 1px solid {p['border_soft']};
    border-right: 1px solid {p['border_soft']};
    padding: 7px 8px;
    font-size: 11px;
    font-weight: 700;
}}
QTableCornerButton::section {{
    background: {p['surface_alt']};
    border: none;
}}

/* --------------------------------------------------------------- tabs */
QTabWidget::pane {{
    border: 1px solid {p['border_soft']};
    border-radius: 10px;
    background: {p['surface']};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {p['text_dim']};
    padding: 8px 16px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
}}
QTabBar::tab:hover {{
    color: {p['text']};
}}
QTabBar::tab:selected {{
    background: {p['surface']};
    color: {p['accent']};
    border-color: {p['border_soft']};
    border-bottom-color: {p['surface']};
}}

/* ------------------------------------------------------------ scrollbar */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p['border']};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p['axis']};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p['border']};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* --------------------------------------------------------------- misc */
QSplitter::handle {{
    background: {p['border_soft']};
}}
QSplitter::handle:horizontal {{
    width: 2px;
}}
QSplitter::handle:vertical {{
    height: 2px;
}}
QProgressBar {{
    background: {p['bg']};
    border: 1px solid {p['border_soft']};
    border-radius: 7px;
    height: 12px;
    text-align: center;
    font-size: 10px;
    color: {p['text_dim']};
}}
QProgressBar::chunk {{
    background: {p['accent']};
    border-radius: 6px;
}}
QGroupBox {{
    border: 1px solid {p['border_soft']};
    border-radius: 10px;
    margin-top: 14px;
    padding-top: 8px;
    font-weight: 700;
    color: {p['text_dim']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QLabel {{
    background: transparent;
}}
QMenu {{
    background: {p['surface_hi']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {p['accent_soft']};
    color: {p['accent']};
}}
"""
