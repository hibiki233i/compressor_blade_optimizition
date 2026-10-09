from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

from blade_shape_cfx_runner import run_cfx_pipeline
from blade_shape_convergence import ConvergencePolicy
from blade_shape_local_config import apply_local_paths
import blade_shape_refinement as refinement
from blade_shape_acquisition import expected_hvi
import blade_shape_pending as pending
from blade_shape_runtime import output_lock, file_identity, atomic_json, case_reservations

try:
    from scipy.stats import qmc
except Exception:  # pragma: no cover
    qmc = None

try:
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
except Exception:  # pragma: no cover
    GaussianProcessRegressor = None
    ConstantKernel = None
    Matern = None
    WhiteKernel = None


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
OBJECTIVE_COLUMNS = ["Efficiency", "MassFlow"]
RESULT_COLUMNS = ["Efficiency", "PressureRatio", "MassFlow", "Power", "totalpressureratio"]
SAMPLE_COLUMNS = [
    "sample_phase",
    "doe_index",
    "al_iteration",
    "batch_index",
    "selection_rank",
    "selection_source",
    "experiment_id",
    "design_role",
]
STATUS_COLUMNS = ["status", "failure_stage", "message", "case_dir", "run_id"]
# Design variables are mapped to the CFturbo meanline by name, never by position.
HUB_BETA_VARIABLES = tuple(f"hub_beta_{i}_deg_offset" for i in range(5))
SHROUD_BETA_VARIABLES = tuple(f"shroud_beta_{i}_deg_offset" for i in range(5))
HUB_THETA_VARIABLE = "hub_theta_deg_offset"
SHROUD_THETA_VARIABLE = "shroud_theta_deg_offset"
GEOMETRY_VARIABLES = (*HUB_BETA_VARIABLES, *SHROUD_BETA_VARIABLES, HUB_THETA_VARIABLE, SHROUD_THETA_VARIABLE)


@dataclass
class BaselineShape:
    hub_beta_rad: np.ndarray
    shroud_beta_rad: np.ndarray
    hub_x: np.ndarray
    shroud_x: np.ndarray
    hub_theta_rad: float
    shroud_theta_rad: float


@dataclass
class CaseResult:
    row: dict[str, Any]
    success: bool


@dataclass
class SelectedCandidate:
    x: np.ndarray
    selection_rank: int
    selection_source: str
    acquisition_score: float = np.nan
    ehvi: float = np.nan
    distance_to_existing: float = np.nan
    pred_mean: np.ndarray | None = None
    pred_std: np.ndarray | None = None
    candidate_role: str = "ehvi"
    metadata: dict[str, Any] = field(default_factory=dict)


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["_config_path"] = str(config_path.resolve())
    apply_local_paths(payload, config_path)
    local = payload.get("_local_paths", {})
    empty = [key for key in local.get("keys", []) if not payload["paths"].get(key)]
    if empty:
        where = local["ini"] if local.get("found") else f"{local['ini']} (not found; copy blade_shape_local.ini.example)"
        print(f"Warning: empty paths {', '.join(empty)}; set them in {where}", file=sys.stderr)
    ConvergencePolicy.from_config(payload)
    geometry_indices(payload)
    surrogate_choice(payload)
    refinement.active_indices(payload)
    return payload


def variable_names(config: dict[str, Any]) -> list[str]:
    return [item["name"] for item in config["variables"]]


def lower_bounds(config: dict[str, Any]) -> np.ndarray:
    return np.array([float(item["lower"]) for item in config["variables"]], dtype=float)


def upper_bounds(config: dict[str, Any]) -> np.ndarray:
    return np.array([float(item["upper"]) for item in config["variables"]], dtype=float)


def _indexed_values(parent: ET.Element) -> list[ET.Element]:
    values = list(parent.findall("Value"))
    return sorted(values, key=lambda item: int(item.attrib.get("Index", "0")))


def extract_baseline(config: dict[str, Any]) -> BaselineShape:
    template = Path(config["paths"]["cft_batch_template"])
    root = ET.parse(template).getroot()
    blade = root.find(".//Updates//BladePropsML[@Name='Main blade']")
    if blade is None:
        raise ValueError(f"Cannot find Main blade update node in {template}")
    beta1 = _indexed_values(blade.find("Beta1"))
    beta2 = _indexed_values(blade.find("Beta2"))
    if len(beta1) < 2 or len(beta2) < 2:
        raise ValueError("Beta1/Beta2 endpoint arrays must contain hub and shroud values.")

    lines = root.findall(".//Updates//TMeanLine")
    by_index = {int(line.attrib.get("Index", "-1")): line for line in lines}
    if 0 not in by_index or 1 not in by_index:
        raise ValueError("Expected TMeanLine Index=0 and Index=1 in batch template updates.")

    def line_values(line: ET.Element, start: float, end: float) -> tuple[np.ndarray, np.ndarray, float]:
        inner = line.find("InnerProgPoints")
        xs = [0.0]
        ys = [start]
        if inner is not None:
            for value in _indexed_values(inner):
                x_node = value.find("x")
                y_node = value.find("y")
                if x_node is None or y_node is None:
                    raise ValueError("InnerProgPoints must contain x and y values.")
                xs.append(float(x_node.text))
                ys.append(float(y_node.text))
        else:
            prog = line.find("ProgPoints")
            if prog is None:
                raise ValueError("TMeanLine must contain InnerProgPoints or ProgPoints.")
            xs = []
            ys = []
            for value in _indexed_values(prog):
                xs.append(float(value.find("x").text))
                ys.append(float(value.find("y").text))
        xs.append(1.0)
        ys.append(end)
        theta_node = line.find("lePos")
        if theta_node is None:
            theta_node = line.find("StackingPhi")
        if theta_node is None:
            raise ValueError("TMeanLine missing lePos or StackingPhi.")
        return np.array(xs, dtype=float), np.array(ys, dtype=float), float(theta_node.text)

    hub_x, hub_beta, hub_theta = line_values(by_index[0], float(beta1[0].text), float(beta2[0].text))
    shroud_x, shroud_beta, shroud_theta = line_values(by_index[1], float(beta1[1].text), float(beta2[1].text))
    return BaselineShape(hub_beta, shroud_beta, hub_x, shroud_x, hub_theta, shroud_theta)


def vector_to_sample(config: dict[str, Any], x: np.ndarray) -> dict[str, float]:
    return {name: float(value) for name, value in zip(variable_names(config), x)}


def sample_to_vector(config: dict[str, Any], sample: dict[str, Any]) -> np.ndarray:
    return np.array([float(sample[name]) for name in variable_names(config)], dtype=float)


def geometry_indices(config: dict[str, Any]) -> dict[str, Any]:
    """Vector positions of the meanline offsets, looked up by variable name.

    Every geometry name must be present exactly once and no other name may
    appear: an unmapped variable would be sampled and modelled but never reach
    CFturbo.
    """
    names = variable_names(config)
    missing = [name for name in GEOMETRY_VARIABLES if name not in names]
    unknown = [name for name in names if name not in GEOMETRY_VARIABLES]
    if missing or unknown or len(names) != len(set(names)):
        raise ValueError(
            "config['variables'] must name each meanline offset exactly once; "
            f"missing: {missing or 'none'}; not mapped to geometry: {unknown or 'none'}"
        )
    position = {name: index for index, name in enumerate(names)}
    return {
        "hub_beta": [position[name] for name in HUB_BETA_VARIABLES],
        "shroud_beta": [position[name] for name in SHROUD_BETA_VARIABLES],
        "hub_theta": position[HUB_THETA_VARIABLE],
        "shroud_theta": position[SHROUD_THETA_VARIABLE],
    }


