"""Reusable presentation widgets shared by every page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import icons, theme


# --------------------------------------------------------------------------
# layout helpers
# --------------------------------------------------------------------------
def clear_layout(layout: QLayout) -> None:
    """Recursively remove and schedule deletion of every item in ``layout``.

    Deleting only the widgets is not enough: a nested layout keeps its own
    children alive, so a repeatedly rebuilt card would stack overlapping
    widgets instead of replacing them.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)
            child.deleteLater()
        else:
            del item


class Card(QFrame):
    """Rounded panel with a title row and a content area."""

    def __init__(self, title: str = "", hint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)

        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("CardHint")
        if title:
            self.header.addWidget(self.title_label)
        if hint:
            self.header.addWidget(self.hint_label)
        self.header.addStretch(1)
        if title or hint:
            outer.addLayout(self.header)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        outer.addLayout(self.body, 1)

    def add_header_widget(self, widget: QWidget) -> None:
        self.header.addWidget(widget)

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(text)


class StatTile(QFrame):
    """Compact KPI tile: big value, caption, optional footnote."""

    def __init__(self, label: str, value: str = "—", foot: str = "",
                 accent: str = "accent", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatTile")
        self.setMinimumWidth(140)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.label = QLabel(label.upper())
        self.label.setObjectName("StatLabel")
        top.addWidget(self.label)
        top.addStretch(1)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet(f"background: {theme.PALETTE.get(accent, accent)}; border-radius: 4px;")
        top.addWidget(self.dot)
        layout.addLayout(top)

        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        self.value.setStyleSheet(f"color: {theme.PALETTE.get(accent, accent)};")
        layout.addWidget(self.value)

        self.foot = QLabel(foot)
        self.foot.setObjectName("StatFoot")
        self.foot.setWordWrap(True)
        layout.addWidget(self.foot)

    def set_value(self, value: str, foot: str | None = None) -> None:
        self.value.setText(value)
        if foot is not None:
            self.foot.setText(foot)


class Badge(QLabel):
    """Small coloured pill."""

    def __init__(self, text: str = "", kind: str = "muted", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignCenter)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        colors = {
            "good": "good",
            "bad": "bad",
            "warn": "warn",
            "info": "blue",
            "accent": "accent",
            "muted": "text_dim",
        }
        color = colors.get(kind, colors["muted"])
        self.setStyleSheet(
            f"color: {theme.PALETTE[color]}; background: {theme.rgba(color, 0.16)};"
            "border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 700;"
        )

    @staticmethod
    def for_status(status: str) -> "Badge":
        text = (status or "unknown").strip().lower()
        mapping = {
            "success": ("成功", "good"),
            "failed": ("失败", "bad"),
            "failure": ("失败", "bad"),
            "unknown": ("未登记", "muted"),
            "pending": ("等待", "warn"),
        }
        label, kind = mapping.get(text, (text or "—", "muted"))
        return Badge(label, kind)


class MessageBar(QFrame):
    """Inline advisory strip with an icon and a message."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(9)
        self.icon = QLabel()
        self.icon.setFixedSize(18, 18)
        layout.addWidget(self.icon, 0, Qt.AlignTop)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setObjectName("CardHint")
        layout.addWidget(self.text, 1)
        self.set_level("info", "")

    def set_level(self, level: str, message: str) -> None:
        palette = {
            "info": ("info", "blue"),
            "good": ("check", "good"),
            "warn": ("alert", "warn"),
            "error": ("alert", "bad"),
        }
        icon_name, color = palette.get(level, palette["info"])
        self.icon.setPixmap(icons.pixmap(icon_name, theme.PALETTE[color], 18))
        self.text.setText(message)
        self.text.setStyleSheet(f"color: {theme.PALETTE[color]};")
        self.setStyleSheet(
            f"#Card {{ background: {theme.rgba(color, 0.10)};"
            f" border: 1px solid {theme.rgba(color, 0.28)}; border-radius: 10px; }}"
        )
        self.setVisible(bool(message))


class EmptyState(QWidget):
    """Friendly placeholder used when an artefact has not been produced yet."""

    def __init__(self, title: str, hint: str = "", icon_name: str = "layers",
                 parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 28, 16, 28)
        layout.setSpacing(8)
        self._glyph = QLabel()
        self._glyph.setPixmap(icons.pixmap(icon_name, theme.PALETTE["text_faint"], 34))
        self._glyph.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._glyph)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("EmptyTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("EmptyHint")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.hint_label.setVisible(bool(hint))

    def set_text(self, title: str, hint: str = "") -> None:
        self.title_label.setText(title)
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))

    def set_icon(self, name: str) -> None:
        self._glyph.setPixmap(icons.pixmap(name, theme.PALETTE["text_faint"], 34))


def tool_button(text: str, icon_name: str = "", variant: str = "ghost",
                tooltip: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.PointingHandCursor)
    button.setProperty("variant", variant)
    if icon_name:
        button.setIcon(icons.icon(icon_name, theme.PALETTE["text_dim"], 16))
    if tooltip:
        button.setToolTip(tooltip)
    return button


def primary_button(text: str, icon_name: str = "play") -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.PointingHandCursor)
    button.setProperty("variant", "primary")
    button.setIcon(icons.icon(icon_name, "#04211d", 16))
    button.setMinimumHeight(34)
    return button


def danger_button(text: str, icon_name: str = "stop") -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.PointingHandCursor)
    button.setProperty("variant", "danger")
    button.setIcon(icons.icon(icon_name, "#ffe9e9", 16))
    button.setMinimumHeight(34)
    return button


class FormRow(QWidget):
    """One labelled row in a settings form."""

    def __init__(self, label: str, widget: QWidget, hint: str = "",
                 label_width: int = 168, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.label = QLabel(label)
        self.label.setObjectName("CardHint")
        self.label.setFixedWidth(label_width)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.label, 0)
        layout.addWidget(widget, 1)
        self.hint = QLabel(hint)
        self.hint.setObjectName("CardHint")
        self.hint.setFixedWidth(150)
        layout.addWidget(self.hint, 0)
        self.widget = widget
