"""Explicit recovery of old interrupted cases and policy-rejected completed solves."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

import blade_shape_active_learning as b
import blade_shape_interrupt as interruption
import blade_shape_pending as pending
import blade_shape_review as review
import blade_shape_signature as signatures
from blade_shape_convergence import ConvergencePolicy, ReviewPolicy, assess_out, review_evidence
from blade_shape_runtime import atomic_json, case_reservations, digest, file_identity, output_lock


def case_for(config: dict, run_id: str) -> Path:
    if not re.fullmatch(r'case_\d{6,}', run_id):
        raise ValueError('Expected a case_XXXXXX run_id.')
    return b.output_dir(config) / 'cases' / run_id


def one_row(config: dict, run_id: str):
    rows = b.load_training(config).loc[lambda frame: frame.run_id.eq(run_id)]
    if len(rows) != 1:
        raise ValueError('Recovery requires exactly one existing training row.')
    if str(rows.iloc[0].get(signatures.COLUMN)) not in signatures.compatible(config):
        raise ValueError('Recovery inputs are incompatible; inspect the physical signature first.')
    return rows.iloc[0]


def interrupted_point(config: dict, entries: list[dict], reservation: dict | None) -> tuple[np.ndarray, dict, str]:
    """Resolve one durable owner before changing or archiving any case evidence."""
    if len(entries) == 1 and reservation is None:
        owner = entries[0]
        arguments = owner['arguments']
    elif not entries and isinstance(reservation, dict):
        owner = reservation
        if any(not isinstance(owner.get(key), str) or not owner[key].strip() for key in ('plan_id', 'point_id')):
            raise ValueError('Boundary reservation is missing its plan or point identity.')
        arguments = {'sample_phase': 'boundary', 'experiment_id': owner['plan_id'],
                     'design_role': owner['point_id'], 'selection_source': 'predefined_boundary'}
    else:
        raise ValueError('No uniquely tracked compatible interrupted point was found.')
    if owner.get('signature') not in signatures.compatible(config):
        raise ValueError('Interrupted point inputs are incompatible; inspect the physical signature first.')
    x = np.asarray(owner.get('x'), dtype=float)
    if x.shape != (len(b.variable_names(config)),) or not np.isfinite(x).all():
        raise ValueError('Interrupted point requires one finite value per design variable.')
    return x, arguments, owner['signature']


def retry_interrupted(config: dict, run_id: str, reason: str) -> dict:
    """Operator confirmation is required for old receipts with no stop marker."""
    if not reason.strip():
        raise ValueError('An interruption reason is required.')
    case = case_for(config, run_id)
    with output_lock(b.output_dir(config)), output_lock(case):
        queue = pending.load(config)
        entries = [entry for entry in queue['entries'] if entry['run_id'] == run_id]
        reservation = case_reservations(b.output_dir(config)).get(run_id)
        x, arguments, signature = interrupted_point(config, entries, reservation)
        rows = b.load_training(config).loc[lambda frame: frame.run_id.eq(run_id)]
        if len(rows):
            row = one_row(config, run_id)
            if (not np.allclose(row[b.variable_names(config)].to_numpy(float), x, rtol=0, atol=1e-10)
                    or row.get('sample_phase') != arguments['sample_phase']
                    or any(row.get(key) != arguments[key] for key in ('experiment_id', 'design_role') if key in arguments)):
                raise ValueError('Recorded row does not match the tracked interrupted point.')
        else:
            row = {**b.vector_to_sample(config, x), **arguments,
                   **{k: np.nan for k in b.RESULT_COLUMNS}, 'run_id': run_id, 'case_dir': str(case),
                   'status': 'failed', 'failure_stage': '', signatures.COLUMN: signature}
        if row['status'] == 'interrupted' and interruption.retry_requested(case):
            return {'run_id': run_id, 'status': 'interrupted', 'retry_on_resume': True}
        if row['status'] != 'failed':
            raise ValueError('Only a failed, genuinely incomplete case can be confirmed as interrupted.')
        if len(rows) and row.get('failure_stage') not in {'geometry', 'pre', 'solve', 'post', 'cfx'}:
            raise ValueError('A recorded rejection is not an interrupted geometry/CFX stage; inspect the failure instead.')
        state_path = case / 'cfx_state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        solve = state.get('stages', {}).get('solve', {})
        restart = state.get('stages', {}).get('solve_restart', {})
        continuing = (solve.get('status') == 'needs_restart' and solve.get('convergence', {}).get('iteration_limit_reached')
                      and restart.get('status') in {None, 'running'} and restart.get('exit_code') is None)
        if (solve.get('convergence') and not continuing) or solve.get('status') in {'complete', 'review'}:
            raise ValueError('A completed/unconverged solve is not an interrupted case.')
        if interruption.recorded_failure(case):
            raise ValueError('A recorded failed stage is not an interrupted case; inspect the failure instead.')
        if len(rows):
            geometry_path = case / 'geometry_state.json'
            geometry = json.loads(geometry_path.read_text()) if geometry_path.exists() else {}
            failure_stage = row.get('failure_stage')
            if failure_stage == 'geometry':
                unfinished = geometry.get('status') == 'running'
            else:
                recorded_stage = state.get('stages', {}).get(failure_stage, {})
                unfinished = (recorded_stage.get('status') in {'running', 'returned'}
                              or (failure_stage == 'solve' and continuing))
            if not unfinished:
                raise ValueError('A recorded failure needs a matching unfinished stage receipt to confirm a legacy interruption.')
        candidate_path = case / 'candidate.json'
        if candidate_path.exists():
            candidate = json.loads(candidate_path.read_text())
            if (candidate.get('run_id') != run_id or not np.allclose(
                    [candidate['variables'][name] for name in b.variable_names(config)], x, rtol=0, atol=1e-10)):
                raise ValueError('Existing candidate does not match the tracked interrupted point.')
        for path in case.glob('*.out'):
            text = path.read_text(errors='replace')
            if re.search(r'(?:CFD Solver finished|CFX[- ]Solver (?:has )?finished)', text, re.I):
                if continuing and path.name == solve.get('out_file') and file_identity(path) == solve.get('out_identity'):
                    continue  # This is the initial result, before the interrupted continuation.
                raise ValueError('A solver-finished marker exists; inspect instead of retrying as interrupted.')
        interruption.assert_idle(case)
        owner_env = interruption.stop_environment(case)
        attempt = dict(run_id=run_id, signature=signatures.physical_signature(config),
                       x=[float(row[name]) for name in b.variable_names(config)], status='interrupted',
                       owner={'token': owner_env[interruption.TOKEN_ENV], 'stop_file': owner_env[interruption.STOP_ENV]},
                       reason=reason.strip(), original_row=pending.safe_json(dict(row)),
                       original_receipt_identity=file_identity(state_path),
                       registered_evidence={name: file_identity(case / name)
                                            for name in interruption.REGISTERED_EVIDENCE})
        atomic_json(case / interruption.ATTEMPT, attempt)
        update = {'status': 'interrupted', 'failure_stage': 'interrupted', 'message': reason.strip()}
        if len(rows):
            review._set(b.training_csv_path(config), run_id, update)
        else:
            b.append_row(config, {**row, **update})
        for entry in entries:
            entry.update(status='interrupted', finalized=False)
        if entries:
            atomic_json(pending.path_for(config), queue)
        for path in (b.diagnostics_csv_path(config), b.output_dir(config) / 'boundary_diagnostics.csv'):
            review._set(path, run_id, {'status': 'interrupted'}, only_existing=True)
        b.write_pareto(config, b.load_training(config))
        return {'run_id': run_id, 'status': 'interrupted', 'retry_on_resume': True}


def validate_completed(config: dict, run_id: str) -> tuple[Path, dict, dict]:
    case = case_for(config, run_id)
    row = one_row(config, run_id)
    if row['status'] != 'failed' or row.get('failure_stage') not in {'solve', 'post'}:
        raise ValueError('Only solver-policy/post failures may be reconsidered; human rejections stay final.')
    state = json.loads((case / 'cfx_state.json').read_text())
    solve = state.get('stages', {}).get('solve', {})
    inputs = state.get('inputs', {})
    cp, rp = ConvergencePolicy.from_config(config), ReviewPolicy.from_config(config)
    if state.get('version') != 2 or solve.get('exit_code') != 0:
        raise ValueError('A normal, recorded solver exit is required.')
    from blade_shape_cfx_runner import final_solve_recorded
    if (solve.get('reason_code') in {'solver_execution', 'solver_output'} or not final_solve_recorded(state)):
        raise ValueError('The final solver invocation failed or its evidence is incomplete; it cannot be reassessed.')
    expected = {'run_id': run_id, 'mesh': file_identity(case / 'Impeller_Mesh.gtm'),
                'candidate': file_identity(case / 'candidate.json'),
                'template_cfx': file_identity(config['paths']['template_cfx']),
                'template_cse': file_identity(config['paths']['template_cse']),
                'p_out_pa': float(config['runtime']['p_out_pa']), 'n_blades': int(config['runtime']['n_blades']),
                'convergence': cp.to_dict(), 'solver': signatures.solver_identity(config['paths']['cfx_bin_dir'])}
    if any(inputs.get(key) != value for key, value in expected.items()):
        raise ValueError('The recorded solver inputs differ from current inputs.')
    for field, identity_field in [('out_file', 'out_identity'), ('result_file', 'result_identity')]:
        name = solve.get(field, '')
        if not name or Path(name).name != name or not solve.get(identity_field):
            raise ValueError('Missing or invalid final-result evidence.')
        if file_identity(case / name) != solve[identity_field]:
            raise ValueError('Final result evidence changed.')
    pre = state['stages'].get('pre', {})
    checks = [(case/'Impeller.def', pre.get('definition_identity')),
              (case/'update_bc.ccl', pre.get('ccl_identity')),
              (case/'convergence.ccl', state.get('convergence_control_identity'))]
    if state.get('restart'):
        checks.append((case/'restart_convergence.ccl', state['restart'].get('ccl_identity')))
    if any(not identity or file_identity(path) != identity for path, identity in checks):
        raise ValueError('Solver definition or controls changed.')
    report = assess_out((case/solve['out_file']).read_text(errors='replace'), cp, history_window=rp.history_window)
    evidence = review_evidence(report, cp, rp)
    if not report['iteration_limit_reached'] or evidence is None:
        raise ValueError('The final solve does not qualify for review under this policy.')
    return case, state, {'convergence': report, 'review': evidence}


def make_plan(config: dict, run_id: str) -> dict:
    case, state, evidence = validate_completed(config, run_id)
    plan = {'version': 1, 'run_id': run_id, 'physical_signature': signatures.physical_signature(config),
            'policy': ReviewPolicy.from_config(config).to_dict(), 'evidence': evidence,
            'receipt_identity': file_identity(case/'cfx_state.json'),
            'training_identity': file_identity(b.training_csv_path(config)),
            'queue_identity': file_identity(pending.path_for(config)),
            'out_identity': state['stages']['solve']['out_identity'],
            'result_identity': state['stages']['solve']['result_identity']}
    return {**plan, 'plan_id': digest(plan)}


def apply_plan(config: dict, plan: dict, reason: str) -> dict:
    if not reason.strip():
        raise ValueError('A reassessment reason is required.')
    if plan.get('version') != 1 or plan.get('plan_id') != digest({k:v for k,v in plan.items() if k != 'plan_id'}):
        raise ValueError('Invalid or edited reassessment plan.')
    if (plan['physical_signature'] != signatures.physical_signature(config)
            or plan['policy'] != ReviewPolicy.from_config(config).to_dict()):
        raise ValueError('Reassessment inputs or policy changed; generate a new plan.')
    case = case_for(config, plan['run_id'])
    with output_lock(b.output_dir(config)), output_lock(case):
        interruption.assert_idle(case)
        registry_path = b.output_dir(config) / 'reassessment_requests.json'
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else {'requests': {}}
        record = registry['requests'].get(plan['plan_id'])
        if record and record['status'] == 'applied':
            return record
        if record is None:
            if (file_identity(b.training_csv_path(config)) != plan['training_identity']
                    or file_identity(pending.path_for(config)) != plan['queue_identity']
                    or file_identity(case/'cfx_state.json') != plan['receipt_identity']):
                raise ValueError('The planned source state changed; generate a new plan.')
            _, original, evidence = validate_completed(config, plan['run_id'])
            record = {'status': 'prepared', 'run_id': plan['run_id'], 'reason': reason.strip(),
                      'plan': plan, 'original_receipt': original, 'evidence': evidence}
            registry['requests'][plan['plan_id']] = record
            atomic_json(registry_path, registry)
        else:
            if one_row(config, plan['run_id'])['status'] not in {'failed', 'review'}:
                raise ValueError('A later decision changed this result; reassessment cannot overwrite it.')
        original = record['original_receipt']
        solve = original['stages']['solve']
        if (file_identity(case/solve['out_file']) != plan['out_identity']
                or file_identity(case/solve['result_file']) != plan['result_identity']):
            raise ValueError('Reassessment evidence changed.')
        decisions = review.decisions_path(config)
        if decisions.exists() and any(d['run_id'] == plan['run_id'] for d in json.loads(decisions.read_text())['decisions']):
            raise ValueError('An existing human decision cannot be overwritten.')
        state = json.loads((case/'cfx_state.json').read_text())
        if state.get('reassessment_plan') != plan['plan_id']:
            if file_identity(case/'cfx_state.json') != plan['receipt_identity']:
                raise ValueError('The prepared solver receipt changed.')
            state = copy.deepcopy(original)
            state['reassessment_plan'] = plan['plan_id']
            state.setdefault('reassessment_history', []).append({'plan_id': plan['plan_id'], 'reason': record['reason'],
                                                                'original_solve': copy.deepcopy(solve)})
            state['stages']['solve'].update(status='review', **record['evidence'])
            atomic_json(case/'cfx_state.json', state)
        current_solve = state.get('stages', {}).get('solve', {})
        if (state.get('inputs') != original.get('inputs') or current_solve.get('status') != 'review'
                or current_solve.get('review') != record['evidence']['review']
                or any(current_solve.get(key) != solve.get(key) for key in
                       ('out_file', 'out_identity', 'result_file', 'result_identity', 'exit_code'))):
            raise ValueError('The prepared reassessment receipt changed; inspect before retrying.')
        runtime, paths = config['runtime'], config['paths']
        # A verified review receipt can only invoke Post; never a new solver.
        from blade_shape_cfx_runner import run_cfx_pipeline
        with interruption.operation(case, 'reassess'):
            result = run_cfx_pipeline(case, plan['run_id'], p_out_pa=float(runtime['p_out_pa']),
                                     cfx_bin_dir=paths['cfx_bin_dir'], template_cfx=paths['template_cfx'],
                                     template_cse=paths['template_cse'], cores=int(runtime['cfx_cores']),
                                     n_blades=int(runtime['n_blades']), convergence=ConvergencePolicy.from_config(config),
                                     review=ReviewPolicy.from_config(config))
        if not result.review or not all(np.isfinite(result.metrics.get(k, np.nan)) for k in b.RESULT_COLUMNS):
            raise ValueError(f'Reassessment Post did not complete: {result.message}; the plan can be retried.')
        if (signatures.physical_signature(config) != plan['physical_signature']
                or file_identity(case/solve['out_file']) != plan['out_identity']
                or file_identity(case/solve['result_file']) != plan['result_identity']):
            raise ValueError('Inputs or result evidence changed during Post; no training row was promoted.')
        for path in (b.diagnostics_csv_path(config), b.output_dir(config)/'boundary_diagnostics.csv'):
            if not path.exists():
                continue
            frame = pd.read_csv(path)
            if 'run_id' not in frame or 'status' not in frame:
                continue
            mask = frame.run_id.astype(str).eq(plan['run_id'])
            if not mask.any():
                continue
            values = {'status': 'review', 'failure_stage': '', 'message': result.message}
            for objective in b.OBJECTIVE_COLUMNS:
                true = float(result.metrics[objective])
                column = f'pred_{objective}'
                pred = (pd.to_numeric(frame.loc[mask, column], errors='coerce') if column in frame
                        else pd.Series(np.nan, index=frame.index[mask]))
                values[f'true_{objective}'] = true
                values[f'prediction_error_{objective}'] = (true - pred).where(np.isfinite(pred))
            review._set(path, plan['run_id'], values, only_existing=True)
        # Publish an actionable review only after its diagnostic data is complete.
        # An interruption before this point can replay without a human decision
        # making the remaining diagnostic updates impossible.
        review._set(b.training_csv_path(config), plan['run_id'], {**result.metrics, 'status': 'review',
                    'failure_stage': '', 'message': result.message})
        queue = pending.load(config)
        for entry in queue['entries']:
            if entry['run_id'] == plan['run_id']:
                entry.update(status='review', finalized=True)
        atomic_json(pending.path_for(config), queue)
        b.write_pareto(config, b.load_training(config))
        record['status'] = 'applied'
        atomic_json(registry_path, registry)
        return {'run_id': plan['run_id'], 'status': 'review', 'metrics': result.metrics, 'plan_id': plan['plan_id']}
