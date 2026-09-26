"""Case browser: list ``cases/``, inspect files, logs and failure stages."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..project import CaseRecord, fmt, fmt_time, human_size, read_text, tail_text
from ..widgets import Badge, Card, EmptyState, MessageBar, tool_button
from .base import Page

STATUS_FILTERS = [("全部状态", "all"), ("仅成功", "success"), ("仅失败", "failed"), ("未登记", "unknown")]
LOG_SUFFIXES = {".log", ".txt", ".out", ".err", ".lst", ".trn"}


class CasesPage(Page):
    title = "算例浏览"
    subtitle = "cases/ 目录、生成文件与各阶段日志"
    nav_label = "算例浏览"
    nav_icon = "folder"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent)
        self._records: list[CaseRecord] = []
        self._selected: CaseRecord | None = None
        self._build()
        self.refresh()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(11)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("按 run_id 或失败信息搜索…")
        self.search.textChanged.connect(self._apply_filter)
        bar.addWidget(self.search, 1)
        self.status_filter = QComboBox()
        for label, key in STATUS_FILTERS:
            self.status_filter.addItem(label, key)
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.status_filter)
        reveal = tool_button("打开 cases 目录", "folder")
        reveal.clicked.connect(self._open_cases_dir)
        bar.addWidget(reveal)
        root.addLayout(bar)

        self.message = MessageBar()
        self.message.setVisible(False)
        root.addWidget(self.message)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # ---------------------------------------------------- left: table
        left = Card("算例列表", "")
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["run_id", "来源", "状态", "失败阶段", "Efficiency", "MassFlow", "文件", "更新时间"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        for column in range(4, 8):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_select)
        left.body.addWidget(self.table)
        self.table_empty = EmptyState(
            "没有算例目录",
            "运行 write-candidate 或 run 后会在输出目录下生成 cases/。",
            "folder",
        )
        left.body.addWidget(self.table_empty)
        splitter.addWidget(left)

        # --------------------------------------------------- right: detail
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        self.detail_header = Card("算例详情", "")
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self.detail_title = QLabel("未选择算例")
        self.detail_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        header_row.addWidget(self.detail_title)
        self.detail_badge = Badge("—", "muted")
        header_row.addWidget(self.detail_badge)
        header_row.addStretch(1)
        self.detail_path = QLabel("")
        self.detail_path.setObjectName("CardHint")
        self.detail_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header_row.addWidget(self.detail_path)
        open_button = tool_button("打开目录", "folder")
        open_button.clicked.connect(self._open_selected_dir)
        header_row.addWidget(open_button)
        self.detail_header.body.addLayout(header_row)
        self.detail_message = QLabel("")
        self.detail_message.setObjectName("CardHint")
        self.detail_message.setWordWrap(True)
        self.detail_header.body.addWidget(self.detail_message)
        self.detail_notice = MessageBar()
        self.detail_notice.setVisible(False)
        self.detail_header.body.addWidget(self.detail_notice)
        right_layout.addWidget(self.detail_header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_tab(), "概览")
        self.tabs.addTab(self._build_files_tab(), "文件")
        self.tabs.addTab(self._build_log_tab(), "日志")
        self.tabs.addTab(self._build_json_tab(), "candidate.json")
        right_layout.addWidget(self.tabs, 1)
        splitter.addWidget(right)
        splitter.setSizes([560, 620])

        self._set_detail_visible(False)

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        self._overview_labels: dict[str, QLabel] = {}
        keys = [
            "run_id", "状态", "失败阶段", "样例来源", "Efficiency", "MassFlow",
            "Power", "PressureRatio", "总压比", "文件数量", "更新时间", "目录",
        ]
        for index, key in enumerate(keys):
            name = QLabel(key)
            name.setStyleSheet(f"color: {theme.PALETTE['text_dim']}; font-weight: 600;")
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            self._overview_labels[key] = value
            grid.addWidget(name, index, 0, Qt.AlignTop)
            grid.addWidget(value, index, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    def _build_files_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels(["文件", "大小", "修改时间"])
        self.file_tree.setRootIsDecorated(False)
        self.file_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.file_tree.itemDoubleClicked.connect(self._open_file_item)
        layout.addWidget(self.file_tree)
        hint = QLabel("双击任意文件用系统默认程序打开。")
        hint.setObjectName("CardHint")
        layout.addWidget(hint)
        return page

    def _build_log_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("日志文件"))
        self.log_picker = QComboBox()
        self.log_picker.setMinimumWidth(240)
        self.log_picker.currentIndexChanged.connect(lambda _: self._load_log())
        row.addWidget(self.log_picker, 1)
        reload_button = tool_button("重新读取", "refresh")
        reload_button.clicked.connect(self._load_log)
        row.addWidget(reload_button)
        layout.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        mono = QFont("SF Mono, Menlo, Consolas, monospace")
        mono.setPixelSize(11)
        self.log_view.setFont(mono)
        self.log_view.setPlaceholderText("该算例没有可读取的文本日志。")
        layout.addWidget(self.log_view, 1)
        self.log_hint = QLabel("")
        self.log_hint.setObjectName("CardHint")
        layout.addWidget(self.log_hint)
        return page

    def _build_json_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        self.json_view = QPlainTextEdit()
        self.json_view.setReadOnly(True)
        mono = QFont("SF Mono, Menlo, Consolas, monospace")
        mono.setPixelSize(11)
        self.json_view.setFont(mono)
        self.json_view.setPlaceholderText("该算例目录下没有 candidate.json。")
        layout.addWidget(self.json_view, 1)
        return page

    # ------------------------------------------------------------ refresh
    def refresh(self) -> None:
        self._records = self.ctx.project.cases()
        self._apply_filter()
        if self._selected is not None:
            same = next((item for item in self._records if item.run_id == self._selected.run_id), None)
            if same is not None:
                self._show_case(same)
            else:
                self._set_detail_visible(False)

    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        status_key = self.status_filter.currentData()
        rows: list[CaseRecord] = []
        for record in self._records:
            if status_key == "success" and record.status != "success":
                continue
            if status_key == "failed" and record.status == "success":
                continue
            if status_key == "unknown" and record.registered:
                continue
            if needle and needle not in " ".join(
                [record.run_id, record.message, record.failure_stage, record.sample_phase]
            ).lower():
                continue
            rows.append(record)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for index, record in enumerate(rows):
            values = [
                record.run_id,
                record.sample_phase or "—",
                record.status,
                record.failure_stage or "—",
                fmt(record.efficiency),
                fmt(record.massflow),
                str(record.files),
                fmt_time(record.modified),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {4, 5, 6}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 2:
                    item.setForeground(theme.status_color(record.status))
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)

        has_rows = bool(rows)
        self.table.setVisible(has_rows)
        self.table_empty.setVisible(not has_rows)
        if not has_rows:
            if self._records:
                self.table_empty.set_text("没有匹配的算例", "调整搜索或筛选条件试试。")
                self.table_empty.set_icon("search")
            else:
                self.table_empty.set_text(
                    "没有算例目录",
                    f"输出目录 {self.ctx.project.cases_dir} 下暂无 case_* 目录。",
                )
                self.table_empty.set_icon("folder")
        self.message.setVisible(False)

    # ------------------------------------------------------------ details
    def _on_select(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return
        row = rows[0].row()
        name = self.table.item(row, 0)
        if name is None:
            return
        record = next((item for item in self._records if item.run_id == name.text()), None)
        if record is not None:
            self._show_case(record)

    def select_run(self, run_id: str) -> None:
        """Public hook so other pages can jump to a case."""
        self.search.clear()
        self.status_filter.setCurrentIndex(0)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == run_id:
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                return
        record = next((item for item in self._records if item.run_id == run_id), None)
        if record is not None:
            self._show_case(record)

    def _set_detail_visible(self, visible: bool) -> None:
        self.tabs.setVisible(visible)
        self.detail_message.setVisible(visible)
        if not visible:
            self.detail_title.setText("未选择算例")
            self.detail_path.setText("")
            self.detail_badge.setText("—")
            self.detail_badge.set_kind("muted")

    def _show_case(self, record: CaseRecord) -> None:
        self._selected = record
        self._set_detail_visible(True)
        self.detail_title.setText(record.run_id)
        self.detail_badge.setText(
            {"success": "成功", "failed": "失败", "failure": "失败"}.get(record.status, record.status or "未登记")
        )
        self.detail_badge.set_kind("good" if record.ok else ("bad" if record.registered else "muted"))
        self.detail_path.setText(str(record.path))
        message = record.message or "（无失败信息）"
        self.detail_message.setText(message)

        if record.path.is_dir():
            self.detail_notice.setVisible(False)
        else:
            self.detail_notice.set_level(
                "warn",
                "该算例目录在本机不存在，因此文件、日志与 candidate.json 均无法显示。\n"
                f"CSV 中记录的路径：{record.path}\n"
                "这通常表示结果是在 CFD 主机（Windows）上生成的。"
                "可用左下角「设置数据目录…」指向本机的输出目录。",
            )

        values = {
            "run_id": record.run_id,
            "状态": record.status or "未登记",
            "失败阶段": record.failure_stage or "—",
            "样例来源": record.sample_phase or "—",
            "Efficiency": fmt(record.efficiency, 6),
            "MassFlow": fmt(record.massflow, 6),
            "Power": fmt(record.power, 6),
            "PressureRatio": "—",
            "总压比": "—",
            "文件数量": str(record.files),
            "更新时间": fmt_time(record.modified),
            "目录": str(record.path),
        }
        row = self._row_for(record)
        if row is not None:
            values["PressureRatio"] = fmt(row.get("PressureRatio"), 6)
            values["总压比"] = fmt(row.get("totalpressureratio"), 6)
        for key, label in self._overview_labels.items():
            label.setText(str(values.get(key, "—")))
            if key == "状态":
                label.setStyleSheet(f"color: {theme.status_color(record.status).name()}; font-weight: 700;")

        self._populate_files(record)
        self._populate_logs(record)
        self._populate_json(record)

    def _row_for(self, record: CaseRecord):
        df = self.ctx.project.training()
        if df.empty or "run_id" not in df.columns:
            return None
        match = df.loc[df["run_id"].astype(str) == record.run_id]
        return match.iloc[0] if not match.empty else None

    def _populate_files(self, record: CaseRecord) -> None:
        self.file_tree.clear()
        files = self.ctx.project.case_files(record)
        for item in files:
            node = QTreeWidgetItem([item["rel"], human_size(item["size"]), fmt_time(item["modified"])])
            node.setData(0, Qt.UserRole, item["path"])
            node.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if item["suffix"] in LOG_SUFFIXES:
                node.setForeground(0, theme.color("accent"))
            self.file_tree.addTopLevelItem(node)

    def _populate_logs(self, record: CaseRecord) -> None:
        files = self.ctx.project.case_files(record)
        logs = [item for item in files if Path(item["name"]).suffix.lower() in LOG_SUFFIXES]
        logs.sort(key=lambda item: -item["size"])
        blocked = self.log_picker.blockSignals(True)
        self.log_picker.clear()
        for item in logs:
            self.log_picker.addItem(item["rel"], item["path"])
        self.log_picker.blockSignals(blocked)
        self.log_view.clear()
        self.log_hint.setText("")
        if logs:
            self._load_log()
        else:
            self.log_view.setPlainText("")

    def _load_log(self) -> None:
        path = self.log_picker.currentData()
        if not path:
            return
        text = tail_text(path, 800)
        self.log_view.setPlainText(text or "（空文件）")
        size = Path(path).stat().st_size if Path(path).exists() else 0
        self.log_hint.setText(f"{path} · {human_size(size)} · 仅显示末尾 800 行")

    def _populate_json(self, record: CaseRecord) -> None:
        path = record.path / "candidate.json"
        if not path.exists():
            candidates = list(record.path.glob("*.json"))
            path = candidates[0] if candidates else path
        if not path.exists():
            self.json_view.setPlainText("")
            return
        raw = read_text(path)
        try:
            pretty = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pretty = raw
        self.json_view.setPlainText(pretty)

    # ------------------------------------------------------------ actions
    def _open_file_item(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.UserRole)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_selected_dir(self) -> None:
        if self._selected is not None and self._selected.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._selected.path)))

    def _open_cases_dir(self) -> None:
        directory = self.ctx.project.cases_dir
        if not directory.exists():
            self.message.set_level("warn", f"目录尚不存在：{directory}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
