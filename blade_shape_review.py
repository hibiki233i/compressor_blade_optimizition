"""Manual review of CFD results that missed the strict RMS target by a small margin.

A training row with ``status=review`` carries real CFX-Post metrics but is not a
label. A person accepts it (the row becomes ``success`` and enters training and
the Pareto fronts) or rejects it (``failed``). Each decision is appended to
``review_decisions.json`` together with the evidence it was made on, and a
decision is refused when that evidence (the .out/.res of the recorded solve)
has changed since the solver wrote it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import blade_shape_active_learning as b
import blade_shape_pending as pending
import blade_shape_signature as signatures
from blade_shape_runtime import atomic_json, file_identity, output_lock

DECISIONS = 'review_decisions.json'
#: Columns added to a reviewed training row; the original message is kept.
REVIEW_COLUMNS = ['review_decision', 'review_reason', 'reviewed_at']
OUTCOME = {'accepted': ('success', ''), 'rejected': ('failed', 'review_rejected')}


def decisions_path(config: dict[str, Any]) -> Path:
    return b.output_dir(config) / DECISIONS


def _solve_state(config: dict[str, Any], run_id: str) -> tuple[Path, dict[str, Any]]:
    case = b.output_dir(config) / 'cases' / run_id
    receipt = case / 'cfx_state.json'
    state = json.loads(receipt.read_text()) if receipt.exists() else {}
    return case, state.get('stages', {}).get('solve', {})


def awaiting(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Results awaiting review with what a reviewer needs to judge them."""
    frame = b.load_training(config)
    out = []
    for _, row in frame.loc[frame.status.eq('review')].iterrows():
        run_id = str(row.run_id)
        case, solve = _solve_state(config, run_id)
        report = solve.get('convergence', {})
        # Enrich old receipts without changing their recorded classification.
        if solve.get('out_file') and (case / solve['out_file']).is_file():
            from blade_shape_convergence import assess_out, ConvergencePolicy, ReviewPolicy
            policy, window = ConvergencePolicy.from_config(config), ReviewPolicy.from_config(config).history_window
            try:
                report = assess_out((case / solve['out_file']).read_text(errors='replace'), policy,
                                    history_window=window)
            except (ValueError, OSError) as exc:  # the recorded report stays available
                report = {**report, 'evidence_error': str(exc)}
        out.append({'run_id': run_id, **{o: float(row[o]) for o in b.OBJECTIVE_COLUMNS},
                    'equations_above_target': solve.get('review', {}).get('equations_above_target'),
                    'rms': report.get('rms'), 'wall_notices': report.get('wall_notices'),
                    'iteration': report.get('iteration'), 'run_iteration': report.get('run_iteration'),
                    'history': report.get('history'),
                    'normalised_imbalance_percent': report.get('normalised_imbalance_percent'),
                    'warnings': solve.get('review', {}).get('warnings', []),
                    'evidence_error': report.get('evidence_error'),
                    'out_file': str(case / solve['out_file']) if solve.get('out_file') else None,
                    'message': row.message})
    return out


def decide(config: dict[str, Any], run_id: str, decision: str, reason: str) -> dict[str, Any]:
    """Record ``accepted`` or ``rejected`` for one result awaiting review."""
    if decision not in OUTCOME:
        raise ValueError(f'Unknown review decision: {decision}')
    if not reason.strip():
        raise ValueError('A review decision needs a reason.')
    with output_lock(b.output_dir(config)):
        path = b.training_csv_path(config)
        frame = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=['run_id', 'status'])
        rows = frame.index[frame.run_id.astype(str).eq(run_id)]
        if len(rows) != 1:
            raise ValueError(f'{run_id}: expected one training row, found {len(rows)}.')
        index = rows[0]
        status = str(frame.at[index, 'status'])
        case, solve = _solve_state(config, run_id)
        registry_path = decisions_path(config)
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else {'decisions': []}
        earlier = [d for d in registry['decisions']
                   if d['run_id'] == run_id and d['out_identity'] == solve.get('out_identity')]
        if status != 'review':
            # Interrupted after the training row was rewritten: only the same
            # decision may finish what is left.
            if not earlier or earlier[-1]['decision'] != decision or OUTCOME[decision][0] != status:
                raise ValueError(f'{run_id} is not awaiting review (status={status}).')
            record = earlier[-1]
        else:
            if solve.get('status') != 'review' or not solve.get('review'):
                raise ValueError(f'{case / "cfx_state.json"} does not record {run_id} as awaiting review; inspect it.')
            if (file_identity(case / solve['out_file']) != solve.get('out_identity')
                    or file_identity(case / solve['result_file']) != solve.get('result_identity')):
                raise ValueError(f'{run_id}: the .out/.res no longer match the recorded solve; '
                                 'a decision cannot be based on changed evidence.')
            # A decision recorded before an interruption is applied, never contradicted.
            if earlier and earlier[-1]['decision'] != decision:
                raise ValueError(f"{run_id} was already {earlier[-1]['decision']} at {earlier[-1]['decided_at']} "
                                 f"({earlier[-1]['reason']}); that decision stands.")
            if earlier:
                record = earlier[-1]
            else:
                record = {'run_id': run_id, 'decision': decision, 'reason': reason.strip(),
                          'decided_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                          'objectives': {o: float(frame.at[index, o]) for o in b.OBJECTIVE_COLUMNS},
                          signatures.COLUMN: str(frame.at[index, signatures.COLUMN])
                          if signatures.COLUMN in frame else None,
                          'out_file': solve['out_file'], 'out_identity': solve['out_identity'],
                          'review': solve['review']}
                registry['decisions'].append(record)
                atomic_json(registry_path, registry)

        new_status, failure_stage = OUTCOME[record['decision']]
        for other in (b.diagnostics_csv_path(config), b.output_dir(config) / 'boundary_diagnostics.csv'):
            _set(other, run_id, {'status': new_status}, only_existing=True)
        queue = pending.load(config)
        entries = [e for e in queue['entries'] if e['run_id'] == run_id and e.get('finalized')]
        for entry in entries:
            entry['status'] = new_status
        if entries:
            atomic_json(pending.path_for(config), queue)
        # The training row goes last but one: while it still says review, the
        # first branch above is not needed to finish an interrupted decision.
        _set(path, run_id, {'status': new_status, 'failure_stage': failure_stage, 'message': _message(record),
                            'review_decision': record['decision'], 'review_reason': record['reason'],
                            'reviewed_at': record['decided_at']})
        b.write_pareto(config, b.load_training(config))
    return record


def _message(record: dict[str, Any]) -> str:
    review = record.get('review') or {}
    above = ', '.join(f'{name} {value:.2g}' for name, value in sorted(review.get('equations_above_target', {}).items()))
    detail = f" (RMS above {review['rms_target']:g} on {above})" if above else ''
    return f"Manually {record['decision']} after review: {record['reason']}{detail}"


def _set(path: Path, run_id: str, values: dict[str, Any], *, only_existing: bool = False) -> None:
    """Rewrite the rows of ``run_id`` in a CSV atomically, keeping every other column."""
    if not path.exists():
        return
    frame = pd.read_csv(path)
    if 'run_id' not in frame or (only_existing and 'status' not in frame):
        return
    mask = frame.run_id.astype(str).eq(run_id)
    if not mask.any():
        return
    for column, value in values.items():
        if column not in frame:
            frame[column] = pd.Series(dtype=object)
        frame[column] = frame[column].astype(object)
        frame.loc[mask, column] = value
    temp = path.with_suffix(path.suffix + '.tmp')
    frame.to_csv(temp, index=False)
    temp.replace(path)
