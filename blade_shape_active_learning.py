from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

from blade_shape_cfx_runner import run_cfx_pipeline

try:
    from scipy.stats import qmc
except Exception:  # pragma: no cover
    qmc = None


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
OBJECTIVE_COLUMNS = ["Efficiency", "PressureRatio", "MassFlow"]
RESULT_COLUMNS = ["Efficiency", "PressureRatio", "MassFlow", "Power", "totalpressureratio"]
STATUS_COLUMNS = ["status", "failure_stage", "message", "case_dir", "run_id"]


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


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["_config_path"] = str(config_path.resolve())
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


def candidate_geometry(config: dict[str, Any], baseline: BaselineShape, x: np.ndarray) -> dict[str, Any]:
    offsets = np.array(x, dtype=float)
    hub_offsets = np.deg2rad(offsets[0:5])
    shroud_offsets = np.deg2rad(offsets[5:10])
    hub_theta = baseline.hub_theta_rad + math.radians(float(offsets[10]))
    shroud_theta = baseline.shroud_theta_rad + math.radians(float(offsets[11]))
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
    constraints = config["constraints"]
    geom = candidate_geometry(config, baseline, x)
    hub = np.array(geom["hub_beta_rad"], dtype=float)
    shroud = np.array(geom["shroud_beta_rad"], dtype=float)
    max_beta_offset = float(constraints["max_beta_offset_deg"])
    max_theta_offset = float(constraints["max_theta_offset_deg"])
    violations: list[str] = []
    if np.max(np.abs(np.rad2deg(hub - baseline.hub_beta_rad))) > max_beta_offset + 1e-9:
        violations.append("hub_beta_offset")
    if np.max(np.abs(np.rad2deg(shroud - baseline.shroud_beta_rad))) > max_beta_offset + 1e-9:
        violations.append("shroud_beta_offset")
    if abs(float(x[10])) > max_theta_offset + 1e-9 or abs(float(x[11])) > max_theta_offset + 1e-9:
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
    return Path(config["paths"]["output_dir"])


def training_csv_path(config: dict[str, Any]) -> Path:
    return output_dir(config) / "training_data.csv"


def all_columns(config: dict[str, Any]) -> list[str]:
    return variable_names(config) + RESULT_COLUMNS + STATUS_COLUMNS


def load_training(config: dict[str, Any]) -> pd.DataFrame:
    path = training_csv_path(config)
    if not path.exists():
        return pd.DataFrame(columns=all_columns(config))
    return pd.read_csv(path)


def append_row(config: dict[str, Any], row: dict[str, Any]) -> None:
    path = training_csv_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([row], columns=all_columns(config))
    frame.to_csv(path, mode="a", header=not path.exists(), index=False)


