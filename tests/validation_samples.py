"""Synthetic incidence-validation outputs for viewer tests (never engineering data).

Profiles are reduced by the validation CLI's own ``reduce_profile`` from made-up
band integrals, so the files match what ``extract`` would write.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path

import blade_shape_incidence_validation as v
from blade_shape_runtime import atomic_json, digest

SPEC = dict(
    schema_version=1, target_id='synthetic_target', res_path='/synthetic/target.res',
    geometry_source='/synthetic/target.cft', geometry_verified=True,
    hub_beta_deg=34.0, shroud_beta_deg=22.0,
    measurement=dict(bands=20, le_station=.22, turbo_domain='R1', normal_sign=1,
                     theta_reference_sign=-1, max_reverse_fraction=.01),
    conditions=dict(rpm=10000.0, inlet_total_pressure_pa=101325.0, inlet_total_temperature_k=288.15,
                    fluid_id='Air Ideal Gas', n_blades=10, simulated_passages=1),
)


def make_spec(**changes) -> dict:
    spec = copy.deepcopy(SPEC)
    for key, value in changes.items():
        if key in {'bands', 'le_station', 'max_reverse_fraction'}:
            spec['measurement'][key] = value
        else:
            spec[key] = value
    return spec


def band_values(spec: dict, incidence, reverse=lambda s: 0.0, ws=60.0) -> dict[str, float]:
    """Band integrals that reduce to ``incidence(span)`` (degrees)."""
    m = spec['measurement']
    bands = m['bands']
    values: dict[str, float] = {}
    net = 0.0
    for j in range(bands):
        low, high = j / bands, (j + 1) / bands
        s = (low + high) / 2 + 0.1 / bands
        forward = 0.002 * (0.6 + math.sin(math.pi * s))
        back = forward * reverse(s)
        metal = spec['hub_beta_deg'] + s * (spec['shroud_beta_deg'] - spec['hub_beta_deg'])
        flow = math.radians(metal - incidence(s))
        wt = m['theta_reference_sign'] * ws / math.tan(flow)
        values.update({f'b{j:03d}_forward': forward, f'b{j:03d}_reverse': back,
                       f'b{j:03d}_ws_flux': forward * ws, f'b{j:03d}_wt_flux': forward * wt,
                       f'b{j:03d}_span_flux': forward * s})
        net += forward - back
    values['surface_mass_flow'] = net
    return values


def write_extract(directory: Path, spec: dict, incidence, reverse=lambda s: 0.0) -> dict:
    """Write profile.csv / summary.json / state.json like ``extract`` does."""
    directory.mkdir(parents=True, exist_ok=False)
    rows, summary = v.reduce_profile(band_values(spec, incidence, reverse), spec)
    summary.update(spec=spec, input_identities={})
    v.write_csv(directory / 'profile.csv', rows)
    atomic_json(directory / 'summary.json', summary)
    atomic_json(directory / 'state.json', dict(status='complete', quality_ok=summary['quality_ok']))
    return summary


def write_legacy(directory: Path, hub=34.0, shroud=22.0) -> dict:
    points = [dict(span=j / 19, beta_cfx_deg=-(90 - (hub + j / 19 * (shroud - hub)) + 2.0 * math.cos(j / 3)))
              for j in range(20)]
    rows, summary = v.legacy_profile(points, hub, shroud)
    summary.update(hub_beta_deg=hub, shroud_beta_deg=shroud)
    directory.mkdir(parents=True, exist_ok=False)
    v.write_csv(directory / 'profile.csv', rows)
    atomic_json(directory / 'summary.json', summary)
    return summary


def write_sweep(directory: Path, stations=(.20, .22, .24), bands=(20, 40)) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, (station, count) in enumerate((s, b) for s in stations for b in bands):
        spec = make_spec(le_station=station, bands=count)
        summary = write_extract(directory / f'measurement_{index:03d}', spec,
                                lambda s, st=station: 2.5 - 8 * s + 9 * s * s + (st - .22) * 20)
        rows.append(dict(le_station=station, bands=count, quality_ok=summary['quality_ok'],
                         incidence_rms_mass_deg=summary['incidence_rms_mass_deg'],
                         mean_abs_incidence_mass_deg=summary['mean_abs_incidence_mass_deg'],
                         net_mass_flow_kg_s=summary['net_mass_flow_kg_s']))
    v.write_csv(directory / 'sensitivity.csv', rows)
    atomic_json(directory / 'sweep_progress.json', dict(status='complete', completed=rows))
    return rows


def write_compare(path: Path, baseline_dir: Path, target_dir: Path, tolerance=.01) -> dict:
    import json
    a = json.loads((baseline_dir / 'summary.json').read_text(encoding='utf-8'))
    b = json.loads((target_dir / 'summary.json').read_text(encoding='utf-8'))
    result = v.compare_results(a, b, tolerance)
    result['sources'] = {str((baseline_dir / 'summary.json').resolve()): None,
                         str((target_dir / 'summary.json').resolve()): None}
    v.new_json(path, result)
    return result


NAMES = ['hub_beta_0_deg_offset', 'shroud_beta_0_deg_offset', 'hub_theta_deg_offset']


def write_plan(directory: Path, step=.25, pressures=(10.0,), complete=None, failed=()) -> dict:
    """plan.json + progress.json shaped like make_plan/run_plan output."""
    points = []
    for pressure in pressures:
        for role, column, sign in (('center', None, 0), ('hub_minus', 0, -1), ('hub_plus', 0, 1),
                                   ('shroud_minus', 1, -1), ('shroud_plus', 1, 1)):
            x = [0.5, -0.2, 0.0]
            if column is not None:
                x[column] += sign * step
            points.append(dict(index=len(points), role=role, p_out_pa=pressure, x=x))
    plan = dict(schema_version=1, kind='endpoint_incidence_sensitivity',
                config=dict(variables=[dict(name=n, lower=-5, upper=5) for n in NAMES]),
                source_identities={}, points=points, output_dir=str(directory), note='synthetic')
    plan['plan_id'] = digest(plan)
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory / 'plan.json', plan)
    done = range(len(points)) if complete is None else complete
    states = {}
    for point in points:
        if point['index'] in failed:
            states[str(point['index'])] = dict(status='failed', run_id=f"case_{point['index']:06d}", message='solve')
        elif point['index'] in done:
            hub, shroud = point['x'][0] - 0.5, point['x'][1] + 0.2
            states[str(point['index'])] = dict(
                status='complete', run_id=f"case_{point['index']:06d}",
                metrics=dict(Efficiency=0.82 + 0.004 * hub - 0.02 * hub ** 2 - 0.002 * shroud,
                             MassFlow=3.2 + 0.05 * hub + 0.03 * shroud))
    status = 'failed' if failed else 'complete' if len(states) == len(points) else 'budget_exhausted'
    atomic_json(directory / 'progress.json', dict(plan_id=plan['plan_id'], points=states, status=status))
    return plan
