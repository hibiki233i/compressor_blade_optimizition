"""Visual editor for ``blade_shape_config.json``.

The form is described declaratively (:class:`Field`) so every section stays a
plain data list.  Saving validates through the same rules the CLI uses and
blocks on errors, then writes atomically while keeping a timestamped backup.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..field_help import HELP, help_document_html, live_hint, tooltip_html
from ..project import Issue, config_errors, describe_local_paths, save_config, validate_config
from ..widgets import (
    Badge,
    Card,
    FormRow,
    MessageBar,
    primary_button,
    tool_button,
)
from .base import Page
from blade_shape_convergence import RESTART_ITERATIONS_RANGE, RMS_TARGET_MAX  # noqa: E402 - path set by ..project


# --------------------------------------------------------------------------
# nested-key helpers
# --------------------------------------------------------------------------
def get_path(data: dict[str, Any], key: str, default: Any = None) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def set_path(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


# --------------------------------------------------------------------------
# field description
# --------------------------------------------------------------------------
@dataclass
class Field:
    key: str
    label: str
    kind: str  # text | int | float | bool | choice | path
    hint: str = ""
    minimum: float = -1e9
    maximum: float = 1e9
    decimals: int = 4
    step: float = 0.1
    choices: tuple[str, ...] = ()
    mode: str = "file"  # for kind=path: file | dir | save


@dataclass
class Section:
    title: str
    hint: str = ""
    fields: list[Field] = dc_field(default_factory=list)


PATH_FIELDS = [
    Field("paths.powershell_exe", "PowerShell 7", "path", "pwsh.exe", mode="file"),
    Field("paths.geometry_script_path", "几何脚本", "path", "Run-BladeShapeGeometryMeshing.ps1", mode="file"),
    Field("paths.cfturbo_exe", "CFturbo", "path", "CFturbo.exe", mode="file"),
    Field("paths.turbogrid_exe", "TurboGrid", "path", "cfxtg.exe", mode="file"),
    Field("paths.cfx_bin_dir", "CFX bin 目录", "path", "求解器目录", mode="dir"),
    Field("paths.template_dir", "模板目录", "path", mode="dir"),
    Field("paths.base_cft", "基准 .cft", "path", mode="file"),
    Field("paths.cft_batch_template", "CFturbo batch 模板", "path", mode="file"),
    Field("paths.turbogrid_template", "TurboGrid 模板", "path", mode="file"),
    Field("paths.template_cfx", "CFX 模板", "path", mode="file"),
    Field("paths.template_cse", "CFX-Post 脚本", "path", mode="file"),
    Field("paths.output_dir", "输出目录", "path", "所有 CSV 与 cases/", mode="dir"),
]

RUNTIME_FIELDS = [
    Field("runtime.cfx_cores", "CFX 核数", "int", minimum=1, maximum=512),
    Field("cfx_convergence.rms_target", "CFX RMS 门槛", "float", minimum=0.000000000001, maximum=RMS_TARGET_MAX,
          decimals=12, step=0.000001),
    Field("cfx_convergence.restart_iterations", "不收敛追加迭代数", "int",
          minimum=RESTART_ITERATIONS_RANGE[0], maximum=RESTART_ITERATIONS_RANGE[1]),
    Field("cfx_convergence.flow_analysis", "CFX Flow 名称", "text"),
    Field("runtime.n_blades", "叶片数", "int", minimum=1, maximum=200),
    Field("runtime.rpm", "声明转速 (rpm)", "float", decimals=2, step=100.0),
    Field("runtime.mass_flow", "声明质量流量", "float", decimals=6, step=0.0001),
    Field("runtime.p_out_pa", "出口静压 (Pa)", "float", decimals=3, step=1.0),
    Field("runtime.alpha0", "声明进气角 alpha0", "float", decimals=3, step=1.0),
    Field("runtime.initial_samples", "初始 DOE 样本数", "int", minimum=0, maximum=10000),
    Field("runtime.iterations", "主动学习迭代数", "int", minimum=0, maximum=10000),
    Field("runtime.batch_size", "每批 CFD 点数", "int", minimum=1, maximum=64),
    Field("runtime.max_new_cfd", "最大新增 CFD 次数", "int", minimum=0, maximum=100000),
    Field("runtime.candidate_pool_size", "候选池大小", "int", minimum=1, maximum=1000000),
    Field("runtime.nsga2_pop_size", "NSGA-II 种群", "int", minimum=4, maximum=100000),
    Field("runtime.nsga2_generations", "NSGA-II 代数", "int", minimum=1, maximum=100000),
    Field("runtime.seed", "随机种子", "int", minimum=0, maximum=2**31 - 1),
]

SURROGATE_FIELDS = [
    Field("surrogate.model", "代理模型", "choice", choices=("gp", "rbf_ridge_ensemble")),
    Field("surrogate.fallback_model", "回退模型", "choice", choices=("rbf_ridge_ensemble", "gp", "none")),
    Field("surrogate.ehvi_y_samples", "EHVI 采样数", "int", minimum=8, maximum=100000),
    Field("surrogate.ehvi_validation_samples", "EHVI 校验采样数", "int", minimum=8, maximum=1000000),
]

PARETO_FIELDS = [
    Field("pareto.use_engineering_tolerance", "启用工程容差", "bool", "关闭后使用严格支配"),
    Field("pareto.tolerances.Efficiency", "Efficiency 容差", "float", decimals=6, step=0.0001),
    Field("pareto.tolerances.MassFlow", "MassFlow 容差", "float", decimals=6, step=0.001),
]

CONSTRAINT_FIELDS = [
    Field("constraints.max_beta_offset_deg", "最大 beta 偏移 (deg)", "float", decimals=3, step=0.5),
    Field("constraints.max_theta_offset_deg", "最大 theta 偏移 (deg)", "float", decimals=3, step=0.1),
    Field("constraints.max_generated_beta_step_deg", "插值后最大相邻 beta 步长 (deg)", "float", decimals=3, step=0.5),
    Field("constraints.baseline_step_margin_deg", "基准步长余量 (deg)", "float", decimals=3, step=0.1),
    Field("constraints.duplicate_distance_norm", "重复距离阈值 (归一化)", "float", decimals=4, step=0.01),
]

SEARCH_FIELDS = [
    Field("search.slice_tolerance_norm", "同切片容差 (归一化)", "float", decimals=8, step=0.001),
]

REFINEMENT_FIELDS = [
    Field("refinement.challenger_min_samples", "挑战模型最小样本", "int", minimum=1, maximum=100000),
    Field("refinement.diagnostic_min_points", "诊断门最少点数", "int", minimum=1, maximum=100000),
    Field("refinement.diagnostic_window", "诊断窗口", "int", minimum=1, maximum=100000),
    Field("refinement.diagnostic_gate.min_coverage_2sigma", "诊断门最低 2σ 覆盖率", "float",
          minimum=0.0001, maximum=1.0, decimals=4, step=0.05),
    Field("refinement.candidate_roles", "候选角色顺序", "csv", "逗号分隔"),
    Field("refinement.boundary_variables", "边界变量", "csv", "逗号分隔"),
    Field("refinement.extension.variable", "外推变量", "text"),
    Field("refinement.extension.value", "外推目标值", "float", decimals=3, step=0.1),
    Field("refinement.local_search.enabled", "启用局部搜索", "bool"),
    Field("refinement.local_search.fraction", "局部候选比例", "float", decimals=3, step=0.05, minimum=0.0, maximum=1.0),
    Field("refinement.local_search.initial_radius_norm", "初始半径 (归一化)", "float", decimals=3, step=0.05),
    Field("refinement.local_search.min_radius_norm", "最小半径 (归一化)", "float", decimals=3, step=0.01),
    Field("refinement.local_search.max_radius_norm", "最大半径 (归一化)", "float", decimals=3, step=0.05),
    Field("refinement.local_search.successes_to_expand", "扩大半径所需成功数", "int", minimum=1, maximum=1000),
    Field("refinement.local_search.failures_to_shrink", "收缩半径所需失败数", "int", minimum=1, maximum=1000),
    Field("refinement.local_search.min_relative_hv_gain", "最小相对 HV 增益", "float", decimals=6, step=0.0001),
]

SECTIONS: list[Section] = [
    Section("路径配置", "本机路径：JSON 中留空的项读写配置旁的 blade_shape_local.ini（不入库）；"
            "在本机不存在时仅告警，不阻止保存。", PATH_FIELDS),
    Section("运行参数", "CFD 工况与运行预算。实际边界条件来自 CFX 模板；转速、流量、进气角只是声明值。", RUNTIME_FIELDS),
    Section("代理模型", "筛选候选的代理模型与 EHVI 采样设置。", SURROGATE_FIELDS),
    Section("Pareto 容差", "工程容差只影响 pareto_front.csv，严格前沿始终另存。", PARETO_FIELDS),
    Section("几何约束", "候选几何的硬约束；修改后需重新生成方案。", CONSTRAINT_FIELDS),
    Section("搜索空间", "同切片容差用于判定固定坐标是否一致。", SEARCH_FIELDS),
    Section("局部细化", "挑战模型、诊断门与局部搜索半径策略。", REFINEMENT_FIELDS),
]


# --------------------------------------------------------------------------
# widgets
# --------------------------------------------------------------------------
class PathRow(QWidget):
    def __init__(self, mode: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.mode = mode
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.edit = QLineEdit()
        layout.addWidget(self.edit, 1)
        browse = tool_button("", "folder", "ghost", "浏览…")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)

    def _browse(self) -> None:
        current = self.edit.text().strip()
        if self.mode == "dir":
            path = QFileDialog.getExistingDirectory(self, "选择目录", current or str(Path.home()))
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择文件", current or str(Path.home()))
        if path:
            self.edit.setText(path)

    def text(self) -> str:
        return self.edit.text()

    def set_text(self, value: str) -> None:
        self.edit.setText(value or "")


class VariableTable(QTableWidget):
    """Design-variable bounds plus active/fixed membership."""

    changed = Signal()

    HEADERS = ["设计变量", "下限 (deg)", "上限 (deg)", "参与搜索", "固定值 (deg)"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(0, len(self.HEADERS), parent)
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, len(self.HEADERS)):
            self.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.setMinimumHeight(260)
        self._rows: list[str] = []

    def load(self, config: dict[str, Any]) -> None:
        self.blockSignals(True)
        variables = config.get("variables", [])
        # Malformed entries are reported by validate_config; never crash the editor.
        variables = [item for item in variables if isinstance(item, dict)] if isinstance(variables, list) else []
        fixed = get_path(config, "search.fixed_variables", {}) or {}
        fixed = fixed if isinstance(fixed, dict) else {}
        active = get_path(config, "search.active_variables", None)
        active = active if isinstance(active, list) else None
        self.setRowCount(len(variables))
        self._rows = []
        for row, item in enumerate(variables):
            name = str(item.get("name", ""))
            self._rows.append(name)
            label = QTableWidgetItem(name)
            label.setFlags(Qt.ItemIsEnabled)
            self.setItem(row, 0, label)

            lower = QDoubleSpinBox()
            lower.setDecimals(6)
            lower.setRange(-360.0, 360.0)
            lower.setSingleStep(0.5)
            lower.setValue(_as_float(item.get("lower")))
            lower.valueChanged.connect(self.changed)
            self.setCellWidget(row, 1, lower)

            upper = QDoubleSpinBox()
            upper.setDecimals(6)
            upper.setRange(-360.0, 360.0)
            upper.setSingleStep(0.5)
            upper.setValue(_as_float(item.get("upper")))
            upper.valueChanged.connect(self.changed)
            self.setCellWidget(row, 2, upper)

            is_active = (name in active) if active is not None else (name not in fixed)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignCenter)
            searchable = QCheckBox()
            searchable.setChecked(bool(is_active))
            searchable.toggled.connect(self.changed)
            holder_layout.addWidget(searchable)
            self.setCellWidget(row, 3, holder)

            value = QDoubleSpinBox()
            value.setDecimals(6)
            value.setRange(-360.0, 360.0)
            value.setSingleStep(0.1)
            value.setValue(_as_float(fixed.get(name)))
            value.setEnabled(not is_active)
            searchable.toggled.connect(lambda checked, widget=value: widget.setEnabled(not checked))
            value.valueChanged.connect(self.changed)
            self.setCellWidget(row, 4, value)
        self.blockSignals(False)

    def apply(self, config: dict[str, Any]) -> None:
        variables = config.get("variables", [])
        fixed: dict[str, float] = {}
        active: list[str] = []
        for row in range(self.rowCount()):
            name = self._rows[row] if row < len(self._rows) else f"var_{row}"
            lower = self.cellWidget(row, 1)
            upper = self.cellWidget(row, 2)
            holder = self.cellWidget(row, 3)
            value = self.cellWidget(row, 4)
            for index, item in enumerate(variables):
                if str(item.get("name")) == name:
                    variables[index]["lower"] = float(lower.value())
                    variables[index]["upper"] = float(upper.value())
                    break
            searchable = holder.findChild(QCheckBox) if holder else None
            if searchable is not None and searchable.isChecked():
                active.append(name)
            else:
                fixed[name] = float(value.value())
        config.setdefault("search", {})
        config["search"]["active_variables"] = active
        config["search"]["fixed_variables"] = fixed


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------
class ConfigPage(Page):
    title = "项目设置"
    subtitle = "可视化编辑 blade_shape_config.json（保存前自动校验并备份）"
    nav_label = "项目设置"
    nav_icon = "tune"
    nav_section = "工作流"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent)
        self._bindings: dict[str, tuple[Field, QWidget]] = {}
        # what each numeric widget showed right after loading (see _collect)
        self._shown: dict[str, Any] = {}
        self._rows: dict[str, FormRow] = {}
        self._dirty = False
        self._build()
        self.reload_from_context()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(11)

        # ---- action bar
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.path_label = QLabel()
        self.path_label.setObjectName("CardHint")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bar.addWidget(self.path_label, 1)
        self.dirty_badge = Badge("未保存修改", "warn")
        self.dirty_badge.setVisible(False)
        bar.addWidget(self.dirty_badge)
        self.save_button = primary_button("保存配置", "save")
        self.save_button.clicked.connect(self.save)
        bar.addWidget(self.save_button)
        save_as = tool_button("另存为…", "file")
        save_as.clicked.connect(self.save_as)
        bar.addWidget(save_as)
        help_button = tool_button("参数说明", "info", tooltip="所有参数的含义、推荐值与确定方法")
        help_button.clicked.connect(self.show_help)
        bar.addWidget(help_button)
        reload_button = tool_button("放弃并重载", "refresh")
        reload_button.clicked.connect(self._confirm_reload)
        bar.addWidget(reload_button)
        root.addLayout(bar)

        self.message = MessageBar()
        root.addWidget(self.message)

        # ---- scrollable form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(scroll, 1)
        content = QWidget()
        scroll.setWidget(content)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 8)
        body.setSpacing(12)

        for section in SECTIONS:
            card = Card(section.title, section.hint)
            if section.fields is PATH_FIELDS:
                # where each machine path came from (INI, AWP_ROOT, or why it is missing)
                self.local_note = QLabel()
                self.local_note.setObjectName("CardHint")
                self.local_note.setWordWrap(True)
                self.local_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
                card.body.addWidget(self.local_note)
            form = QVBoxLayout()
            form.setSpacing(7)
            for spec in section.fields:
                widget = self._make_widget(spec)
                form.addWidget(self._form_row(spec, widget))
            card.body.addLayout(form)
            body.addWidget(card)

        body.addWidget(self._build_variables_card())
        body.addStretch(1)

    def _build_variables_card(self) -> Card:
        card = Card("设计变量边界", "取消勾选后该变量固定为给定值，不再参与搜索")
        self.variable_table = VariableTable()
        self.variable_table.changed.connect(self._mark_dirty)
        card.body.addWidget(self.variable_table)
        note = QLabel(
            "提示：12 个变量的顺序即几何写入顺序，请勿调整行序；"
            "至少保留一个变量参与搜索，且固定值必须落在上下限内。"
        )
        note.setObjectName("CardHint")
        note.setWordWrap(True)
        card.body.addWidget(note)
        return card

    def _form_row(self, spec: Field, widget: QWidget) -> FormRow:
        """A row whose label, value and hint all carry the field's explanation."""
        entry = HELP.get(spec.key)
        row = FormRow(spec.label + (" ⓘ" if entry else ""), widget, entry.hint if entry else spec.hint)
        row.hint.setWordWrap(True)
        if entry:
            tip = tooltip_html(spec.key, spec.label)
            for target in (row.label, row.hint, widget):
                target.setToolTip(tip)
        self._rows[spec.key] = row
        return row

    def _refresh_hints(self) -> None:
        """Recompute hints that depend on the form (degrees for normalised values, counts)."""
        if not hasattr(self, "_config_snapshot"):
            return
        config = self._collect()
        for key, row in self._rows.items():
            entry = HELP.get(key)
            if entry is not None:
                row.hint.setText(live_hint(key, config, get_path(config, key)) or entry.hint)

    def show_help(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("项目设置参数说明")
        dialog.resize(760, 680)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        browser.setHtml(help_document_html(
            [(section.title, [(spec.key, spec.label) for spec in section.fields]) for section in SECTIONS]))
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _make_widget(self, spec: Field) -> QWidget:
        if spec.kind == "bool":
            widget = QCheckBox()
        elif spec.kind == "int":
            widget = QSpinBox()
            widget.setRange(int(spec.minimum), int(spec.maximum))
            widget.setButtonSymbols(QSpinBox.UpDownArrows)
        elif spec.kind == "float":
            widget = QDoubleSpinBox()
            widget.setDecimals(int(spec.decimals))
            widget.setRange(float(spec.minimum), float(spec.maximum))
            widget.setSingleStep(float(spec.step))
            widget.setButtonSymbols(QDoubleSpinBox.UpDownArrows)
        elif spec.kind == "choice":
            widget = QComboBox()
            widget.addItems(list(spec.choices))
            widget.setEditable(False)
        elif spec.kind == "csv":
            widget = QLineEdit()
            widget.setPlaceholderText("以英文逗号分隔")
        elif spec.kind == "path":
            widget = PathRow(spec.mode)
        else:
            widget = QLineEdit()

        if isinstance(widget, QCheckBox):
            widget.toggled.connect(self._mark_dirty)
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.valueChanged.connect(self._mark_dirty)
        elif isinstance(widget, QComboBox):
            widget.currentTextChanged.connect(self._mark_dirty)
        elif isinstance(widget, PathRow):
            widget.edit.textChanged.connect(self._mark_dirty)
        elif isinstance(widget, QLineEdit):
            widget.textChanged.connect(self._mark_dirty)

        self._bindings[spec.key] = (spec, widget)
        return widget

    # -------------------------------------------------------------- state
    def _mark_dirty(self, *_: Any) -> None:
        if not self._dirty:
            self._dirty = True
            self.dirty_badge.setVisible(True)
        self._refresh_hints()

    def _set_dirty(self, value: bool) -> None:
        self._dirty = value
        self.dirty_badge.setVisible(value)

    def reload_from_context(self) -> None:
        """Populate the form from the current project's config."""
        config = copy.deepcopy(self.ctx.project.config)
        from blade_shape_convergence import ConvergencePolicy
        defaults = ConvergencePolicy().to_dict()
        if isinstance(config.get("cfx_convergence", {}), dict):
            config["cfx_convergence"] = {**defaults, **config.get("cfx_convergence", {})}
        from blade_shape_refinement import DEFAULT_MIN_COVERAGE_2SIGMA
        refinement = config.get("refinement")
        if isinstance(refinement, dict) and isinstance(refinement.get("diagnostic_gate", {}), dict):
            refinement["diagnostic_gate"] = {"min_coverage_2sigma": DEFAULT_MIN_COVERAGE_2SIGMA,
                                             **refinement.get("diagnostic_gate", {})}
        self._config_snapshot = config
        self._loaded_config_path = self.ctx.project.config_path
        self._shown = {}
        for key, (spec, widget) in self._bindings.items():
            value = get_path(config, key)
            if value is None:
                continue
            if spec.kind == "bool":
                widget.setChecked(bool(value))
            elif spec.kind in {"int", "float"}:
                try:
                    widget.setValue(type(widget.value())(value))
                except (TypeError, ValueError):
                    continue
                self._shown[key] = widget.value()
            elif spec.kind == "choice":
                index = widget.findText(str(value))
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif spec.kind == "path":
                widget.set_text(str(value))
            elif spec.kind == "csv":
                widget.setText(", ".join(str(part) for part in value) if isinstance(value, (list, tuple)) else str(value))
            else:
                widget.setText(str(value))
        self.variable_table.load(config)
        self.local_note.setText("\n".join(("⚠ " if level == "warning" else "") + message
                                           for level, message in describe_local_paths(config)))
        self.local_note.setVisible(bool(self.local_note.text()))
        self._refresh_hints()
        self.path_label.setText(f"配置文件：{self.ctx.project.config_path or '未选择（保存时另存为）'}")
        self._set_dirty(False)
        self._update_validation(config, saved=True)

    def refresh(self) -> None:
        # a reload triggered elsewhere should not silently drop unsaved edits
        if self._dirty and self._loaded_config_path == self.ctx.project.config_path:
            return
        self.reload_from_context()

    def on_show(self) -> None:
        self.path_label.setText(f"配置文件：{self.ctx.project.config_path or '未选择（保存时另存为）'}")

    # ----------------------------------------------------------- validate
    def _collect(self) -> dict[str, Any]:
        config = copy.deepcopy(self._config_snapshot)
        for key, (spec, widget) in self._bindings.items():
            if spec.kind == "bool":
                set_path(config, key, bool(widget.isChecked()))
            elif spec.kind in {"int", "float"}:
                # A spin box rounds to its decimals and clamps to its range; a value the
                # user has not changed (5/6, a threshold below the widget minimum)
                # stays as the config holds it, and validation judges that value.
                original = get_path(config, key)
                if (key in self._shown and widget.value() == self._shown[key]
                        and isinstance(original, (int, float)) and not isinstance(original, bool)
                        and math.isfinite(original)):
                    continue
                set_path(config, key, (int if spec.kind == "int" else float)(widget.value()))
            elif spec.kind == "choice":
                set_path(config, key, widget.currentText())
            elif spec.kind == "path":
                set_path(config, key, widget.text().strip())
            elif spec.kind == "csv":
                set_path(config, key, [part.strip() for part in widget.text().split(",") if part.strip()])
            else:
                set_path(config, key, widget.text())
        self.variable_table.apply(config)
        return config

    def _update_validation(self, config: dict[str, Any], *, saved: bool) -> list[Issue]:
        issues = validate_config(config)
        errors = [item for item in issues if item.level == "error"]
        warnings = [item for item in issues if item.level == "warning"]
        if errors:
            self.message.set_level("error", "校验未通过：" + "；".join(item.message for item in errors[:4]))
        elif warnings:
            self.message.set_level(
                "warn",
                f"校验通过（{len(warnings)} 条提示）。保存后生效。"
                if saved else f"可保存：{len(warnings)} 条提示，多为本机缺少外部程序。",
            )
        else:
            self.message.set_level("good", "配置校验通过。" if saved else "配置校验通过，可以保存。")
        return issues

    # ---------------------------------------------------------------- save
    def confirm_discard_changes(self, action: str) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "放弃修改？",
            f"当前配置有未保存的修改，确定要放弃并{action}吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _confirm_reload(self) -> None:
        if not self.confirm_discard_changes("重新载入磁盘上的配置"):
            return
        self.ctx.project.reload()
        self.reload_from_context()
        self.ctx.report("已重新载入配置。")

    def _form_matches_current_config(self) -> bool:
        if self._loaded_config_path == self.ctx.project.config_path:
            return True
        QMessageBox.warning(self, "配置已切换", "表单属于先前的配置文件，已载入当前配置。请重新检查后保存。")
        self.reload_from_context()
        return False

    def save(self) -> None:
        if not self._form_matches_current_config():
            return
        if self.ctx.project.config_path is None:
            self.save_as()
            return
        config = self._collect()
        issues = self._update_validation(config, saved=False)
        errors = config_errors(issues)
        if errors:
            QMessageBox.warning(
                self,
                "无法保存",
                "配置存在错误，必须先修正：\n\n" + "\n".join(f"• {item.message}" for item in errors[:8]),
            )
            return
        try:
            save_config(self.ctx.project.config_path, config)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.ctx.reload()
        self.reload_from_context()
        self.ctx.report(f"配置已保存：{self.ctx.project.config_path}")

    def save_as(self) -> None:
        if not self._form_matches_current_config():
            return
        config = self._collect()
        issues = config_errors(validate_config(config))
        if issues:
            QMessageBox.warning(self, "无法保存", "请先修正校验错误。")
            return
        current = self.ctx.project.config_path
        target, _ = QFileDialog.getSaveFileName(
            self, "另存为", str(current) if current else self.ctx.dialog_start(), "JSON (*.json)"
        )
        if not target:
            return
        self.ctx.remember_dialog(target)
        try:
            save_config(target, config, keep_backup=False)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.ctx.set_config_path(target)
        self.reload_from_context()
        self.ctx.report(f"已另存为 {target} 并切换为当前配置。")
