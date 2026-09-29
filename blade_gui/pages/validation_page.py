"""Validation controls are a thin subprocess frontend to the validation CLI."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from .base import Page
from ..commands import VALIDATION_OPTIONS, build_validation
from ..project import CODE_DIR
from ..runner import CommandRunner, default_python

ACTIONS = [('init','准备验证配置（可用字段预填）'), ('extract','提取展向攻角'),
           ('sweep','截面 / 分带敏感性'), ('legacy','复现报告20点指标'),
           ('compare','基准 / 目标工况对比'), ('plan','生成进口角敏感性方案'),
           ('run','运行敏感性 CFD')]
LABELS = {'res':'目标 .res', 'geometry_source':'几何来源 .cft / .cft-batch / candidate.json',
          'output':'新输出文件', 'spec':'验证配置 JSON', 'post_exe':'CFX-Post 程序',
          'output_dir':'新输出目录', 'stations':'前缘上游截面', 'bands':'展向分带数',
          'csv':'原始 ACA CSV', 'hub_beta_deg':'hub 前缘金属角 (°)',
          'shroud_beta_deg':'shroud 前缘金属角 (°)', 'baseline':'基准 summary.json',
          'target':'目标 summary.json', 'flow_tolerance':'流量相对差容差',
          'config':'目标工程配置 JSON', 'candidate':'candidate.json（可选）',
          'step_deg':'进口角扰动 (°)', 'pressures_pa':'背压序列 Pa（可选）',
          'plan':'冻结方案 plan.json', 'max_new_cfd':'本次最多新增 CFD 点数', 'resume':'续跑尚未尝试的点'}


class ValidationPage(Page):
    title = '验证'
    nav_label = '验证'
    nav_icon = 'target'
    subtitle = '展向攻角、测量敏感性和工况对照；真实 CFD 使用独立目录与显式预算'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx,parent)
        self.runner=CommandRunner(CODE_DIR,self)
        ctx.command_runners.append(self.runner)
        root=QVBoxLayout(self)
        self.action_box=QComboBox()
        for key,label in ACTIONS:self.action_box.addItem(label,key)
        root.addWidget(self.action_box)
        self.note=QLabel('准备配置会从可识别的算例文件预填字段；仍须核对几何对应关系、实际工况和方向约定。RMS通过不等于完成网格/守恒验证。')
        self.note.setWordWrap(True);root.addWidget(self.note)
        self.stack=QStackedWidget();self.forms={}
        for action,_ in ACTIONS:
            content=QWidget();form=QFormLayout(content);fields={}
            for key in VALIDATION_OPTIONS[action]:
                if key=='resume':
                    widget=QCheckBox();widget.toggled.connect(self.update_preview)
                elif key=='max_new_cfd':
                    widget=QSpinBox();widget.setRange(0,100000);widget.setValue(1)
                    widget.valueChanged.connect(self.update_preview)
                elif key in {'hub_beta_deg','shroud_beta_deg','step_deg','flow_tolerance'}:
                    widget=QDoubleSpinBox();widget.setRange(-180,180);widget.setDecimals(6)
                    if key=='step_deg':widget.setRange(.000001,180);widget.setValue(.25)
                    elif key=='flow_tolerance':widget.setRange(0,.99);widget.setValue(.01)
                    widget.valueChanged.connect(self.update_preview)
                else:
                    widget=QLineEdit();widget.textChanged.connect(self.update_preview)
                    if key=='stations':widget.setText('0.20 0.22 0.24')
                    if key=='bands':widget.setText('20 40')
                fields[key]=widget
                if isinstance(widget,QLineEdit) and key not in {'stations','bands','pressures_pa'}:
                    row=QWidget();layout=QHBoxLayout(row);layout.setContentsMargins(0,0,0,0)
                    layout.addWidget(widget);browse=QPushButton('浏览…')
                    browse.clicked.connect(lambda _=False,w=widget,k=key:self.browse(w,k))
                    label = '预填角度用 candidate.json（可选）' if action == 'init' and key == 'candidate' else LABELS[key]
                    layout.addWidget(browse);form.addRow(label,row)
                else:form.addRow(LABELS[key],widget)
            self.forms[action]=fields
            scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(content)
            self.stack.addWidget(scroll)
        root.addWidget(self.stack,2)
        self.preview=QPlainTextEdit();self.preview.setReadOnly(True);self.preview.setMaximumHeight(95)
        root.addWidget(self.preview)
        row=QHBoxLayout();self.start_button=QPushButton('执行');self.stop_button=QPushButton('停止')
        self.stop_button.setEnabled(False);self.start_button.clicked.connect(self.start);self.stop_button.clicked.connect(self.stop)
        row.addWidget(self.start_button);row.addWidget(self.stop_button)
        help_button=QPushButton('打开使用说明');help_button.clicked.connect(lambda:self.open_file(CODE_DIR/'README_incidence_validation.md'))
        row.addWidget(help_button)
        edit=QPushButton('打开验证配置');edit.clicked.connect(self.open_spec);row.addWidget(edit)
        root.addLayout(row)
        self.status=QLabel('就绪');root.addWidget(self.status)
        self.log=QPlainTextEdit();self.log.setReadOnly(True);root.addWidget(self.log,2)
        self.action_box.currentIndexChanged.connect(self.change_action)
        self.runner.line.connect(lambda channel,text:self.log.appendPlainText(text))
        self.runner.started.connect(lambda argv:self.set_running(True,'运行中'))
        self.runner.finished.connect(self.finished)
        self.runner.failed.connect(lambda text:self.set_running(False,'启动失败：'+text))
        self._default_config='';self.refresh()

    def browse(self, widget, key):
        if key=='output_dir':
            path=QFileDialog.getExistingDirectory(self,'选择父目录，再在输入框追加新子目录名')
        elif key=='output':
            path,_=QFileDialog.getSaveFileName(self,'新输出文件',widget.text(),'JSON (*.json)')
        else:path,_=QFileDialog.getOpenFileName(self,LABELS[key],widget.text(),'All files (*)')
        if path:widget.setText(path)

    def open_file(self,path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def open_spec(self):
        fields=self.forms[self.action_box.currentData()]
        widget=fields.get('spec') or (fields.get('output') if self.action_box.currentData()=='init' else None)
        if widget and Path(widget.text()).is_file():self.open_file(widget.text())
        else:QMessageBox.information(self,'验证配置','请选择已有验证配置，或先创建配置文件。')

    def change_action(self,index):
        self.stack.setCurrentIndex(index);self.update_preview()

    def build_spec(self):
        action=self.action_box.currentData();values={}
        for key,widget in self.forms[action].items():
            if isinstance(widget,QCheckBox):values[key]=widget.isChecked()
            elif isinstance(widget,(QSpinBox,QDoubleSpinBox)):values[key]=widget.value()
            else:values[key]=widget.text().strip()
        return build_validation(action,**values)

    def update_preview(self,*args):
        if not hasattr(self,'preview'):return
        try:self.preview.setPlainText(self.build_spec().preview(default_python()))
        except ValueError as exc:self.preview.setPlainText(str(exc))

    def refresh(self):
        widget=self.forms['plan']['config']
        if not widget.text() or widget.text()==self._default_config:
            widget.setText(str(self.ctx.project.config_path))
        self._default_config=str(self.ctx.project.config_path)
        paths=self.ctx.project.config.get('paths',{})
        for action in ('extract','sweep'):
            if not self.forms[action]['post_exe'].text() and paths.get('cfx_bin_dir'):
                self.forms[action]['post_exe'].setText(str(Path(paths['cfx_bin_dir'])/'cfx5post.exe'))
        self.update_preview()

    def start(self):
        if any(r.running for r in self.ctx.command_runners):
            self.status.setText('已有运行或验证任务正在执行，请等待完成。');return
        try:spec=self.build_spec()
        except ValueError as exc:QMessageBox.warning(self,'参数未完成',str(exc));return
        if self.action_box.currentData()=='run':
            if QMessageBox.question(self,'启动真实 CFD','将按冻结方案和本次预算运行真实 CFD，结果写入方案的独立目录。继续？',
                                    QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        self.log.clear();self.log.appendPlainText(spec.preview(default_python()))
        self.runner.start(spec.argv(default_python()))

    def stop(self):
        self.log.appendPlainText('正在停止入口进程。外部求解器可能仍在运行，请检查后再恢复。')
        self.runner.stop()

    def set_running(self,running,message):
        self.start_button.setEnabled(not running);self.stop_button.setEnabled(running)
        self.action_box.setEnabled(not running);self.stack.setEnabled(not running);self.status.setText(message)

    def finished(self,code,reason):
        if code==0 and reason=='normal' and self.action_box.currentData()=='init':
            message='已保存待核对配置；请打开 JSON 补齐日志列出的字段后再提取'
        else:
            message='完成' if code==0 and reason=='normal' else ('质量或工况检查未通过，请查看日志' if code==2 else f'执行失败：{code} / {reason}')
        self.set_running(False,message);self.ctx.report('验证：'+message)
