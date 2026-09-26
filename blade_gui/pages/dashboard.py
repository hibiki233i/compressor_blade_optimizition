"""Overview dashboard: KPI tiles, Pareto front, convergence and distributions."""
from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..charts import CategoryBarChart, Series, XYChart
from ..project import OBJECTIVES, fmt, fmt_time
from ..widgets import (
    Badge,
    Card,
    EmptyState,
    MessageBar,
    StatTile,
    clear_layout,
    tool_button,
)
from .base import Page

PHASE_LABELS = {
    "doe": "初始 DOE",
    "active_learning": "主动学习",
    "boundary": "边界实验",
    "fallback": "兜底随机",
    "unknown": "未知",
}


def _successful(frame):
    """Rows whose ``status`` is exactly ``success`` (empty frame when absent)."""
    if frame is None:
        return None
    if getattr(frame, "empty", True) or "status" not in frame.columns:
        return frame.iloc[0:0]
    status = frame["status"].astype(str).str.strip().str.lower()
    return frame.loc[status == "success"]


class DashboardPage(Page):
    title = "总览看板"
    subtitle = "优化进度、Pareto 前沿与关键指标"
    nav_label = "总览看板"
    nav_icon = "dashboard"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent)
        self._build()
        self.refresh()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(22, 18, 22, 22)
        root.setSpacing(14)

        self.message = MessageBar()
        self.message.setVisible(False)
        root.addWidget(self.message)

        # ---- KPI tiles
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tile_attempts = StatTile("总尝试", "0", "training_data.csv 行数", "blue")
        self.tile_success = StatTile("成功", "0", "真实 CFD 成功", "good")
        self.tile_failed = StatTile("失败", "0", "含几何/网格/求解阶段", "bad")
        self.tile_pareto = StatTile("Pareto 解", "0", "工程容差前沿", "accent")
        self.tile_eff = StatTile("最佳效率", "—", "成功样本最大值", "accent")
        self.tile_flow = StatTile("最佳流量", "—", "成功样本最大值", "violet")
        for tile in (self.tile_attempts, self.tile_success, self.tile_failed,
                     self.tile_pareto, self.tile_eff, self.tile_flow):
            tiles.addWidget(tile, 1)
        root.addLayout(tiles)

        # ---- charts row
        charts = QHBoxLayout()
        charts.setSpacing(14)

        self.pareto_card = Card("Pareto 前沿", "Efficiency vs MassFlow · 滚轮缩放 · 双击复位")
        self.pareto_chart = XYChart()
        self.pareto_chart.setMinimumHeight(320)
        self.pareto_chart.set_axis_labels("MassFlow", "Efficiency")
        self.pareto_chart.setToolTip("点击数据点可跳转到对应算例")
        self.pareto_empty = EmptyState("暂无 Pareto 解", "成功样本达到 1 个后才会生成前沿。", "target")
        self.pareto_card.body.addWidget(self.pareto_chart)
        self.pareto_card.body.addWidget(self.pareto_empty)
        fit_button = tool_button("", "refresh", "ghost", "复位缩放")
        fit_button.setFixedWidth(34)
        fit_button.clicked.connect(self.pareto_chart.reset_view)
        self.pareto_card.add_header_widget(fit_button)
        charts.addWidget(self.pareto_card, 3)

        self.conv_card = Card("最优值收敛", "成功样本累计最优")
        self.conv_chart = XYChart()
        self.conv_chart.setMinimumHeight(320)
        self.conv_chart.set_axis_labels("成功样本序号", "Efficiency", "MassFlow")
        self.conv_empty = EmptyState("暂无成功样本", "运行主动学习或导入历史数据后显示。", "chart")
        self.conv_card.body.addWidget(self.conv_chart)
        self.conv_card.body.addWidget(self.conv_empty)
        charts.addWidget(self.conv_card, 2)
        root.addLayout(charts)

        # ---- distributions row
        row = QHBoxLayout()
        row.setSpacing(14)

        self.phase_card = Card("样本来源分布", "")
        self.phase_chart = CategoryBarChart()
        self.phase_chart.setMinimumHeight(200)
        self.phase_empty = EmptyState("暂无数据", "", "layers")
        self.phase_card.body.addWidget(self.phase_chart)
        self.phase_card.body.addWidget(self.phase_empty)
        row.addWidget(self.phase_card, 1)

        self.fail_card = Card("失败阶段分布", "仅统计未成功样本")
        self.fail_chart = CategoryBarChart()
        self.fail_chart.setMinimumHeight(200)
        self.fail_empty = EmptyState("没有失败记录", "所有尝试均已成功。", "check")
        self.fail_card.body.addWidget(self.fail_chart)
        self.fail_card.body.addWidget(self.fail_empty)
        row.addWidget(self.fail_card, 1)

        self.gate_card = Card("代理模型诊断门", "local_diagnostic_gate.json")
        self.gate_body = QVBoxLayout()
        self.gate_body.setSpacing(8)
        self.gate_card.body.addLayout(self.gate_body)
        row.addWidget(self.gate_card, 1)
        root.addLayout(row)
        root.addStretch(1)

    # ------------------------------------------------------------ refresh
    def refresh(self) -> None:
        project = self.ctx.project
        summary = project.summary()
        df = project.training()

        self.tile_attempts.set_value(str(summary.attempts), f"最近更新 {fmt_time(summary.last_modified)}")
        self.tile_success.set_value(str(summary.successes), f"成功率 {summary.success_rate * 100:.1f}%")
        self.tile_failed.set_value(str(summary.failures), "见下方失败阶段分布")
        self.tile_pareto.set_value(
            str(summary.pareto_count), f"严格前沿 {summary.strict_count} 个"
        )
        self.tile_eff.set_value(fmt(summary.best_efficiency), summary.best_run_id or "—")
        self.tile_flow.set_value(fmt(summary.best_massflow), "单通道 × 叶片数")

        self._refresh_message(df)
        self._refresh_pareto()
        self._refresh_convergence()
        self._refresh_distributions(summary)
        self._refresh_gate()

    def _refresh_message(self, df) -> None:
        project = self.ctx.project
        errors = [item for item in project.issues if item.level == "error"]
        warnings = [item for item in project.issues if item.level == "warning"]
        if project.load_error:
            self.message.set_level("error", f"配置无法解析：{project.load_error}")
        elif errors:
            self.message.set_level("error", "配置存在错误：" + "；".join(item.message for item in errors[:3]))
        elif df is None or df.empty:
            self.message.set_level(
                "info",
                f"尚未在 {project.output_dir} 找到 training_data.csv。可在「运行控制」启动主动学习，"
                "或在「项目设置」把数据目录指向已有结果。",
            )
        elif warnings:
            self.message.set_level(
                "warn",
                f"配置可用，但有 {len(warnings)} 条提示（多为本机缺少外部程序路径，属正常）。"
                "详情见「项目设置」。",
            )
        else:
            self.message.set_level("good", "配置校验通过，数据已载入。")

    def _refresh_pareto(self) -> None:
        df = self.ctx.project.training()
        success = _successful(df)
        has_data = success is not None and not success.empty and "Efficiency" in success.columns
        self.pareto_chart.setVisible(bool(has_data))
        self.pareto_empty.setVisible(not has_data)
        if not has_data:
            return

        work = success.copy()
        for col in OBJECTIVES:
            work[col] = pd.to_numeric(work.get(col), errors="coerce")
        work = work.dropna(subset=OBJECTIVES)

        pareto = self.ctx.project.pareto()
        strict = self.ctx.project.pareto_strict()
        pareto_ids = set(pareto.get("run_id", []).astype(str)) if not pareto.empty else set()
        strict_ids = set(strict.get("run_id", []).astype(str)) if not strict.empty else set()

        ids = work.get("run_id", work.index).astype(str)
        dominated_mask = ~ids.isin(pareto_ids)

        dominated = work.loc[dominated_mask]
        front = work.loc[~dominated_mask]

        series: list[Series] = []
        if not dominated.empty:
            series.append(
                Series(
                    "成功样本",
                    theme.color("text_faint"),
                    dominated["MassFlow"].tolist(),
                    dominated["Efficiency"].tolist(),
                    [str(value) for value in dominated.get("run_id", dominated.index)],
                    kind="scatter",
                    marker=3.6,
                    z=0.0,
                )
            )
        if not front.empty:
            ordered = front.sort_values("MassFlow")
            series.append(
                Series(
                    "Pareto 前沿",
                    theme.color("accent"),
                    ordered["MassFlow"].tolist(),
                    ordered["Efficiency"].tolist(),
                    [str(value) for value in ordered.get("run_id", ordered.index)],
                    kind="step",
                    marker=5.0,
                    width=2.2,
                    z=2.0,
                )
            )
            if not strict.empty:
                strict_work = work.loc[ids.isin(strict_ids)]
                if not strict_work.empty:
                    series.append(
                        Series(
                            "严格前沿",
                            theme.color("violet"),
                            strict_work["MassFlow"].tolist(),
                            strict_work["Efficiency"].tolist(),
                            [str(value) for value in strict_work.get("run_id", strict_work.index)],
                            kind="scatter",
                            marker=6.4,
                            ring=True,
                            z=1.0,
                        )
                    )
        self.pareto_chart.set_series(series)

    def _refresh_convergence(self) -> None:
        xs, eff, flow = self.ctx.project.best_so_far()
        has_data = bool(xs)
        self.conv_chart.setVisible(has_data)
        self.conv_empty.setVisible(not has_data)
        if not has_data:
            return
        self.conv_chart.set_series(
            [
                Series("最佳 Efficiency", theme.color("accent"), xs, eff, kind="line", fill=True, marker=0.0, width=2.4),
                Series("最佳 MassFlow", theme.color("violet"), xs, flow, kind="line", axis="right",
                       marker=0.0, width=2.0, dashed=True),
            ]
        )

    def _refresh_distributions(self, summary) -> None:
        phases = [(PHASE_LABELS.get(key, key), value) for key, value in summary.phases.items()]
        phases.sort(key=lambda item: -item[1])
        self.phase_chart.setVisible(bool(phases))
        self.phase_empty.setVisible(not phases)
        if phases:
            self.phase_chart.set_items(phases, theme.series_colors(len(phases)))
            self.phase_card.set_hint(f"{len(phases)} 种来源")

        stages = [(key or "unspecified", value) for key, value in summary.failure_stages.items()]
        stages.sort(key=lambda item: -item[1])
        self.fail_chart.setVisible(bool(stages))
        self.fail_empty.setVisible(not stages)
        if stages:
            self.fail_chart.set_items(stages, [theme.color("bad")] * len(stages))
            self.fail_card.set_hint(f"共 {summary.failures} 次失败")

    def _refresh_gate(self) -> None:
        clear_layout(self.gate_body)

        gate = self.ctx.project.gate()
        if not gate:
            self.gate_body.addWidget(
                EmptyState("尚无诊断门记录", "运行 diagnose 后生成 local_diagnostic_gate.json。", "sigma")
            )
            return

        passed = bool(gate.get("passed"))
        header = QHBoxLayout()
        header.setSpacing(8)
        title = f"最近 {int(gate.get('expected_points', 0) or 0)} 个点"
        header.addWidget(Badge.for_status("success" if passed else "failed"))
        header.addWidget(Badge(title, "muted"))
        header.addStretch(1)
        holder = QWidget()
        holder.setLayout(header)
        self.gate_body.addWidget(holder)

        for objective in OBJECTIVES:
            mae = gate.get(f"mae_{objective}")
            tolerance = gate.get(f"tolerance_{objective}")
            covered = gate.get(f"covered_2sigma_{objective}")
            if mae is None:
                continue
            ok = bool(gate.get(f"passed_{objective}", False))
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(objective)
            label.setStyleSheet(f"color: {theme.PALETTE['text_dim']}; font-weight: 600;")
            row.addWidget(label, 1)
            value = QLabel(f"MAE {fmt(mae)} / 容差 {fmt(tolerance)} · 2σ 覆盖 {covered}")
            value.setStyleSheet(f"color: {theme.PALETTE['text_faint']};")
            row.addWidget(value)
            row.addWidget(Badge("通过" if ok else "未通过", "good" if ok else "bad"))
            wrapper = QWidget()
            wrapper.setLayout(row)
            self.gate_body.addWidget(wrapper)

        if gate.get("first_iteration") is not None:
            footnote = QLabel(f"迭代窗口 {gate.get('first_iteration')} – {gate.get('last_iteration')}")
            footnote.setStyleSheet(f"color: {theme.PALETTE['text_faint']}; font-size: 11px;")
            self.gate_body.addWidget(footnote)
