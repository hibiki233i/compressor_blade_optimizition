"""Independent, auditable spanwise incidence validation; never an optimizer label.

Use --help for path-based preparation, CFX-Post extraction, legacy reproduction,
matched-point comparison and budgeted endpoint-angle sensitivity experiments.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from typing import Any

from blade_shape_flow_diagnostics import LOCATION_NAME, RAW_NAME, parse_measurements, render_session
from blade_shape_runtime import atomic_json, digest, file_identity, output_lock

METHOD = 'forward_mass_velocity_triangle_v1'

REQUIRED_SPEC_FIELDS = (
    'hub_beta_deg', 'shroud_beta_deg', 'measurement.normal_sign',
    'measurement.theta_reference_sign', 'conditions.rpm',
    'conditions.inlet_total_pressure_pa', 'conditions.inlet_total_temperature_k',
    'conditions.fluid_id', 'conditions.n_blades',
)


def finite(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{name} must be finite')
    return number


def validate_spec(spec: dict[str, Any]) -> None:
    if spec.get('schema_version') != 1:
        raise ValueError('Unsupported validation schema')
    missing = pending_spec_fields(spec)
    if missing:
        raise ValueError('Validation spec needs confirmation: ' + ', '.join(missing))
    for name in ('target_id', 'res_path', 'geometry_source'):
        if not spec.get(name):
            raise ValueError(f'Missing {name}')
    for name in ('hub_beta_deg', 'shroud_beta_deg'):
        finite(spec[name], name)
    m = spec['measurement']
    if type(m['bands']) is not int or not 2 <= m['bands'] <= 200:
        raise ValueError('bands must be an integer in [2, 200]')
    if not 0 < finite(m['le_station'], 'le_station') < .25:
        raise ValueError('le_station must be upstream of LE: 0 < station < .25')
    if not LOCATION_NAME.fullmatch(m['turbo_domain']):
        raise ValueError('Unsafe turbo domain')
    for key in ('normal_sign', 'theta_reference_sign'):
        if m.get(key) not in (-1, 1):
            raise ValueError(f'Explicit {key} of -1 or +1 is required')
    if not 0 <= finite(m['max_reverse_fraction'], 'max_reverse_fraction') < .5:
        raise ValueError('max_reverse_fraction must lie in [0, .5)')
    c = spec['conditions']
    for key in ('rpm', 'inlet_total_pressure_pa', 'inlet_total_temperature_k'):
        if finite(c[key], key) <= 0:
            raise ValueError(f'{key} must be positive')
    if not c.get('fluid_id') or type(c['n_blades']) is not int or c['n_blades'] < 1:
        raise ValueError('Explicit fluid_id and positive integer n_blades required')
    passages = c.get('simulated_passages', 1)
    if type(passages) is not int or not 1 <= passages <= c['n_blades']:
        raise ValueError('simulated_passages must be an integer in [1, n_blades]')


def pending_spec_fields(spec: dict[str, Any]) -> list[str]:
    """List unanswered questions before numeric validation or CFX-Post startup."""
    pending = []
    if spec.get('geometry_verified') is not True:
        pending.append('geometry_verified (confirm geometry/result and angle convention)')
    for name in REQUIRED_SPEC_FIELDS:
        current: Any = spec
        for part in name.split('.'):
            current = current.get(part) if isinstance(current, dict) else None
        if current is None or current == '':
            pending.append(name)
    return pending


def _candidate_angles(path: Path) -> tuple[float, float]:
    raw = path.read_text(encoding='utf-8-sig')
    if not raw.strip():
        raise ValueError(f'{path} is empty; expected this project\'s candidate.json')
    try:
        candidate = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{path} is not valid JSON (line {exc.lineno}, column {exc.colno})') from exc
    if not isinstance(candidate, dict) or not isinstance(candidate.get('geometry'), dict):
        raise ValueError(f'{path} has no candidate.geometry object')
    geometry = candidate['geometry']
    values = []
    for key in ('hub_beta_rad', 'shroud_beta_rad'):
        angles = geometry[key]
        if not isinstance(angles, list) or len(angles) != 5:
            raise ValueError(f'{path}: expected five {key} values')
        values.append(math.degrees(finite(angles[0], key)))
    return values[0], values[1]


def _batch_angles(path: Path) -> tuple[float, float]:
    blade = ET.parse(path).getroot().find(".//Updates//BladePropsML[@Name='Main blade']")
    if blade is None:
        raise ValueError(f'{path}: Main blade update is missing')
    beta = blade.find('Beta1')
    if beta is None:
        raise ValueError(f'{path}: Beta1 is missing')
    values = {item.attrib.get('Index'): item.text for item in beta.findall('Value')}
    missing = [str(index) for index in (0, 1) if values.get(str(index)) is None]
    if missing:
        raise ValueError(f'{path}: Beta1 lacks hub/shroud Value Index {", ".join(missing)}')
    return tuple(math.degrees(finite(values[str(index)], f'Beta1[{index}]'))
                 for index in (0, 1))


def initial_spec(res: Path, geometry_source: Path, candidate_path: Path | None = None) -> dict[str, Any]:
    """Prefill only values backed by readable case files; never certify a geometry."""
    result = dict(schema_version=1, target_id=res.stem, res_path=path_argument(res),
                  geometry_source=path_argument(geometry_source), geometry_verified=False,
                  hub_beta_deg=None, shroud_beta_deg=None,
                  measurement=dict(bands=20, le_station=.22, turbo_domain='R1',
                                   normal_sign=None, theta_reference_sign=None,
                                   max_reverse_fraction=.01),
                  conditions=dict(rpm=None, inlet_total_pressure_pa=None,
                                  inlet_total_temperature_k=None, fluid_id=None,
                                  n_blades=None, simulated_passages=1),
                  prefill_sources={}, prefill_inputs={}, prefill_warnings=[])
    if not res.is_file():
        result['prefill_warnings'].append(f'Result file is not accessible during preparation: {res}')
    if not geometry_source.is_file():
        result['prefill_warnings'].append(f'Geometry source is not accessible during preparation: {geometry_source}')
    elif geometry_source.suffix.lower() == '.cft':
        result['prefill_warnings'].append('CFturbo .cft angle parsing is not supported; use a matching candidate.json for provisional angles')
    angle_source = None
    if geometry_source.suffix.lower() == '.cft-batch' and geometry_source.is_file():
        angles = _batch_angles(geometry_source)
        angle_source = geometry_source
    else:
        foreign_res = PureWindowsPath(str(res)).is_absolute() and not res.is_absolute()
        candidate = candidate_path or (geometry_source if geometry_source.suffix.lower() == '.json'
                                       else None if foreign_res else res.parent / 'candidate.json')
        if candidate is not None and candidate.is_file():
            try:
                angles = _candidate_angles(candidate)
                angle_source = candidate
            except (KeyError, TypeError, ValueError, OSError) as exc:
                if candidate == geometry_source:
                    raise ValueError(f'Cannot prefill candidate angles: {exc}') from exc
                result['prefill_warnings'].append(f'Candidate angles not prefilled: {exc}')
        elif candidate_path is not None:
            result['prefill_warnings'].append(f'Candidate angles not prefilled; file is missing: {candidate}')
        elif candidate == geometry_source:
            result['prefill_warnings'].append(f'Candidate geometry is not readable here: {candidate}')
    if angle_source is not None:
        result['hub_beta_deg'], result['shroud_beta_deg'] = angles
        for field in ('hub_beta_deg', 'shroud_beta_deg'):
            result['prefill_sources'][field] = f'{path_argument(angle_source)}: leading-edge beta (radians to degrees)'
        result['prefill_inputs'][path_argument(angle_source)] = file_identity(angle_source)

    receipt = res.parent / 'cfx_state.json'
    if res.is_file() and receipt.is_file():
        try:
            state = json.loads(receipt.read_text(encoding='utf-8'))
            solve = state['stages']['solve']
            if (state.get('version') == 2 and solve.get('status') == 'complete'
                    and solve.get('exit_code') == 0
                    and solve.get('convergence', {}).get('converged') is True
                    and solve.get('result_file') == res.name
                    and solve.get('result_identity') == file_identity(res)):
                n_blades = state['inputs']['n_blades']
                if type(n_blades) is int and n_blades > 0:
                    result['conditions']['n_blades'] = n_blades
                    result['prefill_sources']['conditions.n_blades'] = f'{path_argument(receipt)}: recorded input'
                    result['prefill_inputs'][path_argument(receipt)] = file_identity(receipt)
            else:
                result['prefill_warnings'].append('cfx_state.json does not prove a completed solve for this .res; blade count was not prefilled')
        except (KeyError, TypeError, ValueError, OSError) as exc:
            result['prefill_warnings'].append(f'Could not use cfx_state.json: {exc}')
    return result


def save_initial_spec(path: Path, proposed: dict[str, Any]) -> Path | None:
    """Enrich a matching draft without replacing manual answers or prior evidence."""
    if not path.exists():
        new_json(path, proposed)
        return None
    existing = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(existing, dict) or existing.get('schema_version') != 1:
        raise ValueError(f'Existing output is not a validation spec: {path}')
    for key in ('res_path', 'geometry_source'):
        old, new = existing.get(key), proposed[key]
        if not old or Path(old).resolve() != Path(new).resolve():
            raise ValueError(f'Existing output has a different {key}; choose a new output file: {path}')
    merged = copy.deepcopy(existing)
    merged.setdefault('conditions', {})
    merged.setdefault('prefill_sources', {})
    merged.setdefault('prefill_inputs', {})
    filled = []
    for field in ('hub_beta_deg', 'shroud_beta_deg', 'conditions.n_blades'):
        parts = field.split('.')
        holder = merged if len(parts) == 1 else merged['conditions']
        value = proposed if len(parts) == 1 else proposed['conditions']
        key = parts[-1]
        if holder.get(key) is None and value.get(key) is not None:
            holder[key] = value[key]
            filled.append(field)
            source = proposed['prefill_sources'][field]
            merged['prefill_sources'][field] = source
            for name, identity in proposed['prefill_inputs'].items():
                if source.startswith(name + ':'):
                    merged['prefill_inputs'][name] = identity
    merged['prefill_warnings'] = proposed['prefill_warnings']
    if filled and merged.get('geometry_verified') is True:
        merged['geometry_verified'] = False
        merged['prefill_warnings'].append('Newly prefilled values require renewed geometry confirmation')
    backup = path.with_name(f'{path.name}.bak-{time.time_ns()}')
    shutil.copy2(path, backup)
    atomic_json(path, merged)
    return backup


def verify_prefill_inputs(spec: dict[str, Any]) -> None:
    for name, expected in spec.get('prefill_inputs', {}).items():
        if file_identity(name) != expected:
            raise ValueError(f'Prefill source changed or is unavailable: {name}')


def profile_expressions(spec: dict[str, Any]) -> dict[str, str]:
    """Forward flux weighting; keep negative-flow magnitude separately.

