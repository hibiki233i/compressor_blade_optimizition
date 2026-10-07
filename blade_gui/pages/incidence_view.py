"""Read-only viewer for incidence-validation outputs.

Everything shown is read back from files written by
``blade_shape_incidence_validation.py`` through :mod:`blade_gui.validation_data`.
The view never recomputes incidence; it plots the CLI's bands, overlays a
second result only when the CLI's comparability rules allow it, and keeps the
CLI's disclaimers (no minimum-loss calibration, no numerical acceptance).
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..charts import Series, XYChart
from ..project import fmt
from ..validation_data import (
    ROLE_LABELS,
    ComparisonResult,
    FailedResult,
    PlanResult,
    ProfileResult,
    SpecResult,
    SweepResult,
    comparable,
    flow_difference,
    incidence_delta,
    load_profile,
    load_result,
)
from ..widgets import Badge, Card, ChartCard, EmptyState, MessageBar, StatTile, clear_layout, tool_button

DISCLAIMER = ("流动诊断，不是最小损失攻角标定；quality_ok 不验证残差、网格独立性或工况一致性。"
              "叠加仅用于观察形状差异，是否同工况须以「基准 / 目标工况对比」结果为准。")


def _deg(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}°"


def _pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    # numbers and span ranges must not be elided; spare width goes to the last column
    header.setSectionResizeMode(QHeaderView.ResizeToContents)
    header.setStretchLastSection(True)
    table.setMinimumHeight(220)
    return table


def _cell(text: str, *, align_right: bool = True, color: str | None = None,
          background: QColor | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if align_right:
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if color:
        item.setForeground(theme.color(color))
    if background is not None:
        item.setBackground(background)
    return item


def _incidence_tint(value: float | None, scale: float) -> QColor | None:
    """Diverging cell tint: amber when β_b > β_f (positive i), blue when negative."""
    if value is None or scale <= 0:
        return None
    base = theme.color("amber" if value > 0 else "blue")
    base.setAlphaF(min(0.42, 0.06 + 0.36 * abs(value) / scale))
    return base


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setWidget(widget)
    return area


# --------------------------------------------------------------------------
# panels
# --------------------------------------------------------------------------
class ProfilePanel(QWidget):
    """Spanwise metal/flow angle, incidence, reverse flow and band table."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 6, 0)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel("—")
        self.title.setStyleSheet("font-size: 15px; font-weight: 700;")
        head.addWidget(self.title)
        self.method_badge = Badge("—", "muted")
        head.addWidget(self.method_badge)
        self.quality_badge = Badge("—", "muted")
        head.addWidget(self.quality_badge)
        self.overlay_badge = Badge("", "info")
        head.addWidget(self.overlay_badge)
        head.addStretch(1)
        root.addLayout(head)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tile_rms = StatTile("RMS 攻角", "—", "正向质量加权", "accent", compact=True)
        self.tile_mean = StatTile("平均 |i|", "—", "", "blue", compact=True)
        self.tile_max = StatTile("最大 |i|", "—", "", "amber", compact=True)
        self.tile_flow = StatTile("整轮净流量", "—", "kg/s", "violet", compact=True)
        self.tile_reverse = StatTile("逆流 / 闭合", "—", "", "good", compact=True)
        for tile in (self.tile_rms, self.tile_mean, self.tile_max, self.tile_flow, self.tile_reverse):
            tiles.addWidget(tile, 1)
        root.addLayout(tiles)

        self.message = MessageBar()
        root.addWidget(self.message)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.angle_card = ChartCard("叶片角与来流角 · 展向分布", "β_b 线性插值 · β_f 正向质量平均",
                                    XYChart(), empty_title="没有可用的角度带")
        self.angle_card.chart.set_axis_labels("叶高 span（0 = hub，1 = shroud）", "角度 (°)")
        self.incidence_card = ChartCard("几何攻角 i = β_b − β_f", "虚线为 ±RMS 参考",
                                        XYChart(), empty_title="没有可用的攻角带")
        self.incidence_card.chart.set_axis_labels("叶高 span", "攻角 i (°)")
        self.flow_card = ChartCard("流量分配与逆流占比", "左轴：正向流量份额 · 右轴：逆流占比",
                                   XYChart(), empty_title="旧指标没有流量带")
        self.flow_card.chart.set_axis_labels("叶高 span", "正向流量份额 (%)", "逆流占比 (%)")
        self.extra_card = ChartCard("相对速度分量", "W_s 流向 · W_θ 周向（m/s）",
                                    XYChart(), empty_title="没有速度分量")
        self.extra_card.chart.set_axis_labels("叶高 span", "速度 (m/s)")
        for index, card in enumerate((self.angle_card, self.incidence_card, self.flow_card, self.extra_card)):
            card.setMinimumHeight(300)
            grid.addWidget(card, index // 2, index % 2)
        root.addLayout(grid)

        table_card = Card("分带明细", "攻角列底色：琥珀 = β_b > β_f（正攻角），蓝 = 负攻角，深浅按 |i|")
        self.table = _table(["带", "span 范围", "s_mass", "β_b (°)", "β_f (°)", "i (°)",
                             "正向 kg/s", "逆向 kg/s", "逆流占比"])
        table_card.body.addWidget(self.table)
        root.addWidget(table_card)
        root.addStretch(1)

    # --------------------------------------------------------------- show
    def show_profile(self, result: ProfileResult, baseline: ProfileResult | None = None) -> str:
        """Render ``result``; returns a note when the overlay was refused."""
        note = ""
        if baseline is not None:
            ok, reason = comparable(result, baseline)
            if not ok:
                note, baseline = reason, None

        self.title.setText(result.label)
        self.method_badge.setText("旧指标 · 20 点算术" if result.is_legacy else "质量加权速度三角形")
        self.method_badge.set_kind("warn" if result.is_legacy else "accent")
        quality = result.quality_ok
        if result.is_legacy:
            self.quality_badge.setText("无质量门（旧指标）")
            self.quality_badge.set_kind("muted")
        else:
            self.quality_badge.setText("质量检查通过" if quality else "质量检查未通过")
            self.quality_badge.set_kind("good" if quality else "bad")
        self.overlay_badge.setVisible(baseline is not None)
        if baseline is not None:
            self.overlay_badge.setText(f"叠加基准：{baseline.label}")

        self._fill_tiles(result, baseline)
        self._fill_message(result, baseline, note)
        self._fill_angles(result, baseline)
        self._fill_incidence(result, baseline)
        self._fill_flow(result, baseline)
        self._fill_extra(result, baseline)
        self._fill_table(result)
        return note

    def _fill_tiles(self, result: ProfileResult, baseline: ProfileResult | None) -> None:
        def delta(current: float | None, other: float | None) -> str:
            if baseline is None or current is None or other is None:
                return ""
            return f"相对基准 {current - other:+.3f}°"

        if result.is_legacy:
            self.tile_rms.set_value("—", "旧指标不计算 RMS")
            self.tile_mean.set_value(_deg(result.mean_abs), "20 点算术平均 " + delta(result.mean_abs, baseline and baseline.mean_abs))
            self.tile_max.set_value(_deg(result.max_abs), "由 profile.csv 读取")
            self.tile_flow.set_value("—", "旧指标无流量")
            self.tile_reverse.set_value("—", "")
            return
        self.tile_rms.set_value(_deg(result.rms), "正向质量加权 " + delta(result.rms, baseline and baseline.rms))
        self.tile_mean.set_value(_deg(result.mean_abs), "质量加权 " + delta(result.mean_abs, baseline and baseline.mean_abs))
        self.tile_max.set_value(_deg(result.max_abs), "所有可用带 " + delta(result.max_abs, baseline and baseline.max_abs))
        net = result.summary.get("net_mass_flow_kg_s")
        flow_foot = "kg/s · 已按叶片数换算"
        if baseline is not None:
            relative = flow_difference(result, baseline)
            flow_foot = f"与基准相差 {_pct(relative)}" if relative is not None else flow_foot
        self.tile_flow.set_value(fmt(net, 5), flow_foot)
        closure = result.summary.get("mass_flow_closure_relative")
        reverse = result.total_reverse_fraction()
        self.tile_reverse.set_value(_pct(reverse), f"质量闭合误差 {_pct(closure if isinstance(closure, (int, float)) else None)}")
        threshold = result.measurement.get("max_reverse_fraction")
        bad = reverse is not None and isinstance(threshold, (int, float)) and reverse > threshold
        self.tile_reverse.set_accent("bad" if bad else "good")

    def _fill_message(self, result: ProfileResult, baseline: ProfileResult | None, note: str) -> None:
        issues = result.quality_issues
        if note:
            self.message.set_level("warn", f"未叠加基准：{note}。")
        elif issues:
            shown = "；".join(issues[:6]) + ("；…" if len(issues) > 6 else "")
            self.message.set_level("error", f"质量问题（整体攻角指标留空）：{shown}")
        elif result.is_legacy:
            self.message.set_level("info", "旧指标复现仅限 ACA [-90°, 0°] 象限，与质量加权指标不可混比。" + DISCLAIMER)
        else:
            measurement = result.measurement
            self.message.set_level(
                "info",
                f"截面 Blade Aligned {measurement.get('le_station', '?')} · {measurement.get('bands', '?')} 等宽带 · "
                f"normal_sign {measurement.get('normal_sign', '?')} · θ 参考 {measurement.get('theta_reference_sign', '?')}。"
                + DISCLAIMER,
            )

    def _fill_angles(self, result: ProfileResult, baseline: ProfileResult | None) -> None:
        chart: XYChart = self.angle_card.chart
        span = result.span()
        series: list[Series] = []
        endpoints = result.metal_endpoints()
        if endpoints is not None:
            series.append(Series("叶片角 β_b（线性）", theme.color("accent"), [0.0, 1.0], list(endpoints),
                                 kind="line", marker=0.0, width=2.2, z=1))
        series.append(Series("来流角 β_f", theme.color("blue"), span, result.column("flow_angle_deg"),
                             _band_labels(result), kind="line", marker=4.2, width=2.0, z=2))
        if baseline is not None:
            series.append(Series(f"基准 β_f · {baseline.label}", theme.color("violet"), baseline.span(),
                                 baseline.column("flow_angle_deg"), _band_labels(baseline),
                                 kind="line", marker=3.0, width=1.6, dashed=True, z=0))
        chart.set_series(series)
        self.angle_card.update_data(any(value is not None for value in result.column("flow_angle_deg")))

    def _fill_incidence(self, result: ProfileResult, baseline: ProfileResult | None) -> None:
        chart: XYChart = self.incidence_card.chart
        incidence = result.column("incidence_deg")
        has = any(value is not None for value in incidence)
        series = [Series("零攻角", theme.color("text_faint"), [0.0, 1.0], [0.0, 0.0],
                         kind="line", marker=0.0, width=1.2, dashed=True, z=-2, legend=False)]
        rms = result.rms
        if rms:
            for sign in (1, -1):
                series.append(Series("±RMS", theme.color("axis"), [0.0, 1.0], [sign * rms, sign * rms],
                                     kind="line", marker=0.0, width=1.0, dashed=True, z=-1,
                                     legend=sign > 0))
        series.append(Series(f"i · {result.label}", theme.color("amber"), result.span(), incidence,
                             _band_labels(result), kind="line", marker=4.4, width=2.2, fill=False, z=2))
        if baseline is not None:
            series.append(Series(f"基准 i · {baseline.label}", theme.color("violet"), baseline.span(),
                                 baseline.column("incidence_deg"), _band_labels(baseline),
                                 kind="line", marker=3.0, width=1.6, dashed=True, z=1))
        chart.set_series(series)
        self.incidence_card.set_hint("虚线为 ±RMS 参考" if rms else "整体 RMS 未给出（质量检查未通过或旧指标）")
        self.incidence_card.update_data(has)

    def _fill_flow(self, result: ProfileResult, baseline: ProfileResult | None) -> None:
        chart: XYChart = self.flow_card.chart
        if result.is_legacy:
            chart.set_series([])
            self.flow_card.update_data(False)
            return
        centres = result.band_centres()
        share = [None if value is None else value * 100 for value in result.forward_share()]
        reverse = [None if value is None else value * 100 for value in result.reverse_fraction()]
        series = [
            Series("正向流量份额", theme.color("accent"), centres, share, _band_labels(result),
                   kind="step", marker=0.0, width=2.0, fill=True, z=0),
            Series("逆流占比", theme.color("bad"), centres, reverse, _band_labels(result),
                   kind="line", marker=3.6, width=1.8, axis="right", z=2),
        ]
        threshold = result.measurement.get("max_reverse_fraction")
        if isinstance(threshold, (int, float)):
            series.append(Series("逆流阈值", theme.color("bad"), [0.0, 1.0], [threshold * 100] * 2,
                                 kind="line", marker=0.0, width=1.0, dashed=True, axis="right", z=1))
        chart.set_series(series)
        self.flow_card.update_data(any(value is not None for value in share))

    def _fill_extra(self, result: ProfileResult, baseline: ProfileResult | None) -> None:
        chart: XYChart = self.extra_card.chart
        delta = incidence_delta(result, baseline) if baseline is not None else []
        if delta:
            self.extra_card.set_title("Δi（目标 − 基准）")
            self.extra_card.set_hint("逐带差值；正值 = 目标攻角更大")
            chart.set_axis_labels("叶高 span（带中心）", "Δi (°)")
            xs = [item[0] for item in delta]
            chart.set_series([
                Series("零差值", theme.color("text_faint"), [0.0, 1.0], [0.0, 0.0],
                       kind="line", marker=0.0, width=1.1, dashed=True, z=-1, legend=False),
                Series("Δi", theme.color("pink"), xs, [item[1] for item in delta],
                       kind="line", marker=4.0, width=2.0, z=1),
            ])
            self.extra_card.update_data(True)
            return
        if result.is_legacy:
            self.extra_card.set_title("CFX 原始角 β_cfx")
            self.extra_card.set_hint("Velocity Beta ACA；β_f = 90 − |β_cfx|")
            chart.set_axis_labels("叶高 span", "β_cfx (°)")
            chart.set_series([Series("β_cfx", theme.color("blue"), result.span(), result.column("beta_cfx_deg"),
                                     kind="line", marker=3.6, width=2.0)])
            self.extra_card.update_data(any(v is not None for v in result.column("beta_cfx_deg")))
            return
        self.extra_card.set_title("相对速度分量")
        self.extra_card.set_hint("W_s 流向 · W_θ 周向（m/s，正向质量平均）")
        chart.set_axis_labels("叶高 span", "速度 (m/s)")
        chart.set_series([
            Series("W_s", theme.color("accent"), result.span(), result.column("ws_m_s"),
                   kind="line", marker=3.4, width=2.0),
            Series("W_θ", theme.color("violet"), result.span(), result.column("wt_m_s"),
                   kind="line", marker=3.4, width=2.0),
        ])
        self.extra_card.update_data(any(value is not None for value in result.column("ws_m_s")))

    def _fill_table(self, result: ProfileResult) -> None:
        incidence = result.column("incidence_deg")
        scale = max((abs(value) for value in incidence if value is not None), default=0.0)
        reverse = result.reverse_fraction()
        self.table.setRowCount(len(result.rows))
        for index, row in enumerate(result.rows):
            if result.is_legacy:
                span_text = fmt(row.get("span"), 4)
                weighted = "—"
            else:
                span_text = f"{fmt(row.get('span_low'), 3)} – {fmt(row.get('span_high'), 3)}"
                weighted = fmt(row.get("span_mass"), 4)
            value = incidence[index]
            cells = [
                _cell(str(index), align_right=False, color="text_dim"),
                _cell(span_text, align_right=False),
                _cell(weighted),
                _cell(fmt(row.get("metal_angle_deg"), 5)),
                _cell(fmt(row.get("flow_angle_deg"), 5)),
                _cell("—" if value is None else f"{value:+.3f}",
                      color="text" if value is not None else "text_faint",
                      background=_incidence_tint(value, scale)),
                _cell(fmt(row.get("forward_kg_s"), 5)),
                _cell(fmt(row.get("reverse_kg_s"), 5)),
                _cell(_pct(reverse[index]) if index < len(reverse) else "—"),
            ]
            for column, item in enumerate(cells):
                self.table.setItem(index, column, item)


def _band_labels(result: ProfileResult) -> list[str]:
    if result.is_legacy:
        return [f"{result.label} · 点 {index}" for index in range(len(result.rows))]
    return [f"{result.label} · 带 {index}" for index in range(len(result.rows))]


class SweepPanel(QWidget):
    """Measurement-definition sensitivity on one frozen ``.res``."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 6, 0)
        root.setSpacing(12)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tile_count = StatTile("测量定义", "—", "截面 × 分带", "blue", compact=True)
        self.tile_ok = StatTile("质量通过", "—", "", "good", compact=True)
        self.tile_range = StatTile("RMS 范围", "—", "", "accent", compact=True)
        self.tile_spread = StatTile("RMS 极差", "—", "越小越不依赖测量定义", "amber", compact=True)
        for tile in (self.tile_count, self.tile_ok, self.tile_range, self.tile_spread):
            tiles.addWidget(tile, 1)
        root.addLayout(tiles)
        self.message = MessageBar()
        root.addWidget(self.message)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.rms_card = ChartCard("RMS 攻角 vs 截面位置", "每条线一种分带数", XYChart(),
                                  empty_title="没有质量通过的定义")
        self.rms_card.chart.set_axis_labels("前缘上游截面（Blade Aligned）", "RMS 攻角 (°)")
        self.profile_card = ChartCard("各测量定义的 i(span)", "同一 .res、不同截面/分带", XYChart(),
                                      empty_title="没有可读取的 measurement_* 结果")
        self.profile_card.chart.set_axis_labels("叶高 span", "攻角 i (°)")
        for card in (self.rms_card, self.profile_card):
            card.setMinimumHeight(320)
            row.addWidget(card, 1)
        root.addLayout(row)
        card = Card("sensitivity.csv", "不自动给出截面/分带无关性结论")
        self.table = _table(["截面", "分带", "质量", "RMS (°)", "平均 |i| (°)", "净流量 kg/s"])
        card.body.addWidget(self.table)
        root.addWidget(card)
        root.addStretch(1)

    def show_sweep(self, result: SweepResult) -> None:
        rows = result.rows
        good = [row for row in rows if row.get("quality_ok") is True]
        rms_values = [row.get("incidence_rms_mass_deg") for row in good
                      if isinstance(row.get("incidence_rms_mass_deg"), float)]
        self.tile_count.set_value(str(len(rows)), f"状态 {result.status}")
        self.tile_ok.set_value(f"{len(good)} / {len(rows)}", "")
        self.tile_ok.set_accent("good" if rows and len(good) == len(rows) else "bad")
        if rms_values:
            low, high = min(rms_values), max(rms_values)
            self.tile_range.set_value(f"{low:.2f}–{high:.2f}°", "质量通过的定义")
            self.tile_spread.set_value(f"{high - low:.3f}°", "越小越不依赖测量定义")
        else:
            self.tile_range.set_value("—", "")
            self.tile_spread.set_value("—", "")
        failure = result.progress.get("message")
        if result.status == "failed":
            self.message.set_level("error", f"敏感性提取中途失败（已保留日志，不自动重试）：{failure}")
        else:
            self.message.set_level("info", "截面与分带的 RMS 极差反映测量定义的影响；稳定后再固定定义。"
                                           "不同定义的结果不能用「工况对比」冒充同定义设计对照。")

        by_bands: dict[object, list[tuple[float, float]]] = {}
        for row in good:
            station, rms = row.get("le_station"), row.get("incidence_rms_mass_deg")
            if isinstance(station, float) and isinstance(rms, float):
                by_bands.setdefault(row.get("bands"), []).append((station, rms))
        colors = theme.series_colors(len(by_bands))
        series = []
        for color, (bands, points) in zip(colors, sorted(by_bands.items(), key=lambda item: str(item[0]))):
            points.sort()
            label = f"{int(bands)} 带" if isinstance(bands, float) else f"{bands} 带"
            series.append(Series(label, color, [p[0] for p in points], [p[1] for p in points],
                                 [label] * len(points), kind="line", marker=4.4, width=2.0))
        self.rms_card.chart.set_series(series)
        self.rms_card.update_data(bool(series))

        colors = theme.series_colors(len(result.profiles))
        profile_series = [Series(label, color, profile.span(), profile.column("incidence_deg"),
                                 kind="line", marker=2.6, width=1.6)
                          for color, (label, profile) in zip(colors, result.profiles)]
        self.profile_card.chart.set_series(profile_series)
        self.profile_card.update_data(any(series.points() for series in profile_series))

        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            ok = row.get("quality_ok")
            cells = [
                _cell(fmt(row.get("le_station"), 4), align_right=False),
                _cell(fmt(row.get("bands"), 4)),
                _cell("通过" if ok is True else "未通过", color="good" if ok is True else "bad"),
                _cell(fmt(row.get("incidence_rms_mass_deg"), 5)),
                _cell(fmt(row.get("mean_abs_incidence_mass_deg"), 5)),
                _cell(fmt(row.get("net_mass_flow_kg_s"), 5)),
            ]
            for column, item in enumerate(cells):
                self.table.setItem(index, column, item)


class PlanPanel(QWidget):
    """Budgeted real-CFD endpoint-angle study: progress and local slopes."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 6, 0)
        root.setSpacing(12)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tile_total = StatTile("方案点", "—", "", "blue", compact=True)
        self.tile_done = StatTile("已完成", "—", "", "good", compact=True)
        self.tile_failed = StatTile("失败 / 中断", "—", "须人工核查，不自动重算", "bad", compact=True)
        self.tile_step = StatTile("进口角扰动", "—", "hub / shroud 前缘", "accent", compact=True)
        for tile in (self.tile_total, self.tile_done, self.tile_failed, self.tile_step):
            tiles.addWidget(tile, 1)
        root.addLayout(tiles)
        self.message = MessageBar()
        root.addWidget(self.message)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.eff_card = ChartCard("Efficiency vs 前缘角偏移", "以中心点为 0 · 每条线一个背压", XYChart(),
                                  empty_title="尚无完成的 CFD 点")
        self.eff_card.chart.set_axis_labels("前缘角偏移 Δβ (°)", "Efficiency")
        self.flow_card = ChartCard("MassFlow vs 前缘角偏移", "主程序历史单位，非 SI", XYChart(),
                                   empty_title="尚无完成的 CFD 点")
        self.flow_card.chart.set_axis_labels("前缘角偏移 Δβ (°)", "MassFlow")
        for card in (self.eff_card, self.flow_card):
            card.setMinimumHeight(300)
            row.addWidget(card, 1)
        root.addLayout(row)
        slope_card = Card("局部斜率", "有限差分只描述已计算的点；曲率 < 0 表示局部凸，但不是最小损失角标定")
        self.slopes = _table(["背压 Pa", "端点", "步长 (°)", "dη/dβ (1/°)", "曲率 η", "dṁ/dβ", "格式"])
        self.slopes.setMinimumHeight(150)
        slope_card.body.addWidget(self.slopes)
        root.addWidget(slope_card)
        points_card = Card("方案点状态", "progress.json")
        self.points = _table(["#", "角色", "背压 Pa", "Δβ (°)", "状态", "run_id", "Efficiency", "MassFlow"])
        points_card.body.addWidget(self.points)
        root.addWidget(points_card)
        root.addStretch(1)

    def show_plan(self, result: PlanResult) -> None:
        points = result.points
        done = [point for point in points if point.status == "complete"]
        failed = [point for point in points if point.status in {"failed", "running"}]
        self.tile_total.set_value(str(len(points)), f"计划 {str(result.plan.get('plan_id', ''))[:10]}")
        self.tile_done.set_value(f"{len(done)} / {len(points)}", f"状态 {result.status}")
        self.tile_failed.set_value(str(len(failed)), "须人工核查，不自动重算")
        self.tile_step.set_value(_deg(result.step_deg, 3), "hub / shroud 前缘")
        if result.status == "failed" or failed:
            self.message.set_level("error", "存在失败或中断的点：入口刻意不提供跳过/重算开关，请检查对应 case 后另建方案。")
        elif result.status == "not_started":
            self.message.set_level("info", "方案已冻结，尚未运行。真实 CFD 需显式 --max-new-cfd 预算。")
        else:
            self.message.set_level("info", "背压扫描不保证等流量；每个 case 仍需用验证模块提取 SI 流量与攻角后再比较。"
                                           "MassFlow 为主程序历史单位。")

        for card, metric in ((self.eff_card, "Efficiency"), (self.flow_card, "MassFlow")):
            series = []
            pressures = sorted({point.p_out_pa for point in points}, key=lambda v: (v is None, v))
            palette = theme.series_colors(max(1, 2 * len(pressures)))
            for p_index, pressure in enumerate(pressures):
                for s_index, side in enumerate(("hub", "shroud")):
                    chosen = sorted(
                        (point for point in done if point.p_out_pa == pressure
                         and (point.role == "center" or point.role.startswith(side))),
                        key=lambda point: point.offset_deg,
                    )
                    values = [point.metrics.get(metric) for point in chosen]
                    if not any(isinstance(value, (int, float)) for value in values):
                        continue
                    name = f"{side} · {fmt(pressure, 4)} Pa" if len(pressures) > 1 else side
                    series.append(Series(name, palette[(2 * p_index + s_index) % len(palette)],
                                         [point.offset_deg for point in chosen], values,
                                         [f"{ROLE_LABELS.get(point.role, point.role)} · {point.run_id}" for point in chosen],
                                         kind="line", marker=4.6, width=2.0, dashed=side == "shroud"))
            card.chart.set_series(series)
            card.update_data(bool(series))

        slopes = result.slopes()
        self.slopes.setRowCount(len(slopes))
        for index, row in enumerate(slopes):
            slope = row.get("dEfficiency_ddeg")
            cells = [
                _cell(fmt(row.get("p_out_pa"), 5), align_right=False),
                _cell(row["variable"], align_right=False),
                _cell(fmt(row.get("step_deg"), 4)),
                _cell(fmt(slope, 4), color=None if slope is None else ("good" if slope > 0 else "bad")),
                _cell(fmt(row.get("curvature_Efficiency"), 4)),
                _cell(fmt(row.get("dMassFlow_ddeg"), 4)),
                _cell(row.get("scheme_Efficiency") or "—", align_right=False, color="text_dim"),
            ]
            for column, item in enumerate(cells):
                self.slopes.setItem(index, column, item)

        self.points.setRowCount(len(points))
        for index, point in enumerate(points):
            cells = [
                _cell(str(point.index), align_right=False, color="text_dim"),
                _cell(ROLE_LABELS.get(point.role, point.role), align_right=False),
                _cell(fmt(point.p_out_pa, 5)),
                _cell(f"{point.offset_deg:+.3f}"),
                _cell(point.status, align_right=False, color=None),
                _cell(point.run_id or "—", align_right=False),
                _cell(fmt(point.metrics.get("Efficiency"), 6)),
                _cell(fmt(point.metrics.get("MassFlow"), 6)),
            ]
            cells[4].setForeground(theme.status_color(point.status))
            for column, item in enumerate(cells):
                self.points.setItem(index, column, item)


class KeyValuePanel(QWidget):
    """Comparison verdicts, draft-spec checklists and failure receipts."""

    overlay_requested = Signal(str, str)  # target summary, baseline summary

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 6, 0)
        root.setSpacing(12)
        head = QHBoxLayout()
        self.title = QLabel("—")
        self.title.setStyleSheet("font-size: 15px; font-weight: 700;")
        head.addWidget(self.title)
        self.badge = Badge("—", "muted")
        head.addWidget(self.badge)
        head.addStretch(1)
        self.action = tool_button("叠加查看两条展向分布", "compare", "ghost")
        self.action.clicked.connect(self._emit_overlay)
        head.addWidget(self.action)
        root.addLayout(head)
        self.message = MessageBar()
        root.addWidget(self.message)
        self.card = Card("", "")
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(8)
        self.card.body.addLayout(self.grid)
        root.addWidget(self.card)
        self.list_card = Card("", "")
        self.list_body = QVBoxLayout()
        self.list_body.setSpacing(6)
        self.list_card.body.addLayout(self.list_body)
        root.addWidget(self.list_card)
        root.addStretch(1)
        self._overlay: tuple[str, str] | None = None

    def _emit_overlay(self) -> None:
        if self._overlay:
            self.overlay_requested.emit(*self._overlay)

    def _rows(self, items: list[tuple[str, str, str | None]]) -> None:
        clear_layout(self.grid)
        for index, (key, value, tint) in enumerate(items):
            name = QLabel(key)
            name.setStyleSheet(f"color: {theme.PALETTE['text_dim']}; font-weight: 600;")
            text = QLabel(value)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if tint:
                text.setStyleSheet(f"color: {theme.PALETTE[tint]}; font-weight: 600;")
            self.grid.addWidget(name, index, 0, Qt.AlignTop)
            self.grid.addWidget(text, index, 1)
        self.grid.setColumnStretch(1, 1)

    def _list(self, title: str, hint: str, lines: list[tuple[str, str]]) -> None:
        clear_layout(self.list_body)
        self.list_card.set_title(title)
        self.list_card.set_hint(hint)
        self.list_card.setVisible(bool(lines))
        for icon_tint, text in lines:
            label = QLabel(("● " if icon_tint else "") + text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setStyleSheet(f"color: {theme.PALETTE.get(icon_tint or 'text_dim', theme.PALETTE['text_dim'])};")
            self.list_body.addWidget(label)

    @staticmethod
    def _flag(value: object) -> tuple[str, str]:
        if value is True:
            return "是", "good"
        if value is False:
            return "否", "bad"
        return "—", "text_faint"

    def show_comparison(self, result: ComparisonResult) -> None:
        data = result.data
        usable = data.get("usable_for_matched_point_diagnostic") is True
        self.title.setText(result.path.name)
        self.badge.setText("可作同工况诊断" if usable else "不可作同工况诊断")
        self.badge.set_kind("good" if usable else "bad")
        items = []
        for key, label in (("matched_operating_point", "工况匹配"), ("declared_conditions_match", "声明工况一致"),
                           ("quality_ok", "两侧质量检查"), ("usable_for_matched_point_diagnostic", "可用于同工况诊断"),
                           ("numerical_acceptance_verified", "数值接受已验证")):
            text, tint = self._flag(data.get(key))
            items.append((label, text, tint))
        relative = data.get("mass_flow_difference_relative")
        tolerance = data.get("flow_tolerance_relative")
        items.append(("流量相对差 / 容差", f"{_pct(relative if isinstance(relative, (int, float)) else None)} / "
                                       f"{_pct(tolerance if isinstance(tolerance, (int, float)) else None)}", None))
        delta = data.get("delta_rms_deg")
        items.append(("ΔRMS（目标 − 基准）", _deg(delta if isinstance(delta, (int, float)) else None), None))
        self._rows(items)
        self.card.set_title("对比结论")
        self.card.set_hint("由 compare 命令写出；GUI 不重新判定")
        self.message.set_level("info", str(data.get("note", "")))
        sources = result.source_summaries()
        self._list("来源", "summary.json 及其 SHA-256 身份", [("", str(path)) for path in sources])
        self._overlay = (str(sources[1]), str(sources[0])) if len(sources) == 2 else None
        self.action.setVisible(self._overlay is not None and all(Path(p).is_file() for p in self._overlay))

    def show_spec(self, result: SpecResult) -> None:
        spec = result.spec
        pending = result.pending
        self.title.setText(f"验证配置 · {spec.get('target_id', result.path.stem)}")
        self.badge.setText("可提取" if not pending else f"待确认 {len(pending)} 项")
        self.badge.set_kind("good" if not pending else "warn")
        self.action.setVisible(False)
        verified, tint = self._flag(spec.get("geometry_verified"))
        measurement = spec.get("measurement") if isinstance(spec.get("measurement"), dict) else {}
        conditions = spec.get("conditions") if isinstance(spec.get("conditions"), dict) else {}
        self._rows([
            ("结果文件", str(spec.get("res_path", "—")), None),
            ("几何来源", str(spec.get("geometry_source", "—")), None),
            ("几何已人工确认", verified, tint),
            ("hub / shroud 前缘金属角", f"{_deg(_as_float(spec.get('hub_beta_deg')))} / "
                                     f"{_deg(_as_float(spec.get('shroud_beta_deg')))}", None),
            ("测量定义", f"截面 {measurement.get('le_station', '—')} · {measurement.get('bands', '—')} 带 · "
                         f"normal_sign {measurement.get('normal_sign')} · θ 参考 {measurement.get('theta_reference_sign')}", None),
            ("工况", f"{conditions.get('rpm')} rpm · p0 {conditions.get('inlet_total_pressure_pa')} Pa · "
                     f"T0 {conditions.get('inlet_total_temperature_k')} K · {conditions.get('fluid_id')} · "
                     f"{conditions.get('n_blades')} 叶 / {conditions.get('simulated_passages', 1)} 通道", None),
        ])
        self.card.set_title("配置内容")
        self.card.set_hint(str(result.path))
        if pending:
            self.message.set_level("warn", "提取前须在 JSON 中补齐并核对以下字段；预填值不等于几何已确认。")
        else:
            self.message.set_level("good", "必填字段已齐全。提取仍会核对预填来源文件的身份。")
        lines = [("warn", item) for item in pending]
        sources = spec.get("prefill_sources") if isinstance(spec.get("prefill_sources"), dict) else {}
        lines += [("blue", f"预填 {key} ← {value}") for key, value in sources.items()]
        warnings = spec.get("prefill_warnings") if isinstance(spec.get("prefill_warnings"), list) else []
        lines += [("amber", f"预填警告：{item}") for item in warnings]
        self._list("待确认与预填来源", "", lines)

    def show_failure(self, result: FailedResult) -> None:
        state = result.state
        self.title.setText(result.path.name)
        status = str(state.get("status", "unknown"))
        self.badge.setText({"failed": "提取失败", "running": "未完成/中断"}.get(status, status))
        self.badge.set_kind("bad" if status == "failed" else "warn")
        self.action.setVisible(False)
        self._rows([("状态", status, "bad" if status == "failed" else "amber"),
                    ("阶段", str(state.get("failure_stage", "—")), None),
                    ("信息", str(state.get("message", "—")), None)])
        self.card.set_title("state.json")
        self.card.set_hint("保留的执行证据：extract.cse、cfxpost.log、returncode.json")
        self.message.set_level("error", "该目录没有 summary.json；查看 cfxpost.log 与 cfdpost_error.log 后用新目录重新提取。")
        logs = [("", str(path)) for path in sorted(result.path.glob("*.log"))]
        self._list("日志文件", "", logs)


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------
class IncidenceView(QWidget):
    """Toolbar + stacked panels; loads any validation output path."""

    loaded = Signal(str)  # kind

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.result = None
        self.baseline: ProfileResult | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("执行后自动载入，或打开 extract / sweep / legacy / compare / plan 的输出")
        bar.addWidget(self.path_edit, 1)
        open_dir = tool_button("打开结果目录…", "folder", "ghost")
        open_dir.clicked.connect(self._choose_directory)
        bar.addWidget(open_dir)
        open_file = tool_button("", "file", "ghost", "打开 JSON（对比结果 / 验证配置）")
        open_file.setFixedWidth(36)
        open_file.clicked.connect(self._choose_file)
        bar.addWidget(open_file)
        self.overlay_button = tool_button("叠加基准…", "compare", "ghost",
                                          "选择另一个提取结果目录叠加（须同方法、同测量定义）")
        self.overlay_button.clicked.connect(self._choose_baseline)
        bar.addWidget(self.overlay_button)
        self.clear_button = tool_button("", "trash", "ghost", "清除叠加")
        self.clear_button.setFixedWidth(36)
        self.clear_button.clicked.connect(self.clear_baseline)
        bar.addWidget(self.clear_button)
        reload_button = tool_button("", "refresh", "ghost", "重新读取")
        reload_button.setFixedWidth(36)
        reload_button.clicked.connect(self.reload)
        bar.addWidget(reload_button)
        reveal = tool_button("", "external", "ghost", "在文件管理器中打开")
        reveal.setFixedWidth(36)
        reveal.clicked.connect(self._reveal)
        bar.addWidget(reveal)
        root.addLayout(bar)

        self.error = MessageBar()
        root.addWidget(self.error)

        self.stack = QStackedWidget()
        self.empty = EmptyState(
            "尚未载入验证结果",
            "执行「提取展向攻角」「截面 / 分带敏感性」「复现 20 点指标」「工况对比」或敏感性方案后会自动载入；"
            "也可以打开已有输出目录。叶高方向 0 = hub，1 = shroud。",
            "angle",
        )
        self.profile_panel = ProfilePanel()
        self.sweep_panel = SweepPanel()
        self.plan_panel = PlanPanel()
        self.kv_panel = KeyValuePanel()
        self.kv_panel.overlay_requested.connect(self.load_pair)
        self.stack.addWidget(self.empty)
        for panel in (self.profile_panel, self.sweep_panel, self.plan_panel, self.kv_panel):
            self.stack.addWidget(_scroll(panel))
        root.addWidget(self.stack, 1)
        self._sync_buttons()

    # ------------------------------------------------------------- loading
    def load(self, path: str | Path, *, keep_baseline: bool = False) -> bool:
        if not keep_baseline:
            self.baseline = None
        self.path_edit.setText(str(path))
        try:
            self.result = load_result(path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.result = None
            self.error.set_level("error", f"无法读取验证结果：{exc}")
            self.stack.setCurrentIndex(0)
            self._sync_buttons()
            return False
        self.error.set_level("info", "")
        self._render()
        self.loaded.emit(self.result.kind)
        return True

    def load_pair(self, target: str, baseline: str) -> None:
        try:
            self.baseline = load_profile(Path(baseline).parent if Path(baseline).is_file() else Path(baseline))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.error.set_level("error", f"无法读取基准：{exc}")
            return
        self.load(target, keep_baseline=True)

    def reload(self) -> None:
        if self.path_edit.text():
            baseline_path = self.baseline.path if self.baseline is not None else None
            if self.load(self.path_edit.text()) and baseline_path is not None:
                self.set_baseline(baseline_path)

    def set_baseline(self, path: str | Path) -> None:
        if not isinstance(self.result, ProfileResult):
            self.error.set_level("warn", "叠加基准只适用于展向分布结果（extract / legacy）。")
            return
        try:
            candidate = load_result(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.error.set_level("error", f"无法读取基准：{exc}")
            return
        if not isinstance(candidate, ProfileResult):
            self.error.set_level("warn", "所选目录不是展向分布结果。")
            return
        self.baseline = candidate
        self._render()

    def clear_baseline(self) -> None:
        self.baseline = None
        if self.result is not None:
            self._render()

    def _render(self) -> None:
        result = self.result
        if isinstance(result, ProfileResult):
            note = self.profile_panel.show_profile(result, self.baseline)
            if note:
                self.baseline = None
            self.stack.setCurrentIndex(1)
        elif isinstance(result, SweepResult):
            self.sweep_panel.show_sweep(result)
            self.stack.setCurrentIndex(2)
        elif isinstance(result, PlanResult):
            self.plan_panel.show_plan(result)
            self.stack.setCurrentIndex(3)
        elif isinstance(result, ComparisonResult):
            self.kv_panel.show_comparison(result)
            self.stack.setCurrentIndex(4)
        elif isinstance(result, SpecResult):
            self.kv_panel.show_spec(result)
            self.stack.setCurrentIndex(4)
        elif isinstance(result, FailedResult):
            self.kv_panel.show_failure(result)
            self.stack.setCurrentIndex(4)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        profile = isinstance(self.result, ProfileResult)
        self.overlay_button.setEnabled(profile)
        self.clear_button.setEnabled(profile and self.baseline is not None)

    # ------------------------------------------------------------- dialogs
    def _start_dir(self) -> str:
        text = self.path_edit.text()
        if text:
            path = Path(text)
            return str(path if path.is_dir() else path.parent)
        return str(Path.home())

    def _choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择验证输出目录", self._start_dir())
        if directory:
            self.load(directory)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择验证 JSON", self._start_dir(), "JSON (*.json)")
        if path:
            self.load(path)

    def _choose_baseline(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择基准提取结果目录", self._start_dir())
        if directory:
            self.set_baseline(directory)

    def _reveal(self) -> None:
        text = self.path_edit.text()
        if text and Path(text).exists():
            path = Path(text)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))