def write_iteration_summary(config: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    path = output_dir(config) / "iteration_summary.csv"
    frame = pd.DataFrame(rows)
    frame.to_csv(path, index=False)


def write_pareto(config: dict[str, Any], df: pd.DataFrame) -> pd.DataFrame:
    success = df[df["status"] == "success"].copy()
    if success.empty:
        pareto = pd.DataFrame(columns=df.columns)
    else:
        y = success[OBJECTIVE_COLUMNS].astype(float).to_numpy()
        mask = pareto_mask(y)
        pareto = success.loc[mask].copy()
        pareto = pareto.sort_values(["Efficiency", "PressureRatio", "MassFlow"], ascending=False)
    pareto_path = output_dir(config) / "pareto_front.csv"
    pareto_path.parent.mkdir(parents=True, exist_ok=True)
    pareto.to_csv(pareto_path, index=False)
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
    return lb + unit * (ub - lb)


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
    xn = normalize_x(config, x[None, :])[0]
    en = normalize_x(config, existing)
    return float(np.min(np.linalg.norm(en - xn, axis=1))) >= distance_limit


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
        x = lb + rng.random(len(lb)) * (ub - lb)
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


def pairwise_distances(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    diff = a[:, None, :] - b[None, :, :]
    return np.sqrt(np.sum(diff * diff, axis=2))


def rbf_features(x: np.ndarray, centers: np.ndarray, sigma: float) -> np.ndarray:
    d = pairwise_distances(x, centers)
    phi = np.exp(-(d * d) / (2.0 * sigma * sigma))
    return np.column_stack([np.ones(len(x)), phi])


def pareto_mask(objectives: np.ndarray) -> np.ndarray:
    n = len(objectives)
    mask = np.ones(n, dtype=bool)
    for i in range(n):
        if not mask[i]:
            continue
        dominates_i = np.all(objectives >= objectives[i], axis=1) & np.any(objectives > objectives[i], axis=1)
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
    surrogate: RbfRidgeEnsemble,
    existing: np.ndarray,
    seed: int,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    lb = lower_bounds(config)
    ub = upper_bounds(config)
    pop_size = int(config["runtime"]["nsga2_pop_size"])
    generations = int(config["runtime"]["nsga2_generations"])
    pop = np.array(valid_random_samples(config, baseline, pop_size, seed, existing))
    if len(pop) < pop_size:
        extra = lb + rng.random((pop_size - len(pop), len(lb))) * (ub - lb)
        pop = np.vstack([pop, extra]) if len(pop) else extra

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
        while len(children) < pop_size:
            p1 = tournament(pop, rank, crowd, rng)
            p2 = tournament(pop, rank, crowd, rng)
            alpha = rng.random(len(lb))
            c1 = alpha * p1 + (1.0 - alpha) * p2
            c2 = alpha * p2 + (1.0 - alpha) * p1
            for child in [c1, c2]:
                mutation = rng.normal(0.0, 0.08, size=len(lb)) * (ub - lb)
                mask = rng.random(len(lb)) < 0.25
                child = np.clip(child + mask * mutation, lb, ub)
                if not constraint_violations(config, baseline, child):
                    children.append(child)
                if len(children) >= pop_size:
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
    surrogate: RbfRidgeEnsemble,
    existing: np.ndarray,
    count: int,
) -> list[np.ndarray]:
    if len(candidates) == 0:
        return []
    mean, std = surrogate.predict(candidates)
    obj_norm = normalize_objectives(mean)
    uncertainty = normalize_objectives(std).mean(axis=1)
    predicted_pareto = pareto_mask(mean).astype(float)
    if existing.size:
        dist = pairwise_distances(normalize_x(config, candidates), normalize_x(config, existing)).min(axis=1)
    else:
        dist = np.ones(len(candidates))
    dist_norm = dist / max(float(np.max(dist)), 1e-12)
    score = 2.0 * predicted_pareto + obj_norm.mean(axis=1) + 0.75 * uncertainty + 0.5 * dist_norm
    order = np.argsort(score)[::-1]
    selected: list[np.ndarray] = []
    selected_existing = existing.copy()
    for idx in order:
        x = candidates[idx]
        if constraint_violations(config, baseline, x):
            continue
        if not is_far_enough(config, x, selected_existing):
            continue
        selected.append(x)
        selected_existing = np.vstack([selected_existing, x[None, :]]) if selected_existing.size else x[None, :]
        if len(selected) >= count:
            break
    return selected


def normalize_objectives(values: np.ndarray) -> np.ndarray:
    lo = values.min(axis=0)
    hi = values.max(axis=0)
    return (values - lo) / np.maximum(hi - lo, 1e-12)


def next_case_index(config: dict[str, Any], df: pd.DataFrame) -> int:
    if not df.empty and "run_id" in df.columns:
        ids = []
        for value in df["run_id"].dropna().astype(str):
            if value.startswith("case_"):
                try:
                    ids.append(int(value.split("_")[1]))
                except Exception:
                    pass
        if ids:
            return max(ids) + 1
    cases = output_dir(config) / "cases"
    if not cases.exists():
        return 0
    ids = []
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
) -> CaseResult:
    run_id = f"case_{case_index:06d}"
    sample = vector_to_sample(config, x)
    row: dict[str, Any] = {**sample, **{col: np.nan for col in RESULT_COLUMNS}}
    row.update({"status": "failed", "failure_stage": "", "message": "", "case_dir": "", "run_id": run_id})

    violations = constraint_violations(config, baseline, x)
    if violations:
        row["failure_stage"] = "geometry_rules"
        row["message"] = ";".join(violations)
        append_row(config, row)
        return CaseResult(row, False)

    candidate_path = write_candidate_files(config, baseline, x, run_id)
    row["case_dir"] = str(candidate_path.parent)
    ok, msg = run_geometry(config, candidate_path, dry_run=False)
    if not ok:
        row["failure_stage"] = "geometry"
        row["message"] = msg
        append_row(config, row)
        return CaseResult(row, False)

    runtime = config["runtime"]
    paths = config["paths"]
    cfx = run_cfx_pipeline(
        candidate_path.parent,
        run_id,
        p_out_pa=float(runtime["p_out_pa"]),
        cfx_bin_dir=paths["cfx_bin_dir"],
        template_cfx=paths["template_cfx"],
        template_cse=paths["template_cse"],
        cores=int(runtime["cfx_cores"]),
        n_blades=int(runtime["n_blades"]),
    )
    if not cfx.success:
        row["failure_stage"] = cfx.failure_stage or "cfx"
        row["message"] = cfx.message
        append_row(config, row)
        return CaseResult(row, False)

    for col in RESULT_COLUMNS:
        row[col] = float(cfx.metrics.get(col, np.nan))
    row["status"] = "success"
    row["failure_stage"] = ""
    row["message"] = cfx.message
    append_row(config, row)
    return CaseResult(row, True)


