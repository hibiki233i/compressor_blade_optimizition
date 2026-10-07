"""Qt-free readers for incidence-validation artefacts.

The validation CLI (``blade_shape_incidence_validation.py``) owns every number
that defines a result: band integration, flow/metal angles, incidence, RMS and
the quality gate.  This module only reads those files back for display, applies
the CLI's own comparability rules before two profiles are overlaid, and derives
presentation quantities (band deltas, reverse-flow shares, finite differences
between CFD points that were already computed).  Nothing here is written back
or becomes an optimizer label.
"""
from __future__ import annotations

import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .project import CODE_DIR

if str(CODE_DIR) not in sys.path:  # the validation CLI lives next to the package
    sys.path.insert(0, str(CODE_DIR))

from blade_shape_incidence_validation import METHOD, pending_spec_fields  # noqa: E402

LEGACY_METHOD = "legacy_aca_20_arithmetic_v1"
HUB_VARIABLE = "hub_beta_0_deg_offset"
SHROUD_VARIABLE = "shroud_beta_0_deg_offset"
ROLE_LABELS = {
    "center": "中心", "hub_minus": "hub −", "hub_plus": "hub +",
    "shroud_minus": "shroud −", "shroud_plus": "shroud +",
}


# --------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------
def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    out = []
    for row in rows:
        parsed: dict[str, Any] = {}
        for key, raw in row.items():
            text = (raw or "").strip()
            if text.lower() in {"true", "false"}:
                parsed[key] = text.lower() == "true"
            else:
                number = _number(text) if text else None
                parsed[key] = number if number is not None or not text else text
        out.append(parsed)
    return out


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------
@dataclass
class ProfileResult:
    """``extract`` (mass-weighted bands) or ``legacy`` (20-point ACA) output."""

    path: Path
    rows: list[dict[str, Any]]
    summary: dict[str, Any]
    kind: str = "profile"

    @property
    def method(self) -> str:
        return str(self.summary.get("method", ""))

    @property
    def is_legacy(self) -> bool:
        return self.method == LEGACY_METHOD

    @property
    def spec(self) -> dict[str, Any]:
        spec = self.summary.get("spec")
        return spec if isinstance(spec, dict) else {}

    @property
    def measurement(self) -> dict[str, Any]:
        value = self.spec.get("measurement")
        return value if isinstance(value, dict) else {}

    @property
    def label(self) -> str:
        return str(self.spec.get("target_id") or self.path.name)

    @property
    def quality_ok(self) -> bool | None:
        value = self.summary.get("quality_ok")
        return value if isinstance(value, bool) else None

    @property
    def quality_issues(self) -> list[str]:
        issues = self.summary.get("quality_issues")
        return [str(item) for item in issues] if isinstance(issues, list) else []

    @property
    def rms(self) -> float | None:
        return _number(self.summary.get("incidence_rms_mass_deg"))

    @property
    def mean_abs(self) -> float | None:
        key = "mean_abs_incidence_deg" if self.is_legacy else "mean_abs_incidence_mass_deg"
        return _number(self.summary.get(key))

    @property
    def max_abs(self) -> float | None:
        value = _number(self.summary.get("max_abs_incidence_deg"))
        if value is None and self.is_legacy:
            values = [abs(v) for v in self.column("incidence_deg") if v is not None]
            return max(values) if values else None
        return value

    def column(self, name: str) -> list[float | None]:
        return [_number(row.get(name)) for row in self.rows]

    def span(self) -> list[float | None]:
        """Display span: legacy point, else mass-weighted band span or band centre."""
        if self.is_legacy:
            return self.column("span")
        out = []
        for row in self.rows:
            weighted = _number(row.get("span_mass"))
            low, high = _number(row.get("span_low")), _number(row.get("span_high"))
            centre = (low + high) / 2 if low is not None and high is not None else None
            out.append(weighted if weighted is not None else centre)
        return out

    def band_centres(self) -> list[float | None]:
        if self.is_legacy:
            return self.column("span")
        out = []
        for row in self.rows:
            low, high = _number(row.get("span_low")), _number(row.get("span_high"))
            out.append((low + high) / 2 if low is not None and high is not None else None)
        return out

    def reverse_fraction(self) -> list[float | None]:
        out = []
        for row in self.rows:
            forward, reverse = _number(row.get("forward_kg_s")), _number(row.get("reverse_kg_s"))
            total = (forward or 0.0) + (reverse or 0.0)
            out.append((reverse or 0.0) / total if forward is not None and total > 0 else None)
        return out

    def forward_share(self) -> list[float | None]:
        forward = self.column("forward_kg_s")
        total = sum(value for value in forward if value is not None)
        return [value / total if value is not None and total > 0 else None for value in forward]

    def metal_endpoints(self) -> tuple[float, float] | None:
        """Hub/shroud leading-edge metal angles the CLI interpolated between."""
        source = self.spec if not self.is_legacy else self.summary
        hub, shroud = _number(source.get("hub_beta_deg")), _number(source.get("shroud_beta_deg"))
        return (hub, shroud) if hub is not None and shroud is not None else None

    def total_reverse_fraction(self) -> float | None:
        forward = _number(self.summary.get("forward_mass_flow_kg_s"))
        reverse = _number(self.summary.get("reverse_mass_flow_kg_s"))
        if forward is None or reverse is None or forward + reverse <= 0:
            return None
        return reverse / (forward + reverse)


