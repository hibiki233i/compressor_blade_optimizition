"""Vector icons rendered from inline SVG.

QtSvg ships with PySide6-Essentials, so no icon font or image asset is needed.
Each icon is a 24x24 stroke path using ``currentColor``, which is substituted
with the requested colour before rendering.
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

#: 24x24 line-art paths (stroke, round caps)
_PATHS: dict[str, str] = {
    "dashboard": "M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z",
    "chart": "M4 20V9m5 11V4m5 16v-7m5 7V7",
    "tune": "M4 7h10m4 0h2M4 12h4m4 0h8M4 17h12m4 0h0M14 5v4M8 10v4M16 15v4",
    "play": "M8 5.5v13l11-6.5-11-6.5Z",
    "folder": "M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z",
    "refresh": "M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6",
    "save": "M5 4h11l3 3v13H5V4Zm3 0v6h8V4M8 20v-6h8v6",
    "stop": "M7 7h10v10H7z",
    "check": "M4.5 12.5 9.5 17.5 19.5 6.5",
    "alert": "M12 4 2.5 20h19L12 4Zm0 5.5v5m0 3v.5",
    "info": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13v.5m0 3.5v5",
    "file": "M6 3h8l4 4v14H6V3Zm8 0v4h4M9 12h6M9 16h6",
    "search": "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5 -2 4 4",
    "sigma": "M5 5h14L12 12l7 7H5",
    "target": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-4.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9Zm0-3a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z",
    "layers": "M12 3 3 8l9 5 9-5-9-5Zm-9 8 9 5 9-5M3 15l9 5 9-5",
    "clock": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13v5l3.5 2",
    "download": "M12 4v10m0 0 4-4m-4 4-4-4M5 19h14",
    "trash": "M4 7h16M9 7V5h6v2m-8 0 1 13h8l1-13",
    "plus": "M12 5v14M5 12h14",
    "pin": "M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11Zm0-8.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
}

_FILLED = {"play", "stop", "dashboard", "pin", "target"}


def _svg(name: str, color: str) -> str:
    path = _PATHS.get(name, _PATHS["info"])
    filled = name in _FILLED
    attrs = (
        f'fill="{color}" stroke="none"'
        if filled
        else f'fill="none" stroke="{color}" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round"'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<path {attrs} d="{path}"/></svg>'
    )


@lru_cache(maxsize=256)
def pixmap(name: str, color: str = "#e8eef5", size: int = 22) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(_svg(name, color).encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pm


@lru_cache(maxsize=256)
def icon(name: str, color: str | None = None, size: int = 22) -> QIcon:
    return QIcon(pixmap(name, color or theme.PALETTE["text"], size))