def run_loop(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    runtime = config["runtime"]
    if args.initial_samples is not None:
        runtime["initial_samples"] = int(args.initial_samples)
    if args.iterations is not None:
        runtime["iterations"] = int(args.iterations)
    if args.batch_size is not None:
        runtime["batch_size"] = int(args.batch_size)
    if args.max_new_cfd is not None:
        runtime["max_new_cfd"] = int(args.max_new_cfd)
    if args.seed is not None:
        runtime["seed"] = int(args.seed)

    out = output_dir(config)
    out.mkdir(parents=True, exist_ok=True)
    baseline = extract_baseline(config)
    df = load_training(config)
    if not args.resume and not df.empty:
        raise SystemExit(f"{training_csv_path(config)} already exists. Use --resume to append to it.")

    seed = int(runtime["seed"])
    max_new = int(runtime["max_new_cfd"])
    used_new = 0
    case_idx = next_case_index(config, df)
    summary_rows: list[dict[str, Any]] = []

    existing = existing_vectors(config, df)
    if len(df) < int(runtime["initial_samples"]):
        needed = int(runtime["initial_samples"]) - len(df)
        seeds = lhs_samples(config, needed * 4, seed)
        initial: list[np.ndarray] = []
        for x in seeds:
            if len(initial) >= needed:
                break
            if constraint_violations(config, baseline, x):
                continue
            if not is_far_enough(config, x, existing):
                continue
            initial.append(x)
            existing = np.vstack([existing, x[None, :]]) if existing.size else x[None, :]
        if len(initial) < needed:
            initial.extend(valid_random_samples(config, baseline, needed - len(initial), seed + 1000, existing))
        for x in initial:
            if used_new >= max_new:
                break
            print(f"[CFD] initial {case_idx:06d}")
            result = evaluate_true_cfd(config, baseline, x, case_idx)
            used_new += 1
            case_idx += 1
            df = load_training(config)
            summary_rows.append({"iteration": -1, "run_id": result.row["run_id"], "status": result.row["status"]})
            write_pareto(config, df)

    for iteration in range(int(runtime["iterations"])):
        if used_new >= max_new:
            break
        df = load_training(config)
        success = df[df["status"] == "success"].copy()
        existing = existing_vectors(config, df)
        batch_size = min(int(runtime["batch_size"]), max_new - used_new)
        if len(success) >= 4:
            x_train = success[variable_names(config)].astype(float).to_numpy()
            y_train = success[OBJECTIVE_COLUMNS].astype(float).to_numpy()
            surrogate = RbfRidgeEnsemble(config, seed=seed + iteration).fit(x_train, y_train)
            nsga = nsga2_candidates(config, baseline, surrogate, existing, seed + iteration * 17)
            random_pool = np.array(valid_random_samples(config, baseline, int(runtime["candidate_pool_size"]), seed + iteration * 31, existing))
            candidates = np.vstack([nsga, random_pool]) if len(random_pool) else nsga
            selected = select_acquisition(config, baseline, candidates, surrogate, existing, batch_size)
            if len(selected) < batch_size:
                selected.extend(valid_random_samples(config, baseline, batch_size - len(selected), seed + iteration * 97, existing))
        else:
            selected = valid_random_samples(config, baseline, batch_size, seed + iteration * 97, existing)

        for x in selected[:batch_size]:
            print(f"[CFD] iteration {iteration} case {case_idx:06d}")
            result = evaluate_true_cfd(config, baseline, x, case_idx)
            used_new += 1
            case_idx += 1
            df = load_training(config)
            summary_rows.append({"iteration": iteration, "run_id": result.row["run_id"], "status": result.row["status"]})
            write_pareto(config, df)
            if used_new >= max_new:
                break

    write_iteration_summary(config, summary_rows)
    pareto = write_pareto(config, load_training(config))
    print(f"Done. New CFD attempts: {used_new}. Pareto rows: {len(pareto)}. Output: {out}")


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
    if args.dry_run:
        ok, msg = run_geometry(config, candidate_path, dry_run=True)
        if not ok:
            raise SystemExit(msg)
    print(candidate_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Blade-shape active learning with real CFD feedback.")
    parser.add_argument("--config", default="blade_shape_config.json")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the active-learning CFD loop.")
    run.add_argument("--config", default="blade_shape_config.json")
    run.add_argument("--initial-samples", type=int)
    run.add_argument("--iterations", type=int)
    run.add_argument("--batch-size", type=int)
    run.add_argument("--max-new-cfd", type=int)
    run.add_argument("--seed", type=int)
    run.add_argument("--resume", action="store_true")
    run.set_defaults(func=run_loop)

    wc = sub.add_parser("write-candidate", help="Write one candidate JSON and optionally dry-run XML generation.")
    wc.add_argument("--config", default="blade_shape_config.json")
    wc.add_argument("--index", type=int, default=0)
    wc.add_argument("--seed", type=int)
    wc.add_argument("--dry-run", action="store_true")
    wc.set_defaults(func=write_candidate_command)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        args = parser.parse_args(["run"])
    args.func(args)


if __name__ == "__main__":
    main()