@dataclass
class SweepResult:
    path: Path
    rows: list[dict[str, Any]]
    progress: dict[str, Any]
    profiles: list[tuple[str, ProfileResult]] = field(default_factory=list)
    kind: str = "sweep"

    @property
    def status(self) -> str:
        return str(self.progress.get("status", "complete" if self.rows else "unknown"))


@dataclass
class ComparisonResult:
    path: Path
    data: dict[str, Any]
    kind: str = "compare"

    def source_summaries(self) -> list[Path]:
        sources = self.data.get("sources")
        return [Path(name) for name in sources] if isinstance(sources, dict) else []


@dataclass
class PlanPoint:
    index: int
    role: str
    p_out_pa: float | None
    offset_deg: float
    variable: str
    status: str
    run_id: str
    metrics: dict[str, Any]


@dataclass
class PlanResult:
    path: Path
    plan: dict[str, Any]
    progress: dict[str, Any]
    points: list[PlanPoint]
    kind: str = "plan"

    @property
    def status(self) -> str:
        if not self.progress:
            return "not_started"
        return str(self.progress.get("status") or "running")

    @property
    def step_deg(self) -> float | None:
        steps = [abs(point.offset_deg) for point in self.points if point.role != "center"]
        return max(steps) if steps else None

    def slopes(self) -> list[dict[str, Any]]:
        """Finite differences between completed CFD points, per back pressure.

        Central differences need both perturbations; one-sided ones use the
        centre. They describe the computed points only, not a calibrated
        minimum-loss incidence.
        """
        out = []
        pressures = sorted({point.p_out_pa for point in self.points}, key=lambda v: (v is None, v))
        for pressure in pressures:
            group = {point.role: point for point in self.points if point.p_out_pa == pressure}
            for side in ("hub", "shroud"):
                centre, minus, plus = group.get("center"), group.get(f"{side}_minus"), group.get(f"{side}_plus")
                row: dict[str, Any] = {"p_out_pa": pressure, "variable": side}
                step = plus.offset_deg if plus else (-minus.offset_deg if minus else None)
                row["step_deg"] = step
                for metric in ("Efficiency", "MassFlow"):
                    f0, fm, fp = (_completed_metric(item, metric) for item in (centre, minus, plus))
                    slope = curvature = None
                    scheme = ""
                    if step and fp is not None and fm is not None:
                        slope, scheme = (fp - fm) / (2 * step), "central"
                        if f0 is not None:
                            curvature = (fp - 2 * f0 + fm) / step ** 2
                    elif step and fp is not None and f0 is not None:
                        slope, scheme = (fp - f0) / step, "forward"
                    elif step and fm is not None and f0 is not None:
                        slope, scheme = (f0 - fm) / step, "backward"
                    row[f"d{metric}_ddeg"] = slope
                    row[f"curvature_{metric}"] = curvature
                    row[f"scheme_{metric}"] = scheme
                out.append(row)
        return out


def _completed_metric(point: PlanPoint | None, metric: str) -> float | None:
    if point is None or point.status != "complete":
        return None
    return _number(point.metrics.get(metric))


@dataclass
class SpecResult:
    path: Path
    spec: dict[str, Any]
    kind: str = "spec"

    @property
    def pending(self) -> list[str]:
        return pending_spec_fields(self.spec)


