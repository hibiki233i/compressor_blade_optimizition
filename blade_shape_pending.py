"""Durable ordinary-AL/DOE queue; never reconstruct success from unverified files."""
from __future__ import annotations
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
from blade_shape_runtime import atomic_json, case_reservations


def safe_json(value: Any) -> Any:
    if isinstance(value, dict):return {k:safe_json(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):return [safe_json(v) for v in value]
    if isinstance(value, np.ndarray):return safe_json(value.tolist())
    if isinstance(value, (float,np.floating)):return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):return int(value)
    if isinstance(value, np.bool_):return bool(value)
    return value


def path_for(config: dict[str,Any]) -> Path:
    return Path(config['paths']['output_dir'])/'pending_evaluations.json'


def load(config: dict[str,Any]) -> dict[str,Any]:
    path=path_for(config)
    value=json.loads(path.read_text()) if path.exists() else {'version':1,'entries':[]}
    if value.get('version')!=1:raise ValueError('Unknown pending queue version.')
    return value


def enqueue(config: dict[str,Any], selected: list[Any], arguments: list[dict[str,Any]]) -> None:
    import blade_shape_active_learning as b
    import blade_shape_refinement as r
    queue=load(config)
    index=b.next_case_index(config,b.load_training(config))
    signature=r.physical_signature(config)
    for candidate,metadata in zip(selected,arguments,strict=True):
        entry={'run_id':f'case_{index:06d}', 'signature':signature,'x':candidate.x.tolist(),
               'arguments':safe_json(metadata),'selected':safe_json(asdict(candidate)),'status':'pending','finalized':False}
        queue['entries'].append(entry);index+=1
    atomic_json(path_for(config),queue)
    for entry in queue['entries']:
        if entry['status']=='pending':
            atomic_json(Path(config['paths']['output_dir'])/'predictions'/f"{entry['run_id']}.json",entry)


def check_orphans(config: dict[str,Any], frame: pd.DataFrame) -> None:
    """Old orphan files need explicit provenance review; don't silently skip them."""
    queue=load(config)
    known=set(frame.run_id.astype(str))|{e['run_id'] for e in queue['entries']}|set(case_reservations(config['paths']['output_dir']))
    cases=Path(config['paths']['output_dir'])/'cases'
    for folder in cases.glob('case_*'):
        if folder.name not in known and any((folder/name).exists() for name in ['candidate.json','CFX_Results.txt','cfx_state.json']):
            raise ValueError(f'Untracked case {folder.name}: inspect its provenance before continuing. No new CFD was selected.')


def run(config: dict[str,Any], baseline: Any, budget: int) -> dict[str,Any]:
    import blade_shape_active_learning as b
    import blade_shape_refinement as r
    queue=load(config);used=0;failures=0;summary=[]
    for entry in queue['entries']:
        frame=b.load_training(config)
        recorded=frame.loc[frame.run_id.eq(entry['run_id'])]
        if len(recorded)>1:raise ValueError(f"Duplicate pending run_id: {entry['run_id']}")
        if entry['finalized']:
            if recorded.empty:raise ValueError(f"Completed queue row missing from CSV: {entry['run_id']}")
            continue
        if entry['signature']!=r.physical_signature(config):
            raise ValueError(f"Inputs changed for pending {entry['run_id']}; inspect before resuming.")
        x=np.array(entry['x'],float)
        if not recorded.empty:
            row=recorded.iloc[0]
            if not np.allclose(row[b.variable_names(config)].to_numpy(float),x,rtol=0,atol=1e-10):
                raise ValueError('Pending input and recorded CFD row do not match.')
            if row['sample_phase']!=entry['arguments']['sample_phase']:
                raise ValueError('Pending sample phase mismatch.')
            if row['status'] not in ['success','failed']:raise ValueError('Unrecognized recorded CFD outcome.')
            result=b.CaseResult(row.to_dict(),row['status']=='success')
        else:
            if used>=budget:break
            if entry['status']=='pending':
                entry['before_run_ids']=frame.run_id.tolist()
                entry['status']='running'
                atomic_json(path_for(config),queue)
            candidate=Path(config['paths']['output_dir'])/'cases'/entry['run_id']/'candidate.json'
            if candidate.exists():
                old=json.loads(candidate.read_text())
                if not np.allclose([old['variables'][n] for n in b.variable_names(config)],x,rtol=0,atol=1e-10):
                    raise ValueError('Pending candidate file changed.')
            print(f"[CFD] {entry['arguments']['sample_phase']} {entry['run_id']}")
            result=b.evaluate_true_cfd(config,baseline,x,b.parse_case_number(entry['run_id']),**entry['arguments'])
            used+=1
        before=frame.loc[frame.run_id.isin(entry.get('before_run_ids',[]))]
        metrics=r.improvement_metrics(config,before,result.row)
        if entry['arguments']['sample_phase']=='active_learning':
            d=entry['selected'].copy()
            d['x']=x
            for key in ['pred_mean','pred_std']:
                if d.get(key) is not None:d[key]=np.array(d[key],float)
            for key in ['acquisition_score','ehvi','distance_to_existing']:
                if d.get(key) is None:d[key]=np.nan
            candidate=b.SelectedCandidate(**d)
            candidate.metadata.update(metrics)
            diagnostic=b.build_diagnostic_row(config,iteration=entry['arguments']['al_iteration'],selected=candidate,result=result,
                         pareto_rows_before=len(b.write_pareto(config,before)),
                         pareto_rows_after=len(b.write_pareto(config,b.load_training(config))))
            existing=b.load_existing_diagnostics(config)
            if entry['run_id'] not in existing:b.append_diagnostic_row(config,diagnostic)
            r.write_diagnostics(config)
            r.update_local_search(config,entry['run_id'],metrics,result.success)
        b.write_pareto(config,b.load_training(config))
        entry['status']='success' if result.success else 'failed'
        entry['finalized']=True
        atomic_json(path_for(config),queue)
        failures+=int(not result.success)
        summary.append({'sample_phase':entry['arguments']['sample_phase'],'iteration':entry['arguments'].get('al_iteration',-1),
                        'batch_index':entry['arguments'].get('batch_index',''),'run_id':entry['run_id'],'status':entry['status']})
    return {'new_attempts':used,'failures':failures,'summary':summary}
