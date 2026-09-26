"""Dependency-free chart widgets drawn with :class:`QPainter`.

Two widgets cover every figure in the GUI:

* :class:`XYChart` - scatter / line / staircase series, an optional second
  y-axis, and an optional diagonal reference (used for parity plots).
* :class:`CategoryBarChart` - horizontal bars for count distributions.

Both offer hover tooltips, wheel zoom (double-click to reset) and emit
``pointClicked`` so a chart point can drive the rest of the interface.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def fnt(size: float, bold: bool = False) -> QFont:
    font = QFont()
    font.setPixelSize(int(size))
    font.setWeight(QFont.DemiBold if bold else QFont.Normal)
    return font


def nice_ticks(lo: float, hi: float, target: int = 5) -> tuple[list[float], float]:
    if not math.isfinite(lo) or not math.isfinite(hi):
        return [0.0, 1.0], 1.0
    if hi <= lo:
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(1, target)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    step = magnitude
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = mult * magnitude
        if raw <= step:
            break
    first = math.floor(lo / step + 1e-9)
    last = math.ceil(hi / step - 1e-9)
    if last - first > 40:  # guard against pathological spans
        last = first + 40
    return [index * step for index in range(first, last + 1)], step


def fmt_tick(value: float, step: float) -> str:
    if step <= 0 or not math.isfinite(step):
        return f"{value:g}"
    decimals = int(max(0, -math.floor(math.log10(step) + 1e-9)))
    decimals = min(decimals, 6)
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def fmt_value(value: float, digits: int = 5) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    if value != 0 and (abs(value) < 1e-3 or abs(value) >= 1e6):
        return f"{value:.{digits - 1}e}"
    return f"{value:.{digits}g}"


def padded(lo: float, hi: float, fraction: float = 0.08) -> tuple[float, float]:
    if not math.isfinite(lo) or not math.isfinite(hi):
        return 0.0, 1.0
    if hi <= lo:
        pad = abs(lo) * 0.05 or 0.5
        return lo - pad, hi + pad
    pad = (hi - lo) * fraction
    return lo - pad, hi + pad


@dataclass
class Series:
    """One data series."""

    name: str
    color: QColor
    xs: Sequence[float] = field(default_factory=list)
    ys: Sequence[float] = field(default_factory=list)
    labels: Sequence[str] = field(default_factory=list)
    kind: str = "line"  # line | scatter | step
    axis: str = "left"  # left | right
    marker: float = 4.0
    width: float = 2.0
    fill: bool = False
    dashed: bool = False
    ring: bool = False
    z: float = 0.0

    def points(self) -> list[tuple[float, float, int]]:
        out: list[tuple[float, float, int]] = []
        for index, (x, y) in enumerate(zip(self.xs, self.ys)):
            try:
                fx, fy = float(x), float(y)
            except (TypeError, ValueError):
                continue
            if math.isfinite(fx) and math.isfinite(fy):
                out.append((fx, fy, index))
        return out


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------
class _ChartBase(QWidget):
    pointClicked = Signal(object)  # Series or None; index via attribute

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)
        self._hover: tuple[int, int] | None = None  # (series index, point index)
        self._empty_text = "暂无数据"

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self.update()

    # ------------------------------------------------------------- drawing
    def _fonts(self) -> None:
        self._f_tick = fnt(11)
        self._f_axis = fnt(11, bold=True)
        self._f_legend = fnt(11)
        self._f_tip = fnt(11)
        self._f_empty = fnt(12)

    def _draw_empty(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(theme.color("text_faint")))
        painter.setFont(self._f_empty)
        painter.drawText(rect, Qt.AlignCenter, self._empty_text)

    def _legend_plan(self, series: Iterable[Series], width: float, metrics: QFontMetricsF):
        """Greedy wrap of legend entries; returns ``(placements, row_count)``."""
        placements: list[tuple[Series, float, int]] = []
        x = 0.0
        row = 0
        for item in series:
            entry_width = metrics.horizontalAdvance(item.name) + 34
            if x > 0 and x + entry_width > width:
                row += 1
                x = 0.0
            placements.append((item, x, row))
            x += entry_width
        return placements, row + 1

    def _draw_legend(self, painter: QPainter, origin: QPointF, width: float,
                     series: Iterable[Series]) -> float:
        painter.setFont(self._f_legend)
        metrics = QFontMetricsF(self._f_legend)
        placements, rows = self._legend_plan(series, width, metrics)
        for item, offset, row in placements:
            y = origin.y() + row * 17
            painter.setPen(QPen(item.color, 2.4))
            painter.drawLine(QPointF(origin.x() + offset, y + 6), QPointF(origin.x() + offset + 14, y + 6))
            if item.kind == "scatter":
                painter.setBrush(QBrush(item.color))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(QPointF(origin.x() + offset + 7, y + 6), 3.4, 3.4)
            painter.setPen(QPen(theme.color("text_dim")))
            painter.drawText(QPointF(origin.x() + offset + 19, y + 10), item.name)
        return rows * 17

    def _draw_tooltip(self, painter: QPainter, lines: list[str], anchor: QPointF) -> None:
        if not lines:
            return
        painter.setFont(self._f_tip)
        metrics = QFontMetricsF(self._f_tip)
        width = max(metrics.horizontalAdvance(line) for line in lines) + 20
        height = len(lines) * 16 + 12
        x = anchor.x() + 14
        y = anchor.y() - height - 8
        if x + width > self.width() - 4:
            x = anchor.x() - width - 14
        if x < 4:
            x = 4
        if y < 4:
            y = anchor.y() + 14
        if y + height > self.height() - 4:
            y = self.height() - height - 4
        rect = QRectF(x, y, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(20, 28, 36, 242)))
        painter.drawRoundedRect(rect, 7, 7)
        painter.setPen(QPen(theme.color("border"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, 7, 7)
        for index, line in enumerate(lines):
            color = theme.color("text") if index == 0 else theme.color("text_dim")
            painter.setPen(QPen(color))
            painter.drawText(QPointF(rect.left() + 10, rect.top() + 15 + index * 16), line)

    def _pick(self, pos: QPointF, series: list[Series], to_px) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        best_distance = 22.0
        for series_index, item in enumerate(series):
            if item.kind == "line" and len(item.xs) > 60:
                continue  # too dense to hover meaningfully
            for x, y, point_index in item.points():
                px, py = to_px(x, y, series_index)
                distance = math.hypot(px - pos.x(), py - pos.y())
                if distance < best_distance:
                    best_distance = distance
                    best = (series_index, point_index)
        return best


# --------------------------------------------------------------------------
# XY chart
# --------------------------------------------------------------------------
class XYChart(_ChartBase):
    """Scatter / line / staircase chart with an optional right y-axis."""

    def __init__(self, parent: QWidget | None = None, *, diagonal: bool = False, square: bool = False):
        super().__init__(parent)
        self._series: list[Series] = []
        self.x_label = ""
        self.y_label = ""
        self.y2_label = ""
        self.diagonal = diagonal
        self.square = square
        self._xlim: tuple[float, float] | None = None
        self._ylim: tuple[float, float] | None = None
        self._y2lim: tuple[float, float] | None = None

    # ------------------------------------------------------------ data API
    def set_series(self, series: list[Series]) -> None:
        self._series = list(series)
        self._hover = None
        self.update()

    def set_axis_labels(self, x: str = "", y: str = "", y2: str = "") -> None:
        self.x_label, self.y_label, self.y2_label = x, y, y2
        self.update()

    def reset_view(self) -> None:
        self._xlim = self._ylim = self._y2lim = None
        self.update()

    # -------------------------------------------------------------- scales
    def _auto_limits(self) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], bool]:
        xs: list[float] = []
        left: list[float] = []
        right: list[float] = []
        has_right = False
        for item in self._series:
            for x, y, _ in item.points():
                xs.append(x)
                if item.axis == "right":
                    right.append(y)
                    has_right = True
                else:
                    left.append(y)
        if self.diagonal:
            # A parity plot needs identical x/y limits or the identity line is a lie.
            both = left + right + xs
            if both:
                lo, hi = padded(min(both), max(both), 0.06)
                if self.square:
                    lo, hi = min(lo, 0.0), max(hi, 0.0)
                return (lo, hi), (lo, hi), (lo, hi), has_right
            return (0.0, 1.0), (0.0, 1.0), (0.0, 1.0), has_right
        x_lo, x_hi = padded(min(xs), max(xs), 0.06) if xs else (0.0, 1.0)
        y_lo, y_hi = padded(min(left), max(left), 0.1) if left else (0.0, 1.0)
        if has_right:
            r_lo, r_hi = padded(min(right), max(right), 0.1)
        else:
            r_lo, r_hi = 0.0, 1.0
        return (x_lo, x_hi), (y_lo, y_hi), (r_lo, r_hi), has_right

    # ------------------------------------------------------------- drawing
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        self._fonts()
        rect = QRectF(self.rect())
        if not any(item.points() for item in self._series):
            self._draw_empty(painter, rect)
            painter.end()
            return

        (ax_lo, ax_hi), (ay_lo, ay_hi), (ay2_lo, ay2_hi), has_right = self._auto_limits()
        x_lo, x_hi = self._xlim or (ax_lo, ax_hi)
        y_lo, y_hi = self._ylim or (ay_lo, ay_hi)
        y2_lo, y2_hi = self._y2lim or (ay2_lo, ay2_hi)

        x_ticks, x_step = nice_ticks(x_lo, x_hi)
        y_ticks, y_step = nice_ticks(y_lo, y_hi)
        y2_ticks, y2_step = nice_ticks(y2_lo, y2_hi) if has_right else ([], 0.0)

        tick_metrics = QFontMetricsF(self._f_tick)
        left_width = max((tick_metrics.horizontalAdvance(fmt_tick(v, y_step)) for v in y_ticks), default=30)
        right_width = max((tick_metrics.horizontalAdvance(fmt_tick(v, y2_step)) for v in y2_ticks), default=0)
        axis_font = self._f_axis

        left = rect.left() + 12 + left_width + 8
        right = rect.right() - 12 - (right_width + 8 if has_right else 0) - (14 if self.y_label else 0)
        available = max(20.0, right - left)

        legend_series = [item for item in self._series if item.points()]
        legend_height = 0.0
        if len(legend_series) > 1:
            legend_metrics = QFontMetricsF(self._f_legend)
            _, legend_rows = self._legend_plan(legend_series, available, legend_metrics)
            legend_height = legend_rows * 17 + 6

        top = rect.top() + 6 + legend_height
        bottom = rect.bottom() - 30 - (14 if self.x_label else 0)
        plot = QRectF(left, top, available, max(10.0, bottom - top))
        self._plot = plot

        def to_px(x: float, y: float, series_index: int = 0) -> tuple[float, float]:
            item = self._series[series_index] if 0 <= series_index < len(self._series) else None
            lo, hi = (y2_lo, y2_hi) if item is not None and item.axis == "right" else (y_lo, y_hi)
            px = plot.left() + (x - x_lo) / (x_hi - x_lo) * plot.width() if x_hi > x_lo else plot.left()
            py = plot.bottom() - (y - lo) / (hi - lo) * plot.height() if hi > lo else plot.bottom()
            return px, py

        self._to_px = to_px

        # ---- grid
        painter.setFont(self._f_tick)
        for value in y_ticks:
            if value < y_lo - 1e-12 or value > y_hi + 1e-12:
                continue
            py = to_px(x_lo, value)[1]
            painter.setPen(QPen(theme.color("grid"), 1))
            painter.drawLine(QPointF(plot.left(), py), QPointF(plot.right(), py))
            painter.setPen(QPen(theme.color("text_faint")))
            painter.drawText(QRectF(plot.left() - left_width - 8, py - 8, left_width, 16),
                             Qt.AlignRight | Qt.AlignVCenter, fmt_tick(value, y_step))
        if has_right:
            for value in y2_ticks:
                if value < y2_lo - 1e-12 or value > y2_hi + 1e-12:
                    continue
                py = plot.bottom() - (value - y2_lo) / (y2_hi - y2_lo) * plot.height() if y2_hi > y2_lo else plot.bottom()
                painter.setPen(QPen(theme.color("text_faint")))
                painter.drawText(QRectF(plot.right() + 8, py - 8, right_width + 4, 16),
                                 Qt.AlignLeft | Qt.AlignVCenter, fmt_tick(value, y2_step))
        for value in x_ticks:
            if value < x_lo - 1e-12 or value > x_hi + 1e-12:
                continue
            px = to_px(value, y_lo)[0]
            painter.setPen(QPen(theme.color("grid"), 1))
            painter.drawLine(QPointF(px, plot.top()), QPointF(px, plot.bottom()))
            painter.setPen(QPen(theme.color("text_faint")))
            painter.drawText(QRectF(px - 45, plot.bottom() + 5, 90, 16), Qt.AlignCenter, fmt_tick(value, x_step))

        # ---- axes frame
        painter.setPen(QPen(theme.color("axis"), 1))
        painter.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
        if has_right:
            painter.drawLine(QPointF(plot.right(), plot.top()), QPointF(plot.right(), plot.bottom()))

        # ---- diagonal reference
        if self.diagonal:
            painter.setPen(QPen(theme.color("axis"), 1.2, Qt.DashLine))
            painter.drawLine(QPointF(*to_px(y_lo, y_lo)), QPointF(*to_px(y_hi, y_hi)))

        # ---- series
        painter.setClipRect(plot.adjusted(-1, -1, 1, 1))
        order = sorted(range(len(self._series)), key=lambda i: self._series[i].z)
        for series_index in order:
            item = self._series[series_index]
            points = item.points()
            if not points:
                continue
            color = QColor(item.color)
            if item.fill and item.kind in {"line", "step"} and len(points) > 1:
                path = QPainterPath()
                first = to_px(points[0][0], points[0][1], series_index)
                path.moveTo(first[0], plot.bottom())
                for x, y, _ in points:
                    px, py = to_px(x, y, series_index)
                    path.lineTo(px, py)
                path.lineTo(to_px(points[-1][0], points[-1][1], series_index)[0], plot.bottom())
                path.closeSubpath()
                gradient = QLinearGradient(0, plot.top(), 0, plot.bottom())
                top_color = QColor(color)
                top_color.setAlpha(70)
                bottom_color = QColor(color)
                bottom_color.setAlpha(0)
                gradient.setColorAt(0.0, top_color)
                gradient.setColorAt(1.0, bottom_color)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(gradient))
                painter.drawPath(path)
                painter.setBrush(Qt.NoBrush)

            if item.kind in {"line", "step"}:
                pen = QPen(color, item.width)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                if item.dashed:
                    pen.setStyle(Qt.DashLine)
                painter.setPen(pen)
                path = QPainterPath()
                previous: tuple[float, float] | None = None
                for x, y, _ in points:
                    px, py = to_px(x, y, series_index)
                    if previous is None:
                        path.moveTo(px, py)
                    elif item.kind == "step":
                        path.lineTo(px, previous[1])
                        path.lineTo(px, py)
                    else:
                        path.lineTo(px, py)
                    previous = (px, py)
                painter.drawPath(path)

            if item.kind == "scatter" or item.marker > 0:
                painter.setPen(Qt.NoPen)
                fill_color = QColor(color)
                fill_color.setAlpha(200 if item.kind != "scatter" else 170)
                painter.setBrush(QBrush(fill_color))
                for x, y, _ in points:
                    px, py = to_px(x, y, series_index)
                    radius = item.marker
                    if item.ring:
                        painter.setBrush(Qt.NoBrush)
                        painter.setPen(QPen(color, 1.6))
                        painter.drawEllipse(QPointF(px, py), radius + 1.6, radius + 1.6)
                        painter.setBrush(QBrush(fill_color))
                        painter.setPen(Qt.NoPen)
                    painter.drawEllipse(QPointF(px, py), radius, radius)
        painter.setClipping(False)

        # ---- axis titles
        painter.setPen(QPen(theme.color("text_dim")))
        painter.setFont(axis_font)
        if self.x_label:
            painter.drawText(QRectF(plot.left(), rect.bottom() - 20, plot.width(), 16), Qt.AlignCenter, self.x_label)
        if self.y_label:
            painter.save()
            painter.translate(rect.left() + 12, plot.center().y())
            painter.rotate(-90)
            painter.drawText(QRectF(-plot.height() / 2, -8, plot.height(), 16), Qt.AlignCenter, self.y_label)
            painter.restore()
        if self.y2_label and has_right:
            painter.save()
            painter.translate(rect.right() - 4, plot.center().y())
            painter.rotate(90)
            painter.drawText(QRectF(-plot.height() / 2, -8, plot.height(), 16), Qt.AlignCenter, self.y2_label)
            painter.restore()

        # ---- legend + hover
        if len(legend_series) > 1:
            self._draw_legend(painter, QPointF(plot.left(), rect.top() + 4), plot.width(), legend_series)

        if self._hover:
            series_index, point_index = self._hover
            item = self._series[series_index]
            points = item.points()
            match = next((p for p in points if p[2] == point_index), None)
            if match is not None:
                px, py = to_px(match[0], match[1], series_index)
                painter.setPen(QPen(theme.color("text_faint"), 1, Qt.DotLine))
                painter.drawLine(QPointF(px, plot.top()), QPointF(px, plot.bottom()))
                painter.drawLine(QPointF(plot.left(), py), QPointF(plot.right(), py))
                painter.setPen(QPen(item.color, 1.8))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(px, py), item.marker + 3.5, item.marker + 3.5)
                label = item.labels[point_index] if point_index < len(item.labels) else ""
                lines = [label or item.name,
                         f"{self.x_label or 'x'} = {fmt_value(match[0])}",
                         f"{self.y_label or 'y'} = {fmt_value(match[1])}"]
                self._draw_tooltip(painter, lines, QPointF(px, py))
        painter.end()

    # -------------------------------------------------------------- events
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not hasattr(self, "_to_px"):
            return
        found = self._pick(event.position(), self._series, self._to_px)
        if found != self._hover:
            self._hover = found
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover is not None:
            self._hover = None
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._hover and event.button() == Qt.LeftButton:
            series_index, point_index = self._hover
            item = self._series[series_index]
            label = item.labels[point_index] if point_index < len(item.labels) else ""
            self.pointClicked.emit({"series": item.name, "label": label, "index": point_index})

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not hasattr(self, "_plot") or not any(item.points() for item in self._series):
            return
        (ax_lo, ax_hi), (ay_lo, ay_hi), (ay2_lo, ay2_hi), has_right = self._auto_limits()
        x_lo, x_hi = self._xlim or (ax_lo, ax_hi)
        y_lo, y_hi = self._ylim or (ay_lo, ay_hi)
        y2_lo, y2_hi = self._y2lim or (ay2_lo, ay2_hi)

        factor = 0.85 if event.angleDelta().y() > 0 else 1.18
        pos = event.position()
        plot = self._plot
        rx = min(max((pos.x() - plot.left()) / max(1.0, plot.width()), 0.0), 1.0)
        ry = min(max((plot.bottom() - pos.y()) / max(1.0, plot.height()), 0.0), 1.0)

        def zoom(lo: float, hi: float, ratio: float) -> tuple[float, float]:
            anchor = lo + (hi - lo) * ratio
            return anchor - (anchor - lo) * factor, anchor + (hi - anchor) * factor

        if self.diagonal:
            # keep x and y identical so the identity line stays at 45 degrees
            self._xlim = zoom(x_lo, x_hi, rx)
            self._ylim = zoom(y_lo, y_hi, rx)
        else:
            self._xlim = zoom(x_lo, x_hi, rx)
            self._ylim = zoom(y_lo, y_hi, ry)
        if has_right:
            self._y2lim = (y2_lo, y2_hi)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.reset_view()


# --------------------------------------------------------------------------
# category bars
# --------------------------------------------------------------------------
class CategoryBarChart(_ChartBase):
    """Horizontal bar chart for count distributions."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[tuple[str, float, QColor]] = []
        self._value_suffix = ""
        self.setMinimumHeight(150)

    def set_items(self, items: Sequence[tuple[str, float]], colors: Sequence[QColor] | None = None) -> None:
        palette = list(colors) if colors else theme.series_colors(len(items))
        self._items = [
            (str(name), float(value), palette[index % len(palette)])
            for index, (name, value) in enumerate(items)
        ]
        self.update()

    def set_value_suffix(self, suffix: str) -> None:
        self._value_suffix = suffix
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._fonts()
        rect = QRectF(self.rect())
        if not self._items:
            self._draw_empty(painter, rect)
            painter.end()
            return

        metrics = QFontMetricsF(self._f_tick)
        label_width = min(
            max(metrics.horizontalAdvance(name) for name, _, _ in self._items) + 12,
            max(90.0, rect.width() * 0.42),
        )
        value_width = max(metrics.horizontalAdvance(f"{value:g}") for _, value, _ in self._items) + 14
        top, bottom = rect.top() + 6, rect.bottom() - 6
        left = rect.left() + 6 + label_width
        right = rect.right() - 6 - value_width
        span = max(1.0, right - left)
        values = [value for _, value, _ in self._items]
        signed = any(value < 0 for value in values)
        if signed:
            low = min(0.0, min(values))
            high = max(0.0, max(values))
            if high == low:
                high = low + 1.0
            zero_x = left + span * (0.0 - low) / (high - low)

            def position(value: float) -> float:
                return left + span * (value - low) / (high - low)
        else:
            peak = max(values) or 1.0

            def position(value: float) -> float:
                return left + span * (value / peak)

            zero_x = left
        rows = len(self._items)
        slot = (bottom - top) / rows
        bar_height = min(22.0, max(8.0, slot - 8))

        if signed:
            painter.setPen(QPen(theme.color("grid_strong"), 1))
            painter.drawLine(QPointF(zero_x, top), QPointF(zero_x, bottom))

        for index, (name, value, color) in enumerate(self._items):
            center = top + slot * index + slot / 2
            painter.setPen(QPen(theme.color("text_dim")))
            painter.setFont(self._f_tick)
            text = metrics.elidedText(name, Qt.ElideRight, int(label_width - 8))
            painter.drawText(QRectF(rect.left() + 4, center - 9, label_width - 8, 18),
                             Qt.AlignRight | Qt.AlignVCenter, text)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(theme.color("grid")))
            painter.drawRoundedRect(QRectF(left, center - bar_height / 2, span, bar_height), 4, 4)

            end = position(value)
            bar_left, bar_right = (zero_x, end) if end >= zero_x else (end, zero_x)
            width = bar_right - bar_left
            if width > 0.5:
                gradient = QLinearGradient(bar_left, 0, bar_right, 0)
                gradient.setColorAt(0.0, QColor(color).darker(135))
                gradient.setColorAt(1.0, color)
                painter.setBrush(QBrush(gradient))
                painter.drawRoundedRect(QRectF(bar_left, center - bar_height / 2, max(width, 3), bar_height), 4, 4)
            painter.setPen(QPen(theme.color("text")))
            painter.drawText(QRectF(right + 6, center - 9, value_width, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, f"{value:g}{self._value_suffix}")
        painter.end()
