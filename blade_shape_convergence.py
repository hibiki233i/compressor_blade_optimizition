"""Fail-closed steady CFX RMS acceptance and bounded restart controls."""
from __future__ import annotations

import math
import re
import statistics
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
#: Loosest residual a reviewer may be asked to accept: 1e-4 is the usual
#: "loosely converged" level for steady turbomachinery. Configs may tighten it.
REVIEW_RMS_MAX = 1e-4


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


@dataclass(frozen=True)
class ReviewPolicy:
    """Which unconverged results are offered for manual review instead of discarded.

    A result qualifies when at most ``max_equations_above_target`` equations of
    the final residual table miss the RMS target and each of them stays within
    ``rms_limit``. It never becomes a label by itself: a person accepts or
    rejects it. Not a physical input (it does not change any computed value).
    """
    rms_limit: float = REVIEW_RMS_MAX
    max_equations_above_target: int = 3
    mode: str = 'legacy_count'
    history_window: int = 200

    def __post_init__(self) -> None:
        if not math.isfinite(self.rms_limit) or not 0 < self.rms_limit <= REVIEW_RMS_MAX:
            raise ValueError(f'cfx_review.rms_limit must be positive and <= {REVIEW_RMS_MAX:g}')
        if self.mode not in {'legacy_count', 'residual_limit'}:
            raise ValueError('cfx_review.mode must be legacy_count or residual_limit')
        if type(self.history_window) is not int or not 2 <= self.history_window <= 10000:
            raise ValueError('cfx_review.history_window must be an integer in [2, 10000]')
        if type(self.max_equations_above_target) is not int or self.max_equations_above_target < 0:
            raise ValueError('cfx_review.max_equations_above_target must be a non-negative integer (0 disables review)')

    @classmethod
    def from_config(cls, config: dict) -> 'ReviewPolicy':
        return cls(**config.get('cfx_review', {}))

    def to_dict(self) -> dict:
        return asdict(self)


def review_evidence(report: dict, convergence: ConvergencePolicy, review: ReviewPolicy) -> dict | None:
    """Why an unconverged final table may be accepted by a person, or None.

    Every equation in the table counts, turbulence included: "most equations
    meet the target" is judged on what the reviewer sees.
    """
    if report['converged']:
        return None
    above = {name: value for name, value in report['rms'].items() if value > convergence.rms_target}
    if not above or review.max_equations_above_target == 0:
        return None
    if review.mode == 'legacy_count' and len(above) > review.max_equations_above_target:
        return None
    if any(value > review.rms_limit for value in above.values()):
        return None
    warnings = ['Performance monitor history has not been verified; final metrics alone do not prove stability.']
    if report.get('history', {}).get('max_rms', {}).get('max', 0) > review.rms_limit:
        warnings.append('Residuals exceeded the review limit within the final history window.')
    return {'equations_above_target': above, 'rms_target': convergence.rms_target,
            'policy_version': 2, 'warnings': warnings, **review.to_dict()}


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


WALL_NOTICE = re.compile(
    r'A wall has been placed at portion\(s\) of an (INLET|OUTLET) boundary condition '
    r'\(at\s*([\d.]+)% of the faces,\s*([\d.]+)% of the area\).*?'
    r'The boundary condition name is:\s*(.+?)\.\s', re.I)


def wall_notices(block: str) -> list[dict]:
    """Inlet/outlet faces CFX walled off (backflow) in an iteration block, for review."""
    flat = ' '.join(line.strip().strip('|').strip() for line in block.splitlines()) + ' '
    flat = re.sub(r'\s+', ' ', flat)
    notices = {}
    for side, faces, area, name in WALL_NOTICE.findall(flat):
        notices[(side.lower(), name.strip())] = {'side': side.lower(), 'boundary': name.strip(),
                                                  'faces_percent': float(faces), 'area_percent': float(area)}
    return list(notices.values())


def assess_out(text: str, policy: ConvergencePolicy, *, history_window: int = 200) -> dict:
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
    # CFX 2025 R1 reports the reason before the iteration-limit description.
    # Match that specific summary, rather than treating any run-duration stop
    # (e.g. a wall-clock limit) as permission to continue the solver.
    limit_stop = limit_stop or bool(re.search(
        r'CFD Solver:\s*Run duration reached\s*\(\s*Maximum number of outer iterations\s*\)', flat, re.I))
    limits = re.findall(r'Maximum Number of Iterations\s*=\s*(\d+)', text, re.I)
    declared_limit = int(limits[-1]) if limits else None
    current_iteration = int(last.group(2) or last.group(1))
    exhausted = bool(limit_stop and declared_limit is not None and current_iteration >= declared_limit)
    max_rms = max(values[name] for name in REQUIRED)
    converged = max_rms <= policy.rms_target
    history = []
    for index in range(max(0, len(matches)-history_window), len(matches)):
        end = matches[index+1].start() if index+1 < len(matches) else len(text)
        table = _residuals(text[matches[index].end():end])
        if REQUIRED <= table.keys():
            history.append(table)
    def stats(numbers: list[float]) -> dict:
        return {'min': min(numbers), 'median': statistics.median(numbers), 'max': max(numbers)}
    # Review eligibility looks at every reported equation (turbulence, wall scale
    # included), so the tail summary must too; strict convergence stays on REQUIRED.
    maxima = [max(table.values()) for table in history]
    reported = sorted(set().union(*history))
    history_summary = {'requested_window': history_window, 'count': len(history),
                       'max_rms': stats(maxima),
                       'equations': {name: stats([table[name] for table in history if name in table])
                                     for name in reported},
                       'above_1e4_count': sum(value > 1e-4 for value in maxima),
                       'above_target_count': sum(value > policy.rms_target for value in maxima),
                       'performance_monitors_verified': False}
    imbalance = None
    if 'Normalised Imbalance Summary' in final:
        section = final.split('Normalised Imbalance Summary', 1)[1].split('\n\n', 1)[0]
        imbalance = {name.lower(): float(value) for name, value in re.findall(
            r'^\s*\|\s*([\w-]+)\s*\|\s*[\d.Ee+-]+\s*\|\s*([-+\d.Ee]+)\s*\|', section, re.M)}
    return dict(iteration=int(last.group(1)), run_iteration=int(last.group(2) or last.group(1)),
                rms=values, wall_notices=wall_notices(final), checked_equations=sorted(REQUIRED), max_rms=max_rms,
                rms_target=policy.rms_target,
                converged=converged, iteration_limit_stop=limit_stop, iteration_limit_reached=exhausted,
                declared_iteration_limit=declared_limit, history=history_summary,
                normalised_imbalance_percent=imbalance)


def convergence_ccl(policy: ConvergencePolicy, *, restart: bool = False) -> str:
    lines = [f'FLOW: {policy.flow_analysis}', '  SOLVER CONTROL:']
    if restart:
        lines += ['    CONVERGENCE CONTROL:', f'      Maximum Number of Iterations = {policy.restart_iterations}',
                  '      Minimum Number of Iterations = 1', '    END']
    lines += ['    CONVERGENCE CRITERIA:', '      Residual Type = RMS',
              f'      Residual Target = {policy.rms_target:.12g}', '    END', '  END', 'END', '']
    return '\n'.join(lines)