Normal components and velocities are local to the rotating domain; normal_sign
must orient the measurement surface downstream. The angular plane is the Turbo
streamwise/circumferential plane, not the magnitude of meridional velocity.
"""
    validate_spec(spec)
    m = spec['measurement']
    vn = (f"({m['normal_sign']} * (Velocity u * Normal X + "
          'Velocity v * Normal Y + Velocity w * Normal Z))')
    forward = f'Density * max({vn}, 0 [m s^-1])'
    reverse = f'Density * max(-{vn}, 0 [m s^-1])'
    expressions = {'surface_mass_flow': 'massFlow()\\@Near LE / 1 [kg s^-1]'}
    for j in range(m['bands']):
        low, high = j/m['bands'], (j+1)/m['bands']
        operator = '<=' if j == m['bands']-1 else '<'
        condition = f'Span Normalized >= {low:.10f} && Span Normalized {operator} {high:.10f}'
        for key, quantity, unit in (
            ('forward', forward, 'kg s^-1'),
            ('reverse', reverse, 'kg s^-1'),
            ('ws_flux', f'({forward}) * Velocity Streamwise', 'kg m s^-2'),
            ('wt_flux', f'({forward}) * Velocity Circumferential', 'kg m s^-2'),
            ('span_flux', f'({forward}) * Span Normalized', 'kg s^-1'),
        ):
            expressions[f'b{j:03d}_{key}'] = (
                f'areaInt(if({condition}, {quantity}, 0 [{unit}]))\\@Near LE / 1 [{unit}]'
            )
    return expressions


def reduce_profile(values: dict[str, float], spec: dict[str, Any]) -> tuple[list[dict], dict]:
    validate_spec(spec)
    required = set(profile_expressions(spec))
    if set(values) != required:
        raise ValueError('Profile fields do not match the configured bands')
    values = {k: finite(v, k) for k, v in values.items()}
    m = spec['measurement']; rows = []; issues = []
    for j in range(m['bands']):
        f, r, ws, wt, sf = [values[f'b{j:03d}_{k}'] for k in
                             ('forward', 'reverse', 'ws_flux', 'wt_flux', 'span_flux')]
        if f < 0 or r < 0:
            raise ValueError('Forward and reverse flux magnitudes must be nonnegative')
        low, high = j/m['bands'], (j+1)/m['bands']
        row = dict(span_low=low, span_high=high, forward_kg_s=f, reverse_kg_s=r,
                   span_mass=None, ws_m_s=None, wt_m_s=None, flow_angle_deg=None,
                   metal_angle_deg=None, incidence_deg=None)
        if f <= 0:
            issues.append(f'band {j}: no forward mass flow')
        else:
            s = sf/f
            if not low-1e-7 <= s <= high+1e-7:
                raise ValueError(f'band {j}: weighted span outside band')
            ws /= f; wt /= f
            if ws <= 0 or math.hypot(ws, wt) <= 1e-12:
                issues.append(f'band {j}: non-forward or undefined velocity triangle')
            else:
                flow = math.degrees(math.atan2(ws, m['theta_reference_sign']*wt))
                metal = spec['hub_beta_deg'] + s*(spec['shroud_beta_deg']-spec['hub_beta_deg'])
                row.update(span_mass=s, ws_m_s=ws, wt_m_s=wt, flow_angle_deg=flow,
                           metal_angle_deg=metal, incidence_deg=(metal-flow+180)%360-180)
        if f+r > 0 and r/(f+r) > m['max_reverse_fraction']:
            issues.append(f'band {j}: reverse flux fraction exceeds threshold')
        rows.append(row)
    forward = sum(row['forward_kg_s'] for row in rows)
    reverse = sum(row['reverse_kg_s'] for row in rows)
    surface = abs(values['surface_mass_flow'])
    closure = abs(abs(forward-reverse)-surface)/max(surface, abs(forward-reverse)) if max(surface,abs(forward-reverse)) else None
    if closure is None or closure > .01:
        issues.append('band-integrated and CFX surface mass flow differ by over 1%')
    good = not issues and forward > 0
    summary = dict(method=METHOD, quality_ok=good, quality_issues=issues,
                   forward_mass_flow_kg_s=forward, reverse_mass_flow_kg_s=reverse,
                   mass_flow_closure_relative=closure,
                   net_mass_flow_kg_s=(forward-reverse)*spec['conditions']['n_blades']/spec['conditions'].get('simulated_passages',1),
                   flow_scope='net_mass_flow is whole wheel; profile fluxes are simulated passage',
                   incidence_rms_mass_deg=None, mean_abs_incidence_mass_deg=None,
                   max_abs_incidence_deg=None, min_loss_angle_calibrated=False,
                   numerical_acceptance_verified=False)
    if good:
        summary.update(
            incidence_rms_mass_deg=math.sqrt(sum(row['forward_kg_s']*row['incidence_deg']**2 for row in rows)/forward),
            mean_abs_incidence_mass_deg=sum(row['forward_kg_s']*abs(row['incidence_deg']) for row in rows)/forward,
            max_abs_incidence_deg=max(abs(row['incidence_deg']) for row in rows))
    return rows, summary


def legacy_profile(rows: list[dict], hub: float, shroud: float) -> tuple[list[dict], dict]:
    """Reproduce the report's 20-point arithmetic metric, not a new mass metric."""
    hub = finite(hub, 'hub'); shroud = finite(shroud, 'shroud')
    if len(rows) != 20:
        raise ValueError('Legacy reproduction requires exactly 20 equally spaced points')
    result = []
    for j, row in enumerate(rows):
        s = finite(row['span'], 'span'); beta = finite(row['beta_cfx_deg'], 'beta_cfx_deg')
        if abs(s-j/19) > 1e-6 or not -90 <= beta <= 0:
            raise ValueError('Expected ordered span j/19 and confirmed ACA quadrant [-90, 0]')
        metal = hub+s*(shroud-hub); flow = 90-abs(beta)
        result.append(dict(span=s, beta_cfx_deg=beta, metal_angle_deg=metal,
                           flow_angle_deg=flow, incidence_deg=metal-flow))
    return result, dict(method='legacy_aca_20_arithmetic_v1',
                        mean_abs_incidence_deg=sum(abs(r['incidence_deg']) for r in result)/20,
                        min_loss_angle_calibrated=False, numerical_acceptance_verified=False)


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open('x', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def load_spec(path: Path) -> dict:
    spec = json.loads(path.read_text(encoding='utf-8'))
    for key in ('res_path', 'geometry_source'):
        if spec.get(key):
            if PureWindowsPath(spec[key]).is_absolute() and not Path(spec[key]).is_absolute():
                continue  # Preserve Windows paths when preparing a spec on macOS.
            p = Path(spec[key]).expanduser()
            spec[key] = str((path.resolve().parent/p).resolve() if not p.is_absolute() else p.resolve())
    return spec


def path_argument(path: Path) -> str:
    return str(path) if PureWindowsPath(str(path)).is_absolute() and not path.is_absolute() else str(path.resolve())


def measurement_sweep(spec: dict, post_exe: Path, output: Path,
                      stations: list[float], bands: list[int]) -> list[dict]:
    """Measure one frozen result with multiple definitions, without new CFD."""
    variants=[]
    if not stations or not bands or len(set(stations))!=len(stations) or len(set(bands))!=len(bands):
        raise ValueError('Specify nonempty distinct stations and band counts')
    for station in stations:
        for count in bands:
            current=copy.deepcopy(spec)
            current['measurement'].update(le_station=station,bands=count)
            validate_spec(current);variants.append(current)
    res=Path(spec['res_path']);geometry=Path(spec['geometry_source'])
    if not res.is_file() or not geometry.is_file():
        raise ValueError('Sweep requires existing result and geometry source')
    if res.parent.resolve()==output.resolve() or res.parent.resolve() in output.resolve().parents:
        raise ValueError('Use an independent analysis directory outside the source case')
    identities={str(p.resolve()):file_identity(p) for p in (res,geometry)}
    output.mkdir(parents=True,exist_ok=False)
    atomic_json(output/'sweep_inputs.json',dict(spec=spec,stations=stations,bands=bands,identities=identities))
    rows=[]
    for index,current in enumerate(variants):
        try:
            if identities!={str(p.resolve()):file_identity(p) for p in (res,geometry)}:
                raise ValueError('Sweep inputs changed between extractions')
            summary=extract(current,post_exe,output/f'measurement_{index:03d}')
            rows.append(dict(le_station=current['measurement']['le_station'],bands=current['measurement']['bands'],
                             quality_ok=summary['quality_ok'],incidence_rms_mass_deg=summary['incidence_rms_mass_deg'],
                             mean_abs_incidence_mass_deg=summary['mean_abs_incidence_mass_deg'],
                             net_mass_flow_kg_s=summary['net_mass_flow_kg_s']))
            atomic_json(output/'sweep_progress.json',dict(status='running',completed=rows))
        except Exception as exc:
            atomic_json(output/'sweep_progress.json',dict(status='failed',completed=rows,message=str(exc)))
            raise
    write_csv(output/'sensitivity.csv',rows)
    atomic_json(output/'sweep_progress.json',dict(status='complete',completed=rows,
                                                note='No automatic station/band independence claim'))
    return rows


def extract(spec: dict, post_exe: Path, output: Path) -> dict:
    validate_spec(spec)
    verify_prefill_inputs(spec)
    post_exe = post_exe.resolve(); res = Path(spec['res_path']).resolve()
    geometry = Path(spec['geometry_source']).resolve()
    if res.suffix.lower() != '.res' or not all(p.is_file() for p in (post_exe,res,geometry)):
        raise ValueError('Existing .res, geometry source and CFX-Post executable are required')
    output = output.resolve()
    if output == res.parent or res.parent in output.parents:
        raise ValueError('Use an independent analysis directory outside the source case')
    output.mkdir(parents=True, exist_ok=False)
    identities = {str(p): file_identity(p) for p in (res, geometry)}
    atomic_json(output/'inputs.json', dict(spec=spec, identities=identities,
                                         identity_basis='user-confirmed geometry/result correspondence'))
    expressions = profile_expressions(spec); m=spec['measurement']
    session = output/'extract.cse'
    session.write_text(render_session(expressions, le_station=m['le_station'],
                                     turbo_domain=m['turbo_domain']), encoding='utf-8')
    command=[str(post_exe),'-batch',str(session),'-res',str(res)]
    atomic_json(output/'command.json',dict(argv=command,cwd=str(output)))
    atomic_json(output/'state.json',dict(status='running'))
    try:
        process=subprocess.run(command,cwd=output,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                               text=True,encoding='utf-8',errors='replace',check=False)
        (output/'cfxpost.log').write_text(process.stdout,encoding='utf-8')
        atomic_json(output/'returncode.json',{'returncode':process.returncode})
        error=output/'cfdpost_error.log'
        if process.returncode or (error.exists() and error.read_text(errors='replace').strip()):
            raise RuntimeError('CFX-Post failed; inspect retained logs')
        values=parse_measurements((output/RAW_NAME).read_text(encoding='utf-8'),set(expressions))
        if identities != {str(p):file_identity(p) for p in (res,geometry)}:
            raise ValueError('Input files changed during extraction')
        rows,summary=reduce_profile(values,spec)
        summary.update(spec=spec, input_identities=identities)
        write_csv(output/'profile.csv',rows); atomic_json(output/'summary.json',summary)
        atomic_json(output/'state.json',dict(status='complete',quality_ok=summary['quality_ok']))
        return summary
    except Exception as exc:
        atomic_json(output/'state.json',dict(status='failed',failure_stage='post',message=str(exc)))
        raise


def compare_results(a: dict, b: dict, tolerance: float) -> dict:
    tolerance=finite(tolerance,'flow tolerance')
    if not 0 <= tolerance < 1:
        raise ValueError('flow tolerance must be in [0,1)')
    if a.get('method') != METHOD or b.get('method') != METHOD:
        raise ValueError('Only identical velocity-triangle metrics can be compared')
    if a['spec']['measurement'] != b['spec']['measurement']:
        raise ValueError('Measurement definitions differ')
    c1,c2=a['spec']['conditions'],b['spec']['conditions']
    same_conditions=all(c1.get(k)==c2.get(k) for k in
                        ('rpm','inlet_total_pressure_pa','inlet_total_temperature_k','fluid_id','n_blades'))
    ma,mb=finite(a['net_mass_flow_kg_s'],'mass flow'),finite(b['net_mass_flow_kg_s'],'mass flow')
    relative=abs(ma-mb)/max(abs(ma),abs(mb)) if ma and mb else None
    matched=bool(same_conditions and ma>0 and mb>0 and relative is not None and relative<=tolerance)
    good=a['quality_ok'] and b['quality_ok']
    return dict(matched_operating_point=matched, declared_conditions_match=same_conditions,
                mass_flow_difference_relative=relative, flow_tolerance_relative=tolerance,
                quality_ok=bool(good), delta_rms_deg=(b['incidence_rms_mass_deg']-a['incidence_rms_mass_deg']) if good else None,
                usable_for_matched_point_diagnostic=bool(matched and good),
                numerical_acceptance_verified=False,
                note='Conditions are user-declared; matching is not CFD convergence or a causal loss attribution.')


def new_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,indent=2,allow_nan=False)


