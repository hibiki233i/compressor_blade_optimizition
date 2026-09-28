"""Build the exact argv for every supported CLI action.

Keeping the construction here (instead of inside the page widget) makes the
generated command line pure, testable data.  ``--config`` is always emitted
*after* the sub-command because :mod:`argparse` lets sub-parser defaults
override a value given before the sub-command.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

ENTRY_SCRIPT = "blade_shape_active_learning.py"
CFX_SCRIPT = "blade_shape_cfx_runner.py"

BOUNDARY_STAGES = ["singles", "pairs", "extension"]


@dataclass
class CommandSpec:
    """A ready-to-run command: ``python -u <script> <args...>``."""

    name: str
    args: list[str] = field(default_factory=list)
    summary: str = ""
    script: str = ENTRY_SCRIPT

    def argv(self, python: str = "python") -> list[str]:
        """Full argv including interpreter and entry script."""
        return [python, "-u", self.script, *self.args]

    def preview(self, python: str = "python") -> str:
        return " ".join(shlex.quote(part) for part in self.argv(python))


def _config_args(config_path: str | Path) -> list[str]:
    return ["--config", str(config_path)]


def build_run(
    config_path: str | Path,
    *,
    initial_samples: int | None = None,
    iterations: int | None = None,
    batch_size: int | None = None,
    max_new_cfd: int | None = None,
    seed: int | None = None,
    resume: bool = False,
) -> CommandSpec:
    args = ["run", *_config_args(config_path)]
    if initial_samples is not None:
        args += ["--initial-samples", str(int(initial_samples))]
    if iterations is not None:
        args += ["--iterations", str(int(iterations))]
    if batch_size is not None:
        args += ["--batch-size", str(int(batch_size))]
    if max_new_cfd is not None:
        args += ["--max-new-cfd", str(int(max_new_cfd))]
    if seed is not None:
        args += ["--seed", str(int(seed))]
    if resume:
        args.append("--resume")
    return CommandSpec("run", args, "运行主动学习循环（真实 CFD）")


def build_write_candidate(
    config_path: str | Path,
    *,
    index: int = 0,
    seed: int | None = None,
    dry_run: bool = False,
    offline: bool = False,
) -> CommandSpec:
    args = ["write-candidate", *_config_args(config_path), "--index", str(int(index))]
    if seed is not None:
        args += ["--seed", str(int(seed))]
    if dry_run:
        args.append("--dry-run")
    if offline:
        args.append("--offline")
    return CommandSpec("write-candidate", args, "生成单个候选并校验几何规则")


def build_diagnose(config_path: str | Path) -> CommandSpec:
    return CommandSpec("diagnose", ["diagnose", *_config_args(config_path)], "离线诊断：不启动 CFD")


def build_write_boundary_plan(
    config_path: str | Path,
    *,
    center_run_id: str,
    plan: str | Path,
) -> CommandSpec:
    args = [
        "write-boundary-plan",
        *_config_args(config_path),
        "--center-run-id", str(center_run_id),
        "--plan", str(plan),
    ]
    return CommandSpec("write-boundary-plan", args, "生成版本 2 边界实验方案")


def build_run_boundary(
    config_path: str | Path,
    *,
    plan: str | Path,
    stage: str,
    max_new_cfd: int,
    resume: bool = False,
) -> CommandSpec:
    args = [
        "run-boundary",
        *_config_args(config_path),
        "--plan", str(plan),
        "--stage", str(stage),
        "--max-new-cfd", str(int(max_new_cfd)),
    ]
    if resume:
        args.append("--resume")
    return CommandSpec("run-boundary", args, f"运行边界阶段：{stage}")


def build_check_pre(config_path: str | Path, *, working_dir: str | Path) -> CommandSpec:
    args = [
        "check-pre",
        *_config_args(config_path),
        "--working-dir", str(working_dir),
    ]
    return CommandSpec("check-pre", args, "生成并检查 CFX-Pre 输入（不求解）", script=CFX_SCRIPT)


VALIDATION_SCRIPT = 'blade_shape_incidence_validation.py'
VALIDATION_OPTIONS = {
    'init': ('res', 'geometry_source', 'output'),
    'extract': ('spec', 'post_exe', 'output_dir'),
    'sweep': ('spec', 'post_exe', 'stations', 'bands', 'output_dir'),
    'legacy': ('csv', 'hub_beta_deg', 'shroud_beta_deg', 'output_dir'),
    'compare': ('baseline', 'target', 'flow_tolerance', 'output'),
    'plan': ('config', 'candidate', 'step_deg', 'pressures_pa', 'output_dir'),
    'run': ('plan', 'max_new_cfd', 'resume'),
}


def build_validation(action: str, **values) -> CommandSpec:
    """Only build argv; the validation CLI owns all calculations and execution."""
    if action not in VALIDATION_OPTIONS:
        raise ValueError(f'Unknown validation action: {action}')
    args = [action]
    optional = {'candidate', 'pressures_pa', 'resume'}
    for key in VALIDATION_OPTIONS[action]:
        value = values.get(key)
        if key == 'resume':
            if value: args.append('--resume')
            continue
        if value is None or value == '':
            if key in optional: continue
            raise ValueError(f'请填写 {key}')
        args.append('--' + key.replace('_', '-'))
        if key in {'stations', 'bands', 'pressures_pa'}:
            parts = str(value).replace(',', ' ').split() if isinstance(value, str) else list(value)
            if not parts: raise ValueError(f'{key} 不能为空')
            try:
                numbers = [int(p) if key == 'bands' else float(p) for p in parts]
                import math
                if not all(math.isfinite(n) for n in numbers): raise ValueError('non-finite')
            except (ValueError, TypeError) as exc:
                raise ValueError(f'{key} 需要用空格分隔的数值') from exc
            args.extend(str(n) for n in numbers)
        else:
            args.append(str(value))
    return CommandSpec('validation:' + action, args, '展向攻角验证：' + action, script=VALIDATION_SCRIPT)
