"""Application shell: sidebar navigation, top bar and status bar."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, icons, theme
from .context import AppContext
from .pages import PAGE_CLASSES
from .pages.config_page import ConfigPage
from .widgets import tool_button

SIDEBAR_WIDTH = 224


class NavButton(QPushButton):
    def __init__(self, label: str, icon_name: str, shortcut: str = "", parent: QWidget | None = None):
        super().__init__(f"  {label}", parent)
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(36)
        self.setIcon(icons.icon(icon_name, theme.PALETTE["text_dim"], 18))
        self._icon_name = icon_name
        if shortcut:
            layout = QHBoxLayout(self)
            layout.setContentsMargins(0, 0, 10, 0)
            layout.addStretch(1)
            hint = QLabel(shortcut)
            hint.setObjectName("NavShortcut")
            hint.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(hint)

    def set_active(self, active: bool) -> None:
        super().setChecked(active)
        color = theme.PALETTE["accent"] if active else theme.PALETTE["text_dim"]
        self.setIcon(icons.icon(self._icon_name, color, 18))


class Chip(QFrame):
    """Pill with a small icon and elided text (top bar context)."""

    def __init__(self, icon_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Chip")
        self.setFixedHeight(26)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 0, 11, 0)
        layout.setSpacing(6)
        self.glyph = QLabel()
        self.glyph.setPixmap(icons.pixmap(icon_name, theme.PALETTE["text_faint"], 13))
        layout.addWidget(self.glyph)
        self.text = QLabel()
        self.text.setStyleSheet(f"color: {theme.PALETTE['text_dim']}; font-size: 11px;")
        layout.addWidget(self.text)
        self._max_width = 360

    def set_text(self, text: str, tooltip: str = "", tint: str = "text_dim") -> None:
        metrics = QFontMetrics(self.text.font())
        self.text.setText(metrics.elidedText(text, Qt.ElideMiddle, self._max_width))
        self.text.setStyleSheet(f"color: {theme.PALETTE.get(tint, tint)}; font-size: 11px;")
        self.setToolTip(tooltip or text)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Blade Shape · 主动学习控制台")
        self.setMinimumSize(1180, 760)
        self.resize(1440, 900)
        self.setWindowIcon(icons.brand_icon(64))

        self._build()
        self.ctx.project_reloaded.connect(self._on_project_reloaded)
        self.ctx.status_message.connect(self._on_status)
        self.ctx.case_requested.connect(self._on_case_requested)
        for runner in self.ctx.command_runners:
            runner.started.connect(lambda *_: self._sync_busy())
            runner.finished.connect(lambda *_: QTimer.singleShot(0, self._sync_busy))
            runner.failed.connect(lambda *_: QTimer.singleShot(0, self._sync_busy))

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(15000)
        self._auto_timer.timeout.connect(self._auto_refresh)

        self._on_project_reloaded()
        self._sync_busy()
        self._select_page(0)

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        self.pages = []
        for page_class in PAGE_CLASSES:
            page = page_class(self.ctx)
            self.pages.append(page)
            self.stack.addWidget(page)
        right_layout.addWidget(self.stack, 1)

        status = QFrame()
        status.setObjectName("StatusBar")
        status.setFixedHeight(28)
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(16, 0, 16, 0)
        status_layout.setSpacing(8)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(7, 7)
        status_layout.addWidget(self.status_dot)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("SidebarFooter")
        status_layout.addWidget(self.status_label, 1)
        self.status_right = QLabel("")
        self.status_right.setObjectName("SidebarFooter")
        status_layout.addWidget(self.status_right)
        right_layout.addWidget(status)

        # the sidebar reads page classes; build it after the pages exist
        layout.addWidget(self._build_sidebar())
        layout.addWidget(right, 1)

        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.ctx.reload)
        for index in range(len(self.pages)):
            QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self,
                      activated=lambda i=index: self._select_page(i))

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(3)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.setContentsMargins(4, 0, 0, 0)
        glyph = QLabel()
        glyph.setPixmap(icons.brand_pixmap(30))
        brand.addWidget(glyph)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        name = QLabel("Blade Shape")
        name.setObjectName("Brand")
        subtitle = QLabel("Active Learning Console")
        subtitle.setObjectName("BrandSub")
        titles.addWidget(name)
        titles.addWidget(subtitle)
        brand.addLayout(titles)
        brand.addStretch(1)
        layout.addLayout(brand)
        layout.addSpacing(10)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        section = None
        for index, page_class in enumerate(PAGE_CLASSES):
            current = getattr(page_class, "nav_section", "")
            if current and current != section:
                header = QLabel(current)
                header.setObjectName("NavSection")
                layout.addWidget(header)
                section = current
            shortcut = QKeySequence(f"Ctrl+{index + 1}").toString(QKeySequence.NativeText)
            button = NavButton(page_class.nav_label, page_class.nav_icon, shortcut)
            button.clicked.connect(lambda _=False, i=index: self._select_page(i))
            self.nav_group.addButton(button, index)
            layout.addWidget(button)
        layout.addStretch(1)

        stats = QFrame()
        stats.setObjectName("SidebarStats")
        grid = QGridLayout(stats)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self._stat_values: dict[str, QLabel] = {}
        for position, (key, label, tint) in enumerate((
            ("attempts", "尝试", "text"), ("successes", "成功", "good"),
            ("pareto", "Pareto", "accent"), ("failures", "失败", "bad"),
        )):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            value = QLabel("—")
            value.setObjectName("StatsValue")
            value.setStyleSheet(f"color: {theme.PALETTE[tint]};")
            caption = QLabel(label)
            caption.setObjectName("StatsLabel")
            cell.addWidget(value)
            cell.addWidget(caption)
            grid.addLayout(cell, position // 2, position % 2)
            self._stat_values[key] = value
        layout.addWidget(stats)
        # kept for callers that read the plain-text summary
        self.sidebar_stats = QLabel("—")
        self.sidebar_stats.setVisible(False)
        layout.addWidget(self.sidebar_stats)
        layout.addSpacing(6)

        self.auto_button = tool_button("自动刷新 · 15 s", "clock", "ghost")
        self.auto_button.setCheckable(True)
        self.auto_button.clicked.connect(self._toggle_auto)
        layout.addWidget(self.auto_button)

        refresh = tool_button("刷新数据", "refresh", "ghost", "重新读取配置与结果（Ctrl+R）")
        refresh.clicked.connect(self.ctx.reload)
        layout.addWidget(refresh)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.config_button = tool_button("配置…", "file", "ghost", "切换配置文件")
        self.config_button.clicked.connect(self._choose_config)
        row.addWidget(self.config_button)
        data_button = tool_button("数据目录…", "folder", "ghost", "设置只读数据目录（等价于 --data-dir）")
        data_button.clicked.connect(self._choose_data_dir)
        row.addWidget(data_button)
        layout.addLayout(row)

        layout.addSpacing(6)
        version = QLabel(f"GUI {__version__} · CLI 后端不变")
        version.setObjectName("SidebarFooter")
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version)
        return sidebar

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(68)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 8, 22, 8)
        layout.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.page_title = QLabel("总览看板")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel("")
        self.page_subtitle.setObjectName("PageSubtitle")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)
        layout.addLayout(titles)
        layout.addStretch(1)

        self.busy_chip = Chip("play")
        self.busy_chip.set_text("任务运行中", tint="blue")
        self.busy_chip.setVisible(False)
        layout.addWidget(self.busy_chip)
        self.override_chip = Chip("alert")
        self.override_chip.setVisible(False)
        layout.addWidget(self.override_chip)
        self.config_chip = Chip("file")
        layout.addWidget(self.config_chip)
        self.data_chip = Chip("folder")
        layout.addWidget(self.data_chip)
        # legacy handle: callers/tests may read the plain output-dir text
        self.data_pill = self.data_chip.text
        return bar

    # ------------------------------------------------------------ routing
    def _select_page(self, index: int) -> None:
        if not 0 <= index < len(self.pages):
            return
        self.stack.setCurrentIndex(index)
        page = self.pages[index]
        self.page_title.setText(page.title)
        self.page_subtitle.setText(page.subtitle)
        for position, button in enumerate(self.nav_group.buttons()):
            if isinstance(button, NavButton):
                button.set_active(position == index)
        page.on_show()
        page.refresh()

    def _on_case_requested(self, run_id: str) -> None:
        for index, page in enumerate(self.pages):
            if hasattr(page, "select_run"):
                self._select_page(index)
                page.select_run(run_id)
                return

    # ------------------------------------------------------------- state
    def _on_project_reloaded(self) -> None:
        project = self.ctx.project
        for page in self.pages:
            page.refresh()
        summary = project.summary()
        values = {"attempts": summary.attempts, "successes": summary.successes,
                  "pareto": summary.pareto_count, "failures": summary.failures}
        for key, value in values.items():
            self._stat_values[key].setText(str(value))
        self.sidebar_stats.setText(
            f"尝试 {summary.attempts} · 成功 {summary.successes}\n"
            f"Pareto {summary.pareto_count} · 失败 {summary.failures}"
        )
        self.data_chip.set_text(f"输出 {project.output_dir}", f"数据目录：{project.output_dir}")
        valid = project.config_valid
        self.config_chip.set_text(
            project.config_path.name + ("" if valid else " · 有错误"),
            f"配置文件：{project.config_path}",
            "text_dim" if valid else "bad",
        )
        override = project.data_dir_override is not None
        self.override_chip.setVisible(override)
        if override:
            self.override_chip.set_text("只读数据视图", "运行命令仍写入配置中的 paths.output_dir", "amber")
        self.status_right.setText(
            f"配置 {project.config_path.name} · {'' if valid else '存在校验错误 · '}"
            f"记录 {summary.attempts} 条"
        )

    def _sync_busy(self) -> None:
        busy = any(runner.running for runner in self.ctx.command_runners)
        self.busy_chip.setVisible(busy)
        tint = theme.PALETTE["blue"] if busy else theme.PALETTE["good"]
        self.status_dot.setStyleSheet(f"background: {tint}; border-radius: 3px;")

    def _on_status(self, message: str) -> None:
        self.status_label.setText(message)

    def _toggle_auto(self, checked: bool) -> None:
        if checked:
            self._auto_timer.start()
            self.ctx.report("已开启自动刷新（每 15 秒）。")
        else:
            self._auto_timer.stop()
            self.ctx.report("已关闭自动刷新。")

    def _auto_refresh(self) -> None:
        if getattr(self, "auto_button", None) is not None and self.auto_button.isChecked():
            self.ctx.reload()

    # ----------------------------------------------------------- actions
    def _choose_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择配置文件", str(self.ctx.project.config_path.parent), "JSON (*.json)"
        )
        if not path or Path(path).resolve() == self.ctx.project.config_path.resolve():
            return
        config_page = next(page for page in self.pages if isinstance(page, ConfigPage))
        if not config_page.confirm_discard_changes("切换配置文件"):
            return
        self.ctx.set_config_path(path)
        self.ctx.report(f"已切换配置文件：{path}")

    def _choose_data_dir(self) -> None:
        start = str(self.ctx.project.output_dir)
        if not Path(start).exists():
            start = str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "选择数据目录（包含 training_data.csv）", start)
        if not directory:
            return
        answer = QMessageBox.question(
            self,
            "切换数据目录",
            f"将只读数据目录切换为：\n{directory}\n\n"
            "该目录应包含 training_data.csv / pareto_front.csv 等文件。\n"
            "选择“否”可恢复为配置文件中的 paths.output_dir。",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Cancel:
            return
        self.ctx.set_data_dir(directory if answer == QMessageBox.Yes else None)
