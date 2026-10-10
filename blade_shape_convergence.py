"""Fail-closed steady CFX RMS acceptance and bounded restart controls."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

#: Loosest accepted final RMS residual (max over equations in the last .out
#: block). 1e-5 is the usual "well converged" level for steady turbomachinery
#: runs; configs may only tighten it.
RMS_TARGET_MAX = 1e-5
#: Inclusive bounds for the single bounded continuation after an initial solve
#: exhausts its iterations without meeting the RMS target. Empirical, from this
#: impeller: earlier non-converged cases that reached the RMS target from the
#: restart did so within about 1500–2000 extra iterations; fewer rarely
#: converged and more rarely helped. Revisit for a different machine or mesh.
RESTART_ITERATIONS_RANGE = (1500, 2000)


@dataclass(frozen=True)
class ConvergencePolicy:
    rms_target: float = RMS_TARGET_MAX
    restart_iterations: int = RESTART_ITERATIONS_RANGE[1]
    flow_analysis: str = 'Flow Analysis 1'

    def __post_init__(self) -> None:
        if not math.isfinite(self.rms_target) or not 0 < self.rms_target <= RMS_TARGET_MAX:
            raise ValueError(f'CFX RMS target must be positive and <= {RMS_TARGET_MAX:g}')
        low, high = RESTART_ITERATIONS_RANGE
        if type(self.restart_iterations) is not int or not low <= self.restart_iterations <= high:
            raise ValueError(f'CFX restart_iterations must be an integer in [{low}, {high}]')
        if not re.fullmatch(r'[A-Za-z0-9 _.-]+', self.flow_analysis):
            raise ValueError('Invalid CFX flow analysis name')

    @classmethod
    def from_config(cls, config: dict) -> 'ConvergencePolicy':
        options = config.get('cfx_convergence', {})
        return cls(**options)

    def to_dict(self) -> dict:
        return asdict(self)


ITERATION = re.compile(r'OUTER\s+LOOP\s+ITERATION\s*=\s*(\d+)(?:\s*\(\s*(\d+)\s*\))?', re.I)
#: Equations that must appear in the final table and meet rms_target. This
#: matches CFX's own stopping test, which leaves turbulence out: a real run
#: reported "All target criteria reached" at W-Mom 9.9E-06 with O-TurbFreq
#: 2.2E-05. Turbulence and any other reported equations are recorded only.
REQUIRED = {'u-mom', 'v-mom', 'w-mom', 'p-mass', 'h-energy'}


def _residuals(block: str) -> dict[str, float]:
    """Read the first equation residual table in ``block``.

    Real CFX output can insert one-column ``****** Notice ******`` boxes between
    rows of the table and follows it with other ``|`` tables (e.g. "Locations
    of Maximum Residuals"). Rows whose column count differs from the header are
    skipped, and the table ends at the first blank line or plain text. A table
    cut short is still rejected by the required-equation check.
    """
    values = {}; width = None
    for line in block.splitlines():
        stripped = line.strip()
        if width is None:
            if 'RMS Res' in line and 'Equation' in line:
                width = len(stripped.strip('|').split('|'))
            continue
        if not stripped or not stripped.startswith(('|', '+')):
            break
        if re.fullmatch(r'[+\-=|]+', stripped):
            continue
        columns = [part.strip() for part in stripped.strip('|').split('|')]
        if len(columns) != width or not columns[0]:
            continue
        equation = re.sub(r'\s+', '', columns[0]).lower()
        try:
            residual = float(columns[2].replace('D', 'E').replace('d', 'e'))
        except ValueError as exc:
            raise ValueError(f'Invalid RMS residual for {equation}: {columns[2]}') from exc
        if not math.isfinite(residual) or residual < 0:
            raise ValueError(f'Non-finite/negative RMS residual for {equation}')
        values[equation] = max(values.get(equation, 0), residual)
    return values


def assess_out(text: str, policy: ConvergencePolicy) -> dict:
    matches = list(ITERATION.finditer(text))
    if not matches:
        raise ValueError('No steady OUTER LOOP ITERATION in CFX .out')
    last = matches[-1]
    final = text[last.end():]
    # A truncated output must not borrow a successful earlier residual table.
    if not re.search(r'(?:CFD Solver finished|CFX[- ]Solver (?:has )?finished)', final, re.I):
        raise ValueError('CFX .out lacks final solver-finished marker')
    values = _residuals(final)
    expected = set(REQUIRED)
    if len(matches) > 1:
        expected.update(_residuals(text[matches[-2].end():last.start()]))
    if not expected <= values.keys():
        raise ValueError(f'Missing final RMS equations: {sorted(expected-values.keys())}')
    flat = re.sub(r'\s+', ' ', final)
    limit_stop = bool(re.search(r'maximum number of (?:time[- ]step |outer loop )?iterations.*?(?:reached|terminat)', flat, re.I))
    limits = re.findall(r'Maximum Number of Iterations\s*=\s*(\d+)', text, re.I)
    declared_limit = int(limits[-1]) if limits else None
    current_iteration = int(last.group(2) or last.group(1))
    exhausted = bool(limit_stop and declared_limit is not None and current_iteration >= declared_limit)
    max_rms = max(values[name] for name in REQUIRED)
    converged = max_rms <= policy.rms_target
    return dict(iteration=int(last.group(1)), run_iteration=int(last.group(2) or last.group(1)),
                rms=values, checked_equations=sorted(REQUIRED), max_rms=max_rms,
                rms_target=policy.rms_target,
                converged=converged, iteration_limit_reached=exhausted,
                declared_iteration_limit=declared_limit)


def convergence_ccl(policy: ConvergencePolicy, *, restart: bool = False) -> str:
    lines = [f'FLOW: {policy.flow_analysis}', '  SOLVER CONTROL:']
    if restart:
        lines += ['    CONVERGENCE CONTROL:', f'      Maximum Number of Iterations = {policy.restart_iterations}',
                  '      Minimum Number of Iterations = 1', '    END']
    lines += ['    CONVERGENCE CRITERIA:', '      Residual Type = RMS',
              f'      Residual Target = {policy.rms_target:.12g}', '    END', '  END', 'END', '']
    return '\n'.join(lines)