@dataclass
class FailedResult:
    """An output directory whose ``state.json`` records a failed extraction."""

    path: Path
    state: dict[str, Any]
    kind: str = "failed"


Result = ProfileResult | SweepResult | ComparisonResult | PlanResult | SpecResult | FailedResult


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def load_profile(directory: Path) -> ProfileResult:
    summary = _read_json(directory / "summary.json")
    if not isinstance(summary, dict):
        raise ValueError(f"{directory / 'summary.json'} 不是对象")
    rows = _read_csv(directory / "profile.csv") if (directory / "profile.csv").is_file() else []
    return ProfileResult(directory, rows, summary)


def load_plan(directory: Path) -> PlanResult:
    plan = _read_json(directory / "plan.json")
    if not isinstance(plan, dict) or plan.get("kind") != "endpoint_incidence_sensitivity":
        raise ValueError(f"{directory / 'plan.json'} 不是进口角敏感性方案")
    progress_path = directory / "progress.json"
    progress = _read_json(progress_path) if progress_path.is_file() else {}
    states = progress.get("points", {}) if isinstance(progress, dict) else {}
    names = [str(item.get("name")) for item in plan.get("config", {}).get("variables", [])]
    points = plan.get("points", [])
    centres = {point.get("p_out_pa"): point.get("x") for point in points if point.get("role") == "center"}
    out = []
    for point in points:
        role = str(point.get("role", ""))
        variable = HUB_VARIABLE if role.startswith("hub") else SHROUD_VARIABLE if role.startswith("shroud") else ""
        offset = 0.0
        centre = centres.get(point.get("p_out_pa"))
        if variable and variable in names and centre is not None:
            column = names.index(variable)
            offset = float(point["x"][column]) - float(centre[column])
        state = states.get(str(point.get("index")), {})
        metrics = state.get("metrics") if isinstance(state.get("metrics"), dict) else {}
        out.append(PlanPoint(
            index=int(point.get("index", len(out))), role=role, p_out_pa=_number(point.get("p_out_pa")),
            offset_deg=offset, variable=variable, status=str(state.get("status", "pending")),
            run_id=str(state.get("run_id", "")), metrics=metrics,
        ))
    return PlanResult(directory, plan, progress if isinstance(progress, dict) else {}, out)


def load_sweep(directory: Path) -> SweepResult:
    rows = _read_csv(directory / "sensitivity.csv") if (directory / "sensitivity.csv").is_file() else []
    progress_path = directory / "sweep_progress.json"
    progress = _read_json(progress_path) if progress_path.is_file() else {}
    if not rows and isinstance(progress, dict):
        rows = [item for item in progress.get("completed", []) if isinstance(item, dict)]
    profiles = []
    for child in sorted(directory.glob("measurement_*")):
        if (child / "summary.json").is_file():
            try:
                profile = load_profile(child)
            except (OSError, ValueError):
                continue
            measurement = profile.measurement
            label = f"截面 {measurement.get('le_station', '?')} · {measurement.get('bands', '?')} 带"
            profiles.append((label, profile))
    return SweepResult(directory, rows, progress if isinstance(progress, dict) else {}, profiles)


def load_result(path: str | Path) -> Result:
    """Detect and read any validation output (directory or JSON file)."""
    target = Path(path)
    if target.is_file():
        name = target.name.lower()
        if name == "profile.csv" or name == "summary.json":
            return load_profile(target.parent)
        if name in {"sensitivity.csv", "sweep_progress.json", "sweep_inputs.json"}:
            return load_sweep(target.parent)
        if name in {"plan.json", "progress.json"}:
            return load_plan(target.parent)
        if target.suffix.lower() != ".json":
            raise ValueError(f"无法识别的文件类型：{target.name}")
        data = _read_json(target)
        if not isinstance(data, dict):
            raise ValueError(f"{target.name} 不是 JSON 对象")
        if "matched_operating_point" in data:
            return ComparisonResult(target, data)
        if data.get("kind") == "endpoint_incidence_sensitivity":
            return load_plan(target.parent)
        if data.get("schema_version") == 1 and "res_path" in data:
            return SpecResult(target, data)
        if "method" in data and (target.parent / "profile.csv").is_file():
            return load_profile(target.parent)
        raise ValueError(f"{target.name} 不是可识别的验证产物")
    if not target.is_dir():
        raise ValueError(f"路径不存在：{target}")
    if (target / "plan.json").is_file():
        return load_plan(target)
    if any((target / name).is_file() for name in ("sensitivity.csv", "sweep_progress.json", "sweep_inputs.json")):
        return load_sweep(target)
    if (target / "summary.json").is_file():
        return load_profile(target)
    state_path = target / "state.json"
    if state_path.is_file():
        state = _read_json(state_path)
        if isinstance(state, dict):
            return FailedResult(target, state)
    raise ValueError(f"{target} 中没有 summary.json / sensitivity.csv / plan.json 等验证产物")


