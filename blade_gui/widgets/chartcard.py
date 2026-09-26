"""A :class:`Card` that swaps between a chart and an empty-state placeholder."""
from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..charts import _ChartBase
from .common import Card, EmptyState, tool_button


class ChartCard(Card):
    def __init__(
        self,
        title: str,
        hint: str = "",
        chart: _ChartBase | None = None,
        *,
        empty_title: str = "暂无数据",
        empty_hint: str = "",
        empty_icon: str = "chart",
        resettable: bool = True,
        parent: QWidget | None = None,
    ):
        super().__init__(title, hint, parent)
        if chart is None:
            from ..charts import XYChart

            chart = XYChart()
        self.chart = chart
        self.empty = EmptyState(empty_title, empty_hint, empty_icon)
        self.body.addWidget(self.chart)
        self.body.addWidget(self.empty)
        if resettable and hasattr(chart, "reset_view"):
            button = tool_button("", "refresh", "ghost", "复位缩放")
            button.setFixedWidth(34)
            button.clicked.connect(chart.reset_view)
            self.add_header_widget(button)
        self._update_visibility(False)

    def _update_visibility(self, has_data: bool) -> None:
        self.chart.setVisible(has_data)
        self.empty.setVisible(not has_data)

    def update_data(self, has_data: bool) -> None:
        self._update_visibility(bool(has_data))
