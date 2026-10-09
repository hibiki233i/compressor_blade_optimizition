"""Incidence validation: a form frontend to the validation CLI plus a result viewer.

Every action runs ``blade_shape_incidence_validation.py`` as a child process;
the right-hand viewer only reads the files that CLI writes.
"""
from __future__ import annotations

import json
import math
import time
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..commands import VALIDATION_OPTIONS, build_validation
from ..persist import remember
from ..project import CODE_DIR, Project
from ..runner import CommandRunner, default_python
from ..validation_data import case_validation_inputs, suggest_new_path
from ..widgets import (Badge, Card, MessageBar, danger_button, fit_stack_to_current, form_label,
                       primary_button, tool_button)
from .base import Page
from .incidence_view import IncidenceView

ACTIONS = [('init', '准备验证配置（可用字段预填）'), ('extract', '提取展向攻角'),
           ('sweep', '截面 / 分带敏感性'), ('aca', '导出 ACA 20 点 CSV'), ('legacy', '复现报告20点指标'),
           ('compare', '基准 / 目标工况对比'), ('plan', '生成进口角敏感性方案'),
           ('run', '运行敏感性 CFD')]
LABELS = {'res': '目标 .res', 'geometry_source': '几何来源 .cft / .cft-batch / candidate.json',
          'output': '新输出文件', 'spec': '验证配置 JSON', 'post_exe': 'CFX-Post 程序',
          'output_dir': '新输出目录', 'stations': '前缘上游截面', 'bands': '展向分带数',
          'session': '保存的 ACA session (.cse)', 'csv': '原始 ACA CSV', 'hub_beta_deg': 'hub 前缘金属角 (°)',
          'shroud_beta_deg': 'shroud 前缘金属角 (°)', 'baseline': '基准 summary.json',
          'target': '目标 summary.json', 'flow_tolerance': '流量相对差容差',
          'config': '目标工程配置 JSON', 'candidate': 'candidate.json（可选）',
          'step_deg': '进口角扰动 (°)', 'pressures_pa': '背压序列 Pa（可选）',
          'plan': '冻结方案 plan.json', 'max_new_cfd': '本次最多新增 CFD 点数', 'resume': '续跑尚未尝试的点'}
#: what each action does, and how much it touches (shown above the form)
DESCRIPTIONS = {
    'init': ('写新文件', 'info', '登记 .res 与几何来源，生成待核对的验证配置 JSON；只预填有文件依据的角度和叶片数，'
             '不读取二进制 .res，也不会把几何标记为已确认。'),
    'extract': ('只读后处理', 'good', '用 CFX-Post 在前缘上游 Blade Aligned 截面按等宽叶高带积分，'
                '得到正向质量加权的来流角 β_f、线性叶片角 β_b 与几何攻角 i = β_b − β_f。'),
    'sweep': ('只读后处理', 'good', '对同一 .res 依次改变截面位置与分带数，检验攻角统计量对测量定义的敏感性；失败即停。'),
    'aca': ('只读后处理', 'good', '用保存的 Turbo 展向测量线 session 让 CFX-Post 读取已有 .res，导出 20 点面积周向平均 '
            'Velocity Beta ACA，检查点数、j/19 叶高与单位后整理为 span,beta_cfx_deg 两列 CSV；成功后自动填入「复现报告20点指标」。'),
    'legacy': ('写新目录', 'info', '用原始 20 点 Velocity Beta ACA 曲线复现报告的算术平均攻角，仅限已确认的 [-90°, 0°] 象限。'),
    'compare': ('写新文件', 'info', '比较两个同方法、同测量定义的提取结果；检查声明工况与整轮净流量差，给出能否作同工况诊断。'),
    'plan': ('写新目录', 'info', '冻结 hub/shroud 前缘角 ±step 的五点设计（可选背压扫描）及全部输入身份；不启动 CFD。'),
    'run': ('真实 CFD', 'bad', '按冻结方案调用原 CFD 链，结果写入方案的独立目录；失败或中断的点不会自动重算。'),
}
PATH_FILTERS = {
    'res': 'CFX 结果 (*.res);;所有文件 (*)',
    'geometry_source': '几何来源 (*.cft *.cft-batch *.json);;所有文件 (*)',
    'candidate': 'candidate.json (*.json)',
    'spec': '验证配置 (*.json)',
    'post_exe': '程序 (*.exe cfx5post*);;所有文件 (*)',
    'session': 'CFX-Post session (*.cse);;所有文件 (*)',
    'csv': 'CSV (*.csv)',
    'baseline': 'summary.json (summary.json *.json)',
    'target': 'summary.json (summary.json *.json)',
    'config': '配置 (*.json)',
    'plan': 'plan.json (plan.json *.json)',
}
NEW_DIR_STEMS = {'extract': 'extract', 'sweep': 'measurement_sweep', 'aca': 'aca', 'legacy': 'legacy',
                 'plan': 'endpoint_study'}


