"""Palette, fonts and Qt style sheet for the blade-shape GUI.

The look is a flat, dark "engineering console" theme: deep graphite surfaces,
a teal brand accent, blue/violet data series and colour-coded status
semantics.  All colours live in :data:`PALETTE` so charts and widgets stay
consistent.  Qt style sheets cannot draw arrows or check marks once a combo,
spin box or check box is restyled, so :func:`ensure_assets` renders those small
glyphs to PNG files that the style sheet references.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
PALETTE: dict[str, str] = {
    "bg": "#0b1016",
    "surface": "#121922",
    "surface_alt": "#17202a",
    "surface_hi": "#1e2834",
    "border": "#263140",
    "border_soft": "#1b2430",
    "text": "#e6edf5",
    "text_dim": "#97a5b5",
    "text_faint": "#5f6c7a",
    "accent": "#2dd4bf",
    "accent_dim": "#14b8a6",
    "accent_soft": "rgba(45, 212, 191, 0.13)",
    "blue": "#60a5fa",
    "violet": "#a78bfa",
    "amber": "#fbbf24",
    "orange": "#fb923c",
    "pink": "#f472b6",
    "good": "#34d399",
    "bad": "#f87171",
    "warn": "#fbbf24",
    "grid": "#1c2531",
    "grid_strong": "#283341",
    "axis": "#3a4656",
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
    "complete": PALETTE["good"],
    "failed": PALETTE["bad"],
    "failure": PALETTE["bad"],
    "pending": PALETTE["amber"],
    "running": PALETTE["blue"],
    "skipped": PALETTE["text_faint"],
}

#: text on a filled accent button
ON_ACCENT = "#042420"

UI_FAMILIES = [
    "Inter", "SF Pro Text", "Segoe UI Variable Text", "Segoe UI", "PingFang SC",
    "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC",
    "Helvetica Neue", "Arial",
]
MONO_FAMILIES = ["JetBrains Mono", "SF Mono", "Cascadia Mono", "Menlo", "Consolas", "DejaVu Sans Mono"]


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
# fonts
# --------------------------------------------------------------------------
def available_families(preferred: list[str]) -> list[str]:
    """Installed members of ``preferred`` (needs a QGuiApplication).

    Naming only installed families avoids Qt's slow alias scan for missing
    fonts and keeps CJK text on a real CJK face.
    """
    try:
        installed = set(QFontDatabase.families())
    except Exception:  # noqa: BLE001 - no application yet
        return []
    return [family for family in preferred if family in installed]


def mono_font(pixel_size: int = 11) -> QFont:
    """Fixed-pitch font.

    ``QFont("SF Mono, Menlo, ...")`` names one non-existent family and silently
    falls back to a proportional face, so resolve real families instead.
    """
    families = available_families(MONO_FAMILIES)
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    if families:
        font.setFamilies(families)
    font.setStyleHint(QFont.Monospace)
    font.setPixelSize(pixel_size)
    return font


def application_palette() -> QPalette:
    """Dark palette so native/unstyled surfaces (dialogs, popups) match."""
    palette = QPalette()
    roles = {
        QPalette.Window: PALETTE["bg"], QPalette.WindowText: PALETTE["text"],
        QPalette.Base: PALETTE["bg"], QPalette.AlternateBase: PALETTE["surface_alt"],
        QPalette.Text: PALETTE["text"], QPalette.Button: PALETTE["surface_alt"],
        QPalette.ButtonText: PALETTE["text"], QPalette.ToolTipBase: PALETTE["surface_hi"],
        QPalette.ToolTipText: PALETTE["text"], QPalette.Highlight: PALETTE["accent_dim"],
        QPalette.HighlightedText: ON_ACCENT, QPalette.PlaceholderText: PALETTE["text_faint"],
        QPalette.Link: PALETTE["accent"],
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(PALETTE["text_faint"]))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(PALETTE["text_faint"]))
    return palette


# --------------------------------------------------------------------------
# style-sheet glyphs
# --------------------------------------------------------------------------
def ensure_assets() -> dict[str, str]:
    """Render chevrons/check marks to PNG and return ``{name: posix path}``.

    Files live in a per-palette temp folder (``@2x`` variants for HiDPI); an
    unwritable temp dir simply leaves the default Fusion glyphs in place.
    """
    from . import icons  # noqa: PLC0415 - icons imports this module

    glyphs = {
        "chevron_down": ("chevron_down", PALETTE["text_dim"], 12),
        "chevron_up": ("chevron_up", PALETTE["text_dim"], 10),
        "chevron_down_small": ("chevron_down", PALETTE["text_dim"], 10),
        "check": ("check_bold", ON_ACCENT, 12),
        "dash": ("dash", PALETTE["text_dim"], 12),
    }
    key = hashlib.sha1(repr(sorted(glyphs.items())).encode()).hexdigest()[:10]
    folder = Path(tempfile.gettempdir()) / f"blade_gui_assets_{key}"
    out: dict[str, str] = {}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, (icon_name, tint, size) in glyphs.items():
            path = folder / f"{name}.png"
            if not path.exists():
                icons.pixmap(icon_name, tint, size).save(str(path))
            hi = folder / f"{name}@2x.png"
            if not hi.exists():
                icons.pixmap(icon_name, tint, size * 2).save(str(hi))
            out[name] = path.as_posix()
    except OSError:
        return {}
    return out


# --------------------------------------------------------------------------
# style sheet
# --------------------------------------------------------------------------
def build_stylesheet(assets: dict[str, str] | None = None, families: list[str] | None = None) -> str:
    p = PALETTE
    a = assets or {}
    family_list = ", ".join(f'"{name}"' for name in (families or UI_FAMILIES))

    def image(name: str) -> str:
        return f"image: url({a[name]});" if name in a else ""

    return f"""
