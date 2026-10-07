"""Analytics page: variable effects, surrogate quality and active-learning progress."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..charts import CategoryBarChart, Series, XYChart
from ..project import OBJECTIVES, fmt
from ..widgets import ChartCard
from .base import Page

PHASE_LABELS = {
    "doe": "初始 DOE",
    "active_learning": "主动学习",
    "boundary": "边界实验",
    "fallback": "兜底随机",
}

PHASE_COLORS = {
    "doe": "blue",
    "active_learning": "accent",
    "boundary": "amber",
    "fallback": "pink",
}


def _successful(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "status" not in frame.columns:
        return pd.DataFrame()
    status = frame["status"].astype(str).str.strip().str.lower()
    return frame.loc[status == "success"].copy()


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


class AnalyticsPage(Page):
    title = "数据分析"
    subtitle = "变量相关性、代理模型预测质量与主动学习进程"
    nav_label = "数据分析"
    nav_icon = "chart"
    nav_section = "监控"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent)
        self._build()
        self.refresh()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(QLabel("目标函数"))
        self.objective_box = QComboBox()
        self.objective_box.addItems(OBJECTIVES)
        self.objective_box.setFixedWidth(150)
        self.objective_box.currentTextChanged.connect(lambda _: self.refresh())
        header.addWidget(self.objective_box)
        header.addStretch(1)
        root.addLayout(header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_variables_tab(), "变量分析")
        self.tabs.addTab(self._build_surrogate_tab(), "代理预测质量")
        self.tabs.addTab(self._build_progress_tab(), "主动学习进程")

    def _build_variables_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.corr_card = ChartCard(
            "变量-目标相关性",
            "成功样本上的 Pearson r（正=该变量增大目标增大）",
            CategoryBarChart(),
            empty_title="样本不足",
            empty_hint="至少需要 3 个成功样本才能给出相关性。",
            empty_icon="sigma",
            resettable=False,
        )
        layout.addWidget(self.corr_card, 2)

        self.scatter_card = ChartCard(
            "变量-目标散点",
            "按样本来源着色",
            XYChart(),
            empty_title="暂无成功样本",
            empty_icon="target",
        )
        self.scatter_card.chart.set_axis_labels("变量取值 (deg)", "目标值")
        picker = QComboBox()
        picker.setFixedWidth(190)
        picker.currentTextChanged.connect(lambda _: self._refresh_variable_scatter())
        self.variable_box = picker
        self.scatter_card.add_header_widget(picker)
        layout.addWidget(self.scatter_card, 3)
        return page

    def _build_surrogate_tab(self) -> QWidget:
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(12)

        self.parity_cards: dict[str, ChartCard] = {}
        for column, objective in enumerate(OBJECTIVES):
            card = ChartCard(
                f"{objective} 预测 vs 真实",
                "虚线为理想对角线",
                XYChart(diagonal=True),
                empty_title="暂无配对记录",
                empty_hint="主动学习记录 CFD 前预测值后才会出现。",
                empty_icon="target",
            )
            card.chart.set_axis_labels(f"真实 {objective}", f"预测 {objective}")
            self.parity_cards[objective] = card
            grid.addWidget(card, 0, column)

        self.error_card = ChartCard(
            "预测误差随迭代",
            "误差 = 预测 − 真实",
            XYChart(),
            empty_title="暂无误差记录",
            empty_icon="sigma",
        )
        self.error_card.chart.set_axis_labels("主动学习迭代", "预测误差")
        grid.addWidget(self.error_card, 1, 0)

        self.std_card = ChartCard(
            "预测标准差随迭代",
            "代理模型不确定度（校准后）",
            XYChart(),
            empty_title="暂无不确定度记录",
            empty_icon="sigma",
        )
        self.std_card.chart.set_axis_labels("主动学习迭代", "std")
        grid.addWidget(self.std_card, 1, 1)
        return page

    def _build_progress_tab(self) -> QWidget:
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(12)

        self.hv_card = ChartCard(
            "每轮超体积增益",
            "hv_gain · 真实目标空间",
            CategoryBarChart(),
            empty_title="暂无超体积记录",
            empty_hint="需要较新版本的诊断文件。",
            empty_icon="layers",
            resettable=False,
        )
        grid.addWidget(self.hv_card, 0, 0)

        self.source_card = ChartCard(
            "候选来源分布",
            "selection_source",
            CategoryBarChart(),
            empty_title="暂无候选来源",
            empty_icon="layers",
            resettable=False,
        )
        grid.addWidget(self.source_card, 0, 1)

        self.radius_card = ChartCard(
            "局部搜索半径",
            "local_radius_norm",
            XYChart(),
            empty_title="暂无局部搜索记录",
            empty_icon="target",
        )
        self.radius_card.chart.set_axis_labels("记录序号", "半径 (归一化)")
        grid.addWidget(self.radius_card, 1, 0)

        self.front_card = ChartCard(
            "前沿规模变化",
            "每轮 CFD 前后 Pareto 行数",
            XYChart(),
            empty_title="暂无前沿记录",
            empty_icon="target",
        )
        self.front_card.chart.set_axis_labels("记录序号", "Pareto 行数")
        grid.addWidget(self.front_card, 1, 1)
        return page

    # ------------------------------------------------------------ refresh
    def refresh(self) -> None:
        df = self.ctx.project.training()
        success = _successful(df)
        diagnostics = self.ctx.project.diagnostics()

        self._sync_variable_box()
        self._refresh_correlation(success)
        self._refresh_variable_scatter()
        self._refresh_parity(diagnostics)
        self._refresh_error(diagnostics)
        self._refresh_std(diagnostics)
        self._refresh_progress(diagnostics)

    def _sync_variable_box(self) -> None:
        names = [str(item["name"]) for item in self.ctx.project.config.get("variables", [])]
        current = self.variable_box.currentText()
        if [self.variable_box.itemText(i) for i in range(self.variable_box.count())] != names:
            blocked = self.variable_box.blockSignals(True)
            self.variable_box.clear()
            self.variable_box.addItems(names)
            if current in names:
                self.variable_box.setCurrentText(current)
            self.variable_box.blockSignals(blocked)

    def _refresh_correlation(self, success: pd.DataFrame) -> None:
        objective = self.objective_box.currentText() or OBJECTIVES[0]
        names = [str(item["name"]) for item in self.ctx.project.config.get("variables", [])]
        target = _numeric(success, objective)
        items: list[tuple[str, float]] = []
        if len(success) >= 3 and not target.empty:
            valid = target.notna()
            for name in names:
                column = _numeric(success, name)
                mask = valid & column.notna()
                if mask.sum() < 3:
                    continue
                left = column[mask].to_numpy(dtype=float)
                right = target[mask].to_numpy(dtype=float)
                if np.std(left) <= 0 or np.std(right) <= 0:
                    continue
                r = float(np.corrcoef(left, right)[0, 1])
                if math.isfinite(r):
                    items.append((name.replace("_deg_offset", ""), round(r, 3)))
        items.sort(key=lambda item: -abs(item[1]))
        chart: CategoryBarChart = self.corr_card.chart
        chart.set_items(
            items,
            [theme.color("good") if value >= 0 else theme.color("bad") for _, value in items],
        )
        chart.set_value_suffix("")
        self.corr_card.hint_label.setText(
            f"{objective} · {len(items)} 个变量 · 成功样本 {int(len(success))}"
        )
        self.corr_card.update_data(bool(items))

    def _refresh_variable_scatter(self) -> None:
        name = self.variable_box.currentText()
        objective = self.objective_box.currentText() or OBJECTIVES[0]
        success = _successful(self.ctx.project.training())
        chart: XYChart = self.scatter_card.chart
        chart.set_axis_labels(f"{name} (deg)", objective)
        if success.empty or not name or name not in success.columns:
            self.scatter_card.update_data(False)
            return
        frame = success.copy()
        frame["_x"] = _numeric(frame, name)
        frame["_y"] = _numeric(frame, objective)
        frame["_phase"] = frame.get("sample_phase", pd.Series(index=frame.index, dtype=object))
        frame = frame.dropna(subset=["_x", "_y"])
        if frame.empty:
            self.scatter_card.update_data(False)
            return

        series: list[Series] = []
        phases = list(frame["_phase"].fillna("unknown").astype(str).unique())
        for phase in phases:
            if phase not in PHASE_LABELS:
                continue
            subset = frame.loc[frame["_phase"].fillna("unknown").astype(str) == phase]
            series.append(
                Series(
                    PHASE_LABELS[phase],
                    theme.color(PHASE_COLORS[phase]),
                    subset["_x"].tolist(),
                    subset["_y"].tolist(),
                    [str(value) for value in subset.get("run_id", subset.index)],
                    kind="scatter",
                    marker=5.0,
                )
            )
        unknown = frame.loc[~frame["_phase"].fillna("unknown").astype(str).isin(PHASE_LABELS)]
        if not unknown.empty:
            series.append(
                Series(
                    "其他",
                    theme.color("text_faint"),
                    unknown["_x"].tolist(),
                    unknown["_y"].tolist(),
                    [str(value) for value in unknown.get("run_id", unknown.index)],
                    kind="scatter",
                    marker=4.0,
                )
            )
        chart.set_series(series)
        self.scatter_card.update_data(bool(series))

    def _refresh_parity(self, diagnostics: pd.DataFrame) -> None:
        for objective, card in self.parity_cards.items():
            chart: XYChart = card.chart
            if diagnostics.empty:
                card.update_data(False)
                continue
            true_values = _numeric(diagnostics, f"true_{objective}")
            pred_values = _numeric(diagnostics, f"pred_{objective}")
            frame = pd.DataFrame({"true": true_values, "pred": pred_values})
            labels = diagnostics.get("run_id", pd.Series(index=diagnostics.index, dtype=object)).astype(str)
            frame["label"] = labels
            frame = frame.dropna(subset=["true", "pred"])
            if frame.empty:
                card.update_data(False)
                continue
            chart.set_series(
                [
                    Series(
                        f"{objective}",
                        theme.color("accent"),
                        frame["true"].tolist(),
                        frame["pred"].tolist(),
                        frame["label"].tolist(),
                        kind="scatter",
                        marker=5.2,
                    )
                ]
            )
            error = (frame["pred"] - frame["true"]).abs()
            card.set_hint(f"n={len(frame)} · MAE={fmt(float(error.mean()))}")
            card.update_data(True)

    def _refresh_error(self, diagnostics: pd.DataFrame) -> None:
        objective = self.objective_box.currentText() or OBJECTIVES[0]
        chart: XYChart = self.error_card.chart
        chart.set_axis_labels("主动学习迭代", f"{objective} 预测误差")
        if diagnostics.empty:
            self.error_card.update_data(False)
            return
        error = _numeric(diagnostics, f"prediction_error_{objective}")
        if error.isna().all():
            error = _numeric(diagnostics, f"true_{objective}") - _numeric(diagnostics, f"pred_{objective}")
        iteration = _numeric(diagnostics, "iteration")
        frame = pd.DataFrame({"x": iteration, "y": error})
        frame["label"] = diagnostics.get("run_id", pd.Series(index=diagnostics.index, dtype=object)).astype(str)
        frame = frame.dropna(subset=["x", "y"])
        if frame.empty:
            self.error_card.update_data(False)
            return
        series = [
            Series(
                "预测误差",
                theme.color("amber"),
                frame["x"].tolist(),
                frame["y"].tolist(),
                frame["label"].tolist(),
                kind="scatter",
                marker=5.0,
            )
        ]
        low, high = float(frame["x"].min()), float(frame["x"].max())
        if low == high:
            low, high = low - 0.5, high + 0.5
        series.append(Series("零误差", theme.color("text_faint"), [low, high], [0.0, 0.0],
                             kind="line", marker=0.0, width=1.2, dashed=True, z=-1))
        chart.set_series(series)
        self.error_card.set_hint(f"{objective} · n={len(frame)} · 平均绝对误差 {fmt(float(frame['y'].abs().mean()))}")
        self.error_card.update_data(True)

    def _refresh_std(self, diagnostics: pd.DataFrame) -> None:
        objective = self.objective_box.currentText() or OBJECTIVES[0]
        chart: XYChart = self.std_card.chart
        chart.set_axis_labels("主动学习迭代", f"{objective} 预测标准差")
        if diagnostics.empty:
            self.std_card.update_data(False)
            return
        column = f"std_{objective}" if f"std_{objective}" in diagnostics.columns else f"raw_std_{objective}"
        std = _numeric(diagnostics, column)
        iteration = _numeric(diagnostics, "iteration")
        frame = pd.DataFrame({"x": iteration, "y": std, "label": diagnostics.get(
            "run_id", pd.Series(index=diagnostics.index, dtype=object)).astype(str)})
        frame = frame.dropna(subset=["x", "y"])
        if frame.empty:
            self.std_card.update_data(False)
            return
        chart.set_series(
            [
                Series("预测标准差", theme.color("violet"), frame["x"].tolist(), frame["y"].tolist(),
                       frame["label"].tolist(), kind="scatter", marker=4.6),
            ]
        )
        self.std_card.set_hint(f"{objective} · 字段 {column}")
        self.std_card.update_data(True)

    def _refresh_progress(self, diagnostics: pd.DataFrame) -> None:
        # hyper-volume gain
        gain_chart: CategoryBarChart = self.hv_card.chart
        if not diagnostics.empty and "hv_gain" in diagnostics.columns:
            gains = _numeric(diagnostics, "hv_gain")
            iteration = _numeric(diagnostics, "iteration")
            frame = pd.DataFrame({"x": iteration, "y": gains})
            frame = frame.dropna(subset=["x", "y"])
            items = [
                (f"iter {int(row.x)}", float(row.y))
                for row in frame.itertuples()
            ]
            gain_chart.set_items(items, [theme.color("accent") if value >= 0 else theme.color("bad")
                                         for _, value in items])
            self.hv_card.update_data(bool(items))
        else:
            self.hv_card.update_data(False)

        # selection sources
        source_chart: CategoryBarChart = self.source_card.chart
        summary = self.ctx.project.summary()
        items = sorted(summary.selection_sources.items(), key=lambda item: -item[1])
        source_chart.set_items(items, theme.series_colors(len(items)))
        self.source_card.update_data(bool(items))

        # local radius
        radius_chart: XYChart = self.radius_card.chart
        if not diagnostics.empty and "local_radius_norm" in diagnostics.columns:
            radius = _numeric(diagnostics, "local_radius_norm")
            frame = pd.DataFrame({"y": radius}).dropna()
            frame = frame.loc[frame["y"] > 0]
            if not frame.empty:
                xs = list(range(1, len(frame) + 1))
                radius_chart.set_series(
                    [Series("局部半径", theme.color("amber"), xs, frame["y"].tolist(),
                            kind="line", marker=3.6, fill=True, width=2.0)]
                )
                self.radius_card.update_data(True)
            else:
                self.radius_card.update_data(False)
        else:
            self.radius_card.update_data(False)

        # Pareto front size
        front_chart: XYChart = self.front_card.chart
        if not diagnostics.empty and "pareto_rows_after" in diagnostics.columns:
            before = _numeric(diagnostics, "pareto_rows_before")
            after = _numeric(diagnostics, "pareto_rows_after")
            frame = pd.DataFrame({"before": before, "after": after}).dropna()
            if not frame.empty:
                xs = list(range(1, len(frame) + 1))
                front_chart.set_series(
                    [
                        Series("评估前", theme.color("text_faint"), xs, frame["before"].tolist(),
                               kind="line", marker=0.0, width=1.6, dashed=True),
                        Series("评估后", theme.color("accent"), xs, frame["after"].tolist(),
                               kind="step", marker=3.4, width=2.2, fill=True),
                    ]
                )
                self.front_card.update_data(True)
            else:
                self.front_card.update_data(False)
        else:
            self.front_card.update_data(False)
