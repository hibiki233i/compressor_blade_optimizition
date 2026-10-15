"""External-tool stand-ins shared by tests and the offline benchmark."""
from __future__ import annotations

import json
from pathlib import Path

from blade_shape_cfx_runner import CfxResult


def fake_geometry(config, candidate_path, dry_run=False):
    (candidate_path.parent / 'Impeller_Mesh.gtm').write_text('synthetic test mesh')
    return True, 'ok'


def synthetic_cfx(case_dir: Path, run_id: str, **kwargs) -> CfxResult:
    """Smooth conflicting objectives, evaluated from the actual candidate file.

    Values are dimensionless synthetic scores on a scale similar to test data;
    no solver, convergence assessment or engineering units are implied.
    """
    candidate = json.loads((case_dir / 'candidate.json').read_text(encoding='utf-8'))
    if candidate['run_id'] != run_id:
        raise ValueError('Synthetic evaluation received a different candidate run_id.')
    x = candidate['variables']
    h0 = x['hub_beta_0_deg_offset']
    h4 = x['hub_beta_4_deg_offset']
    s0 = x['shroud_beta_0_deg_offset']
    s2 = x['shroud_beta_2_deg_offset']
    s3 = x['shroud_beta_3_deg_offset']
    theta = x['hub_theta_deg_offset']
    return CfxResult(
        success=True,
        metrics={
            'Efficiency': .762 + .001*h0 - .0003*h0**2 - .0004*(h4-2)**2
                          - .0002*s2**2 + .0005*theta,
            'MassFlow': 4.25 - .015*h0 + .012*s0 - .003*s3**2 + .006*h4,
            'PressureRatio': 2., 'Power': 1., 'totalpressureratio': 2.1,
        },
        message='SYNTHETIC offline benchmark; not CFD evidence',
    )