def candidate_geometry(config: dict[str, Any], baseline: BaselineShape, x: np.ndarray) -> dict[str, Any]:
    offsets = np.array(x, dtype=float)
    index = geometry_indices(config)
    hub_offsets = np.deg2rad(offsets[index["hub_beta"]])
    shroud_offsets = np.deg2rad(offsets[index["shroud_beta"]])
    hub_theta = baseline.hub_theta_rad + math.radians(float(offsets[index["hub_theta"]]))
    shroud_theta = baseline.shroud_theta_rad + math.radians(float(offsets[index["shroud_theta"]]))
    return {
        "hub_beta_rad": (baseline.hub_beta_rad + hub_offsets).tolist(),
        "shroud_beta_rad": (baseline.shroud_beta_rad + shroud_offsets).tolist(),
        "hub_theta_rad": float(hub_theta),
        "shroud_theta_rad": float(shroud_theta),
        "baseline_hub_beta_rad": baseline.hub_beta_rad.tolist(),
        "baseline_shroud_beta_rad": baseline.shroud_beta_rad.tolist(),
        "baseline_hub_theta_rad": float(baseline.hub_theta_rad),
        "baseline_shroud_theta_rad": float(baseline.shroud_theta_rad),
        "hub_x": baseline.hub_x.tolist(),
        "shroud_x": baseline.shroud_x.tolist(),
    }


