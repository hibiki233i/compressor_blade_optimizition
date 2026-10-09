"""Export the report's 20-point ``Velocity Beta ACA`` curve from an existing ``.res``.

CFX-Post replays a saved Turbo spanwise-line session (``extract_aca.cse``) on the
result; this module only copies that session, runs it in a new directory, checks
the Generic Export and tidies it into the two-column ``span,beta_cfx_deg`` CSV
consumed by ``blade_shape_incidence_validation.py legacy``. The session is the
single source of the measurement definition: its settings are parsed and
recorded, never assumed. No geometry, mesh or solve is run.
"""
from __future__ import annotations

import csv
import math
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from blade_shape_runtime import atomic_json, file_identity

METHOD = 'cfx_post_saved_session_aca_20_v1'
SPAN_POINTS = 20
SPAN_TOLERANCE = 1e-6
SESSION_NAME = 'extract_aca.cse'
SUMMARY_NAME = 'extraction_summary.json'
SPAN_COLUMN = 'Span Normalized'
BETA_COLUMN = re.compile(r'Velocity Beta ACA on (?P<line>.+?)\s*\[\s*degree\s*\]')
#: settings worth recording from the session (all occurrences are kept)
SESSION_KEYS = (
    'CFX Post Version', 'Turbo Domain List', 'Turbo Line Mode', 'Streamwise Location',
    'Span Min', 'Span Max', 'Span Points', 'Include Boundary Points',
    'Point Distribution Method', 'Circumferential Average Mode', 'Export File', 'Precision',
)


def session_settings(text: str) -> dict[str, list[str]]:
    """Distinct ``Key = value`` values per recorded key, in session order."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition('=')
        key, value = key.strip(), value.strip()
        if sep and key in SESSION_KEYS and value not in found.setdefault(key, []):
            found[key].append(value)
    return found


def export_name(settings: dict[str, list[str]]) -> str:
    """The session must export to one relative file inside the analysis directory."""
    names = settings.get('Export File', [])
    if len(names) != 1:
        raise ValueError(f'Session must declare exactly one Export File; found {names or "none"}')
    name = names[0].strip('"\'')
    windows, posix = PureWindowsPath(name), PurePosixPath(name)
    # drive-relative (D:x.csv) and rooted (\\x.csv) names escape a joined directory on Windows
    if (not name or windows.drive or windows.root or posix.root
            or '..' in windows.parts or '..' in posix.parts):
        raise ValueError(f'Export File must be a relative path inside the output directory: {name!r}')
    return name


def parse_generic_export(text: str) -> tuple[list[str], list[list[str]]]:
    """Header and rows of the ``[Data]`` table in a CFX-Post Generic Export."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == '[Data]')
    except StopIteration:
        raise ValueError('CFX-Post export has no [Data] table') from None
    body = iter(lines[start + 1:])
    header = next((line for line in body if line.strip()), None)
    if header is None:
        raise ValueError('CFX-Post export [Data] table has no header')
    rows = []
    for line in body:
        if not line.strip() or line.lstrip().startswith('['):
            break
        rows.append([cell.strip() for cell in line.split(',')])
    return [cell.strip() for cell in header.split(',')], rows