def make_plan(config_path: Path, candidate_path: Path | None, output: Path, step: float,
              pressures: list[float] | None) -> dict:
    """Freeze existing 12D endpoint perturbations; no invented spanwise geometry."""
    import numpy as np
    import blade_shape_active_learning as al
    config=al.load_config(config_path)
    # Freeze the existing CLI's cwd-relative path interpretation at plan creation.
    for key,value in config['paths'].items():
        config['paths'][key]=str(Path(value).expanduser().resolve())
    baseline=al.extract_baseline(config)
    if candidate_path is not None:
        candidate=json.loads(candidate_path.read_text(encoding='utf-8'))
        x=al.sample_to_vector(config,candidate['variables'])
        if candidate['geometry'] != al.candidate_geometry(config,baseline,x):
            raise ValueError('Target candidate geometry does not match this config/template')
    else:
        x=np.zeros(len(config['variables']))
    # An isolated sensitivity study fixes all other coordinates at this target,
    # which may differ from the optimizer's current conditional slice.
    config=copy.deepcopy(config)
    names=al.variable_names(config)
    endpoints={'hub_beta_0_deg_offset','shroud_beta_0_deg_offset'}
    config['search']['active_variables']=[n for n in names if n in endpoints]
    config['search']['fixed_variables']={n:float(x[j]) for j,n in enumerate(names) if n not in endpoints}
    step=finite(step,'step')
    if step<=0: raise ValueError('step must be positive')
    pressures=pressures or [config['runtime']['p_out_pa']]
    pressures=[finite(p,'pressure') for p in pressures]
    if len(set(pressures))!=len(pressures): raise ValueError('Duplicate pressure stations')
    output=output.resolve(); original=al.output_dir(config).resolve()
    if output==original or original in output.parents or output in original.parents:
        raise ValueError('Sensitivity output must be separate from the optimization output')
    names=al.variable_names(config)
    points=[]
    for p in pressures:
        for role,variable,sign in [('center',None,0),('hub_minus','hub_beta_0_deg_offset',-1),
                                  ('hub_plus','hub_beta_0_deg_offset',1),
                                  ('shroud_minus','shroud_beta_0_deg_offset',-1),
                                  ('shroud_plus','shroud_beta_0_deg_offset',1)]:
            point=x.copy()
            if variable: point[names.index(variable)]+=sign*step
            reasons=al.constraint_violations(config,baseline,point)
            for j,definition in enumerate(config['variables']):
                if not definition['lower']<=point[j]<=definition['upper']:
                    reasons.append(f"out_of_bounds:{definition['name']}")
            if reasons: raise ValueError(f'{role}: {reasons}; reduce step or review config bounds')
            points.append(dict(index=len(points),role=role,p_out_pa=p,x=point.tolist()))
    sources=[config_path]+([candidate_path] if candidate_path is not None else [])
    identities={str(p.resolve()):file_identity(p) for p in sources}
    for key in ('cft_batch_template','base_cft','turbogrid_template','template_cfx','template_cse','geometry_script_path'):
        path=Path(config['paths'][key]).resolve()
        identity=file_identity(path)
        if identity is None: raise ValueError(f'Missing physical input {key}: {path}')
        identities[str(path)]=identity
    config=copy.deepcopy(config);config['paths']['output_dir']=str(output/'cfd')
    plan=dict(schema_version=1,kind='endpoint_incidence_sensitivity',config=config,
              source_identities=identities,points=points,output_dir=str(output),
              note='Pressure sweep is not equal-flow validation; extract achieved flow and compare separately.')
    plan['plan_id']=digest(plan)
    output.mkdir(parents=True,exist_ok=False)
    atomic_json(output/'plan.json',plan)
    return plan


