"""The variable box, geometry limits and slice that older fixtures were written for.

On 2026-11-16 the shipped config moved to curve-space bounds (see
docs/doe-bounds-2026-11-16.md). Tests whose numbers come from the earlier box
(an extension just above 4.5, trial steps near that bound, golden selections,
the eight-point probe on the frozen slice) pin it here instead of following
the shipped file.
"""
from __future__ import annotations

from typing import Any

BOUNDS = {
    'hub_beta_0_deg_offset': (-3.0, 4.0), 'hub_beta_1_deg_offset': (-3.5, 2.0),
    'hub_beta_2_deg_offset': (-3.5, 3.0), 'hub_beta_3_deg_offset': (-4.0, 2.5),
    'hub_beta_4_deg_offset': (-2.5, 4.5), 'shroud_beta_0_deg_offset': (-4.0, 3.0),
    'shroud_beta_1_deg_offset': (-3.0, 3.0), 'shroud_beta_2_deg_offset': (-3.0, 3.0),
    'shroud_beta_3_deg_offset': (-3.0, 3.5), 'shroud_beta_4_deg_offset': (-4.5, 2.0),
    'hub_theta_deg_offset': (-1.5, 1.5), 'shroud_theta_deg_offset': (-1.5, 1.5),
}
FROZEN = ('hub_beta_1_deg_offset', 'hub_beta_2_deg_offset', 'shroud_beta_1_deg_offset',
          'shroud_beta_4_deg_offset', 'shroud_theta_deg_offset')


def pin(config: dict[str, Any], *, frozen: bool = False) -> dict[str, Any]:
    """Put the earlier box (and, with ``frozen``, the earlier seven-variable slice) into ``config``."""
    for variable in config['variables']:
        variable['lower'], variable['upper'] = BOUNDS[variable['name']]
    config['constraints'].update(max_beta_offset_deg=6.0, max_theta_offset_deg=1.5)
    config.setdefault('refinement', {})['extension'] = {'variable': 'hub_beta_4_deg_offset', 'value': 5.0}
    if frozen:
        names = [variable['name'] for variable in config['variables']]
        config['search'] = {'active_variables': [name for name in names if name not in FROZEN],
                            'fixed_variables': {name: 0.0 for name in FROZEN},
                            'slice_tolerance_norm': 1e-08}
    return config
