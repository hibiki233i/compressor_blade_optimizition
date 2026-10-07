"""Run control: build a CLI command, stream its output, stop it if needed."""
from __future__ import annotations

import time
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import commands, theme
from ..project import CODE_DIR, Project, config_errors, external_tools
from ..runner import CommandRunner, default_python
from ..widgets import (
    Badge,
    Card,
    MessageBar,
    clear_layout,
    danger_button,
    fit_stack_to_current,
    primary_button,
    tool_button,
)
from .base import Page

ACTIONS = [
    ("run", "运行主动学习"),
    ("write-candidate", "生成候选（可 dry-run）"),
    ("diagnose", "离线诊断"),
    ("write-boundary-plan", "生成边界方案"),
    ("run-boundary", "运行边界阶段"),
]

ERROR_HINTS = ("error", "traceback", "failed", "failure", "exception", "错误", "失败")
WARN_HINTS = ("warning", "warn", "deprecat", "注意")
GOOD_HINTS = ("done", "success", "完成", "ok")


class RunPage(Page):
    title = "运行控制"
    subtitle = "以子进程方式调用原始 CLI，实时回显日志"
    nav_label = "运行控制"
    nav_icon = "play"
    nav_section = "工作流"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent)
        self._started_at = 0.0
        self._params_initialized = False
        self._run_project: Project | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._build()
        self.refresh()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(11)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # =============================================== left: command form
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(12)

        action_card = Card("动作", "所有动作都会启动独立的 Python 子进程")
        self.action_box = QComboBox()
        for key, label in ACTIONS:
            self.action_box.addItem(label, key)
        self.action_box.currentIndexChanged.connect(self._on_action_changed)
        action_card.body.addWidget(self.action_box)
        left_layout.addWidget(action_card)

        params_card = Card("参数", "留空则沿用 blade_shape_config.json 中的取值")
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_run_form())
        self.stack.addWidget(self._build_candidate_form())
        self.stack.addWidget(self._build_diagnose_form())
        self.stack.addWidget(self._build_plan_form())
        self.stack.addWidget(self._build_boundary_form())
        params_card.body.addWidget(self.stack)
        left_layout.addWidget(params_card)

        preview_card = Card("命令预览", "实际执行的就是这一行")
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(76)
        mono = theme.mono_font(11)
        self.preview.setFont(mono)
        preview_card.body.addWidget(self.preview)
        copy_row = QHBoxLayout()
        copy_row.addStretch(1)
        copy_button = tool_button("复制命令", "file")
        copy_button.clicked.connect(self._copy_command)
        copy_row.addWidget(copy_button)
        preview_card.body.addLayout(copy_row)
        left_layout.addWidget(preview_card)

        self.guard = MessageBar()
        left_layout.addWidget(self.guard)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = primary_button("开始运行", "play")
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button, 1)
        self.stop_button = danger_button("停止", "stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.stop_button)
        reset_button = tool_button("重置参数", "refresh", "ghost", "重新读取配置中的运行预算")
        reset_button.clicked.connect(self.apply_config_defaults)
        buttons.addWidget(reset_button)
        left_layout.addLayout(buttons)
        left_layout.addStretch(1)
        splitter.addWidget(left)

        # ============================================== right: status + log
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(12)

        env_card = Card("本机环境检查", "CFD 全链路需要 Windows + 已安装的 CFturbo/ANSYS")
        self.env_body = QVBoxLayout()
        self.env_body.setSpacing(4)
        env_card.body.addLayout(self.env_body)
        right_layout.addWidget(env_card)

        status_card = Card("运行状态", "")
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status_badge = Badge("空闲", "muted")
        status_row.addWidget(self.status_badge)
        self.status_text = QLabel("等待启动")
        self.status_text.setObjectName("CardHint")
        status_row.addWidget(self.status_text, 1)
        self.elapsed = QLabel("00:00")
        self.elapsed.setObjectName("CardHint")
        status_row.addWidget(self.elapsed)
        status_card.body.addLayout(status_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        status_card.body.addWidget(self.progress)
        right_layout.addWidget(status_card)

        log_card = Card("实时日志", "stdout 与 stderr 合并输出")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        self.log.setFont(mono)
        self.log.setPlaceholderText("尚未运行任何命令。")
        log_card.body.addWidget(self.log, 1)
        log_buttons = QHBoxLayout()
        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        log_buttons.addWidget(self.autoscroll)
        log_buttons.addStretch(1)
        clear_button = tool_button("清空", "trash")
        clear_button.clicked.connect(self.log.clear)
        log_buttons.addWidget(clear_button)
        save_button = tool_button("保存日志", "save")
        save_button.clicked.connect(self._save_log)
        log_buttons.addWidget(save_button)
        open_button = tool_button("打开输出目录", "folder")
        open_button.clicked.connect(self._open_output)
        log_buttons.addWidget(open_button)
        log_card.body.addLayout(log_buttons)
        right_layout.addWidget(log_card, 1)
        splitter.addWidget(right)
        splitter.setSizes([520, 620])

        # ---- runner
        self.runner = CommandRunner(CODE_DIR, self)
        self.ctx.command_runners.append(self.runner)
        self.runner.started.connect(self._on_started)
        self.runner.line.connect(self._on_line)
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)

        self._on_action_changed()
        for widget in self._parameter_widgets():
            self._connect_dirty(widget)

    # ------------------------------------------------------- action forms
    def _spin(self, minimum: int, maximum: int, value: int = 0) -> QSpinBox:
        box = QSpinBox()
        box.setRange(minimum, maximum)
        box.setValue(value)
        return box

    def _build_run_form(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(7)
        self.run_initial = self._spin(0, 100000)
        self.run_iterations = self._spin(0, 100000)
        self.run_batch = self._spin(1, 512, 1)
        self.run_max = self._spin(0, 100000)
        self.run_seed = self._spin(0, 2**31 - 1)
        self.run_resume = QCheckBox("--resume（沿用已有 CSV 与 case 编号）")
        form.addRow("初始 DOE 样本", self.run_initial)
        form.addRow("主动学习迭代", self.run_iterations)
        form.addRow("每批点数", self.run_batch)
        form.addRow("最大新增 CFD", self.run_max)
        form.addRow("随机种子", self.run_seed)
        form.addRow("", self.run_resume)
        return page

    def _build_candidate_form(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(7)
        self.cand_index = self._spin(0, 100000, 0)
        self.cand_seed = self._spin(0, 2**31 - 1)
        self.cand_dry = QCheckBox("--dry-run（调用 PowerShell 生成几何，不启动 CFturbo）")
        self.cand_offline = QCheckBox("--offline（仅校验 Python 规则，跳过 PowerShell）")
        self.cand_offline.setChecked(True)
        form.addRow("候选序号", self.cand_index)
        form.addRow("随机种子", self.cand_seed)
        form.addRow("", self.cand_dry)
        form.addRow("", self.cand_offline)
        return page

    def _build_diagnose_form(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel(
            "离线诊断不启动任何 CFD：会写出 training_slice_audit.csv、role_diagnostics.csv "
            "与 local_diagnostic_gate.json。"
        )
        note.setWordWrap(True)
        note.setObjectName("CardHint")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _build_plan_form(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(7)
        self.plan_center = QComboBox()
        self.plan_center.setEditable(True)
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(6)
        holder_layout.addWidget(self.plan_center, 1)
        self.plan_path = QLineEdit("boundary_plan_v2.json")
        browse = tool_button("", "folder", "ghost", "选择保存位置")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._pick_plan_save)
        holder_layout.addWidget(self.plan_path, 1)
        holder_layout.addWidget(browse)
        form.addRow("中心算例 run_id", self.plan_center)
        form.addRow("方案文件", holder)
        note = QLabel("中心算例必须是成功且同切片的真实记录；可从下拉列表选择。")
        note.setObjectName("CardHint")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    def _build_boundary_form(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(7)
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(6)
        self.boundary_plan = QComboBox()
        self.boundary_plan.setEditable(True)
        holder_layout.addWidget(self.boundary_plan, 1)
        browse = tool_button("", "folder", "ghost", "选择方案文件")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._pick_plan_open)
        holder_layout.addWidget(browse)
        self.boundary_stage = QComboBox()
        self.boundary_stage.addItems(commands.BOUNDARY_STAGES)
        self.boundary_max = self._spin(0, 100000, 1)
        self.boundary_resume = QCheckBox("--resume")
        form.addRow("方案文件", holder)
        form.addRow("阶段", self.boundary_stage)
        form.addRow("最大新增 CFD", self.boundary_max)
        form.addRow("", self.boundary_resume)
        return page

    def _parameter_widgets(self) -> list[QWidget]:
        return [
            self.run_initial, self.run_iterations, self.run_batch, self.run_max, self.run_seed,
            self.run_resume, self.cand_index, self.cand_seed, self.cand_dry, self.cand_offline,
            self.plan_center, self.plan_path, self.boundary_stage, self.boundary_max,
            self.boundary_resume, self.boundary_plan,
        ]

    def _connect_dirty(self, widget: QWidget) -> None:
        if isinstance(widget, QSpinBox):
            widget.valueChanged.connect(lambda *_: self._update_preview())
        elif isinstance(widget, QCheckBox):
            widget.toggled.connect(lambda *_: self._update_preview())
        elif isinstance(widget, QLineEdit):
            widget.textChanged.connect(lambda *_: self._update_preview())
        elif isinstance(widget, QComboBox):
            widget.currentTextChanged.connect(lambda *_: self._update_preview())

    # ------------------------------------------------------------ refresh
    def refresh(self) -> None:
        self._run_project = None
        if not self._params_initialized:
            self.apply_config_defaults()
            self._params_initialized = True
        self._sync_center_runs()
        self._sync_plans()
        self._refresh_environment()
        self._update_preview()

    def _configured_project(self) -> Project:
        """Read the output directory used by CLI actions, not the viewing override."""
        if self.ctx.project.data_dir_override is None:
            return self.ctx.project
        if self._run_project is None:
            self._run_project = Project(self.ctx.project.config_path)
        return self._run_project

    def _existing_run_work(self) -> tuple[int, int]:
        project = self._configured_project()
        return len(project.training()), len(project.pending().get("entries", []))

    def apply_config_defaults(self) -> None:
        """Copy runtime values from the config into the form."""
        runtime = self.ctx.project.config.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}

        def number(key: str, default: int) -> int:
            # An invalid value is reported by validate_config; keep the form usable.
            try:
                return int(runtime.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        self.run_initial.setValue(number("initial_samples", 0))
        self.run_iterations.setValue(number("iterations", 0))
        self.run_batch.setValue(max(1, number("batch_size", 1)))
        self.run_max.setValue(number("max_new_cfd", 0))
        self.run_seed.setValue(number("seed", 0))
        self.ctx.report("已按 blade_shape_config.json 重置运行参数。")

    def _sync_center_runs(self) -> None:
        current = self.plan_center.currentText()
        df = self._configured_project().training()
        runs: list[str] = []
        if not df.empty and "status" in df.columns and "run_id" in df.columns:
            status = df["status"].astype(str).str.strip().str.lower()
            runs = [str(value) for value in df.loc[status == "success", "run_id"].tolist()]
        if [self.plan_center.itemText(i) for i in range(self.plan_center.count())] == runs:
            return
        blocked = self.plan_center.blockSignals(True)
        self.plan_center.clear()
        self.plan_center.addItems(runs)
        if current:
            self.plan_center.setCurrentText(current)
        self.plan_center.blockSignals(blocked)

    def _sync_plans(self) -> None:
        current = self.boundary_plan.currentText()
        plans = [str(item) for item in self._configured_project().available_plans()]
        if [self.boundary_plan.itemText(i) for i in range(self.boundary_plan.count())] != plans:
            blocked = self.boundary_plan.blockSignals(True)
            self.boundary_plan.clear()
            self.boundary_plan.addItems(plans)
            if current:
                self.boundary_plan.setCurrentText(current)
            self.boundary_plan.blockSignals(blocked)
        if not self.plan_path.text().strip() and plans:
            self.plan_path.setText(plans[0])

    def _refresh_environment(self) -> None:
        clear_layout(self.env_body)
        for label, path, exists in external_tools(self.ctx.project.config):
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            name = QLabel(label)
            name.setStyleSheet(f"color: {theme.PALETTE['text_dim']}; font-weight: 600;")
            name.setFixedWidth(96)
            layout.addWidget(name)
            value = QLabel(path or "未配置")
            value.setStyleSheet(f"color: {theme.PALETTE['text_faint']}; font-size: 11px;")
            value.setToolTip(path)
            layout.addWidget(value, 1)
            layout.addWidget(Badge("已找到" if exists else "缺失", "good" if exists else "muted"))
            self.env_body.addWidget(row)

    # ------------------------------------------------------------ preview
    def _current_action(self) -> str:
        return self.action_box.currentData()

    def _on_action_changed(self) -> None:
        index = self.action_box.currentIndex()
        self.stack.setCurrentIndex(max(0, index))
        fit_stack_to_current(self.stack)
        self._update_preview()

    def _build_spec(self) -> commands.CommandSpec:
        config_path = self.ctx.project.config_path
        action = self._current_action()
        if action == "run":
            return commands.build_run(
                config_path,
                initial_samples=self.run_initial.value(),
                iterations=self.run_iterations.value(),
                batch_size=self.run_batch.value(),
                max_new_cfd=self.run_max.value(),
                seed=self.run_seed.value(),
                resume=self.run_resume.isChecked(),
            )
        if action == "write-candidate":
            return commands.build_write_candidate(
                config_path,
                index=self.cand_index.value(),
                seed=self.cand_seed.value(),
                dry_run=self.cand_dry.isChecked(),
                offline=self.cand_offline.isChecked(),
            )
        if action == "diagnose":
            return commands.build_diagnose(config_path)
        if action == "write-boundary-plan":
            return commands.build_write_boundary_plan(
                config_path,
                center_run_id=self.plan_center.currentText().strip(),
                plan=self.plan_path.text().strip() or "boundary_plan_v2.json",
            )
        return commands.build_run_boundary(
            config_path,
            plan=self.boundary_plan.currentText().strip(),
            stage=self.boundary_stage.currentText(),
            max_new_cfd=self.boundary_max.value(),
            resume=self.boundary_resume.isChecked(),
        )

    def _update_preview(self) -> None:
        try:
            spec = self._build_spec()
            self.preview.setPlainText(spec.preview(default_python()))
        except Exception as exc:  # noqa: BLE001
            self.preview.setPlainText(f"无法生成命令：{exc}")
        self._refresh_guard()

    def _refresh_guard(self) -> None:
        project = self.ctx.project
        errors = config_errors(project.issues)
        action = self._current_action()

        if self.runner.running:
            self.guard.set_level("info", "任务运行中，完成后会自动刷新数据。")
            return
        if errors:
            self.guard.set_level("error", "配置存在错误，请先在「项目设置」修正：" + errors[0].message)
            return
        if action == "run" and not self.run_resume.isChecked():
            training_count, pending_count = self._existing_run_work()
            if training_count or pending_count:
                detail = f"已有 {training_count} 条训练记录"
                if pending_count:
                    detail += f"、{pending_count} 个待处理算例"
                self.guard.set_level(
                    "warn",
                    f"实际运行目录 {self._configured_project().output_dir} {detail}：不带 --resume 时 run 会主动退出。",
                )
                return
        if action == "write-boundary-plan" and not self.plan_center.currentText().strip():
            self.guard.set_level("warn", "请选择一个成功算例的 run_id 作为中心点。")
            return
        if action == "run-boundary" and not self.boundary_plan.currentText().strip():
            self.guard.set_level("warn", "请选择边界方案 JSON 文件。")
            return
        if action == "run-boundary":
            self.guard.set_level(
                "warn",
                "该动作会启动真实 CFD（Windows + CFturbo/ANSYS）。确认前置阶段结果后再继续。",
            )
            return
        configured = self._configured_project().output_dir
        if project.output_dir != configured:
            self.guard.set_level(
                "info",
                f"命令结果写入 {configured}；当前数据页面查看 {project.output_dir}。",
            )
            return
        self.guard.set_level("info", "参数就绪。真实 CFD 只能在装有 CFturbo/ANSYS 的 Windows 机器上运行。")

    # -------------------------------------------------------------- start
    def start(self) -> None:
        if any(runner.running for runner in self.ctx.command_runners):
            self.ctx.report("已有运行或验证任务正在执行，请等待完成。")
            return
        errors = config_errors(self.ctx.project.issues)
        if errors:
            QMessageBox.warning(self, "无法运行", "配置存在错误：\n\n" + "\n".join(f"• {i.message}" for i in errors[:6]))
            return
        action = self._current_action()
        if action in {"run", "run-boundary"}:
            message = (
                "该动作会调用 CFturbo、TurboGrid 与 CFX 执行真实 CFD 计算，可能耗时很久并占用较多资源。\n\n"
                "确认开始吗？"
            )
            if action == "run" and not self.run_resume.isChecked() and any(self._existing_run_work()):
                message = "检测到已有训练数据或待处理算例且未勾选 --resume，run 会立即退出。\n\n仍然继续吗？"
            answer = QMessageBox.question(self, "确认启动", message,
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        spec = self._build_spec()
        argv = spec.argv(default_python())
        self.log.clear()
        self._append("info", f"$ {' '.join(argv)}")
        self.progress.setVisible(True)
        self.runner.start(argv)
        if not self.runner.running:
            self.progress.setVisible(False)

    def stop(self) -> None:
        if not self.runner.running:
            return
        self._append("warn", "正在请求停止子进程…（CFD 求解器的子进程可能需要手动确认）")
        self.runner.stop()

    # ------------------------------------------------------------ signals
    def _on_started(self, argv: list[str]) -> None:
        self._started_at = time.time()
        self._timer.start()
        self.status_badge.setText("运行中")
        self.status_badge.set_kind("info")
        self.status_text.setText("命令已启动")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.progress.setVisible(True)

    def _on_line(self, _channel: str, text: str) -> None:
        self._append("auto", text)
        stripped = text.strip()
        if stripped:
            self.status_text.setText(stripped[:160])

    def _on_finished(self, exit_code: int, reason: str) -> None:
        self._timer.stop()
        self._tick()
        self.progress.setVisible(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if exit_code == 0 and reason == "normal":
            self.status_badge.setText("已完成")
            self.status_badge.set_kind("good")
            self._append("good", f"进程正常结束（exit=0），用时 {self.elapsed.text()}。")
        else:
            self.status_badge.setText("异常结束")
            self.status_badge.set_kind("bad")
            self._append("error", f"进程结束：exit={exit_code} reason={reason}")
        self.ctx.reload()
        self._refresh_guard()

    def _on_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_badge.setText("启动失败")
        self.status_badge.set_kind("bad")
        self._append("error", message)

    def _tick(self) -> None:
        if not self._started_at:
            return
        elapsed = int(time.time() - self._started_at)
        self.elapsed.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d}")

    # --------------------------------------------------------------- log
    def _append(self, level: str, text: str) -> None:
        if level == "auto":
            lowered = text.lower()
            if any(hint in lowered for hint in ERROR_HINTS):
                level = "error"
            elif any(hint in lowered for hint in WARN_HINTS):
                level = "warn"
            elif any(hint in lowered for hint in GOOD_HINTS):
                level = "good"
            else:
                level = "plain"
        colors = {
            "info": theme.PALETTE["blue"],
            "good": theme.PALETTE["good"],
            "warn": theme.PALETTE["warn"],
            "error": theme.PALETTE["bad"],
            "plain": theme.PALETTE["text"],
        }
        color = colors.get(level, theme.PALETTE["text"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{theme.PALETTE["text_faint"]}">{stamp}</span> '
            f'<span style="color:{color}">{escape(text)}</span>'
        )
        if self.autoscroll.isChecked():
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.preview.toPlainText())
        self.ctx.report("命令已复制到剪贴板。")

    def _save_log(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self, "保存日志", str(Path.home() / "blade_gui_run.log"), "Log (*.log *.txt)"
        )
        if not target:
            return
        try:
            Path(target).write_text(self.log.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.ctx.report(f"日志已保存：{target}")

    def _open_output(self) -> None:
        directory = self._configured_project().output_dir
        if not directory.exists():
            QMessageBox.information(self, "目录不存在", f"{directory} 尚未创建。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _pick_plan_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择边界方案", str(self._configured_project().output_dir), "JSON (*.json)"
        )
        if path:
            self.boundary_plan.setCurrentText(path)

    def _pick_plan_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "保存边界方案", str(self._configured_project().output_dir / "boundary_plan_v2.json"), "JSON (*.json)"
        )
        if path:
            self.plan_path.setText(path)