def run_plan(path: Path, budget: int, resume: bool) -> dict:
    import numpy as np
    import blade_shape_active_learning as al
    if budget<0: raise ValueError('budget cannot be negative')
    plan=json.loads(path.read_text(encoding='utf-8')); signed=copy.deepcopy(plan)
    signature=signed.pop('plan_id')
    if digest(signed)!=signature or plan.get('kind')!='endpoint_incidence_sensitivity':
        raise ValueError('Plan signature/type mismatch')
    root=Path(plan['output_dir'])
    for name,identity in plan['source_identities'].items():
        if identity != file_identity(name): raise ValueError(f'Frozen input changed: {name}')
    config=plan['config'];baseline=al.extract_baseline(config)
    with output_lock(root), output_lock(al.output_dir(config)):
        state_path=root/'progress.json'
        if state_path.exists():
            if not resume: raise ValueError('Existing experiment requires --resume')
            state=json.loads(state_path.read_text(encoding='utf-8'))
            if state['plan_id']!=signature: raise ValueError('Progress belongs to another plan')
        else:
            if list(al.output_dir(config).glob('training_data.csv')) or (al.output_dir(config)/'cases').exists():
                raise ValueError('Untracked CFD artifacts in experiment output')
            state=dict(plan_id=signature,points={})
        used=0
        for point in plan['points']:
            key=str(point['index']); previous=state['points'].get(key,{})
            if previous.get('status')=='complete': continue
            if previous:
                raise ValueError(f'Case {key} failed/interrupted; inspect it. Automatic retry is disabled.')
            if used>=budget: break
            state['points'][key]=dict(status='running',run_id=f"case_{point['index']:06d}")
            atomic_json(state_path,state)
            current=copy.deepcopy(config);current['runtime']['p_out_pa']=point['p_out_pa']
            try:
                result=al.evaluate_true_cfd(current,baseline,np.array(point['x']),point['index'],
                                            sample_phase='incidence_validation',experiment_id=signature,
                                            design_role=point['role'])
                state['points'][key].update(status='complete' if result.success else 'failed',
                                             metrics=result.row)
                # Existing rows use NaN for unavailable CSV values; JSON must use null.
                state['points'][key]['metrics']={k:(None if isinstance(v,float) and not math.isfinite(v) else v)
                                                  for k,v in result.row.items()}
                atomic_json(state_path,state);used+=1
                if not result.success: break
            except Exception as exc:
                state['points'][key].update(status='failed',message=str(exc))
                atomic_json(state_path,state);raise
        state['new_attempts_this_call']=used
        state['status']=('failed' if any(p['status']=='failed' for p in state['points'].values()) else
                         'complete' if len(state['points'])==len(plan['points']) else 'budget_exhausted')
        atomic_json(state_path,state)
        return state


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init',help='Prefill a draft spec from supported case files; manual review remains required')
    init.add_argument('--res',type=Path,required=True);init.add_argument('--geometry-source',type=Path,required=True)
    init.add_argument('--candidate',type=Path,help='Optional candidate.json for provisional leading-edge angles')
    init.add_argument('--output',type=Path,required=True)
    ext=commands.add_parser('extract',help='CFX-Post only; does not run a new CFD solution')
    ext.add_argument('--spec',type=Path,required=True);ext.add_argument('--post-exe',type=Path,required=True)
    ext.add_argument('--output-dir',type=Path,required=True)
    sweep=commands.add_parser('sweep',help='Extract station/band sensitivity from the same frozen .res')
    sweep.add_argument('--spec',type=Path,required=True);sweep.add_argument('--post-exe',type=Path,required=True)
    sweep.add_argument('--output-dir',type=Path,required=True)
    sweep.add_argument('--stations',type=float,nargs='+',required=True)
    sweep.add_argument('--bands',type=int,nargs='+',required=True)
    legacy=commands.add_parser('legacy',help='Reproduce confirmed-quadrant ACA 20-point report metric')
    legacy.add_argument('--csv',type=Path,required=True);legacy.add_argument('--hub-beta-deg',type=float,required=True)
    legacy.add_argument('--shroud-beta-deg',type=float,required=True);legacy.add_argument('--output-dir',type=Path,required=True)
    compare=commands.add_parser('compare')
    compare.add_argument('--baseline',type=Path,required=True);compare.add_argument('--target',type=Path,required=True)
    compare.add_argument('--flow-tolerance',type=float,default=.01);compare.add_argument('--output',type=Path,required=True)
    plan=commands.add_parser('plan',help='Freeze endpoint perturbations and optional back-pressure sweep')
    plan.add_argument('--config',type=Path,required=True)
    plan.add_argument('--candidate',type=Path,help='Existing matching candidate; omit to use the target batch template itself')
    plan.add_argument('--output-dir',type=Path,required=True);plan.add_argument('--step-deg',type=float,required=True)
    plan.add_argument('--pressures-pa',type=float,nargs='+')
    run=commands.add_parser('run',help='Run budgeted real CFD from a frozen sensitivity plan')
    run.add_argument('--plan',type=Path,required=True);run.add_argument('--max-new-cfd',type=int,required=True)
    run.add_argument('--resume',action='store_true')
    args=parser.parse_args(argv)
    if args.command=='init':
        spec = initial_spec(args.res, args.geometry_source, args.candidate)
        backup = save_initial_spec(args.output, spec)
        saved = json.loads(args.output.read_text(encoding='utf-8'))
        print(('Updated' if backup else 'Created') + ' draft validation spec: ' + str(args.output))
        if backup:
            print('Previous spec backup: ' + str(backup))
        if saved['prefill_sources']:
            print('Prefilled: ' + ', '.join(saved['prefill_sources']))
        for warning in saved['prefill_warnings']:
            print('Prefill warning: ' + warning)
        print('Still requires review: ' + ', '.join(pending_spec_fields(saved)))
    elif args.command=='extract':
        summary=extract(load_spec(args.spec),args.post_exe,args.output_dir)
        print(json.dumps(summary,indent=2));return 0 if summary['quality_ok'] else 2
    elif args.command=='sweep':
        rows=measurement_sweep(load_spec(args.spec),args.post_exe,args.output_dir,args.stations,args.bands)
        return 0 if all(row['quality_ok'] for row in rows) else 2
    elif args.command=='legacy':
        with args.csv.open(encoding='utf-8-sig',newline='') as stream: rows=list(csv.DictReader(stream))
        rows,summary=legacy_profile(rows,args.hub_beta_deg,args.shroud_beta_deg)
        summary.update(source_path=str(args.csv.resolve()),source_identity=file_identity(args.csv),
                       hub_beta_deg=args.hub_beta_deg,shroud_beta_deg=args.shroud_beta_deg)
        args.output_dir.mkdir(parents=True,exist_ok=False)
        write_csv(args.output_dir/'profile.csv',rows);atomic_json(args.output_dir/'summary.json',summary)
    elif args.command=='compare':
        a=json.loads(args.baseline.read_text(encoding='utf-8'));b=json.loads(args.target.read_text(encoding='utf-8'))
        result=compare_results(a,b,args.flow_tolerance)
        result['sources']={str(p.resolve()):file_identity(p) for p in (args.baseline,args.target)}
        new_json(args.output,result);return 0 if result['usable_for_matched_point_diagnostic'] else 2
    elif args.command=='plan':
        plan=make_plan(args.config,args.candidate,args.output_dir,args.step_deg,args.pressures_pa)
        print(f"Frozen {len(plan['points'])} cases in {args.output_dir/'plan.json'}")
    else:
        result=run_plan(args.plan,args.max_new_cfd,args.resume)
        print(json.dumps(result,indent=2));return 1 if result['status']=='failed' else 0
    return 0


if __name__=='__main__':
    raise SystemExit(main())
