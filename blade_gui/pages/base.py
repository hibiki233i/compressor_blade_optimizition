"""Page base class."""
from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..context import AppContext


class Page(QWidget):
    """Base class for a navigation page."""

    #: shown in the top bar
    title = "页面"
    subtitle = ""
    #: sidebar entry
    nav_label = "页面"
    nav_icon = "info"

    def __init__(self, ctx: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx

    def refresh(self) -> None:
        """Re-read data and repaint. Called on every project reload."""

    def on_show(self) -> None:
        """Called when the page becomes the active page."""