* {{
    font-family: {family_list};
    font-size: 13px;
    outline: none;
}}
QWidget {{
    background: transparent;
    color: {p['text']};
}}
QMainWindow, QDialog {{
    background: {p['bg']};
}}
QComboBoxPrivateContainer {{
    background: {p['surface_hi']};
    border: 1px solid {p['border']};
    border-radius: 8px;
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
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #111821, stop:1 #0d131a);
    border-right: 1px solid {p['border_soft']};
}}
#Brand {{
    background: transparent;
    color: {p['text']};
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.2px;
}}
#BrandSub {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 11px;
}}
#NavSection {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.2px;
    padding: 10px 12px 2px 12px;
}}
#SidebarFooter {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 11px;
}}
#SidebarStats {{
    background: {p['surface']};
    border: 1px solid {p['border_soft']};
    border-radius: 10px;
}}
#StatsValue {{
    background: transparent;
    font-size: 15px;
    font-weight: 700;
}}
#StatsLabel {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 10px;
    font-weight: 600;
}}
#TopBar {{
    background: {p['bg']};
    border-bottom: 1px solid {p['border_soft']};
}}
#PageTitle {{
    background: transparent;
    font-size: 19px;
    font-weight: 700;
}}
#PageSubtitle {{
    background: transparent;
    color: {p['text_dim']};
    font-size: 12px;
}}
#Chip {{
    background: {p['surface']};
    border: 1px solid {p['border_soft']};
    border-radius: 12px;
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
    padding: 8px 10px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#NavItem:hover {{
    background: {p['surface_alt']};
    color: {p['text']};
}}
QPushButton#NavItem:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(45, 212, 191, 0.22), stop:1 rgba(45, 212, 191, 0.04));
    color: {p['accent']};
    font-weight: 600;
}}
#NavShortcut {{
    background: transparent;
    color: {p['text_faint']};
    font-size: 10px;
}}

/* ------------------------------------------------------------ containers */
#Card {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #141c26, stop:1 #111821);
    border: 1px solid {p['border_soft']};
    border-radius: 14px;
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
#FormLabel {{
    background: transparent;
    color: {p['text_dim']};
    font-size: 12px;
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
#Divider {{
    background: {p['border_soft']};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}

