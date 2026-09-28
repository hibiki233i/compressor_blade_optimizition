"""Fail-closed steady CFX RMS acceptance and bounded restart controls."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ConvergencePolicy:
    rms_target: float = 1e-5
    restart_iterations: int = 2000
    flow_analysis: str = 'Flow Analysis 1'

    def __post_init__(self) -> None:
        if not math.isfinite(self.rms_target) or not 0 < self.rms_target <= 1e-5:
            raise ValueError('CFX RMS target must be positive and <= 1e-5')
        if type(self.restart_iterations) is not int or not 1500 <= self.restart_iterations <= 2000:
            raise ValueError('CFX restart_iterations must be an integer in [1500, 2000]')
        if not re.fullmatch(r'[A-Za-z0-9 _.-]+', self.flow_analysis):
            raise ValueError('Invalid CFX flow analysis name')

    @classmethod
    def from_config(cls, config: dict) -> 'ConvergencePolicy':
        options = config.get('cfx_convergence', {})
        return cls(**options)

    def to_dict(self) -> dict:
        return asdict(self)


ITERATION = re.compile(r'OUTER\s+LOOP\s+ITERATION\s*=\s*(\d+)(?:\s*\(\s*(\d+)\s*\))?', re.I)
REQUIRED = {'u-mom', 'v-mom', 'w-mom', 'p-mass', 'h-energy'}


def _residuals(block: str) -> dict[str, float]:
    values = {}; in_table = False
    for line in block.splitlines():
        if 'RMS Res' in line and 'Equation' in line:
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip() or re.fullmatch(r'[\s+\-=|]+', line):
            continue
        if not line.strip().startswith('|'):
            in_table = False
            continue
        columns = [part.strip() for part in line.strip().strip('|').split('|')]
        if len(columns) < 5 or not columns[0]:
            raise ValueError('Incomplete final RMS table row')
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
    if not re.search(r'(?:CFD Solver finished|CFX[- ]Solver finished)', final, re.I):
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
    converged = max(values.values()) <= policy.rms_target
    return dict(iteration=int(last.group(1)), run_iteration=int(last.group(2) or last.group(1)),
                rms=values, max_rms=max(values.values()), rms_target=policy.rms_target,
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