def _friendly(exc: Exception) -> str:
    """Name the form field instead of the CLI option in "请填写 <key>" errors."""
    message = str(exc)
    if message.startswith('请填写 '):
        key = message.removeprefix('请填写 ')
        return f'请填写「{LABELS.get(key, key)}」'
    return message


class ValidationPage(Page):
    title = '验证'
    nav_label = '验证'
    nav_icon = 'angle'
    nav_section = '检查与验证'
    subtitle = '展向几何攻角诊断、测量敏感性和工况对照；真实 CFD 使用独立目录与显式预算'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.runner = CommandRunner(CODE_DIR, self)
        ctx.command_runners.append(self.runner)
        self._started_at = 0.0
        self._running_action = ''
        self._build()
        self.runner.line.connect(lambda _channel, text: self._append(text))
        self.runner.started.connect(lambda _argv: self.set_running(True, '运行中'))
        self.runner.finished.connect(self.finished)
        self.runner.failed.connect(lambda text: self.set_running(False, '启动失败：' + text))
        self.refresh()
        self._remember_inputs()

    def _remember_inputs(self) -> None:
        """Every field starts empty (or at its neutral default) and keeps what was typed last."""
        remember(self.ctx, 'validation/action', self.action_box)
        remember(self.ctx, 'validation/splitter', self._splitter)
        for action, fields in self.forms.items():
            for key, widget in fields.items():
                remember(self.ctx, f'validation/{action}/{key}', widget)

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(10)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)
        self._splitter = splitter

        # ================================================ left: command form
        column = QWidget()
        left = QVBoxLayout(column)
        left.setContentsMargins(0, 0, 8, 0)
        left.setSpacing(12)

        action_card = Card('动作', '全部以子进程调用 blade_shape_incidence_validation.py')
        self.action_box = QComboBox()
        for key, label in ACTIONS:
            self.action_box.addItem(label, key)
        action_card.body.addWidget(self.action_box)
        info = QHBoxLayout()
        info.setSpacing(8)
        self.effect_badge = Badge('—', 'muted')
        info.addWidget(self.effect_badge, 0, Qt.AlignTop)
        self.note = QLabel()
        self.note.setObjectName('CardHint')
        self.note.setWordWrap(True)
        info.addWidget(self.note, 1)
        action_card.body.addLayout(info)
        left.addWidget(action_card)

        params = Card('参数', '路径可手填或浏览；新输出文件/目录必须尚不存在')
        self.stack = QStackedWidget()
        self.forms: dict[str, dict[str, QWidget]] = {}
        for action, _ in ACTIONS:
            self.stack.addWidget(self._build_form(action))
        params.body.addWidget(self.stack)
        left.addWidget(params)

        preview_card = Card('命令预览', '实际执行的就是这一行')
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(86)
        self.preview.setFont(theme.mono_font(11))
        preview_card.body.addWidget(self.preview)
        left.addWidget(preview_card)

        self.guard = MessageBar()
        left.addWidget(self.guard)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = primary_button('执行', 'play')
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button, 1)
        self.stop_button = danger_button('停止', 'stop')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button)
        left.addLayout(buttons)

        copy_button = tool_button('', 'copy', 'ghost', '复制命令')
        copy_button.setFixedWidth(38)
        copy_button.clicked.connect(self._copy_command)
        buttons.addWidget(copy_button)

        extras = QHBoxLayout()
        extras.setSpacing(8)
        edit = tool_button('打开验证配置', 'file', 'ghost', '用系统关联程序打开 spec JSON')
        edit.clicked.connect(self.open_spec)
        extras.addWidget(edit, 1)
        help_button = tool_button('使用说明', 'info', 'ghost', 'README_incidence_validation.md')
        help_button.clicked.connect(lambda: self.open_file(CODE_DIR / 'README_incidence_validation.md'))
        extras.addWidget(help_button, 1)
        left.addLayout(extras)

        status_row = QHBoxLayout()
        self.status = QLabel('就绪')
        self.status.setObjectName('CardHint')
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.elapsed = QLabel('')
        self.elapsed.setObjectName('CardHint')
        status_row.addWidget(self.elapsed)
        left.addLayout(status_row)
        left.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(column)
        scroll.setMinimumWidth(380)
        splitter.addWidget(scroll)

        # =============================================== right: viewer + log
        self.tabs = QTabWidget()
        self.view = IncidenceView()
        self.tabs.addTab(self.view, '攻角诊断')
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.setSpacing(8)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        self.log.setFont(theme.mono_font(11))
        self.log.setPlaceholderText('尚未执行验证命令。')
        log_layout.addWidget(self.log, 1)
        log_buttons = QHBoxLayout()
        log_buttons.addStretch(1)
        clear = tool_button('清空', 'trash', 'ghost')
        clear.clicked.connect(self.log.clear)
        log_buttons.addWidget(clear)
        log_layout.addLayout(log_buttons)
        self.tabs.addTab(log_page, '运行日志')
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 880])

        self.action_box.currentIndexChanged.connect(self.change_action)
        self.change_action(0)

    def _build_form(self, action: str) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        fields: dict[str, QWidget] = {}
        if action == 'init':
            form.addRow(form_label('从优化算例填入'), self._build_case_picker())
        for key in VALIDATION_OPTIONS[action]:
            label = LABELS[key]
            if action == 'init' and key == 'candidate':
                label = '预填角度用 candidate.json（可选）'
            if key == 'resume':
                widget = QCheckBox('--resume')
                widget.toggled.connect(self.update_preview)
                fields[key] = widget
                form.addRow(form_label(label), widget)
                continue
            if key == 'max_new_cfd':
                widget = QSpinBox()
                widget.setRange(0, 100000)
                widget.setValue(1)
                widget.valueChanged.connect(self.update_preview)
            elif key in {'hub_beta_deg', 'shroud_beta_deg', 'step_deg', 'flow_tolerance'}:
                widget = QDoubleSpinBox()
                widget.setRange(-180, 180)
                widget.setDecimals(6)
                if key == 'step_deg':
                    widget.setRange(.000001, 180)
                    widget.setValue(.25)
                elif key == 'flow_tolerance':
                    widget.setRange(0, .99)
                    widget.setSingleStep(.005)
                    widget.setValue(.01)
                widget.valueChanged.connect(self.update_preview)
            else:
                widget = QLineEdit()
                widget.textChanged.connect(self.update_preview)
                if key == 'stations':
                    widget.setText('0.20 0.22 0.24')
                    widget.setPlaceholderText('0 < 截面 < 0.25，空格分隔')
                elif key == 'bands':
                    widget.setText('20 40')
                    widget.setPlaceholderText('2–200 的整数，空格分隔')
                elif key == 'pressures_pa':
                    widget.setPlaceholderText('留空 = 配置中的 p_out_pa')
            fields[key] = widget
            if isinstance(widget, QLineEdit) and key not in {'stations', 'bands', 'pressures_pa'}:
                row = QWidget()
                layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(6)
                layout.addWidget(widget, 1)
                browse = tool_button('', 'folder', 'ghost', '浏览…')
                browse.setFixedWidth(36)
                browse.clicked.connect(lambda _=False, w=widget, k=key, a=action: self.browse(w, k, a))
                layout.addWidget(browse)
                form.addRow(form_label(label), row)
            else:
                form.addRow(form_label(label), widget)
        self.forms[action] = fields
        return page

    def _build_case_picker(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.case_box = QComboBox()
        self.case_box.setToolTip('配置输出目录中已登记的成功算例；填入 .res、candidate.json 并建议新的配置路径')
        layout.addWidget(self.case_box, 1)
        fill = tool_button('填入', 'download', 'ghost')
        fill.clicked.connect(self.fill_from_case)
        layout.addWidget(fill)
        return row

    # ------------------------------------------------------------ helpers
    def _configured_project(self) -> Project:
        """Cases for validation come from the run output, not a read-only view."""
        project = self.ctx.project
        return project if project.data_dir_override is None else Project(project.config_path)

    def _sync_cases(self) -> None:
        current = self.case_box.currentData()
        # the visible data view: its case folders are the ones present locally
        records = [record for record in self.ctx.project.cases() if record.ok]
        blocked = self.case_box.blockSignals(True)
        self.case_box.clear()
        if not records:
            self.case_box.addItem('（没有成功算例）', '')
        for record in records:
            text = (f'{record.run_id}  ·  η {record.efficiency:.4f}' if math.isfinite(record.efficiency)
                    else record.run_id)
            self.case_box.addItem(text, str(record.path))
        index = self.case_box.findData(current)
        if index >= 0:
            self.case_box.setCurrentIndex(index)
        self.case_box.blockSignals(blocked)

    def fill_from_case(self) -> None:
        case_dir = self.case_box.currentData()
        if not case_dir:
            self.status.setText('没有可用的成功算例。')
            return
        found = case_validation_inputs(case_dir)
        fields = self.forms['init']
        for key in ('res', 'geometry_source', 'candidate'):
            if found[key]:
                fields[key].setText(found[key])
        if not fields['output'].text().strip():
            root = Path(self._configured_project().output_dir).parent / 'incidence_validation' / Path(case_dir).name
            fields['output'].setText(str(root / 'spec.json'))
        notes = found['notes'] or ['已填入；仍须核对几何对应关系、角度约定与实际工况。']
        self.guard.set_level('warn' if found['notes'] else 'info', '；'.join(notes))

    def browse(self, widget: QLineEdit, key: str, action: str = '') -> None:
        current = widget.text().strip()
        start = self.ctx.dialog_start(current)
        if key == 'output_dir':
            parent = QFileDialog.getExistingDirectory(self, '选择父目录（将在其中新建输出子目录）',
                                                      str(Path(start).parent) if current else start)
            if parent:
                self.ctx.remember_dialog(parent, is_dir=True)
                widget.setText(suggest_new_path(parent, NEW_DIR_STEMS.get(action, 'validation')))
            return
        if key == 'output':
            suffix_name = 'comparison.json' if action == 'compare' else 'spec.json'
            path, _ = QFileDialog.getSaveFileName(self, '新输出文件', current or str(Path(start or '.') / suffix_name),
                                                  'JSON (*.json)')
        else:
            path, _ = QFileDialog.getOpenFileName(self, LABELS[key], start, PATH_FILTERS.get(key, '所有文件 (*)'))
        if path:
            self.ctx.remember_dialog(path)
            widget.setText(path)

    def open_file(self, path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def open_spec(self) -> None:
        fields = self.forms[self.action_box.currentData()]
        widget = fields.get('spec') or (fields.get('output') if self.action_box.currentData() == 'init' else None)
        if widget and Path(widget.text()).is_file():
            self.open_file(widget.text())
        else:
            QMessageBox.information(self, '验证配置', '请选择已有验证配置，或先创建配置文件。')

    def _copy_command(self) -> None:
        QApplication.clipboard().setText(self.preview.toPlainText())
        self.ctx.report('验证命令已复制到剪贴板。')

    # ------------------------------------------------------------ preview
    def change_action(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        fit_stack_to_current(self.stack)
        effect, kind, text = DESCRIPTIONS[self.action_box.itemData(index)]
        self.effect_badge.setText(effect)
        self.effect_badge.set_kind(kind)
        self.note.setText(text)
        self.update_preview()

    def build_spec(self):
        action = self.action_box.currentData()
        values = {}
        for key, widget in self.forms[action].items():
            if isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                values[key] = widget.value()
            else:
                values[key] = widget.text().strip()
        return build_validation(action, **values)

    def update_preview(self, *args) -> None:
        if not hasattr(self, 'preview'):
            return
        try:
            self.preview.setPlainText(self.build_spec().preview(default_python()))
        except ValueError as exc:
            self.preview.setPlainText(_friendly(exc))
        self._refresh_guard()

    def _refresh_guard(self) -> None:
        if not hasattr(self, 'guard') or self.runner.running:
            return
        action = self.action_box.currentData()
        fields = self.forms[action]
        output = fields.get('output_dir') or fields.get('output')
        if action == 'run':
            self.guard.set_level('warn', '会启动 CFturbo / TurboGrid / CFX 真实计算；预算是设计点数，'
                                         '每点最多含一次受控追加求解。')
        elif output is not None and output.text().strip() and Path(output.text().strip()).exists() \
                and action != 'init':
            self.guard.set_level('error', f'输出已存在：{output.text().strip()}。CLI 拒绝覆盖，请换一个新路径。')
        elif action in {'extract', 'sweep', 'aca'} and fields['post_exe'].text().strip() \
                and not Path(fields['post_exe'].text().strip()).is_file():
            self.guard.set_level('info', '本机找不到 CFX-Post；提取须在装有 ANSYS 的 Windows 主机上运行。')
        elif action == 'init':
            self.guard.set_level('info', '预填值只是候选；提取前须把 geometry_verified 等字段在 JSON 中人工确认。')
        else:
            self.guard.set_level('', '')

    def refresh(self) -> None:
        # suggestions only: paths stay empty until typed or browsed, then are remembered
        config = self.ctx.project.config_path
        self.forms['plan']['config'].setPlaceholderText(f'例如当前配置 {config}' if config else '')
        paths = self.ctx.project.config.get('paths')
        paths = paths if isinstance(paths, dict) else {}
        hint = f"例如 {Path(paths['cfx_bin_dir']) / 'cfx5post.exe'}" if paths.get('cfx_bin_dir') else ''
        for action in ('extract', 'sweep', 'aca'):
            self.forms[action]['post_exe'].setPlaceholderText(hint)
        self._sync_cases()
        self.update_preview()

    # -------------------------------------------------------------- start
    def start(self) -> None:
        if any(r.running for r in self.ctx.command_runners):
            self.status.setText('已有运行或验证任务正在执行，请等待完成。')
            return
        try:
            spec = self.build_spec()
        except ValueError as exc:
            QMessageBox.warning(self, '参数未完成', _friendly(exc))
            return
        action = self.action_box.currentData()
        if action == 'run':
            if QMessageBox.question(self, '启动真实 CFD', '将按冻结方案和本次预算运行真实 CFD，结果写入方案的独立目录。继续？',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        self._running_action = action
        self.log.clear()
        self._append('$ ' + spec.preview(default_python()), 'info')
        self.tabs.setCurrentIndex(1)
        self.runner.start(spec.argv(default_python()))

    def stop(self) -> None:
        self._append('正在停止入口进程。外部求解器可能仍在运行，请检查后再恢复。', 'warn')
        self.runner.stop()

    def set_running(self, running: bool, message: str) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.action_box.setEnabled(not running)
        self.stack.setEnabled(not running)
        self.status.setText(message)
        if running:
            self._started_at = time.time()
            self.elapsed.setText('')
            self.guard.set_level('info', '任务运行中；完成后会自动载入输出。')
        elif self._started_at:
            seconds = int(time.time() - self._started_at)
            self.elapsed.setText(f'{seconds // 60:02d}:{seconds % 60:02d}')

    def finished(self, code: int, reason: str) -> None:
        action = self._running_action or self.action_box.currentData()
        aca_csv = self._aca_csv() if action == 'aca' and code == 0 and reason == 'normal' else ''
        if code == 0 and reason == 'normal' and action == 'init':
            message = '已保存待核对配置；请打开 JSON 补齐日志列出的字段后再提取'
        elif aca_csv:
            self.forms['legacy']['csv'].setText(aca_csv)
            message = 'ACA CSV 已导出并填入「复现报告20点指标」；仍须填写同一 .res 几何的 hub/shroud 前缘金属角'
        elif code == 0 and reason == 'normal':
            message = '完成'
        elif code == 2:
            message = '质量或工况检查未通过，请查看诊断与日志'
        else:
            message = f'执行失败：{code} / {reason}'
        self.set_running(False, message)
        self._append(f'进程结束：exit={code} reason={reason}', 'good' if code == 0 else 'error')
        self.ctx.report('验证：' + message)
        self._load_output(action, code)
        self._refresh_guard()

    def _aca_csv(self) -> str:
        """The two-column CSV recorded by a completed ``aca`` run, if any."""
        summary = Path(self.forms['aca']['output_dir'].text().strip()) / 'extraction_summary.json'
        try:
            data = json.loads(summary.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return ''
        path = data.get('aca_csv') if isinstance(data, dict) and data.get('status') == 'complete' else None
        if not path or not Path(path).is_file():
            return ''
        if data.get('legacy_aca_quadrant_ok') is False:
            self._append('ACA 角度不全在 [-90°, 0°]：CSV 已保存，但旧 20 点指标会拒绝该象限。', 'warn')
        return str(path)

    def _load_output(self, action: str, code: int) -> None:
        """Show what the finished command wrote (also failures that kept evidence)."""
        fields = self.forms.get(action, {})
        target = ''
        if action in {'extract', 'sweep', 'legacy', 'plan'}:
            target = fields['output_dir'].text().strip()
        elif action in {'init', 'compare'}:
            target = fields['output'].text().strip()
        elif action == 'run':
            target = str(Path(fields['plan'].text().strip()).parent)
        if target and Path(target).exists() and self.view.load(target):
            self.tabs.setCurrentIndex(0)

    # --------------------------------------------------------------- log
    def _append(self, text: str, level: str = 'auto') -> None:
        if level == 'auto':
            lowered = text.lower()
            if any(hint in lowered for hint in ('traceback', 'error', 'failed', '失败')):
                level = 'error'
            elif any(hint in lowered for hint in ('warning', 'still requires', 'prefill warning')):
                level = 'warn'
            else:
                level = 'plain'
        colors = {'info': 'blue', 'good': 'good', 'warn': 'warn', 'error': 'bad', 'plain': 'text'}
        stamp = time.strftime('%H:%M:%S')
        self.log.appendHtml(
            f'<span style="color:{theme.PALETTE["text_faint"]}">{stamp}</span> '
            f'<span style="color:{theme.PALETTE[colors.get(level, "text")]}">{escape(text)}</span>'
        )
