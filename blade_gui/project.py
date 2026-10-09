"""Data access for the GUI.

This module deliberately contains **no Qt imports**: it is the single place that
knows how to read the configuration, the CSV artefacts and the ``cases/`` tree.
Everything else in :mod:`blade_gui` renders what this module returns.

Wherever the CLI already exposes a loader (``blade_shape_active_learning``),
that loader is reused so the GUI and the CLI can never disagree about schema
normalisation.  When the config is temporarily invalid the readers fall back to
plain :mod:`pandas` reads so the GUI can still start and be used to fix it.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CODE_DIR = Path(__file__).resolve().parent.parent
if str(CODE_DIR) not in sys.path:  # allow `python -m blade_gui` from anywhere
    sys.path.insert(0, str(CODE_DIR))

from blade_shape_local_config import META_KEY as LOCAL_META_KEY  # noqa: E402
from blade_shape_local_config import (  # noqa: E402
    apply_local_paths, local_ini_path, split_local_paths, write_local_paths,
)

#: the tracked example config (shareable settings only; paths come from the local INI)
DEFAULT_CONFIG_PATH = CODE_DIR / "blade_shape_config.json"

OBJECTIVES = ["Efficiency", "MassFlow"]
RESULT_COLUMNS = ["Efficiency", "PressureRatio", "MassFlow", "Power", "totalpressureratio"]


# --------------------------------------------------------------------------
# lazy handles on the CLI modules
# --------------------------------------------------------------------------
def cli_module():
    """Return the imported ``blade_shape_active_learning`` module."""
    import blade_shape_active_learning as al  # noqa: PLC0415 - lazy on purpose

    return al


def refinement_module():
    import blade_shape_refinement as ref  # noqa: PLC0415

    return ref


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def _ini_hint(config: dict[str, Any]) -> str:
    meta = config.get(LOCAL_META_KEY)
    return Path(meta["ini"]).name if isinstance(meta, dict) and meta.get("ini") else "本机 blade_shape_local.ini"


def read_config_file(path: str | Path) -> dict[str, Any]:
    """Read a config file *without* validating it."""
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{config_path} 的顶层必须是一个 JSON 对象。")
    payload["_config_path"] = str(config_path.resolve())
    return apply_local_paths(payload, config_path)


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    message: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"[{self.level}] {self.message}"


def validate_config(config: dict[str, Any]) -> list[Issue]:
    """Validate a config dict and return human-readable issues.

    Errors block a run; warnings are advisory (typically missing files, which is
    expected when the GUI is opened on a machine that is not the CFD host).
    """
    issues: list[Issue] = []
    # A hand-edited file can hold the wrong JSON type anywhere; report it instead
    # of raising, because the GUI must still open to let the user repair it.
    for section in ("paths", "runtime", "constraints", "search", "cfx_convergence"):
        if section in config and not isinstance(config[section], dict):
            issues.append(Issue("error", f"{section} 必须是 JSON 对象。"))
    if any(issue.level == "error" for issue in issues):
        return issues
    try:
        from blade_shape_convergence import ConvergencePolicy
        ConvergencePolicy.from_config(config)
    except (ValueError, TypeError) as exc:
        issues.append(Issue("error", f"CFX 收敛设置无效：{exc}"))

    variables = config.get("variables")
    if not isinstance(variables, list) or not variables:
        issues.append(Issue("error", "variables 不能为空。"))
        variables = []

    names: list[str] = []
    for idx, item in enumerate(variables):
        if not isinstance(item, dict):
            issues.append(Issue("error", f"variables[{idx}] 必须是包含 name/lower/upper 的对象。"))
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            issues.append(Issue("error", f"variables[{idx}] 缺少 name。"))
            continue
        names.append(name)
        try:
            lower, upper = float(item["lower"]), float(item["upper"])
        except (KeyError, TypeError, ValueError):
            issues.append(Issue("error", f"{name}: lower/upper 必须是数字。"))
            continue
        if not np.isfinite([lower, upper]).all():
            issues.append(Issue("error", f"{name}: lower/upper 必须是有限数值。"))
        elif lower >= upper:
            issues.append(Issue("error", f"{name}: lower ({lower:g}) 必须小于 upper ({upper:g})。"))

    if len(names) != len(set(names)):
        issues.append(Issue("error", "变量名必须唯一。"))

    # Delegate the active/fixed partition + slice tolerance checks to the CLI.
    try:
        refinement_module().active_indices(config)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
        issues.append(Issue("error", f"搜索空间定义无效：{exc}"))

    runtime = config.get("runtime", {})
    for key, minimum in (("initial_samples", 0), ("iterations", 0), ("batch_size", 1), ("max_new_cfd", 0)):
        value = runtime.get(key)
        if value is None:
            continue
        try:
            if int(value) < minimum:
                issues.append(Issue("error", f"runtime.{key} 必须 >= {minimum}（当前 {value}）。"))
        except (TypeError, ValueError):
            issues.append(Issue("error", f"runtime.{key} 必须是整数。"))

    constraints = config.get("constraints", {})
    for key in ("max_beta_offset_deg", "max_theta_offset_deg", "max_generated_beta_step_deg",
                "baseline_step_margin_deg", "duplicate_distance_norm"):
        value = constraints.get(key)
        if value is None:
            continue
        try:
            if float(value) <= 0:
                issues.append(Issue("warning", f"constraints.{key} 应大于 0（当前 {value}）。"))
        except (TypeError, ValueError):
            issues.append(Issue("error", f"constraints.{key} 必须是数字。"))

    # path existence is a warning: the GUI is often opened off the CFD host
    for key in ("geometry_script_path", "base_cft", "cft_batch_template", "turbogrid_template",
                "template_cfx", "template_cse", "powershell_exe", "cfturbo_exe", "turbogrid_exe"):
        value = config.get("paths", {}).get(key)
        if not value:
            issues.append(Issue("warning", f"paths.{key} 未配置（填在 {_ini_hint(config)}）。"))
        elif not Path(str(value)).exists():
            issues.append(Issue("warning", f"paths.{key} 在本机不存在：{value}"))

    out = config.get("paths", {}).get("output_dir")
    if not out:
        issues.append(Issue("error", f"paths.output_dir 未配置（填在 {_ini_hint(config)}）。"))
    return issues


def config_errors(issues: list[Issue]) -> list[Issue]:
    return [item for item in issues if item.level == "error"]


def save_config(path: str | Path, config: dict[str, Any], *, keep_backup: bool = True) -> Path:
    """Atomically write ``config`` to ``path``, keeping one ``.bak`` copy."""
    config_path = Path(path)
    # machine paths go back to the local INI next to the (possibly new) config file
    shared, local = split_local_paths(config)
    payload = {key: value for key, value in shared.items() if not key.startswith("_")}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    ini = local_ini_path(config_path)
    previous = ini.read_bytes() if local and ini.is_file() else None
    if local:
        write_local_paths(ini, local)
    try:
        if keep_backup and config_path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup_dir = config_path.parent / "config_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_path, backup_dir / f"{config_path.name}.{stamp}.bak")
        temp = config_path.with_suffix(config_path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(config_path)
    except BaseException:
        # a failed save must not leave half of it (the machine paths) applied
        if local and previous is not None:
            ini.write_bytes(previous)
        elif local:
            ini.unlink(missing_ok=True)
        raise
    return config_path


def external_tools(config: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """``(label, path, exists)`` for every external program the pipeline calls."""
    paths = config.get("paths")
    paths = paths if isinstance(paths, dict) else {}
    pairs = [
        ("PowerShell 7", paths.get("powershell_exe", "")),
        ("CFturbo", paths.get("cfturbo_exe", "")),
        ("TurboGrid", paths.get("turbogrid_exe", "")),
        ("CFX bin", paths.get("cfx_bin_dir", "")),
    ]
    return [(label, str(value), bool(value) and Path(str(value)).exists()) for label, value in pairs]


# --------------------------------------------------------------------------
# project
# --------------------------------------------------------------------------
@dataclass
class CaseRecord:
    run_id: str
    path: Path
    status: str = "unknown"
    failure_stage: str = ""
    message: str = ""
    sample_phase: str = ""
    efficiency: float = float("nan")
    massflow: float = float("nan")
    power: float = float("nan")
    files: int = 0
    modified: float = 0.0
    registered: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "success"


@dataclass
class Summary:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    pareto_count: int = 0
    strict_count: int = 0
    best_efficiency: float = float("nan")
    best_massflow: float = float("nan")
    best_power: float = float("nan")
    best_run_id: str = ""
    phases: dict[str, int] = field(default_factory=dict)
    failure_stages: dict[str, int] = field(default_factory=dict)
    selection_sources: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    last_modified: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


class Project:
    """Read-only view over one configuration + its output directory."""

    def __init__(self, config_path: str | Path | None = None, data_dir: str | Path | None = None):
        # ``None`` = nothing chosen yet; the GUI then shows an empty project
        self.config_path = Path(config_path) if config_path else None
        self.data_dir_override = Path(data_dir) if data_dir else None
        self._cache: dict[str, tuple[float, Any]] = {}
        self.config: dict[str, Any] = {}
        self.issues: list[Issue] = []
        self.load_error: str = ""
        self.reload()

    # ---------------------------------------------------------------- load
    def reload(self) -> None:
        self._cache.clear()
        self.issues = []
        self.load_error = ""
        if self.config_path is None:
            self.config = {"paths": {}, "runtime": {}, "variables": []}
            self.load_error = "尚未选择配置文件"
            self.issues = [Issue("error", "尚未选择配置文件：点击侧栏「配置…」选择 blade_shape_config.json。")]
            return
        try:
            self.config = read_config_file(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.config = {"paths": {}, "runtime": {}, "variables": []}
            self.load_error = str(exc)
            self.issues = [Issue("error", f"无法读取配置：{exc}")]
            return
        try:
            self.issues = validate_config(self.config)
        except Exception as exc:  # noqa: BLE001 - keep the GUI usable for repairs
            self.issues = [Issue("error", f"配置结构无效：{exc}")]

    @property
    def config_valid(self) -> bool:
        return not self.load_error and not config_errors(self.issues)

    # --------------------------------------------------------------- paths
    @property
    def output_dir(self) -> Path:
        if self.data_dir_override:
            return self.data_dir_override
        paths = self.config.get("paths")
        value = paths.get("output_dir") if isinstance(paths, dict) else None
        return Path(str(value)) if value else CODE_DIR / "blade_al_runs"

    def path(self, name: str) -> Path:
        return self.output_dir / name

    @property
    def cases_dir(self) -> Path:
        return self.output_dir / "cases"

    # ---------------------------------------------------------- cached read
    def _cached(self, key: str, path: Path, loader):
        try:
            stat = path.stat()
            stamp = stat.st_mtime_ns
        except OSError:
            self._cache.pop(key, None)
            return None
        hit = self._cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        try:
            value = loader()
        except Exception:  # noqa: BLE001 - a broken artefact must not kill the GUI
            value = None
        self._cache[key] = (stamp, value)
        return value

    def _read_csv(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path)

    def training(self) -> pd.DataFrame:
        path = self.path("training_data.csv")
        raw = self._cached("training_raw", path, lambda: self._read_csv(path))
        if raw is None or raw.empty:
            return raw if raw is not None else pd.DataFrame()

        def normalize() -> pd.DataFrame:
            try:
                return cli_module().normalize_training_frame(self.config, raw.copy())
            except Exception:  # noqa: BLE001 - never let a bad config kill the view
                return raw.copy()

        frame = self._cached("training_norm", path, normalize)
        if frame is None:
            return pd.DataFrame()
        # callers may add helper columns; hand out a copy so the cache stays clean
        return frame.copy()

    def pareto(self) -> pd.DataFrame:
        path = self.path("pareto_front.csv")
        frame = self._cached("pareto", path, lambda: self._read_csv(path))
        return frame if frame is not None else pd.DataFrame()

    def pareto_strict(self) -> pd.DataFrame:
        path = self.path("pareto_front_strict.csv")
        frame = self._cached("strict", path, lambda: self._read_csv(path))
        return frame if frame is not None else pd.DataFrame()

    def diagnostics(self) -> pd.DataFrame:
        path = self.path("active_learning_diagnostics.csv")
        frame = self._cached("diagnostics", path, lambda: self._read_csv(path))
        return frame if frame is not None else pd.DataFrame()

    def iteration_summary(self) -> pd.DataFrame:
        path = self.path("iteration_summary.csv")
        frame = self._cached("iteration", path, lambda: self._read_csv(path))
        return frame if frame is not None else pd.DataFrame()

    def json_artifact(self, name: str) -> dict[str, Any] | None:
        path = self.path(name)
        return self._cached(f"json:{name}", path, lambda: json.loads(path.read_text(encoding="utf-8")))

    def gate(self) -> dict[str, Any] | None:
        """Diagnostic gate, preferring the newer local gate file."""
        for name in ("local_diagnostic_gate.json", "diagnostic_gate.json"):
            value = self.json_artifact(name)
            if isinstance(value, dict):
                return value
        return None

    def local_state(self) -> dict[str, Any] | None:
        value = self.json_artifact("local_search_state.json")
        return value if isinstance(value, dict) else None

    def pending(self) -> dict[str, Any]:
        value = self.json_artifact("pending_evaluations.json")
        return value if isinstance(value, dict) else {}

    def hv_reference(self) -> list[float] | None:
        value = self.json_artifact("hypervolume_reference.json")
        if isinstance(value, dict):
            ref = value.get("reference")
            if isinstance(ref, list):
                return [float(x) for x in ref]
        return None

    # ------------------------------------------------------------- summary
    def summary(self) -> Summary:
        df = self.training()
        out = Summary()
        if df.empty:
            out.pareto_count = len(self.pareto())
            out.strict_count = len(self.pareto_strict())
            return out

        out.attempts = len(df)
        # Align with df.index even when an un-normalized CSV lacks the column.
        status = df["status"] if "status" in df.columns else pd.Series("", index=df.index, dtype=object)
        status = status.astype(str).str.strip().str.lower()
        out.successes = int((status == "success").sum())
        out.failures = int(out.attempts - out.successes)

        numeric = df.copy()
        for col in OBJECTIVES + ["Power"]:
            if col in numeric.columns:
                numeric[col] = pd.to_numeric(numeric[col], errors="coerce")
        success = numeric.loc[status == "success"]

        if not success.empty:
            if "Efficiency" in success and success["Efficiency"].notna().any():
                idx = success["Efficiency"].idxmax()
                out.best_efficiency = float(success.loc[idx, "Efficiency"])
                out.best_run_id = str(success.loc[idx].get("run_id", ""))
            if "MassFlow" in success and success["MassFlow"].notna().any():
                out.best_massflow = float(success["MassFlow"].max())
            if "Power" in success and success["Power"].notna().any():
                out.best_power = float(success["Power"].max())

        if "sample_phase" in df.columns:
            out.phases = {
                str(k): int(v)
                for k, v in df["sample_phase"].fillna("unknown").astype(str).value_counts().items()
            }
        if "failure_stage" in df.columns:
            failed = df.loc[status != "success", "failure_stage"].fillna("unspecified").astype(str)
            failed = failed.replace({"": "unspecified", "nan": "unspecified"})
            out.failure_stages = {str(k): int(v) for k, v in failed.value_counts().items()}
        if "selection_source" in df.columns:
            src = df["selection_source"].dropna().astype(str)
            src = src[src.str.strip() != ""]
            out.selection_sources = {str(k): int(v) for k, v in src.value_counts().items()}
        if "al_iteration" in df.columns:
            values = pd.to_numeric(df["al_iteration"], errors="coerce").dropna()
            out.iterations = int(values.max()) + 1 if not values.empty else 0

        out.pareto_count = len(self.pareto())
        out.strict_count = len(self.pareto_strict())
        path = self.path("training_data.csv")
        out.last_modified = path.stat().st_mtime if path.exists() else 0.0
        return out

    def best_so_far(self) -> tuple[list[int], list[float], list[float]]:
        """Cumulative best Efficiency / MassFlow over successful evaluations."""
        df = self.training()
        if df.empty or "status" not in df.columns:
            return [], [], []
        status = df["status"].astype(str).str.strip().str.lower()
        success = df.loc[status == "success"]
        if success.empty:
            return [], [], []
        numeric = success.copy()
        for col in OBJECTIVES:
            numeric[col] = pd.to_numeric(numeric.get(col), errors="coerce")
        numeric = numeric.dropna(subset=OBJECTIVES)
        if numeric.empty:
            return [], [], []
        eff = numeric["Efficiency"].cummax().tolist()
        flow = numeric["MassFlow"].cummax().tolist()
        xs = list(range(1, len(eff) + 1))
        return xs, [float(v) for v in eff], [float(v) for v in flow]

    # --------------------------------------------------------------- cases
    def cases(self) -> list[CaseRecord]:
        df = self.training()
        by_run: dict[str, pd.Series] = {}
        if not df.empty and "run_id" in df.columns:
            for _, row in df.iterrows():
                key = str(row.get("run_id", "")).strip()
                if key:
                    by_run[key] = row

        records: list[CaseRecord] = []
        root = self.cases_dir
        if root.is_dir():
            for path in sorted(root.glob("case_*")):
                if not path.is_dir():
                    continue
                record = CaseRecord(run_id=path.name, path=path)
                row = by_run.get(path.name)
                if row is not None:
                    record.registered = True
                    record.status = str(row.get("status", "") or "unknown").strip().lower() or "unknown"
                    record.failure_stage = _clean(row.get("failure_stage"))
                    record.message = _clean(row.get("message"))
                    record.sample_phase = _clean(row.get("sample_phase"))
                    record.efficiency = _num(row.get("Efficiency"))
                    record.massflow = _num(row.get("MassFlow"))
                    record.power = _num(row.get("Power"))
                try:
                    record.files = sum(1 for item in path.rglob("*") if item.is_file())
                    record.modified = path.stat().st_mtime
                except OSError:
                    pass
                records.append(record)

        known = {item.run_id for item in records}
        for run_id, row in by_run.items():
            case_dir = _clean(row.get("case_dir"))
            if run_id in known or not case_dir:
                continue
            path = Path(case_dir)
            records.append(
                CaseRecord(
                    run_id=run_id,
                    path=path,
                    registered=True,
                    status=str(row.get("status", "") or "unknown").strip().lower() or "unknown",
                    failure_stage=_clean(row.get("failure_stage")),
                    message=_clean(row.get("message")),
                    sample_phase=_clean(row.get("sample_phase")),
                    efficiency=_num(row.get("Efficiency")),
                    massflow=_num(row.get("MassFlow")),
                    power=_num(row.get("Power")),
                    files=sum(1 for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0,
                    modified=path.stat().st_mtime if path.exists() else 0.0,
                )
            )
        records.sort(key=lambda item: _case_number(item.run_id))
        return records

    def case_files(self, record: CaseRecord) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not record.path.is_dir():
            return out
        for path in sorted(record.path.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append(
                {
                    "name": path.name,
                    "rel": str(path.relative_to(record.path)),
                    "path": str(path),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "suffix": path.suffix.lower(),
                }
            )
        return out

    def available_plans(self) -> list[Path]:
        candidates: list[Path] = []
        for base in (self.output_dir, CODE_DIR, self.config_path.parent if self.config_path else None):
            if base is not None and base.is_dir():
                candidates.extend(sorted(base.glob("boundary_plan*.json")))
        seen: dict[str, Path] = {}
        for item in candidates:
            seen[str(item.resolve())] = item
        return sorted(seen.values())


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return "" if text.lower() in {"nan", "none"} else text.strip()


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def _case_number(run_id: str) -> int:
    digits = "".join(ch for ch in str(run_id) if ch.isdigit())
    return int(digits) if digits else 1 << 30


# --------------------------------------------------------------------------
# small helpers shared by pages
# --------------------------------------------------------------------------
def tail_text(path: str | Path, lines: int = 400) -> str:
    """Return the last ``lines`` lines of a text file, tolerating bad encodings."""
    target = Path(path)
    if not target.is_file():
        return ""
    try:
        with target.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = min(size, max(4096, lines * 220))
            handle.seek(max(0, size - block))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    return "\n".join(rows[-lines:])


def read_text(path: str | Path, limit: int = 200_000) -> str:
    target = Path(path)
    if not target.is_file():
        return ""
    try:
        data = target.read_bytes()[:limit]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def human_size(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def fmt(value: Any, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(number):
        return "—"
    if number != 0 and (abs(number) < 1e-3 or abs(number) >= 1e6):
        return f"{number:.{digits}e}"
    return f"{number:.{digits}g}"


def fmt_time(stamp: float) -> str:
    if not stamp:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