# --------------------------------------------------------------------------
# comparisons (display only; the compare CLI remains the matched-point judge)
# --------------------------------------------------------------------------
def comparable(target: ProfileResult, baseline: ProfileResult) -> tuple[bool, str]:
    """Mirror compare_results' definitional checks before overlaying profiles."""
    if target.method != baseline.method:
        return False, "方法不同：旧 ACA 指标与质量加权速度三角形指标不能混合比较"
    if target.method == METHOD and target.measurement != baseline.measurement:
        return False, "测量定义不同（截面、分带、方向约定或阈值），不能逐带比较"
    if len(target.rows) != len(baseline.rows):
        return False, "分带数量不同"
    return True, ""


def incidence_delta(target: ProfileResult, baseline: ProfileResult) -> list[tuple[float, float]]:
    """Per-band ``(band centre, i_target - i_baseline)`` for comparable profiles."""
    ok, _ = comparable(target, baseline)
    if not ok:
        return []
    out = []
    for span, a, b in zip(target.band_centres(), target.column("incidence_deg"),
                          baseline.column("incidence_deg")):
        if span is not None and a is not None and b is not None:
            out.append((span, a - b))
    return out


def flow_difference(target: ProfileResult, baseline: ProfileResult) -> float | None:
    a = _number(target.summary.get("net_mass_flow_kg_s"))
    b = _number(baseline.summary.get("net_mass_flow_kg_s"))
    if a is None or b is None or max(abs(a), abs(b)) == 0:
        return None
    return abs(a - b) / max(abs(a), abs(b))


# --------------------------------------------------------------------------
# helpers for preparing validation runs from optimisation cases
# --------------------------------------------------------------------------
def case_validation_inputs(case_dir: str | Path) -> dict[str, Any]:
    """Locate a case's accepted ``.res`` and ``candidate.json`` (never certify them)."""
    case = Path(case_dir)
    out: dict[str, Any] = {"res": "", "geometry_source": "", "candidate": "", "notes": []}
    candidate = case / "candidate.json"
    if candidate.is_file():
        out["geometry_source"] = out["candidate"] = str(candidate)
    else:
        out["notes"].append("算例目录中没有 candidate.json")
    receipt = case / "cfx_state.json"
    result_file = ""
    if receipt.is_file():
        try:
            solve = _read_json(receipt)["stages"]["solve"]
            if solve.get("status") == "complete":
                result_file = str(solve.get("result_file") or "")
            else:
                out["notes"].append(f"cfx_state.json 记录的求解状态为 {solve.get('status')}，不是已完成结果")
        except (OSError, ValueError, KeyError, TypeError):
            out["notes"].append("cfx_state.json 无法读取")
    if result_file and (case / result_file).is_file():
        out["res"] = str(case / result_file)
    elif not result_file:
        results = sorted(case.glob("*.res"))
        if len(results) == 1:
            out["res"] = str(results[0])
            out["notes"].append("未找到完成凭据；仅按唯一 .res 填入，请人工确认其为已接受结果")
        elif results:
            out["notes"].append(f"存在 {len(results)} 个 .res 且无完成凭据，请手动选择")
        else:
            out["notes"].append("算例目录中没有 .res")
    else:
        out["notes"].append(f"完成凭据指向的 {result_file} 在本机不存在")
    return out


def suggest_new_path(parent: str | Path, stem: str, suffix: str = "") -> str:
    """A not-yet-existing ``parent/stem_YYYYmmdd-HHMMSS[suffix]`` path."""
    base = Path(parent)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = base / f"{stem}_{stamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = base / f"{stem}_{stamp}_{counter}{suffix}"
        counter += 1
    return str(candidate)