def tidy_export(header: list[str], rows: list[list[str]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Check variables, units and the j/19 span layout; keep raw numeric text."""
    match = BETA_COLUMN.fullmatch(header[1]) if len(header) == 2 else None
    if len(header) != 2 or header[0] != SPAN_COLUMN or match is None:
        raise ValueError(f'Unexpected export variables or units: {header}')
    values = []
    for row in rows:
        if len(row) != 2:
            raise ValueError(f'Expected exactly {SPAN_POINTS} finite points; malformed row {row}')
        try:
            span, beta = float(row[0]), float(row[1])
        except ValueError:
            raise ValueError(f'Expected exactly {SPAN_POINTS} finite points; non-numeric row {row}') from None
        if not (math.isfinite(span) and math.isfinite(beta)):
            raise ValueError(f'Expected exactly {SPAN_POINTS} finite points; non-finite row {row}')
        values.append((span, beta))
    if len(values) != SPAN_POINTS:
        raise ValueError(f'Expected exactly {SPAN_POINTS} finite points; found {len(values)}')
    deviation = max(abs(span - j / (SPAN_POINTS - 1)) for j, (span, _) in enumerate(values))
    if deviation > SPAN_TOLERANCE:
        raise ValueError(f'Exported span positions do not match j/{SPAN_POINTS - 1}; max deviation {deviation:.3g}')
    betas = [beta for _, beta in values]
    tidy = [dict(span=row[0], beta_cfx_deg=row[1]) for row in rows]
    return tidy, dict(points=len(tidy), max_span_deviation=deviation, line_name=match['line'],
                      beta_column=header[1], beta_min_deg=min(betas), beta_max_deg=max(betas),
                      legacy_aca_quadrant_ok=all(-90 <= beta <= 0 for beta in betas))


def extract_aca(res: Path, post_exe: Path, session: Path, output: Path) -> dict[str, Any]:
    """Run the saved session on ``res`` in a new directory; failures keep every log."""
    res, post_exe, session = res.resolve(), post_exe.resolve(), session.resolve()
    if res.suffix.lower() != '.res' or not res.is_file():
        raise ValueError(f'An existing .res is required: {res}')
    for path, name in ((post_exe, 'CFX-Post executable'), (session, 'saved CFX-Post session')):
        if not path.is_file():
            raise ValueError(f'Missing {name}: {path}')
    output = output.resolve()
    if output == res.parent or res.parent in output.parents:
        raise ValueError('Use an independent analysis directory outside the source case')
    text = session.read_text(encoding='utf-8', errors='replace')
    settings = session_settings(text)
    raw_name = export_name(settings)
    output.mkdir(parents=True, exist_ok=False)
    copied = output / SESSION_NAME
    shutil.copy2(session, copied)
    identities = {str(path): file_identity(path) for path in (res, session)}
    command = [str(post_exe), '-batch', str(copied), '-res', str(res)]
    atomic_json(output / 'extraction_inputs.json', dict(
        method=METHOD, res_path=str(res), session_template=str(session), post_exe=str(post_exe),
        command=command, cwd=str(output), raw_export=raw_name, session_settings=settings,
        input_identities=identities,
        note='Settings are read from the saved session; the line position must be checked per geometry.'))
    summary_path = output / SUMMARY_NAME
    atomic_json(summary_path, dict(method=METHOD, status='running'))
    try:
        process = subprocess.run(command, cwd=output, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding='utf-8', errors='replace', check=False)
        (output / 'cfxpost.log').write_text(process.stdout or '', encoding='utf-8')
        atomic_json(output / 'returncode.json', {'returncode': process.returncode})
        raw, error = (output / raw_name).resolve(), output / 'cfdpost_error.log'
        if output not in raw.parents:
            raise ValueError(f'Export File resolves outside the output directory: {raw}')
        error_text = error.read_text(errors='replace').strip() if error.exists() else ''
        if process.returncode or error_text or not raw.is_file():
            # same reporting as `extract`: CFX-Post may exit 0 and only log the CEL error
            detail = next((line.strip() for line in error_text.splitlines() if 'error' in line.lower()), '')
            raise RuntimeError(f'CFX-Post export failed (returncode={process.returncode}); logs retained at {output}'
                               + (f'; {detail[:500]}' if detail else ''))
        if identities != {str(path): file_identity(path) for path in (res, session)}:
            raise ValueError('Input changed during extraction')
        rows, checks = tidy_export(*parse_generic_export(raw.read_text(encoding='utf-8-sig', errors='replace')))
        aca_csv = output / f'{res.stem}_beta_aca_{SPAN_POINTS}.csv'
        with aca_csv.open('x', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['span', 'beta_cfx_deg'])
            writer.writeheader()
            writer.writerows(rows)
        summary = dict(method=METHOD, status='complete', aca_csv=str(aca_csv),
                       aca_csv_identity=file_identity(aca_csv), raw_export=str(raw),
                       raw_export_identity=file_identity(raw), **checks, incidence_calculated=False,
                       note='Extraction and format checks only; not CFD acceptance or a legacy-metric result.')
        atomic_json(summary_path, summary)
        return summary
    except Exception as exc:
        atomic_json(summary_path, dict(method=METHOD, status='failed', message=str(exc)))
        raise