/* ------------------------------------------------------------ stat tile */
#StatTile {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #151e28, stop:1 #111821);
    border: 1px solid {p['border_soft']};
    border-radius: 14px;
}}
#StatValue {{
    background: transparent;
    font-size: 24px;
    font-weight: 700;
}}
#StatValueCompact {{
    background: transparent;
    font-size: 19px;
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
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2dd4bf, stop:1 #14b8a6);
    border: 1px solid #2dd4bf;
    color: {ON_ACCENT};
}}
QPushButton[variant="primary"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5eead4, stop:1 #2dd4bf);
    border-color: #5eead4;
}}
QPushButton[variant="primary"]:disabled {{
    background: {p['surface_alt']};
    border-color: {p['border_soft']};
    color: {p['text_faint']};
}}
QPushButton[variant="danger"] {{
    background: rgba(248, 113, 113, 0.14);
    border-color: rgba(248, 113, 113, 0.55);
    color: #fecaca;
}}
QPushButton[variant="danger"]:hover {{
    background: rgba(248, 113, 113, 0.26);
    border-color: {p['bad']};
}}
QPushButton[variant="danger"]:disabled {{
    background: {p['surface']};
    border-color: {p['border_soft']};
    color: {p['text_faint']};
}}
QPushButton[variant="ghost"] {{
    background: transparent;
    border-color: {p['border_soft']};
    color: {p['text_dim']};
}}
QPushButton[variant="ghost"]:hover {{
    background: {p['surface_alt']};
    color: {p['text']};
    border-color: {p['axis']};
}}
QPushButton[variant="ghost"]:checked {{
    background: {p['accent_soft']};
    color: {p['accent']};
    border-color: rgba(45, 212, 191, 0.45);
}}

/* ---------------------------------------------------------------- input */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p['bg']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 5px 9px;
    min-height: 20px;
    selection-background-color: {p['accent_dim']};
    selection-color: {ON_ACCENT};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: {p['axis']};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {p['accent_dim']};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {p['text_faint']};
    background: {p['surface']};
}}
QLineEdit[readOnly="true"] {{
    background: {p['surface']};
    color: {p['text_dim']};
}}
QComboBox {{
    padding-right: 28px;
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    {image('chevron_down')}
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background: {p['surface_hi']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    selection-background-color: {p['accent_soft']};
    selection-color: {p['accent']};
    padding: 4px;
    outline: none;
}}
QSpinBox, QDoubleSpinBox {{
    padding-right: 24px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 20px;
    border: none;
    border-left: 1px solid {p['border_soft']};
    border-top-right-radius: 8px;
    background: {p['surface_alt']};
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 20px;
    border: none;
    border-left: 1px solid {p['border_soft']};
    border-bottom-right-radius: 8px;
    background: {p['surface_alt']};
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {p['surface_hi']};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    {image('chevron_up')}
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    {image('chevron_down_small')}
    width: 10px;
    height: 10px;
}}
QCheckBox, QRadioButton {{
    background: transparent;
    spacing: 8px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {p['axis']};
    border-radius: 5px;
    background: {p['bg']};
}}
QCheckBox::indicator:hover {{
    border-color: {p['accent_dim']};
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p['accent']};
    border-color: {p['accent']};
    {image('check')}
}}
QCheckBox::indicator:disabled {{
    border-color: {p['border_soft']};
}}

/* ---------------------------------------------------------------- table */
QTableWidget, QTableView, QTreeWidget, QListWidget {{
    background: {p['surface']};
    alternate-background-color: #151d27;
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
QTableWidget::item:hover, QTreeWidget::item:hover {{
    background: rgba(255, 255, 255, 0.03);
}}
QHeaderView {{
    background: transparent;
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
    border-radius: 12px;
    background: {p['surface']};
    top: -1px;
}}
QTabBar {{
    background: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: {p['text_dim']};
    padding: 8px 16px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    font-weight: 600;
}}
QTabBar::tab:hover {{
    color: {p['text']};
    background: {p['surface_alt']};
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
    border-radius: 4px;
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
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p['axis']};
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
    background: transparent;
}}
QSplitter::handle:horizontal {{
    width: 8px;
}}
QSplitter::handle:vertical {{
    height: 8px;
}}
QSplitter::handle:hover {{
    background: {p['accent_soft']};
}}
QProgressBar {{
    background: {p['bg']};
    border: 1px solid {p['border_soft']};
    border-radius: 4px;
    max-height: 6px;
    text-align: center;
    font-size: 10px;
    color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #14b8a6, stop:1 #60a5fa);
    border-radius: 3px;
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
QMessageBox {{
    background: {p['surface']};
}}
"""
