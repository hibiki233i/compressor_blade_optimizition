"""Conditional search, prospective diagnostics and staged real-CFD experiments.

All geometry vectors retain their original twelve-coordinate layout. This module
never promotes a prediction or a dry-run to a successful CFD observation.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from blade_shape_acquisition import hv_reference_point
from blade_shape_runtime import output_lock, case_reservations, reserve_case, stream_seed, atomic_json as durable_json

OBJECTIVES = ['Efficiency', 'MassFlow']

#: Prediction intervals are mean ± INTERVAL_SIGMAS·σ (the *_2sigma diagnostics).
INTERVAL_SIGMAS = 2.0
#: Calibration inflates σ by max(1, q/INTERVAL_SIGMAS), where q is this quantile of
#: |true − pred|/σ over recent prospective residuals, so that the 2σ interval
#: would have covered 95% of them. σ is never shrunk below the model's own value.
CALIBRATION_COVERAGE = 0.95
#: Default for refinement.diagnostic_gate.min_coverage_2sigma: with the default
#: diagnostic_min_points of 6, one point may fall outside its 2σ interval.
DEFAULT_MIN_COVERAGE_2SIGMA = 5/6


def active_indices(config: dict[str, Any]) -> list[int]:
    names = [v['name'] for v in config['variables']]
    if len(names) != len(set(names)):
        raise ValueError('Variable names must be unique.')
    search = config.get('search', {})
    fixed = search.get('fixed_variables', {})
    active = search.get('active_variables', [n for n in names if n not in fixed])
    if not active or len(set(active)) != len(active) or set(active) & set(fixed) or set(active) | set(fixed) != set(names):
        raise ValueError('Active and fixed variables must form a nonempty, disjoint partition.')
    for v in config['variables']:
        if not np.isfinite([v['lower'],v['upper']]).all() or v['lower'] >= v['upper']:
            raise ValueError(f"Invalid bounds: {v['name']}")
        if v['name'] in fixed and not v['lower'] <= float(fixed[v['name']]) <= v['upper']:
            raise ValueError(f"Fixed value outside bounds: {v['name']}")
    tol = float(search.get('slice_tolerance_norm', 1e-8))
    if not np.isfinite(tol) or tol <= 0:
        raise ValueError('slice_tolerance_norm must be positive and finite.')
    return [i for i,n in enumerate(names) if n in active]


def enforce_fixed(config: dict[str, Any], x: np.ndarray) -> np.ndarray:
    active_indices(config)
    out = np.asarray(x, dtype=float).copy()
    for i,v in enumerate(config['variables']):
        if v['name'] in config.get('search', {}).get('fixed_variables', {}):
            out[...,i] = config['search']['fixed_variables'][v['name']]
    return out


def slice_distance(config: dict[str, Any], x: np.ndarray) -> np.ndarray:
    active_indices(config)
    a = np.atleast_2d(np.asarray(x, dtype=float))
    fixed = config.get('search', {}).get('fixed_variables', {})
    idx = [i for i,v in enumerate(config['variables']) if v['name'] in fixed]
    if not idx:
        return np.zeros(len(a))
    values = np.array([fixed[config['variables'][i]['name']] for i in idx])
    span = np.array([config['variables'][i]['upper']-config['variables'][i]['lower'] for i in idx])
    return np.linalg.norm((a[:,idx]-values)/span, axis=1)


def same_slice_mask(config: dict[str, Any], x: np.ndarray) -> np.ndarray:
    return slice_distance(config,x) <= float(config.get('search',{}).get('slice_tolerance_norm',1e-8))


def slice_id(config: dict[str, Any]) -> str:
    return digest({'variables': config['variables'], 'search': config.get('search',{})})[:16]


def training_partition(config: dict[str, Any], frame: pd.DataFrame) -> tuple[pd.DataFrame,pd.DataFrame]:
    names = [v['name'] for v in config['variables']]
    clean = frame.loc[frame.status.eq('success')].copy()
    numeric = clean[names+OBJECTIVES].apply(pd.to_numeric,errors='coerce')
    clean = clean.loc[np.isfinite(numeric.to_numpy()).all(axis=1)].copy()
    clean[names+OBJECTIVES] = numeric.loc[clean.index]
    return clean, clean.loc[same_slice_mask(config,clean[names].to_numpy())].copy()


def active_distances(config: dict[str, Any], candidates: np.ndarray, existing: np.ndarray) -> np.ndarray:
    idx = active_indices(config)
    # A far-away inactive coordinate is not equivalent evidence for this slice.
    local = existing[same_slice_mask(config,existing)] if len(existing) else existing
    if not len(local):
        return np.full(len(candidates),np.sqrt(len(idx)))
    span = np.array([v['upper']-v['lower'] for v in config['variables']])[idx]
    distances = (candidates[:,None,idx]-local[None,:,idx])/span
    return np.linalg.norm(distances,axis=2).min(axis=1)


class ConditionalSurrogate:
    """Full-input primary and independently fitted same-slice challenger.

    Calibration uses only earlier prospective residuals from this slice and the
    same primary model. No in-sample residuals are used to inflate certainty.
    """
    def __init__(self, config: dict[str,Any], frame: pd.DataFrame, factory: Callable, seed: int,
                 diagnostics: pd.DataFrame | None = None):
        self.config = config
        names = [v['name'] for v in config['variables']]
        self.active = active_indices(config)
        history,local = training_partition(config,frame)
        self.primary = factory(config,history[names].to_numpy(),history[OBJECTIVES].to_numpy(),seed)
        self.model_name = type(self.primary).__name__ + ':full12'
        self.history_count,self.slice_count = len(history),len(local)
        self.challenger = None
        self.challenger_message = 'Insufficient same-slice samples.'
        minimum = max(4,int(config.get('refinement',{}).get('challenger_min_samples',12)))
        if len(local) >= minimum and len(self.active)<len(names):
            local_config = copy.deepcopy(config)
            local_config.pop('search',None)
            local_config['variables'] = [config['variables'][i] for i in self.active]
            try:
                self.challenger = factory(local_config,local[names].to_numpy()[:,self.active],local[OBJECTIVES].to_numpy(),seed)
                self.challenger_message = 'Available; trained only on matching fixed coordinates.'
            except (ValueError,RuntimeError,np.linalg.LinAlgError) as exc:
                self.challenger_message = f'Unavailable: {exc}'
        self.scale = np.ones(2)
        self.calibration_count = 0
        if diagnostics is not None and {'slice_id','surrogate_model','status'} <= set(diagnostics.columns):
            d = diagnostics.loc[(diagnostics.slice_id==slice_id(config)) &
                                (diagnostics.surrogate_model==self.model_name) & diagnostics.status.eq('success')]
            d = d.drop_duplicates('run_id',keep='last').tail(int(config.get('refinement',{}).get('diagnostic_window',12)))
            required = [f'{prefix}_{o}' for o in OBJECTIVES for prefix in ['true','pred','raw_std']]
            if set(required)<=set(d.columns):
                vals = d[required].apply(pd.to_numeric,errors='coerce')
                d = d.loc[np.isfinite(vals).all(axis=1)]
                self.calibration_count = len(d)
                if len(d)>=int(config.get('refinement',{}).get('diagnostic_min_points',6)):
                    for j,o in enumerate(OBJECTIVES):
                        ratios = (d[f'true_{o}']-d[f'pred_{o}']).abs()/np.maximum(d[f'raw_std_{o}'],1e-12)
                        self.scale[j] = max(1.,float(np.quantile(ratios,CALIBRATION_COVERAGE))/INTERVAL_SIGMAS)

    def predict(self, x: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
        mean,std = self.primary.predict(x)
        return mean,std*self.scale

    def metadata(self, x: np.ndarray) -> dict[str,Any]:
        _,raw = self.primary.predict(x[None,:])
        data = {'slice_id':slice_id(self.config),'candidate_on_slice':True,
                'inactive_distance_norm':float(slice_distance(self.config,x)[0]),
                'surrogate_model':self.model_name,'history_train_count':self.history_count,
                'slice_train_count':self.slice_count,'calibration_count':self.calibration_count,
                'challenger_status':self.challenger_message}
        for j,o in enumerate(OBJECTIVES):
            data[f'raw_std_{o}'] = float(raw[0,j])
            data[f'calibration_scale_{o}'] = float(self.scale[j])
        if self.challenger is not None:
            mean,std = self.challenger.predict(x[None,self.active])
            for j,o in enumerate(OBJECTIVES):
                data[f'challenger_pred_{o}'] = float(mean[0,j])
                data[f'challenger_std_{o}'] = float(std[0,j])
        return data


def hypervolume_2d(values: np.ndarray, reference: np.ndarray) -> float:
    values = np.asarray(values,dtype=float).reshape(-1,2)
    values = values[np.isfinite(values).all(axis=1)&np.all(values>reference,axis=1)]
    top,area = float(reference[1]),0.
    for a,f in values[np.argsort(values[:,0])[::-1]]:
        if f>top:
            area += (a-reference[0])*(f-top)
            top=f
    return float(area)


def atomic_json(path: Path, value: dict[str,Any]) -> None:
    durable_json(path, value)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def hv_reference(config: dict[str,Any], frame: pd.DataFrame) -> np.ndarray | None:
    path=Path(config['paths']['output_dir'])/'hypervolume_reference.json'
    if path.exists():
        payload=json.loads(path.read_text())
        if payload.get('objectives')!=OBJECTIVES:
            raise ValueError('Hypervolume reference objective mismatch.')
        result=np.array(payload['reference'],dtype=float)
        if result.shape!=(2,) or not np.isfinite(result).all():
            raise ValueError('Invalid persisted hypervolume reference.')
        return result
    clean,_=training_partition(config,frame)
    if clean.empty:return None
    doe=clean.loc[clean.sample_phase.eq('doe')]
    basis=doe if not doe.empty else clean
    y=basis[OBJECTIVES].to_numpy(float)
    ref=hv_reference_point(y)
    atomic_json(path,{'objectives':OBJECTIVES,'reference':ref.tolist(),
                      'source_run_ids':basis.run_id.tolist(),'source':'DOE' if not doe.empty else 'first available successes'})
    return ref


def improvement_metrics(config: dict[str,Any], before: pd.DataFrame, result: dict[str,Any]) -> dict[str,Any]:
    ref=hv_reference(config,before)
    if ref is None:return {}
    clean,_=training_partition(config,before)
    y=clean[OBJECTIVES].to_numpy(float)
    previous=hypervolume_2d(y,ref)
    success=result.get('status')=='success'
    value=np.array([result.get(o,np.nan) for o in OBJECTIVES],dtype=float)
    success=success and np.isfinite(value).all()
    after=hypervolume_2d(np.vstack([y,value]),ref) if success else previous
    tol=np.array([config.get('pareto',{}).get('tolerances',{}).get(o,0) for o in OBJECTIVES])
    # Engineering nondomination is reported independently from strict HV gain.
    dominated=bool(len(y) and np.any(np.all(y>=value-tol,axis=1)&np.any(y>value+tol,axis=1))) if success else True
    out={'hv_before':previous,'hv_after':after,'hv_gain':after-previous,
         'engineering_nondominated':bool(success and not dominated)}
    for j,o in enumerate(OBJECTIVES):
        out[f'delta_best_{o}']=float(max(0.,value[j]-y[:,j].max())) if success and len(y) else 0.
    return out


def role_diagnostics(frame: pd.DataFrame, prefix: str = 'pred') -> pd.DataFrame:
    columns=['candidate_role','objective','prediction_channel','n','mae','rmse','bias','coverage_2sigma','mean_interval_width_2sigma']
    if frame.empty or 'status' not in frame:return pd.DataFrame(columns=columns)
    frame=frame.loc[frame.status.eq('success')].copy()
    if 'run_id' in frame:frame=frame.drop_duplicates('run_id',keep='last')
    frame['candidate_role']=frame.get('candidate_role',pd.Series('legacy',index=frame.index)).fillna('legacy')
    rows=[]
    for role,g in frame.groupby('candidate_role'):
        for o in OBJECTIVES:
            pred=f'{prefix}_{o}';std=f"{'challenger_std' if prefix=='challenger_pred' else 'std'}_{o}"
            if {pred,f'true_{o}',std} - set(g.columns):continue
            q=g[[pred,f'true_{o}',std]].apply(pd.to_numeric,errors='coerce')
            q=q.loc[np.isfinite(q).all(axis=1)&(q[std]>=0)]
            if q.empty:continue
            e=q[f'true_{o}']-q[pred]
            rows.append(dict(candidate_role=role,objective=o,prediction_channel=prefix,n=len(q),
                             mae=e.abs().mean(),rmse=np.sqrt(np.mean(e**2)),bias=e.mean(),
                             coverage_2sigma=np.mean(e.abs()<=INTERVAL_SIGMAS*q[std]),
                             mean_interval_width_2sigma=(2*INTERVAL_SIGMAS*q[std]).mean()))
    return pd.DataFrame(rows,columns=columns)


def diagnostic_min_coverage(config: dict[str,Any]) -> float:
    """Minimum 2σ coverage for the prospective diagnostic gate (engineering acceptance policy)."""
    gate=config.get('refinement',{}).get('diagnostic_gate',{})
    value=gate.get('min_coverage_2sigma',DEFAULT_MIN_COVERAGE_2SIGMA) if isinstance(gate,dict) else None
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<value<=1:
        raise ValueError('refinement.diagnostic_gate.min_coverage_2sigma must be a number in (0, 1].')
    return float(value)


def write_diagnostics(config: dict[str,Any]) -> dict[str,Any]:
    out=Path(config['paths']['output_dir']);path=out/'active_learning_diagnostics.csv'
    d=pd.read_csv(path) if path.exists() else pd.DataFrame()
    current=d.loc[d.slice_id.eq(slice_id(config))].copy() if 'slice_id' in d else d.iloc[:0].copy()
    current=current.drop_duplicates('run_id',keep='last') if 'run_id' in current else current
    if not current.empty and 'surrogate_model' in current:
        current=current.loc[current.surrogate_model.eq(current.surrogate_model.iloc[-1])]
    current=current.tail(int(config.get('refinement',{}).get('diagnostic_window',12)))
    table=pd.concat([role_diagnostics(current),role_diagnostics(current,'challenger_pred')],ignore_index=True)
    out.mkdir(parents=True,exist_ok=True)
    table.to_csv(out/'role_diagnostics.csv',index=False)
    # Historical rows without a slice identifier remain useful, but never pass a current gate.
    role_diagnostics(d).to_csv(out/'role_diagnostics_history.csv',index=False)
    minimum=int(config.get('refinement',{}).get('diagnostic_min_points',6))
    coverage=diagnostic_min_coverage(config)
    gate={'slice_id':slice_id(config),'purpose':'prospective_ehvi_prediction',
          'minimum_points':minimum,'min_coverage_2sigma':coverage,'passed':False,
          'predefined_singles_require_this_gate':False,'objectives':{}}
    for o in OBJECTIVES:
        q=table.loc[(table.candidate_role=='ehvi')&(table.objective==o)&(table.prediction_channel=='pred')]
        tol=float(config['pareto']['tolerances'][o])
        if q.empty:gate['objectives'][o]={'n':0,'passed':False};continue
        row=q.iloc[0]
        gate['objectives'][o]={'n':int(row['n']),'mae':float(row.mae),'tolerance':tol,
                               'coverage_2sigma':float(row.coverage_2sigma),
                               'mean_interval_width_2sigma':float(row.mean_interval_width_2sigma),
                               'passed':bool(row['n']>=minimum and row.mae<=tol and row.coverage_2sigma>=coverage)}
    gate['passed']=all(v['passed'] for v in gate['objectives'].values())
    atomic_json(out/'local_diagnostic_gate.json',gate)
    return gate


def physical_signature(config: dict[str,Any]) -> str:
    keys=['cft_batch_template','base_cft','turbogrid_template','template_cfx','template_cse','geometry_script_path']
    assets={}
    for key in keys:
        path=Path(config['paths'].get(key,''))
        assets[key]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    return digest({'variables':config['variables'],'constraints':config['constraints'],'search':config.get('search',{}),
                   'cfx_convergence':config.get('cfx_convergence',{}),
                   'runtime':{k:config['runtime'].get(k) for k in ['n_blades','rpm','mass_flow','p_out_pa','alpha0']},'assets':assets})


def point_config(config: dict[str,Any], point: dict[str,Any]) -> dict[str,Any]:
    out=copy.deepcopy(config)
    if point['stage']=='extension':
        ext=config['refinement']['extension']
        for v in out['variables']:
            if v['name']==ext['variable']:v['upper']=float(ext['value'])
    return out


def write_boundary_plan(config: dict[str,Any], center_run_id: str, path: Path) -> dict[str,Any]:
    with output_lock(config['paths']['output_dir']):
        return _write_boundary_plan(config, center_run_id, path)


def trial_level(config: dict[str,Any], name: str, center: float) -> tuple[float,list[str]]:
    variable = next(v for v in config['variables'] if v['name'] == name)
    setting = config['refinement'].get('boundary_levels', {}).get(name)
    if setting is None:
        target = float(variable['upper'])
        return target, [] if abs(target-center)>1e-8 else ['zero_trial_step']
    minimum, maximum = float(setting['min_step_deg']), float(setting['max_step_deg'])
    if not 0 < minimum <= maximum:
        raise ValueError(f'Invalid step limits: {name}')
    if 'value' in setting:
        target = float(setting['value'])
    else:
        step = float(setting['step_deg'])
        if not np.isfinite(step) or step <= 0:
            raise ValueError(f'Invalid step: {name}')
        target = min(center+step, variable['upper'])
        if target-center < minimum:
            target = max(center-step, variable['lower'])
    issues = []
    if not np.isfinite(target) or not variable['lower'] <= target <= variable['upper']:
        issues.append('trial_level_outside_bounds')
    if not minimum-1e-10 <= abs(target-center) <= maximum+1e-10:
        issues.append('trial_step_outside_limits')
    return target, issues


def _write_boundary_plan(config: dict[str,Any], center_run_id: str, path: Path) -> dict[str,Any]:
    import blade_shape_active_learning as b
    path=Path(path)
    if path.exists():raise FileExistsError(f'Plan already exists: {path}')
    active=active_indices(config);names=b.variable_names(config)
    controls=config['refinement']['boundary_variables']
    if len(controls)!=4 or len(set(controls))!=4 or any(n not in [names[i] for i in active] for n in controls):
        raise ValueError('Exactly four distinct active boundary variables are required.')
    frame=b.load_training(config)
    _,local=training_partition(config,frame)
    selected=local.loc[local.run_id.eq(center_run_id)]
    if len(selected)!=1:raise ValueError('Center must be a unique successful same-slice CFD row.')
    center=selected.iloc[0][names].to_numpy(float)
    if not np.allclose(center,enforce_fixed(config,center),rtol=0,atol=1e-10):
        raise ValueError('Center fixed coordinates must match the configured frozen vector.')
    idx=[names.index(n) for n in controls]
    levels = {i: trial_level(config,names[i],float(center[i])) for i in idx}
    points=[{'point_id':'center','stage':'singles','x':center.tolist(),'issues':[]}]
    for size in [1,2]:
        for combo in itertools.combinations(idx,size):
            x=center.copy()
            for i in combo:x[i]=levels[i][0]
            points.append({'point_id':'+'.join(names[i] for i in combo),'stage':'singles' if size==1 else 'pairs',
                           'x':x.tolist(),'issues':[f'{names[i]}:{issue}' for i in combo for issue in levels[i][1]]})
    ext=config['refinement']['extension'];ei=names.index(ext['variable'])
    if ei not in idx or not np.isfinite(ext['value']) or ext['value']<=config['variables'][ei]['upper']:
        raise ValueError('Extension must increase the high endpoint of one boundary variable.')
    control=center.copy();control[ei]=config['variables'][ei]['upper']
    matching=[p for p in points if np.allclose(p['x'],control,rtol=0,atol=1e-12)]
    if matching:
        control_id=matching[0]['point_id']
    else:
        control_id='extension_control'
        issues=[]
        setting=config['refinement'].get('boundary_levels',{}).get(names[ei])
        if setting and abs(control[ei]-center[ei])>float(setting['max_step_deg'])+1e-10:
            issues.append('extension_control_too_far_from_center')
        points.append({'point_id':control_id,'stage':'extension','x':control.tolist(),'issues':issues})
    x=control.copy();x[ei]=float(ext['value'])
    points.append({'point_id':'extension','stage':'extension','x':x.tolist(),'control_id':control_id,'issues':[]})
    baseline=b.extract_baseline(config)
    stages={stage:{'valid':True,'issues':[]} for stage in ['singles','pairs','extension']}
    for point in points:
        point['issues'].extend(b.constraint_violations(point_config(config,point),baseline,np.array(point['x'])))
        if point['issues']:
            stages[point['stage']]['valid']=False
            stages[point['stage']]['issues'].append({'point_id':point['point_id'],'issues':point['issues']})
    payload={'version':2,'center_run_id':center_run_id,'center_x':center.tolist(),'slice_id':slice_id(config),
             'signature':physical_signature(config),'points':points,'stages':stages,
             'variables':names,'extension':ext,'boundary_levels':config['refinement'].get('boundary_levels',{}),
             'validation':'Python geometry rules only; mesh/CFD not run'}
    payload['plan_id']=digest(payload)
    atomic_json(path,payload)
    return payload


def run_boundary(config: dict[str,Any], path: Path, stage: str, max_new: int, resume: bool) -> dict[str,Any]:
    with output_lock(config['paths']['output_dir']):
        return _run_boundary(config,path,stage,max_new,resume)


def _run_boundary(config: dict[str,Any], path: Path, stage: str, max_new: int, resume: bool) -> dict[str,Any]:
    import blade_shape_active_learning as b
    b.check_geometry_script(config)
    path=Path(path)
    if stage not in ['singles','pairs','extension'] or max_new<=0:
        raise ValueError('Choose singles/pairs/extension and a positive CFD budget.')
    plan=json.loads(path.read_text());content={k:v for k,v in plan.items() if k!='plan_id'}
    if plan.get('version') != 2:
        raise ValueError('Create a version-2 plan; old plans are not silently migrated.')
    if plan.get('plan_id')!=digest(content) or plan['signature']!=physical_signature(config):
        raise ValueError('Plan or geometry/operating configuration changed; create a new plan.')
    if plan['extension']!=config['refinement']['extension']:
        raise ValueError('Extension configuration changed.')
    if not plan['stages'][stage]['valid']:
        raise ValueError(f"Stage {stage} is blocked: {plan['stages'][stage]['issues']}")
    state_path=path.with_suffix('.state.json')
    boundary_csv=Path(config['paths']['output_dir'])/'boundary_diagnostics.csv'
    exists=state_path.exists()
    state=json.loads(state_path.read_text()) if exists else {'plan_id':plan['plan_id'],'points':{}}
    if state['plan_id']!=plan['plan_id']:raise ValueError('State belongs to another plan.')
    if exists and not resume:raise ValueError('State exists. Use --resume to continue.')
    frame=b.load_training(config)
    reservations=case_reservations(config['paths']['output_dir'])
    for run_id,owner in reservations.items():
        if owner.get('plan_id')==plan['plan_id'] and owner['point_id'] not in state['points']:
            state['points'][owner['point_id']]={'run_id':run_id,'status':'running'}
    for p in plan['points']:
        saved=state['points'].get(p['point_id'],{})
        if not saved.get('run_id'):continue
        rows=frame.loc[frame.run_id.eq(saved['run_id'])]
        if len(rows)>1:raise ValueError('Duplicate recorded run_id; resolve before resuming.')
        if len(rows):
            row=rows.iloc[0]
            if not np.allclose(row[plan['variables']].to_numpy(float),p['x'],rtol=0,atol=1e-10) or row.get('experiment_id')!=plan['plan_id']:
                raise ValueError('Reserved case does not match this experiment.')
            saved['status']=str(row.status)
            preceding=frame.loc[frame.run_id.map(b.parse_case_number).fillna(-1)<b.parse_case_number(saved['run_id'])]
            saved.update(improvement_metrics(config,preceding,row.to_dict()))
            if saved.get('prediction'):
                diagnostic={**saved['prediction'], **saved.get('metrics',{}),
                            **improvement_metrics(config,preceding,row.to_dict()),'status':str(row.status)}
                diagnostic.update({f'true_{o}':float(row[o]) if pd.notna(row[o]) else None for o in OBJECTIVES})
                existing=pd.read_csv(boundary_csv) if boundary_csv.exists() else pd.DataFrame()
                if existing.empty or saved['run_id'] not in existing.run_id.values:
                    b.append_compatible_csv(boundary_csv,diagnostic,list(diagnostic))
        elif saved.get('status') in ['success','failed']:
            raise ValueError('Checkpoint outcome is missing from training_data.csv.')
    required=['singles'] if stage=='pairs' else ['singles','pairs'] if stage=='extension' else []
    if any(state['points'].get(p['point_id'],{}).get('status')!='success' for p in plan['points'] if p['stage'] in required):
        raise ValueError('Earlier stages must finish successfully before starting this stage.')
    if exists:
        atomic_json(state_path,state)
    baseline=b.extract_baseline(config)
    used=0
    for p in plan['points']:
        if p['stage']!=stage:continue
        saved=state['points'].get(p['point_id'],{})
        if saved.get('status')=='success':continue
        if saved.get('status')=='failed':
            raise ValueError('Failed point requires inspection and a new experiment; no automatic CFD retry.')
        if used>=max_new:break
        cfg=point_config(config,p);x=np.array(p['x'])
        issues=b.constraint_violations(cfg,baseline,x)
        if issues:raise ValueError(f"Geometry rules failed: {p['point_id']}: {issues}")
        before=b.load_training(config)
        index=int(saved['run_id'].split('_')[1]) if saved.get('run_id') else b.next_case_index(config,before)
        run_id=f'case_{index:06d}'
        reserve_case(config['paths']['output_dir'],run_id,{'plan_id':plan['plan_id'],'point_id':p['point_id'],
                                                        'x':p['x'],'signature':plan['signature']})
        candidate=Path(config['paths']['output_dir'])/'cases'/run_id/'candidate.json'
        if candidate.exists():
            stored=json.loads(candidate.read_text())
            if not np.allclose([stored['variables'][n] for n in plan['variables']],x,rtol=0,atol=1e-10):
                raise ValueError('Existing candidate differs from reserved boundary design.')
        if not saved.get('prediction'):
            history,_=training_partition(config,before)
            prediction={'run_id':run_id,'candidate_role':'boundary_'+stage,'experiment_id':plan['plan_id'],
                        'point_id':p['point_id'],'slice_id':plan['slice_id']}
            if len(history)>=4:
                diagnostics_path=Path(config['paths']['output_dir'])/'active_learning_diagnostics.csv'
                try:
                    diagnostics=pd.read_csv(diagnostics_path) if diagnostics_path.exists() else pd.DataFrame()
                    model=ConditionalSurrogate(config,before,b.fit_surrogate,index,diagnostics)
                    mean,std=model.predict(x[None,:])
                    metadata=model.metadata(x)
                    if not np.isfinite(mean).all() or not np.isfinite(std).all():
                        raise ValueError('Non-finite model predictions.')
                    prediction.update(metadata)
                    for j,o in enumerate(OBJECTIVES):
                        prediction[f'pred_{o}']=float(mean[0,j]);prediction[f'std_{o}']=float(std[0,j])
                    prediction['prediction_status']='available'
                except (ValueError,RuntimeError,OSError,pd.errors.ParserError,np.linalg.LinAlgError) as exc:
                    prediction['prediction_status']='unavailable'
                    prediction['prediction_message']=str(exc)
            saved={**saved,'prediction':prediction}
        saved.update({'run_id':run_id,'status':'running'});state['points'][p['point_id']]=saved
        atomic_json(state_path,state)
        result=b.evaluate_true_cfd(cfg,baseline,x,index,sample_phase='boundary',
                                   experiment_id=plan['plan_id'],design_role=p['point_id'],selection_source='predefined_boundary')
        used+=1;saved['status']='success' if result.success else 'failed'
        saved.update(improvement_metrics(config,before,result.row))
        diagnostic={**saved['prediction'],**improvement_metrics(config,before,result.row),'status':result.row['status']}
        diagnostic.update({f'true_{o}':float(result.row[o]) if pd.notna(result.row.get(o)) else None for o in OBJECTIVES})
        b.append_compatible_csv(boundary_csv,diagnostic,list(diagnostic))
        atomic_json(state_path,state)
        b.write_pareto(config,b.load_training(config))
        if not result.success:break
    statuses=[state['points'].get(p['point_id'],{}).get('status','pending') for p in plan['points'] if p['stage']==stage]
    stage_status='failed' if 'failed' in statuses else 'completed' if all(s=='success' for s in statuses) else 'budget_exhausted'
    return {'new_cfd_attempts':used,'stage_status':stage_status,'state_path':str(state_path),'points':state['points']}


def local_state(config: dict[str,Any]) -> dict[str,Any]:
    settings=config.get('refinement',{}).get('local_search',{})
    key=digest({'slice':slice_id(config),'settings':settings})
    path=Path(config['paths']['output_dir'])/'local_search_state.json'
    state=json.loads(path.read_text()) if path.exists() else {}
    if state.get('key')!=key:
        state={'key':key,'radius_norm':float(settings.get('initial_radius_norm',.15)),
               'success_streak':0,'failure_streak':0,'processed_run_ids':[]}
    lo=float(settings.get('min_radius_norm',.05));hi=float(settings.get('max_radius_norm',.3))
    if not 0<lo<=state['radius_norm']<=hi:
        raise ValueError('Invalid local-search radius bounds.')
    return state


def local_regions(config: dict[str,Any], frame: pd.DataFrame) -> list[dict[str,Any]]:
    import blade_shape_active_learning as b
    settings=config.get('refinement',{}).get('local_search',{})
    if not settings.get('enabled',False):return []
    _,local=training_partition(config,frame)
    coordinates=local[b.variable_names(config)].to_numpy(float)
    local=local.loc[np.all(coordinates>=b.lower_bounds(config),axis=1)&np.all(coordinates<=b.upper_bounds(config),axis=1)]
    if local.empty:return []
    y=local[OBJECTIVES].to_numpy(float)
    front=local.loc[b.pareto_mask(y)]
    fy=front[OBJECTIVES].to_numpy(float)
    scaled=(fy-fy.min(axis=0))/np.maximum(np.ptp(fy,axis=0),1e-12)
    selected=list(dict.fromkeys([int(np.argmax(fy[:,0])),int(np.argmax(fy[:,1])),int(np.argmin(np.linalg.norm(1-scaled,axis=1)))]))
    radius=local_state(config)['radius_norm']
    lb,ub=b.lower_bounds(config),b.upper_bounds(config)
    regions=[]
    for position in selected:
        row=front.iloc[position];x=row[b.variable_names(config)].to_numpy(float)
        lower=enforce_fixed(config,np.maximum(lb,x-radius*(ub-lb)))
        upper=enforce_fixed(config,np.minimum(ub,x+radius*(ub-lb)))
        regions.append({'center_run_id':row.run_id,'center':x,'lower':lower,'upper':upper,'radius_norm':radius})
    return regions


def local_samples(config: dict[str,Any], baseline: Any, count: int, seed: int,
                  existing: np.ndarray, regions: list[dict[str,Any]]) -> list[np.ndarray]:
    import blade_shape_active_learning as b
    if not regions or count<=0:return []
    rng=np.random.default_rng(seed);out=[]
    for attempt in range(count*500):
        region=regions[attempt%len(regions)]
        x=enforce_fixed(config,region['lower']+rng.random(len(config['variables']))*(region['upper']-region['lower']))
        combined=np.vstack([existing,np.array(out)]) if out and len(existing) else np.array(out) if out else existing
        if not b.constraint_violations(config,baseline,x) and b.is_far_enough(config,x,combined):
            out.append(x)
            if len(out)==count:break
    return out


def candidate_pool(config: dict[str,Any], baseline: Any, frame: pd.DataFrame, seed: int,
                   regions: list[dict[str,Any]]) -> tuple[np.ndarray,list[str]]:
    import blade_shape_active_learning as b
    count=int(config['runtime']['candidate_pool_size'])
    fraction=float(config.get('refinement',{}).get('local_search',{}).get('fraction',.7))
    if not 0<=fraction<=1 or count<1:raise ValueError('Invalid local candidate fraction or pool size.')
    existing=b.existing_vectors(config,frame)
    local=local_samples(config,baseline,int(round(count*fraction)) if regions else 0,seed,existing,regions)
    combined=np.vstack([existing,np.array(local)]) if local and len(existing) else np.array(local) if local else existing
    global_points=b.valid_random_samples(config,baseline,count-len(local),stream_seed(seed,'pool_global'),combined)
    values=np.array(local+global_points).reshape(-1,len(config['variables']))
    return values,['local_pool']*len(local)+['global_pool']*len(global_points)


def update_local_search(config: dict[str,Any], run_id: str, metrics: dict[str,Any], success: bool) -> None:
    settings=config.get('refinement',{}).get('local_search',{})
    if not settings.get('enabled',False):return
    state=local_state(config)
    if run_id in state['processed_run_ids']:return
    state['processed_run_ids'].append(run_id)
    threshold=float(settings.get('min_relative_hv_gain',1e-4))*max(float(metrics.get('hv_before',0)),1e-12)
    improved=success and float(metrics.get('hv_gain',0))>threshold
    if improved:
        state['success_streak']+=1;state['failure_streak']=0
    else:
        state['failure_streak']+=1;state['success_streak']=0
    if state['success_streak']>=int(settings.get('successes_to_expand',3)):
        state['radius_norm']=min(float(settings.get('max_radius_norm',.3)),2*state['radius_norm'])
        state['success_streak']=0
    if state['failure_streak']>=int(settings.get('failures_to_shrink',3)):
        state['radius_norm']=max(float(settings.get('min_radius_norm',.05)),state['radius_norm']/2)
        state['failure_streak']=0
    atomic_json(Path(config['paths']['output_dir'])/'local_search_state.json',state)
