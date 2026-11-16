"""Fixed eight-point CFD probe around one successful design, in its own output directory.

Points, in order:
  center_replicate      the center again; the rest stops unless it reproduces the recorded result
  shroud_beta_0_3.5     extension past the configured upper bound
  shroud_beta_0_4.0     skipped when the 3.5 degree point failed
  five single steps     one frozen variable at a time, from the center

Results stay in --output-dir and never enter the optimizer's training data or Pareto
fronts. The shared config is not changed: bounds and search settings are lifted only in
memory, and neither is part of the physical signature. Rows with status=review keep their
real values here and do not pause the run. Failed points are never recomputed.
It was written for the earlier seven-variable search (five frozen variables) and
refuses a config whose fixed variables differ, which includes the current shared one.

    python blade_shape_probe.py --center-run-id case_000040 --output-dir D:/probe8 --max-new-cfd 0
    python blade_shape_probe.py --center-run-id case_000040 --output-dir D:/probe8 --max-new-cfd 1
    python blade_shape_probe.py --center-run-id case_000040 --output-dir D:/probe8 --max-new-cfd 7 --resume

--max-new-cfd 0 checks inputs and geometry rules and starts nothing. Results are written
to probe_results.csv in the output directory after every point.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
from typing import Any

import numpy as np
import pandas as pd

import blade_shape_active_learning as b
import blade_shape_refinement as refinement
import blade_shape_signature as signatures
from blade_shape_runtime import atomic_json, digest, output_lock

REPLICATE = 'center_replicate'
EXTENSION_VARIABLE = 'shroud_beta_0_deg_offset'
EXTENSION_VALUES = (3.5, 4.0)
#: One step per frozen variable, toward the value that variable was frozen at in the
#: earlier study; the sizes are the largest steps allowed by refinement.boundary_levels.
FROZEN_STEPS = {
    'hub_beta_1_deg_offset': -1.0,
    'hub_beta_2_deg_offset': -1.0,
    'shroud_beta_1_deg_offset': -1.0,
    'shroud_beta_4_deg_offset': 1.0,
    'shroud_theta_deg_offset': -0.5,
}
#: Failures that say nothing about the design: the next points would fail the same way.
STOP_STAGES = {'environment', 'geometry_rules'}
PLAN_NAME = 'probe_plan.json'
RESULTS_NAME = 'probe_results.csv'


def load_center(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    """A status=success row with valid objectives, produced under the current physical inputs."""
    if not b.training_csv_path(config).is_file():
        raise ValueError(f'No training data at {b.training_csv_path(config)}')
    with output_lock(b.output_dir(config)):
        frame = b.load_training(config)
    history, _ = refinement.training_partition(config, frame, accepted_only=True)
    rows = history.loc[history.run_id.eq(run_id)]
    if len(rows) != 1:
        raise ValueError(f'Center {run_id} must be one status=success row under the current physical inputs.')
    row = rows.iloc[0]
    # Rounded so that a later rewrite of the CSV cannot change the frozen plan's identity.
    return {'run_id': run_id, 'x': np.round(row[b.variable_names(config)].to_numpy(float), 10).tolist(),
            **{name: float(row[name]) for name in ('Efficiency', 'MassFlow', 'Power')}}


def experiment_config(config: dict[str, Any], output: Path) -> dict[str, Any]:
    """Study config with every variable free, the extension bound lifted and a new output directory."""
    fixed = config.get('search', {}).get('fixed_variables', {})
    if set(fixed) != set(FROZEN_STEPS):
        raise ValueError(f'This probe expects frozen variables {sorted(FROZEN_STEPS)}, config has {sorted(fixed)}.')
    out = copy.deepcopy(config)
    out['paths']['output_dir'] = str(output)
    out['search'] = {'active_variables': b.variable_names(config), 'fixed_variables': {}}
    for variable in out['variables']:
        if variable['name'] == EXTENSION_VARIABLE:
            variable['upper'] = max(float(variable['upper']), *EXTENSION_VALUES)
    return out


def build_plan(config: dict[str, Any], center: dict[str, Any]) -> dict[str, Any]:
    names = b.variable_names(config)

    def moved(name: str, value: float) -> list[float]:
        x = list(center['x'])
        x[names.index(name)] = float(value)
        return x

    designs = [(REPLICATE, list(center['x']))]
    designs += [(f'shroud_beta_0_{value:.1f}', moved(EXTENSION_VARIABLE, value)) for value in EXTENSION_VALUES]
    designs += [(f"{name.removesuffix('_deg_offset')}_{step:+.1f}",
                 moved(name, center['x'][names.index(name)] + step)) for name, step in FROZEN_STEPS.items()]
    plan = {'kind': 'fixed_probe', 'version': 1, 'center_run_id': center['run_id'], 'variables': names,
            'signature': signatures.physical_signature(config),
            'points': [{'index': index, 'point_id': point_id, 'x': x} for index, (point_id, x) in enumerate(designs)]}
    return {**plan, 'plan_id': digest(plan), 'center': center}


def preflight(config: dict[str, Any], plan: dict[str, Any]) -> Any:
    """Refuse the whole plan before any CFD when an input is missing or a point breaks a rule."""
    paths = config['paths']
    problems = [f'missing physical input {key}: {paths.get(key)!r}' for key in signatures.ASSET_KEYS
                if not Path(str(paths.get(key) or '')).is_file()]
    problems += [f"{tool} not found in cfx_bin_dir: {paths.get('cfx_bin_dir')!r}"
                 for tool in ('cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe')
                 if not (Path(str(paths.get('cfx_bin_dir') or '')) / tool).is_file()]
    problems += [f'{key} not found: {paths.get(key)!r}' for key in ('powershell_exe', 'cfturbo_exe', 'turbogrid_exe')
                 if not (Path(str(paths.get(key) or '')).is_file() or shutil.which(str(paths.get(key) or '')))]
    if problems:
        raise ValueError('; '.join(problems))
    b.check_geometry_script(config)
    baseline = b.extract_baseline(config)
    for point in plan['points']:
        issues = b.constraint_violations(config, baseline, np.array(point['x'], dtype=float))
        if issues:
            problems.append(f"{point['point_id']}: {issues}")
    if problems:
        raise ValueError('Geometry rules failed; nothing was started. ' + '; '.join(problems))
    return baseline


def prepare_output(output: Path, plan: dict[str, Any], resume: bool) -> None:
    path = output / PLAN_NAME
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')).get('plan_id') != plan['plan_id']:
            raise ValueError('This output directory holds a different probe: the center, the steps or the '
                             'physical inputs changed. Inspect it, or use a new output directory.')
        if not resume:
            raise ValueError('Existing probe requires --resume.')
        return
    leftovers = [item.name for item in output.iterdir() if item.name != '.optimizer.lock']
    if leftovers:
        raise ValueError(f'Output directory has files but no {PLAN_NAME}: {leftovers[:5]}')
    atomic_json(path, plan)


def replicate_problem(row: dict[str, Any], center: dict[str, Any], tolerances: np.ndarray) -> str:
    if row.get('status') not in {'success', 'review'}:
        return f"it ended as {row.get('status')}: {row.get('message')}"
    change = np.array([float(row[name]) - center[name] for name in b.OBJECTIVE_COLUMNS])
    if np.any(np.abs(change) > tolerances):
        return f'objectives moved by {change.tolist()}, beyond tolerances {tolerances.tolist()}'
    return ''


def has_values(row: dict[str, Any]) -> bool:
    return np.isfinite([float(row.get(name, np.nan)) for name in ('Efficiency', 'MassFlow', 'Power')]).all()


def specific_work(row: dict[str, Any]) -> tuple[float, float]:
    """Work and lost work in J/kg; Power/MassFlow in the recorded internal units give J/kg after 1e-6."""
    work = float(row['Power']) / float(row['MassFlow']) * 1e-6
    return work, (1 - float(row['Efficiency'])) * work


def write_results(output: Path, plan: dict[str, Any], rows: dict[str, dict[str, Any]],
                  notes: dict[str, str]) -> pd.DataFrame:
    """Each point against the recomputed center, or against the recorded center until it exists."""
    replicate = rows.get(REPLICATE, {})
    reference = replicate if has_values(replicate) else plan['center']
    base_work, base_loss = specific_work(reference)
    table = []
    for point in plan['points']:
        row = rows.get(point['point_id'], {})
        entry: dict[str, Any] = {'point_id': point['point_id'], 'run_id': row.get('run_id', ''),
                                 'status': row.get('status', notes.get(point['point_id'], 'not_run')),
                                 'failure_stage': row.get('failure_stage', ''), 'Efficiency': np.nan,
                                 'MassFlow': np.nan, 'work_J_kg': np.nan, 'lost_work_J_kg': np.nan,
                                 'delta_eta_pp': np.nan, 'delta_massflow': np.nan, 'delta_work': np.nan,
                                 'delta_lost_work': np.nan, 'message': row.get('message', '')}
        if has_values(row):
            work, loss = specific_work(row)
            entry.update(Efficiency=float(row['Efficiency']), MassFlow=float(row['MassFlow']), work_J_kg=work,
                         lost_work_J_kg=loss, delta_eta_pp=100 * (float(row['Efficiency']) - reference['Efficiency']),
                         delta_massflow=float(row['MassFlow']) - reference['MassFlow'],
                         delta_work=work - base_work, delta_lost_work=loss - base_loss)
        table.append(entry)
    frame = pd.DataFrame(table)
    temporary = output / (RESULTS_NAME + '.tmp')
    frame.to_csv(temporary, index=False)
    temporary.replace(output / RESULTS_NAME)
    return frame


def run_probe(args: argparse.Namespace) -> int:
    if args.max_new_cfd < 0:
        raise ValueError('--max-new-cfd cannot be negative.')
    config = b.load_config(args.config)
    output = Path(args.output_dir).resolve()
    original = b.output_dir(config).resolve()
    if output == original or original in output.parents or output in original.parents:
        raise ValueError('The probe output must be separate from the optimization output.')
    center = load_center(config, args.center_run_id)
    study = experiment_config(config, output)
    plan = build_plan(study, center)
    baseline = preflight(study, plan)
    names = plan['variables']
    print(f"Probe around {center['run_id']}: Efficiency={center['Efficiency']:.6f} MassFlow={center['MassFlow']:.6f}")
    for point in plan['points']:
        changed = [f'{names[j]}={point["x"][j]:.4f}' for j in range(len(names)) if point['x'][j] != center['x'][j]]
        print(f"  {point['index']} {point['point_id']:<22} {', '.join(changed) or 'same as center'}")
    print('Inputs and geometry rules passed for all points.')
    if args.max_new_cfd == 0 and not (output / PLAN_NAME).exists():
        print('No CFD started (--max-new-cfd 0).')
        return 0

    with output_lock(output):
        prepare_output(output, plan, args.resume)
        signatures.check(study, signatures.training_records(study, b.load_training(study)), None)
        signatures.pin(study)
        tolerances = b.objective_tolerances(config)
        rows: dict[str, dict[str, Any]] = {}
        notes: dict[str, str] = {}
        used = 0
        stop = ''
        for point in plan['points']:
            point_id = point['point_id']
            x = np.array(point['x'], dtype=float)
            # load_training keeps 'boundary' rows as recorded; it rewrites others as DOE/AL samples.
            metadata = {'sample_phase': 'boundary', 'experiment_id': plan['plan_id'], 'design_role': point_id,
                        'selection_source': 'predefined_probe'}
            result = b.recover_recorded_case(study, baseline, x, point['index'],
                                             producing_signature=plan['signature'], **metadata)
            if result is None:
                if stop or used >= args.max_new_cfd or notes.get(point_id):
                    continue
                print(f"[CFD] {point['index'] + 1}/{len(plan['points'])} {point_id}", flush=True)
                result = b.evaluate_true_cfd(study, baseline, x, point['index'], **metadata)
                used += 1
            row = rows[point_id] = dict(result.row)
            write_results(output, plan, rows, notes)
            print(f"      {row.get('run_id')} status={row.get('status')} Efficiency={row.get('Efficiency')} "
                  f"MassFlow={row.get('MassFlow')}", flush=True)
            if point_id == REPLICATE:
                problem = replicate_problem(row, center, tolerances)
                if problem:
                    stop = f'{REPLICATE} did not reproduce {center["run_id"]}: {problem}'
            elif row.get('failure_stage') in STOP_STAGES:
                stop = f"{point_id} stopped at {row.get('failure_stage')}: {row.get('message')}"
            elif point_id == f'shroud_beta_0_{EXTENSION_VALUES[0]:.1f}' and row.get('status') == 'failed':
                notes[f'shroud_beta_0_{EXTENSION_VALUES[1]:.1f}'] = 'skipped'
        frame = write_results(output, plan, rows, notes)

    remaining = int(frame.status.eq('not_run').sum())
    with pd.option_context('display.width', 200, 'display.max_columns', 20, 'display.max_colwidth', 40):
        print(frame.drop(columns=['message', 'failure_stage']).to_string(index=False))
    print(f'New CFD attempts: {used}. Not yet run: {remaining}. Results: {output / RESULTS_NAME}')
    if stop:
        print(f'STOPPED: {stop}. No further CFD started. Failed cases are not recomputed; '
              'after fixing the cause, use a new --output-dir.', file=sys.stderr)
        return 1
    if remaining:
        print('Rerun with --resume and a positive --max-new-cfd to continue.')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='blade_shape_config.json')
    parser.add_argument('--center-run-id', required=True, help='A status=success row in the main training data.')
    parser.add_argument('--output-dir', required=True, help='New directory, separate from paths.output_dir.')
    parser.add_argument('--max-new-cfd', type=int, required=True, help='New CFD attempts this call; 0 only checks.')
    parser.add_argument('--resume', action='store_true', help='Continue an existing probe in --output-dir.')
    args = parser.parse_args(argv)
    try:
        return run_probe(args)
    except KeyboardInterrupt:
        print('Interrupted. Check that no CFX solver is still running, then rerun with --resume.', file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