def _interp_beta(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    grid = np.linspace(0.0, 1.0, 31)
    return np.interp(grid, xs, ys)


def constraint_violations(config: dict[str, Any], baseline: BaselineShape, x: np.ndarray) -> list[str]:
    x = np.asarray(x, dtype=float)
    if x.shape != (len(config['variables']),) or not np.isfinite(x).all():
        return ['invalid_vector']
    constraints = config["constraints"]
    geom = candidate_geometry(config, baseline, x)
    hub = np.array(geom["hub_beta_rad"], dtype=float)
    shroud = np.array(geom["shroud_beta_rad"], dtype=float)
    max_beta_offset = float(constraints["max_beta_offset_deg"])
    max_theta_offset = float(constraints["max_theta_offset_deg"])
    violations: list[str] = []
    if np.any(x < lower_bounds(config)-1e-10) or np.any(x > upper_bounds(config)+1e-10):
        violations.append('variable_bounds')
    if not refinement.same_slice_mask(config,x)[0]:
        violations.append('fixed_variables')
    if np.max(np.abs(np.rad2deg(hub - baseline.hub_beta_rad))) > max_beta_offset + 1e-9:
        violations.append("hub_beta_offset")
    if np.max(np.abs(np.rad2deg(shroud - baseline.shroud_beta_rad))) > max_beta_offset + 1e-9:
        violations.append("shroud_beta_offset")
    index = geometry_indices(config)
    if (abs(float(x[index["hub_theta"]])) > max_theta_offset + 1e-9
            or abs(float(x[index["shroud_theta"]])) > max_theta_offset + 1e-9):
        violations.append("theta_offset")

    hub_base_step = np.max(np.abs(np.diff(np.rad2deg(_interp_beta(baseline.hub_x, baseline.hub_beta_rad)))))
    shr_base_step = np.max(np.abs(np.diff(np.rad2deg(_interp_beta(baseline.shroud_x, baseline.shroud_beta_rad)))))
    requested = float(constraints["max_generated_beta_step_deg"])
    margin = float(constraints.get("baseline_step_margin_deg", 0.5))
    hub_limit = max(requested, hub_base_step + margin)
    shr_limit = max(requested, shr_base_step + margin)
    hub_step = np.max(np.abs(np.diff(np.rad2deg(_interp_beta(baseline.hub_x, hub)))))
    shr_step = np.max(np.abs(np.diff(np.rad2deg(_interp_beta(baseline.shroud_x, shroud)))))
    if hub_step > hub_limit + 1e-9:
        violations.append("hub_beta_step")
    if shr_step > shr_limit + 1e-9:
        violations.append("shroud_beta_step")
    return violations


def output_dir(config: dict[str, Any]) -> Path:
    value = str(config["paths"].get("output_dir") or "").strip()
    if not value:
        # Path('') would silently mean the current directory
        ini = config.get("_local_paths", {}).get("ini", "blade_shape_local.ini")
        raise ValueError(f"paths.output_dir is empty; set it in {ini} (see blade_shape_local.ini.example)")
    return Path(value)


def training_csv_path(config: dict[str, Any]) -> Path:
    return output_dir(config) / "training_data.csv"


def diagnostics_csv_path(config: dict[str, Any]) -> Path:
    return output_dir(config) / "active_learning_diagnostics.csv"


def all_columns(config: dict[str, Any]) -> list[str]:
    return variable_names(config) + RESULT_COLUMNS + SAMPLE_COLUMNS + STATUS_COLUMNS


def diagnostic_columns(config: dict[str, Any]) -> list[str]:
    columns = [
        "iteration",
        "run_id",
        "status",
        "selection_rank",
        "selection_source",
        "candidate_role", "slice_id", "candidate_on_slice", "inactive_distance_norm",
        "surrogate_model", "history_train_count", "slice_train_count", "calibration_count", "challenger_status",
        "hv_before", "hv_after", "hv_gain", "engineering_nondominated",
        "ehvi_samples", "ehvi_base_samples", "ehvi_base_estimate", "ehvi_sampling_change",
        "local_region_count", "local_radius_norm",
        "acquisition_score",
        "ehvi",
        "distance_to_existing",
        "pareto_rows_before",
        "pareto_rows_after",
        "case_dir",
        "failure_stage",
        "message",
    ]
    for objective in OBJECTIVE_COLUMNS:
        columns.extend([f"pred_{objective}", f"raw_std_{objective}", f"std_{objective}",
                        f"calibration_scale_{objective}", f"true_{objective}", f"prediction_error_{objective}",
                        f"challenger_pred_{objective}", f"challenger_std_{objective}", f"delta_best_{objective}"])
    return columns


def load_training(config: dict[str, Any]) -> pd.DataFrame:
    path = training_csv_path(config)
    if not path.exists():
        return pd.DataFrame(columns=all_columns(config))
    return normalize_training_frame(config, pd.read_csv(path))


def normalize_training_frame(config: dict[str, Any], frame: pd.DataFrame) -> pd.DataFrame:
    columns = list(dict.fromkeys(all_columns(config) + list(frame.columns)))
    if frame.empty:
        return pd.DataFrame(columns=columns)

    diagnostics = load_existing_diagnostics(config)
    initial_samples = int(config.get("runtime", {}).get("initial_samples", 0))
    batch_size = max(1, int(config.get("runtime", {}).get("batch_size", 1)))

    for column in columns:
        if column not in frame.columns:
            frame[column] = np.nan
    for column in ["sample_phase", "selection_source", "failure_stage", "message", "case_dir", "run_id"]:
        if column in frame.columns:
            frame[column] = frame[column].astype("object")

    for idx, row in frame.iterrows():
        if row.get('sample_phase') == 'boundary':
            continue
        run_number = parse_case_number(row.get("run_id", ""))
        diag = diagnostics.get(str(row.get("run_id", "")))
        if diag:
            inferred_phase = "active_learning"
        elif run_number is not None and run_number < initial_samples:
            inferred_phase = "doe"
        else:
            inferred_phase = "active_learning"

        if (
            pd.isna(frame.at[idx, "sample_phase"])
            or str(frame.at[idx, "sample_phase"]).strip() == ""
        ):
            frame.at[idx, "sample_phase"] = inferred_phase
        if frame.at[idx, "sample_phase"] == "doe":
            if pd.isna(frame.at[idx, "doe_index"]) and run_number is not None:
                frame.at[idx, "doe_index"] = run_number
            frame.at[idx, "al_iteration"] = np.nan
            frame.at[idx, "batch_index"] = np.nan
            frame.at[idx, "selection_rank"] = np.nan
            frame.at[idx, "selection_source"] = ""
        else:
            frame.at[idx, "doe_index"] = np.nan
            al_offset = None if run_number is None else max(0, run_number - initial_samples)
            if pd.isna(frame.at[idx, "al_iteration"]):
                if diag and str(diag.get("iteration", "")).strip() != "":
                    frame.at[idx, "al_iteration"] = diag.get("iteration")
                elif al_offset is not None:
                    frame.at[idx, "al_iteration"] = al_offset // batch_size
            if pd.isna(frame.at[idx, "batch_index"]):
                if diag and str(diag.get("selection_rank", "")).strip() != "":
                    frame.at[idx, "batch_index"] = diag.get("selection_rank")
                elif al_offset is not None:
                    frame.at[idx, "batch_index"] = al_offset % batch_size + 1
            if pd.isna(frame.at[idx, "selection_rank"]):
                if diag and str(diag.get("selection_rank", "")).strip() != "":
                    frame.at[idx, "selection_rank"] = diag.get("selection_rank")
                elif not pd.isna(frame.at[idx, "batch_index"]):
                    frame.at[idx, "selection_rank"] = frame.at[idx, "batch_index"]
            if pd.isna(frame.at[idx, "selection_source"]) or str(frame.at[idx, "selection_source"]).strip() == "":
                frame.at[idx, "selection_source"] = diag.get("selection_source", "unknown_active_learning") if diag else "unknown_active_learning"

    return frame[columns]


def parse_case_number(run_id: Any) -> int | None:
    text = str(run_id)
    if not text.startswith("case_"):
        return None
    try:
        return int(text.split("_", 1)[1])
    except Exception:
        return None


def load_existing_diagnostics(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    path = diagnostics_csv_path(config)
    if not path.exists():
        return {}
    try:
        frame = pd.read_csv(path)
    except Exception:
        return {}
    if "run_id" not in frame.columns:
        return {}
    return {str(row["run_id"]): row.to_dict() for _, row in frame.iterrows()}


def ensure_training_schema(config: dict[str, Any]) -> None:
    path = training_csv_path(config)
    if not path.exists():
        return
    frame = normalize_training_frame(config, pd.read_csv(path))
    frame.to_csv(path, index=False)


def append_compatible_csv(path: Path, row: dict[str, Any], columns: list[str],
                          normalizer: Any = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(path) if path.exists() else pd.DataFrame()
    if normalizer is not None and not existing.empty:
        existing = normalizer(existing)
    order = list(dict.fromkeys(list(existing.columns) + columns + list(row)))
    frame = pd.concat([existing.reindex(columns=order), pd.DataFrame([row]).reindex(columns=order)], ignore_index=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    frame.to_csv(temp, index=False)
    temp.replace(path)


def append_row(config: dict[str, Any], row: dict[str, Any]) -> None:
    append_compatible_csv(training_csv_path(config), row, all_columns(config),
                          lambda df: normalize_training_frame(config, df))


def append_diagnostic_row(config: dict[str, Any], row: dict[str, Any]) -> None:
    append_compatible_csv(diagnostics_csv_path(config), row, diagnostic_columns(config))


def write_iteration_summary(config: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    path = output_dir(config) / "iteration_summary.csv"
    frame = pd.DataFrame(rows)
    frame.to_csv(path, index=False)


def write_pareto(config: dict[str, Any], df: pd.DataFrame) -> pd.DataFrame:
    success = df[df["status"] == "success"].copy()
    # A success row with a missing/non-finite objective (e.g. a hand-edited CSV)
    # is never dominated under NaN comparisons and would leak into both fronts.
    objectives = success[OBJECTIVE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    success = success.loc[np.isfinite(objectives.to_numpy(dtype=float)).all(axis=1)]
    if success.empty:
        pareto = pd.DataFrame(columns=df.columns)
        strict_pareto = pd.DataFrame(columns=df.columns)
    else:
        y = success[OBJECTIVE_COLUMNS].astype(float).to_numpy()
        strict_mask = pareto_mask(y)
        strict_pareto = success.loc[strict_mask].copy()
        mask = pareto_mask(y, objective_tolerances(config))
        pareto = success.loc[mask].copy()
        pareto = pareto.sort_values(OBJECTIVE_COLUMNS, ascending=False)
        strict_pareto = strict_pareto.sort_values(OBJECTIVE_COLUMNS, ascending=False)
    pareto_path = output_dir(config) / "pareto_front.csv"
    strict_pareto_path = output_dir(config) / "pareto_front_strict.csv"
    pareto_path.parent.mkdir(parents=True, exist_ok=True)
    pareto.to_csv(pareto_path, index=False)
    strict_pareto.to_csv(strict_pareto_path, index=False)
    return pareto


def lhs_samples(config: dict[str, Any], count: int, seed: int) -> np.ndarray:
    lb = lower_bounds(config)
    ub = upper_bounds(config)
    if count <= 0:
        return np.empty((0, len(lb)))
    if qmc is not None:
        sampler = qmc.LatinHypercube(d=len(lb), seed=seed)
        unit = sampler.random(count)
    else:
        rng = np.random.default_rng(seed)
        unit = rng.random((count, len(lb)))
    return refinement.enforce_fixed(config, lb + unit * (ub - lb))


def normalize_x(config: dict[str, Any], x: np.ndarray) -> np.ndarray:
    lb = lower_bounds(config)
    ub = upper_bounds(config)
    return (x - lb) / np.maximum(ub - lb, 1e-12)


def existing_vectors(config: dict[str, Any], df: pd.DataFrame) -> np.ndarray:
    names = variable_names(config)
    if df.empty:
        return np.empty((0, len(names)))
    present = df.dropna(subset=names)
    if present.empty:
        return np.empty((0, len(names)))
    return present[names].astype(float).to_numpy()


def is_far_enough(config: dict[str, Any], x: np.ndarray, existing: np.ndarray) -> bool:
    if existing.size == 0:
        return True
    distance_limit = float(config["constraints"].get("duplicate_distance_norm", 0.02))
    return float(refinement.active_distances(config, x[None, :], existing)[0]) >= distance_limit


def valid_random_samples(
    config: dict[str, Any],
    baseline: BaselineShape,
    count: int,
    seed: int,
    existing: np.ndarray,
) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    lb = lower_bounds(config)
    ub = upper_bounds(config)
    out: list[np.ndarray] = []
    attempts = 0
    while len(out) < count and attempts < count * 500:
        attempts += 1
        x = refinement.enforce_fixed(config, lb + rng.random(len(lb)) * (ub - lb))
        if constraint_violations(config, baseline, x):
            continue
        if not is_far_enough(config, x, existing):
            continue
        if out and not is_far_enough(config, x, np.vstack(out)):
            continue
        out.append(x)
    return out


class RbfRidgeEnsemble:
    def __init__(self, config: dict[str, Any], n_models: int = 16, ridge: float = 1e-6, seed: int = 42):
        self.config = config
        self.n_models = n_models
        self.ridge = ridge
        self.seed = seed
        self.models: list[dict[str, np.ndarray]] = []
        self.y_mean: np.ndarray | None = None
        self.y_std: np.ndarray | None = None

    def fit(self, x_raw: np.ndarray, y_raw: np.ndarray) -> "RbfRidgeEnsemble":
        rng = np.random.default_rng(self.seed)
        x = normalize_x(self.config, x_raw)
        self.y_mean = y_raw.mean(axis=0)
        self.y_std = y_raw.std(axis=0)
        self.y_std[self.y_std < 1e-9] = 1.0
        y = (y_raw - self.y_mean) / self.y_std
        n = len(x)
        center_count = min(max(4, n), 32)
        self.models.clear()
        for _ in range(self.n_models):
            idx = rng.integers(0, n, size=n)
            center_idx = rng.choice(n, size=center_count, replace=n < center_count)
            centers = x[center_idx]
            sigma = float(np.median(pairwise_distances(centers, centers)))
            if not np.isfinite(sigma) or sigma < 1e-6:
                sigma = 0.35
            phi = rbf_features(x[idx], centers, sigma)
            a = phi.T @ phi + self.ridge * np.eye(phi.shape[1])
            b = phi.T @ y[idx]
            weights = np.linalg.solve(a, b)
            self.models.append({"centers": centers, "sigma": np.array([sigma]), "weights": weights})
        return self

    def predict(self, x_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.y_mean is None or self.y_std is None or not self.models:
            raise RuntimeError("Surrogate has not been fitted.")
        x = normalize_x(self.config, x_raw)
        preds = []
        for model in self.models:
            phi = rbf_features(x, model["centers"], float(model["sigma"][0]))
            pred = phi @ model["weights"]
            preds.append(pred * self.y_std + self.y_mean)
        stack = np.stack(preds, axis=0)
        return stack.mean(axis=0), stack.std(axis=0)


class GpKrigingSurrogate:
    """Independent-output Gaussian-process surrogate for EGO/MOEGO sampling."""

    def __init__(self, config: dict[str, Any], seed: int = 42):
        if GaussianProcessRegressor is None:
            raise RuntimeError("scikit-learn is not available.")
        self.config = config
        self.seed = seed
        self.models: list[Any] = []
        self.y_mean: np.ndarray | None = None
        self.y_std: np.ndarray | None = None

    def fit(self, x_raw: np.ndarray, y_raw: np.ndarray) -> "GpKrigingSurrogate":
        x = normalize_x(self.config, x_raw)
        self.y_mean = y_raw.mean(axis=0)
        self.y_std = y_raw.std(axis=0)
        self.y_std[self.y_std < 1e-9] = 1.0
        y = (y_raw - self.y_mean) / self.y_std
        self.models.clear()
        for target_idx in range(y.shape[1]):
            kernel = (
                ConstantKernel(1.0, (1e-3, 1e3))
                * Matern(length_scale=np.ones(x.shape[1]), length_scale_bounds=(1e-2, 1e2), nu=2.5)
                + WhiteKernel(noise_level=1e-5, noise_level_bounds=(1e-8, 1e-1))
            )
            model = GaussianProcessRegressor(
                kernel=kernel,
                normalize_y=False,
                n_restarts_optimizer=3,
                random_state=self.seed + target_idx,
            )
            model.fit(x, y[:, target_idx])
            self.models.append(model)
        return self

    def predict(self, x_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.y_mean is None or self.y_std is None or not self.models:
            raise RuntimeError("Surrogate has not been fitted.")
        x = normalize_x(self.config, x_raw)
        means = []
        stds = []
        for model_idx, model in enumerate(self.models):
            mean, std = model.predict(x, return_std=True)
            means.append(mean * self.y_std[model_idx] + self.y_mean[model_idx])
            stds.append(np.maximum(std * self.y_std[model_idx], 1e-12))
        return np.column_stack(means), np.column_stack(stds)


SURROGATE_MODELS = {
    "gp": "gp", "kriging": "gp", "gaussian_process": "gp",
    "rbf": "rbf_ridge_ensemble", "rbf_ridge_ensemble": "rbf_ridge_ensemble",
}
NO_FALLBACK = "none"


def surrogate_choice(config: dict[str, Any]) -> tuple[str, str]:
    """Canonical (model, fallback_model); unknown names are errors, not silent RBF."""
    settings = config.get("surrogate", {})
    model = str(settings.get("model", "gp")).lower()
    fallback = str(settings.get("fallback_model", "rbf_ridge_ensemble")).lower()
    if model not in SURROGATE_MODELS:
        raise ValueError(f"surrogate.model must be one of {sorted(SURROGATE_MODELS)}, got {model!r}")
    if fallback != NO_FALLBACK and fallback not in SURROGATE_MODELS:
        raise ValueError(f"surrogate.fallback_model must be one of {sorted(SURROGATE_MODELS)} "
                         f"or {NO_FALLBACK!r}, got {fallback!r}")
    return SURROGATE_MODELS[model], fallback if fallback == NO_FALLBACK else SURROGATE_MODELS[fallback]


def _gp_unavailable(x_train: np.ndarray) -> str:
    if GaussianProcessRegressor is None:
        return "scikit-learn is unavailable"
    if len(x_train) < 3:
        return f"GP needs at least 3 samples, got {len(x_train)}"
    return ""


def fit_surrogate(config: dict[str, Any], x_train: np.ndarray, y_train: np.ndarray, seed: int) -> Any:
    model, fallback = surrogate_choice(config)
    reason = _gp_unavailable(x_train) if model == "gp" else ""
    if not reason:
        return (GpKrigingSurrogate(config, seed=seed) if model == "gp"
                else RbfRidgeEnsemble(config, seed=seed)).fit(x_train, y_train)
    if fallback == NO_FALLBACK or (fallback == "gp" and _gp_unavailable(x_train)):
        raise RuntimeError(f"Surrogate {model} unavailable ({reason}) and fallback_model={fallback!r} cannot replace it.")
    print(f"[surrogate] {reason}; using fallback_model {fallback}.")
    return (GpKrigingSurrogate(config, seed=seed) if fallback == "gp"
            else RbfRidgeEnsemble(config, seed=seed)).fit(x_train, y_train)


def pairwise_distances(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    diff = a[:, None, :] - b[None, :, :]
    return np.sqrt(np.sum(diff * diff, axis=2))


def rbf_features(x: np.ndarray, centers: np.ndarray, sigma: float) -> np.ndarray:
    d = pairwise_distances(x, centers)
    phi = np.exp(-(d * d) / (2.0 * sigma * sigma))
    return np.column_stack([np.ones(len(x)), phi])


def objective_tolerances(config: dict[str, Any]) -> np.ndarray:
    pareto_settings = config.get("pareto", {})
    if not bool(pareto_settings.get("use_engineering_tolerance", False)):
        return np.zeros(len(OBJECTIVE_COLUMNS), dtype=float)
    values = pareto_settings.get("tolerances", {})
    return np.array([float(values.get(name, 0.0)) for name in OBJECTIVE_COLUMNS], dtype=float)


def pareto_mask(objectives: np.ndarray, eps: np.ndarray | None = None) -> np.ndarray:
    n = len(objectives)
    mask = np.ones(n, dtype=bool)
    tolerance = np.zeros(objectives.shape[1], dtype=float) if eps is None else np.array(eps, dtype=float)
    for i in range(n):
        if not mask[i]:
            continue
        dominates_i = np.all(objectives >= objectives[i] - tolerance, axis=1) & np.any(
            objectives > objectives[i] + tolerance, axis=1
        )
        dominates_i[i] = False
        if np.any(dominates_i):
            mask[i] = False
    return mask


def non_dominated_sort(objectives: np.ndarray) -> list[list[int]]:
    n = len(objectives)
    dominates = [set() for _ in range(n)]
    dominated_count = np.zeros(n, dtype=int)
    fronts: list[list[int]] = [[]]
    for p in range(n):
        for q in range(n):
            if p == q:
                continue
            if np.all(objectives[p] >= objectives[q]) and np.any(objectives[p] > objectives[q]):
                dominates[p].add(q)
            elif np.all(objectives[q] >= objectives[p]) and np.any(objectives[q] > objectives[p]):
                dominated_count[p] += 1
        if dominated_count[p] == 0:
            fronts[0].append(p)
    current = 0
    while current < len(fronts) and fronts[current]:
        next_front: list[int] = []
        for p in fronts[current]:
            for q in dominates[p]:
                dominated_count[q] -= 1
                if dominated_count[q] == 0:
                    next_front.append(q)
        current += 1
        if next_front:
            fronts.append(next_front)
    return fronts


def crowding_distance(objectives: np.ndarray, front: list[int]) -> dict[int, float]:
    if not front:
        return {}
    distance = {idx: 0.0 for idx in front}
    values = objectives[front]
    for j in range(values.shape[1]):
        order = np.argsort(values[:, j])
        distance[front[order[0]]] = float("inf")
        distance[front[order[-1]]] = float("inf")
        span = values[order[-1], j] - values[order[0], j]
        if abs(span) < 1e-12:
            continue
        for k in range(1, len(order) - 1):
            distance[front[order[k]]] += float((values[order[k + 1], j] - values[order[k - 1], j]) / span)
    return distance


def nsga2_candidates(
    config: dict[str, Any],
    baseline: BaselineShape,
    surrogate: Any,
    existing: np.ndarray,
    seed: int,
    regions: list[dict[str, Any]] | None = None,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    lb = lower_bounds(config)
    ub = upper_bounds(config)
    pop_size = int(config["runtime"]["nsga2_pop_size"])
    generations = int(config["runtime"]["nsga2_generations"])
    pop = np.array(refinement.local_samples(config,baseline,pop_size,seed,existing,regions) if regions
                   else valid_random_samples(config, baseline, pop_size, seed, existing))
    if len(pop) < pop_size:
        if not len(pop):
            return np.empty((0, len(lb)))
        pop_size = len(pop)

    for _ in range(generations):
        mean, _ = surrogate.predict(pop)
        fronts = non_dominated_sort(mean)
        rank = np.full(len(pop), 9999, dtype=int)
        crowd = np.zeros(len(pop), dtype=float)
        for r, front in enumerate(fronts):
            rank[front] = r
            cd = crowding_distance(mean, front)
            for idx, value in cd.items():
                crowd[idx] = value

        children = []
        attempts = 0
        while len(children) < pop_size and attempts < pop_size * 100:
            attempts += 1
            p1 = tournament(pop, rank, crowd, rng)
            p2 = tournament(pop, rank, crowd, rng)
            alpha = rng.random(len(lb))
            c1 = alpha * p1 + (1.0 - alpha) * p2
            c2 = alpha * p2 + (1.0 - alpha) * p1
            for child in [c1, c2]:
                mutation = rng.normal(0.0, 0.08, size=len(lb)) * (ub - lb)
                mask = rng.random(len(lb)) < 0.25
                mask[[i for i in range(len(lb)) if i not in refinement.active_indices(config)]] = False
                child = refinement.enforce_fixed(config, np.clip(child + mask * mutation, lb, ub))
                if regions:
                    idx=refinement.active_indices(config)
                    region=min(regions,key=lambda q:np.linalg.norm((child[idx]-q['center'][idx])/(ub-lb)[idx]))
                    child=np.clip(child,region['lower'],region['upper'])
                if not constraint_violations(config, baseline, child):
                    children.append(child)
                if len(children) >= pop_size:
                    break
        if not children:
            break
        combined = np.vstack([pop, np.array(children)])
        combined_mean, _ = surrogate.predict(combined)
        fronts = non_dominated_sort(combined_mean)
        next_pop = []
        for front in fronts:
            if len(next_pop) + len(front) <= pop_size:
                next_pop.extend(front)
            else:
                cd = crowding_distance(combined_mean, front)
                ordered = sorted(front, key=lambda idx: cd.get(idx, 0.0), reverse=True)
                next_pop.extend(ordered[: pop_size - len(next_pop)])
                break
        pop = combined[next_pop]
    return pop


def tournament(pop: np.ndarray, rank: np.ndarray, crowd: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    a, b = rng.integers(0, len(pop), size=2)
    if rank[a] < rank[b]:
        return pop[a]
    if rank[b] < rank[a]:
        return pop[b]
    return pop[a] if crowd[a] >= crowd[b] else pop[b]


def select_acquisition(
    config: dict[str, Any],
    baseline: BaselineShape,
    candidates: np.ndarray,
    candidate_sources: list[str],
    surrogate: Any,
    existing: np.ndarray,
    observed_objectives: np.ndarray,
    count: int,
) -> list[SelectedCandidate]:
    if len(candidates) == 0:
        return []
    if len(candidate_sources) != len(candidates):
        raise ValueError("candidate_sources must have the same length as candidates.")
    mean, std = surrogate.predict(candidates)
    acquisition = expected_hvi(config, observed_objectives, mean, std)
    ehvi = acquisition['scores']
    dist = refinement.active_distances(config, candidates, existing)
    selected: list[SelectedCandidate] = []
    selected_existing = existing.copy()
    available = np.ones(len(candidates), dtype=bool)
    roles = config.get('refinement', {}).get('candidate_roles', ['ehvi', 'uncertainty', 'diversity'])
    if not roles or set(roles) - {'ehvi','uncertainty','diversity'}:
        raise ValueError('Unknown or empty candidate roles.')
    for rank in range(count):
        role = roles[rank % len(roles)]
        dist = refinement.active_distances(config, candidates, selected_existing)
        if role == 'ehvi':
            score = ehvi
        elif role == 'uncertainty':
            tolerance = np.maximum(objective_tolerances(config), 1e-12)
            score = (std / tolerance).mean(axis=1)
        else:
            score = dist
        for idx in np.argsort(score)[::-1]:
            if not available[idx]:
                continue
            available[idx] = False
            x = candidates[idx]
            if constraint_violations(config, baseline, x) or not is_far_enough(config, x, selected_existing):
                continue
            selected.append(SelectedCandidate(
                x=x, selection_rank=len(selected)+1, selection_source=candidate_sources[idx],
                candidate_role=role, acquisition_score=float(score[idx]), ehvi=float(ehvi[idx]),
                distance_to_existing=float(dist[idx]), pred_mean=mean[idx].copy(), pred_std=std[idx].copy(),
                metadata={**(surrogate.metadata(x) if hasattr(surrogate,'metadata') else {}),
                          'ehvi_samples':acquisition['samples'], 'ehvi_base_samples':acquisition['base_samples'],
                          'ehvi_base_estimate':float(acquisition['base_scores'][idx]),
                          'ehvi_sampling_change':float(acquisition['sampling_change'][idx])}))
            selected_existing = np.vstack([selected_existing, x[None,:]]) if selected_existing.size else x[None,:]
            break

    return selected


def normalize_vector(values: np.ndarray) -> np.ndarray:
    lo = float(np.min(values))
    hi = float(np.max(values))
    return (values - lo) / max(hi - lo, 1e-12)


def normalize_objectives(values: np.ndarray) -> np.ndarray:
    lo = values.min(axis=0)
    hi = values.max(axis=0)
    return (values - lo) / np.maximum(hi - lo, 1e-12)


def approximate_expected_hvi(
    config: dict[str, Any],
    observed_objectives: np.ndarray,
    mean: np.ndarray,
    std: np.ndarray,
) -> np.ndarray:
    """Exact 2D improvement for common (quasi-)Monte-Carlo predictive samples."""
    if len(mean) == 0:
        return np.array([])
    return expected_hvi(config, observed_objectives, mean, std)['scores']


def dominated_by_any(points: np.ndarray, pareto: np.ndarray) -> np.ndarray:
    if pareto.size == 0:
        return np.zeros(len(points), dtype=bool)
    dominated = np.zeros(len(points), dtype=bool)
    for y in pareto:
        dominated |= np.all(points <= y, axis=1)
    return dominated


def fallback_selections(samples: list[np.ndarray], source: str, start_rank: int = 1) -> list[SelectedCandidate]:
    return [
        SelectedCandidate(x=x, selection_rank=start_rank + idx, selection_source=source, candidate_role="fallback")
        for idx, x in enumerate(samples)
    ]


def build_diagnostic_row(
    config: dict[str, Any],
    *,
    iteration: int,
    selected: SelectedCandidate,
    result: CaseResult,
    pareto_rows_before: int,
    pareto_rows_after: int,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "iteration": iteration,
        "run_id": result.row.get("run_id", ""),
        "status": result.row.get("status", ""),
        "selection_rank": selected.selection_rank,
        "selection_source": selected.selection_source,
        "candidate_role": selected.candidate_role,
        **selected.metadata,
        "acquisition_score": selected.acquisition_score,
        "ehvi": selected.ehvi,
        "distance_to_existing": selected.distance_to_existing,
        "pareto_rows_before": pareto_rows_before,
        "pareto_rows_after": pareto_rows_after,
        "case_dir": result.row.get("case_dir", ""),
        "failure_stage": result.row.get("failure_stage", ""),
        "message": result.row.get("message", ""),
    }
    for idx, objective in enumerate(OBJECTIVE_COLUMNS):
        pred = float(selected.pred_mean[idx]) if selected.pred_mean is not None else np.nan
        std = float(selected.pred_std[idx]) if selected.pred_std is not None else np.nan
        true = float(result.row.get(objective, np.nan))
        row[f"pred_{objective}"] = pred
        row[f"std_{objective}"] = std
        row[f"true_{objective}"] = true
        row[f"prediction_error_{objective}"] = true - pred if np.isfinite(true) and np.isfinite(pred) else np.nan
    return row


def next_case_index(config: dict[str, Any], df: pd.DataFrame) -> int:
    ids = []
    if not df.empty and "run_id" in df.columns:
        for value in df["run_id"].dropna().astype(str):
            if value.startswith("case_"):
                try:
                    ids.append(int(value.split("_")[1]))
                except Exception:
                    pass
    ids.extend(parse_case_number(name) for name in case_reservations(output_dir(config)) if parse_case_number(name) is not None)
    queue = pending.load(config)
    ids.extend(parse_case_number(e['run_id']) for e in queue['entries'] if parse_case_number(e['run_id']) is not None)
    cases = output_dir(config) / "cases"
    if not cases.exists():
        return max(ids) + 1 if ids else 0
    for path in cases.glob("case_*"):
        try:
            ids.append(int(path.name.split("_")[1]))
        except Exception:
            pass
    return max(ids) + 1 if ids else 0


def write_candidate_files(
    config: dict[str, Any],
    baseline: BaselineShape,
    x: np.ndarray,
    run_id: str,
) -> Path:
    case_dir = output_dir(config) / "cases" / run_id
    case_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "variables": vector_to_sample(config, x),
        "geometry": candidate_geometry(config, baseline, x),
        "constraints": config["constraints"],
    }
    candidate_path = case_dir / "candidate.json"
    candidate_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    pd.DataFrame([vector_to_sample(config, x)]).to_csv(case_dir / "blade_shape_params.csv", index=False)
    return candidate_path


def run_geometry(config: dict[str, Any], candidate_path: Path, dry_run: bool = False) -> tuple[bool, str]:
    paths = config["paths"]
    cmd = [
        str(paths["powershell_exe"]),
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(paths["geometry_script_path"]),
        "-CandidateJson",
        str(candidate_path),
        "-WorkingDir",
        str(candidate_path.parent),
        "-CFturboExe",
        str(paths["cfturbo_exe"]),
        "-TurboGridExe",
        str(paths["turbogrid_exe"]),
        "-CftBatchTemplate",
        str(paths["cft_batch_template"]),
        "-BaseCft",
        str(paths["base_cft"]),
        "-TurboGridTemplate",
        str(paths["turbogrid_template"]),
    ]
    # The mesh periodicity must match the count CFX uses to scale MassFlow/Power.
    # The script used to hard-code 10; only pass the flag when it differs, so an
    # older script copy still works for 10 blades and fails loudly otherwise.
    n_blades = int(config["runtime"]["n_blades"])
    if n_blades != 10:
        cmd += ["-BladeCount", str(n_blades)]
    if dry_run:
        cmd.append("-DryRun")
    log_path = candidate_path.parent / ("geometry_dry_run.log" if dry_run else "geometry.log")
    with log_path.open("w", encoding="utf-8", errors="ignore") as log:
        log.write("COMMAND: " + " ".join(cmd) + "\n\n")
        proc = subprocess.run(
            cmd,
            cwd=str(candidate_path.parent),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    if proc.returncode != 0:
        return False, f"geometry failed with exit code {proc.returncode}; see {log_path}"
    return True, "geometry success"


def evaluate_true_cfd(
    config: dict[str, Any],
    baseline: BaselineShape,
    x: np.ndarray,
    case_index: int,
    *,
    sample_phase: str,
    doe_index: int | float = np.nan,
    al_iteration: int | float = np.nan,
    batch_index: int | float = np.nan,
    selection_rank: int | float = np.nan,
    selection_source: str = "",
    experiment_id: str = "",
    design_role: str = "",
) -> CaseResult:
    run_id = f"case_{case_index:06d}"
    sample = vector_to_sample(config, x)
    row: dict[str, Any] = {**sample, **{col: np.nan for col in RESULT_COLUMNS}}
    row.update(
        {
            "sample_phase": sample_phase,
            "doe_index": doe_index,
            "al_iteration": al_iteration,
            "batch_index": batch_index,
            "selection_rank": selection_rank,
            "selection_source": selection_source,
            "experiment_id": experiment_id,
            "design_role": design_role,
            "status": "failed",
            "failure_stage": "",
            "message": "",
            "case_dir": "",
            "run_id": run_id,
        }
    )

    violations = constraint_violations(config, baseline, x)
    if violations:
        row["failure_stage"] = "geometry_rules"
        row["message"] = ";".join(violations)
        append_row(config, row)
        return CaseResult(row, False)

    case_dir = output_dir(config) / 'cases' / run_id
    candidate_path = case_dir / 'candidate.json'
    if candidate_path.exists():
        previous = json.loads(candidate_path.read_text())
        if not np.allclose([previous['variables'][n] for n in variable_names(config)],x,rtol=0,atol=1e-10):
            raise ValueError('Refusing to overwrite a different existing candidate.')
        if previous.get('run_id') != run_id or previous.get('geometry') != candidate_geometry(config,baseline,x):
            raise ValueError('Existing candidate geometry does not match its parameter vector and baseline.')
    else:
        candidate_path = write_candidate_files(config, baseline, x, run_id)
    row['case_dir'] = str(case_dir)
    receipt_path = case_dir / 'geometry_state.json'
    signature = refinement.digest({'physical':refinement.physical_signature(config), 'x':x.tolist(), 'candidate':file_identity(candidate_path)})
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    if receipt is not None:
        if receipt.get('signature') != signature or receipt.get('status') != 'complete' or receipt.get('mesh') != file_identity(case_dir/'Impeller_Mesh.gtm'):
            row.update(failure_stage='geometry',message='Geometry receipt is incomplete or inputs/mesh changed. Inspect this case.')
            append_row(config,row)
            return CaseResult(row,False)
    else:
        if (case_dir/'cfx_state.json').exists() or (case_dir/'CFX_Results.txt').exists() or list(case_dir.glob('*.res')):
            row.update(failure_stage='geometry',message='Unverified legacy geometry/CFX artifacts; no geometry completion receipt.')
            append_row(config,row)
            return CaseResult(row,False)
        atomic_json(receipt_path,{'signature':signature,'status':'running'})
        try:
            ok,msg = run_geometry(config,candidate_path,dry_run=False)
        except OSError as exc:
            ok,msg=False,str(exc)
            row['failure_stage']='environment'
            (case_dir/'environment.log').write_text(msg,encoding='utf-8')
        if not ok:
            row['failure_stage']=row['failure_stage'] or 'geometry'
            row['message']=msg
            atomic_json(receipt_path,{'signature':signature,'status':'failed','message':msg})
            append_row(config,row)
            return CaseResult(row,False)
        mesh = file_identity(case_dir/'Impeller_Mesh.gtm')
        if mesh is None:
            row.update(failure_stage='mesh',message='Geometry layer returned success without a mesh.')
            atomic_json(receipt_path,{'signature':signature,'status':'failed','message':row['message']})
            append_row(config,row)
            return CaseResult(row,False)
        atomic_json(receipt_path,{'signature':signature,'status':'complete','mesh':mesh})

    runtime = config["runtime"]
    paths = config["paths"]
    try:
        cfx = run_cfx_pipeline(
            candidate_path.parent,
            run_id,
            p_out_pa=float(runtime["p_out_pa"]),
            cfx_bin_dir=paths["cfx_bin_dir"],
            template_cfx=paths["template_cfx"],
            template_cse=paths["template_cse"],
            cores=int(runtime["cfx_cores"]),
            n_blades=int(runtime["n_blades"]),
            convergence=ConvergencePolicy.from_config(config),
        )
    except OSError as exc:
        row['failure_stage'] = 'environment'
        row['message'] = str(exc)
        (candidate_path.parent / 'environment.log').write_text(str(exc), encoding='utf-8')
        append_row(config, row)
        return CaseResult(row, False)
    if not cfx.success:
        row["failure_stage"] = cfx.failure_stage or "cfx"
        row["message"] = cfx.message
        append_row(config, row)
        return CaseResult(row, False)

    for col in RESULT_COLUMNS:
        row[col] = float(cfx.metrics.get(col, np.nan))
    if not np.isfinite([row[o] for o in OBJECTIVE_COLUMNS]).all():
        row['failure_stage'] = 'post'
        row['message'] = 'Non-finite objective returned by CFX-Post.'
        append_row(config, row)
        return CaseResult(row, False)
    row["status"] = "success"
    row["failure_stage"] = ""
    row["message"] = cfx.message
    append_row(config, row)
    return CaseResult(row, True)


def run_loop(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    with output_lock(output_dir(config)):
        _run_loop(args, config)


def _run_loop(args: argparse.Namespace, config: dict[str, Any]) -> None:
    runtime = config['runtime']
    for name in ['initial_samples', 'iterations', 'batch_size', 'max_new_cfd', 'seed']:
        value = getattr(args, name, None)
        if value is not None:
            runtime[name] = int(value)
    if runtime['max_new_cfd'] < 0 or runtime['batch_size'] < 1 or runtime['iterations'] < 0:
        raise ValueError('Invalid run budget or batch size.')
    out = output_dir(config)
    out.mkdir(parents=True, exist_ok=True)
    df = load_training(config)
    if not args.resume and (not df.empty or pending.load(config)['entries']):
        raise SystemExit('Existing training/pending data found. Use --resume.')
    pending.check_orphans(config, df)
    baseline = extract_baseline(config)
    seed, max_new = int(runtime['seed']), int(runtime['max_new_cfd'])
    recovered = pending.run(config, baseline, max_new)
    used_new, failures, summary = recovered['new_attempts'], recovered['failures'], recovered['summary']
    df = load_training(config)
    if len(df) < runtime['initial_samples'] and used_new < max_new:
        needed = min(runtime['initial_samples']-len(df), max_new-used_new)
        existing = existing_vectors(config, df)
        initial = []
        for x in lhs_samples(config, needed*4, seed):
            if len(initial) >= needed:break
            if not constraint_violations(config, baseline, x) and is_far_enough(config,x,existing):
                initial.append(x)
                existing = np.vstack([existing,x[None,:]]) if existing.size else x[None,:]
        if len(initial) < needed:
            initial.extend(valid_random_samples(config,baseline,needed-len(initial),seed+1000,existing))
        candidates = fallback_selections(initial, 'doe')
        index = next_case_index(config,df)
        pending.enqueue(config,candidates,[{'sample_phase':'doe','doe_index':index+i} for i in range(len(candidates))])
        result=pending.run(config,baseline,max_new-used_new)
        used_new+=result['new_attempts'];failures+=result['failures'];summary.extend(result['summary'])
    df=load_training(config)
    iterations=pd.to_numeric(df.loc[df.sample_phase.eq('active_learning'),'al_iteration'],errors='coerce')
    first=int(iterations.max())+1 if iterations.notna().any() else 0
    for iteration in range(first,first+runtime['iterations']):
        if used_new>=max_new:break
        df=load_training(config)
        success,_=refinement.training_partition(config,df)
        existing=existing_vectors(config,df)
        batch=min(runtime['batch_size'],max_new-used_new)
        if len(success)>=4:
            reference=refinement.hv_reference(config,df)
            config['_ehvi_reference']=reference.tolist()
            diagnostics=pd.read_csv(diagnostics_csv_path(config)) if diagnostics_csv_path(config).exists() else pd.DataFrame()
            surrogate=refinement.ConditionalSurrogate(config,success,fit_surrogate,seed+iteration,diagnostics)
            regions=refinement.local_regions(config,df)
            nsga=nsga2_candidates(config,baseline,surrogate,existing,seed+iteration*17,regions=regions)
            pool,sources=refinement.candidate_pool(config,baseline,df,seed+iteration*31,regions)
            candidates=np.vstack([nsga,pool]) if len(pool) else nsga
            selected=select_acquisition(config,baseline,candidates,['nsga_local' if regions else 'nsga']*len(nsga)+sources,
                                          surrogate,existing,success[OBJECTIVE_COLUMNS].to_numpy(float),batch)
            if len(selected)<batch:
                extra=valid_random_samples(config,baseline,batch-len(selected),seed+iteration*97,existing)
                selected.extend(fallback_selections(extra,'fallback_random',len(selected)+1))
        else:
            selected=fallback_selections(valid_random_samples(config,baseline,batch,seed+iteration*97,existing),'fallback_random')
        if not selected:
            raise RuntimeError('No feasible candidates could be generated; no CFD was started.')
        if len(success)>=4:
            for item in selected:
                item.metadata.update(local_region_count=len(regions),local_radius_norm=regions[0]['radius_norm'] if regions else 0.)
        arguments=[{'sample_phase':'active_learning','al_iteration':iteration,'batch_index':c.selection_rank,
                    'selection_rank':c.selection_rank,'selection_source':c.selection_source} for c in selected]
        pending.enqueue(config,selected,arguments)
        result=pending.run(config,baseline,max_new-used_new)
        used_new+=result['new_attempts'];failures+=result['failures'];summary.extend(result['summary'])
    write_iteration_summary(config,summary)
    front=write_pareto(config,load_training(config))
    print(f'Done. New CFD attempts: {used_new}. Failed outcomes: {failures}. Pareto rows: {len(front)}. Output: {out}')
    if failures:
        raise SystemExit(1)


def write_candidate_command(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    baseline = extract_baseline(config)
    seed = int(args.seed if args.seed is not None else config["runtime"]["seed"])
    count = max(1, int(args.index) + 1)
    samples = lhs_samples(config, count, seed)
    x = samples[int(args.index)]
    if constraint_violations(config, baseline, x):
        alternatives = valid_random_samples(config, baseline, 1, seed + int(args.index) + 100, np.empty((0, len(x))))
        if not alternatives:
            raise SystemExit("Could not generate a valid candidate.")
        x = alternatives[0]
    run_id = f"dry_candidate_{int(args.index):03d}"
    candidate_path = write_candidate_files(config, baseline, x, run_id)
    if args.dry_run and not args.offline:
        ok, msg = run_geometry(config, candidate_path, dry_run=True)
        if not ok:
            raise SystemExit(msg)
    if args.offline:
        print('OFFLINE: Python candidate/geometry rules only; CFturbo XML and external programs not validated.')
    print(candidate_path)


def refinement_command(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.command == 'diagnose':
        history, local = refinement.training_partition(config, load_training(config))
        report = history.copy()
        report['inactive_distance_norm'] = refinement.slice_distance(config, report[variable_names(config)].to_numpy())
        report['on_current_slice'] = refinement.same_slice_mask(config, report[variable_names(config)].to_numpy())
        output_dir(config).mkdir(parents=True, exist_ok=True)
        report.to_csv(output_dir(config) / 'training_slice_audit.csv', index=False)
        print(json.dumps({'history_successes':len(history), 'same_slice_successes':len(local),
                          'gate':refinement.write_diagnostics(config)}, indent=2))
    elif args.command == 'write-boundary-plan':
        plan = refinement.write_boundary_plan(config, args.center_run_id, Path(args.plan))
        print(json.dumps({'plan':args.plan, 'plan_id':plan['plan_id'], 'points':len(plan['points']), 'stages':plan['stages']}, indent=2))
    else:
        result = refinement.run_boundary(config, Path(args.plan), args.stage, args.max_new_cfd, args.resume)
        print(json.dumps(result, indent=2))
        if result['stage_status'] == 'failed':
            raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Blade-shape active learning with real CFD feedback.")
    parser.add_argument("--config", default="blade_shape_config.json")
    sub = parser.add_subparsers(dest="command")
    # Sub-parser defaults overwrite a value parsed before the sub-command, so a
    # leading ``--config other.json run`` used to fall back silently to the
    # default file. SUPPRESS keeps the top-level value unless repeated here.
    config_default = argparse.SUPPRESS

    run = sub.add_parser("run", help="Run the active-learning CFD loop.")
    run.add_argument("--config", default=config_default)
    run.add_argument("--initial-samples", type=int)
    run.add_argument("--iterations", type=int)
    run.add_argument("--batch-size", type=int)
    run.add_argument("--max-new-cfd", type=int)
    run.add_argument("--seed", type=int)
    run.add_argument("--resume", action="store_true")
    run.set_defaults(func=run_loop)

    wc = sub.add_parser("write-candidate", help="Write one candidate JSON and optionally dry-run XML generation.")
    wc.add_argument("--config", default=config_default)
    wc.add_argument("--index", type=int, default=0)
    wc.add_argument("--seed", type=int)
    wc.add_argument("--dry-run", action="store_true")
    wc.add_argument("--offline", action="store_true", help="Validate Python candidate rules only; skip PowerShell.")
    wc.set_defaults(func=write_candidate_command)
    for name in ['diagnose', 'write-boundary-plan', 'run-boundary']:
        command = sub.add_parser(name)
        command.add_argument('--config', default=config_default)
        if name != 'diagnose':
            command.add_argument('--plan', required=True)
        if name == 'write-boundary-plan':
            command.add_argument('--center-run-id', required=True)
        if name == 'run-boundary':
            command.add_argument('--stage', choices=['singles','pairs','extension'], required=True)
            command.add_argument('--max-new-cfd', type=int, required=True)
            command.add_argument('--resume', action='store_true')
        command.set_defaults(func=refinement_command)
    return parser


def main() -> None:
    parser = build_parser()
    argv = sys.argv[1:]
    args = parser.parse_args(argv)
    if args.command is None:
        # Keep any top-level options (notably --config) when defaulting to run.
        args = parser.parse_args([*argv, "run"])
    config = load_config(args.config)
    with output_lock(output_dir(config)):
        args.func(args)


if __name__ == "__main__":
    main()
