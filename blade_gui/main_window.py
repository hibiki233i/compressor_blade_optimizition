"""Application shell: sidebar navigation, top bar and status bar."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme
from .context import AppContext
from .pages import PAGE_CLASSES
from .pages.config_page import ConfigPage
from .widgets import tool_button

SIDEBAR_WIDTH = 212


class NavButton(QPushButton):
    def __init__(self, label: str, icon_name: str, parent: QWidget | None = None):
        super().__init__(f"  {label}", parent)
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(38)
        self.setIcon(icons.icon(icon_name, theme.PALETTE["text_dim"], 18))
        self._icon_name = icon_name

    def set_active(self, active: bool) -> None:
        super().setChecked(active)
        color = theme.PALETTE["accent"] if active else theme.PALETTE["text_dim"]
        self.setIcon(icons.icon(self._icon_name, color, 18))


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Blade Shape · 主动学习控制台")
        self.setMinimumSize(1180, 760)
        self.resize(1440, 900)
        self.setWindowIcon(icons.icon("target", theme.PALETTE["accent"], 64))

        self._build()
        self.ctx.project_reloaded.connect(self._on_project_reloaded)
        self.ctx.status_message.connect(self._on_status)
        self.ctx.case_requested.connect(self._on_case_requested)

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(15000)
        self._auto_timer.timeout.connect(self._auto_refresh)

        self._on_project_reloaded()
        self._select_page(0)

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

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
        layout.addWidget(right, 1)

        status = QFrame()
        status.setObjectName("StatusBar")
        status.setFixedHeight(26)
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(14, 0, 14, 0)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("SidebarFooter")
        status_layout.addWidget(self.status_label, 1)
        self.status_right = QLabel("")
        self.status_right.setObjectName("SidebarFooter")
        status_layout.addWidget(self.status_right)
        right_layout.addWidget(status)

        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.ctx.reload)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self._select_page(0))
        QShortcut(QKeySequence("Ctrl+2"), self, activated=lambda: self._select_page(1))
        QShortcut(QKeySequence("Ctrl+3"), self, activated=lambda: self._select_page(2))
        QShortcut(QKeySequence("Ctrl+4"), self, activated=lambda: self._select_page(3))
        QShortcut(QKeySequence("Ctrl+5"), self, activated=lambda: self._select_page(4))

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(6)

        brand = QHBoxLayout()
        brand.setSpacing(9)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("target", theme.PALETTE["accent"], 26))
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
        layout.addSpacing(14)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, page_class in enumerate(PAGE_CLASSES):
            button = NavButton(page_class.nav_label, page_class.nav_icon)
            button.clicked.connect(lambda _=False, i=index: self._select_page(i))
            self.nav_group.addButton(button, index)
            layout.addWidget(button)
        layout.addStretch(1)

        self.sidebar_stats = QLabel("—")
        self.sidebar_stats.setObjectName("SidebarFooter")
        self.sidebar_stats.setWordWrap(True)
        layout.addWidget(self.sidebar_stats)

        self.auto_button = tool_button("自动刷新 (15s)", "clock", "ghost")
        self.auto_button.setCheckable(True)
        self.auto_button.clicked.connect(self._toggle_auto)
        layout.addWidget(self.auto_button)

        refresh = tool_button("刷新数据  Ctrl+R", "refresh")
        refresh.clicked.connect(self.ctx.reload)
        layout.addWidget(refresh)

        self.config_button = tool_button("切换配置文件…", "file")
        self.config_button.clicked.connect(self._choose_config)
        layout.addWidget(self.config_button)

        data_button = tool_button("设置数据目录…", "folder")
        data_button.clicked.connect(self._choose_data_dir)
        layout.addWidget(data_button)

        layout.addSpacing(6)
        version = QLabel("GUI 1.0 · CLI 后端不变")
        version.setObjectName("SidebarFooter")
        layout.addWidget(version)
        return sidebar

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(64)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(22, 8, 22, 8)
        layout.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.page_title = QLabel("总览看板")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel("")
        self.page_subtitle.setObjectName("PageSubtitle")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)
        layout.addLayout(titles)
        layout.addStretch(1)

        self.data_pill = QLabel("")
        self.data_pill.setObjectName("CardHint")
        self.data_pill.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.data_pill)
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
        self.sidebar_stats.setText(
            f"尝试 {summary.attempts} · 成功 {summary.successes}\n"
            f"Pareto {summary.pareto_count} · 失败 {summary.failures}"
        )
        self.data_pill.setText(f"输出目录：{project.output_dir}")
        self.data_pill.setToolTip(str(project.config_path))
        self.status_right.setText(
            f"配置 {project.config_path.name} · {'' if project.config_valid else '存在校验错误 · '}"
            f"记录 {summary.attempts} 条"
        )

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
