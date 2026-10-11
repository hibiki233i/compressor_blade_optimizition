"""What a real CFD label depends on, and explicit acceptance of input changes.

The physical signature covers only inputs that change the answer for an
already chosen design vector: variable names/order (how ``x`` maps to the
meanline), declared operating point, CFX convergence settings and the content
of the geometry/mesh/CFX templates and geometry script, and the CFX solver
release. Search settings, variable bounds, geometry rules, core count and
install locations do not change the result for a fixed ``x`` and are left out.

Every training row, pending entry and boundary plan carries the signature it
was produced under. A run refuses to continue while any record carries a
signature that is not compatible with the current one; the user either moves
to a new output directory or accepts the change with a stated reason. The
acceptance is appended to ``input_signatures.json`` together with the inputs
behind each signature, so later mismatch messages can name what changed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from blade_shape_runtime import atomic_json, digest

ASSET_KEYS = ('cft_batch_template', 'base_cft', 'turbogrid_template', 'template_cfx', 'template_cse',
              'geometry_script_path')
RUNTIME_KEYS = ('n_blades', 'rpm', 'mass_flow', 'p_out_pa', 'alpha0')
COLUMN = 'physical_signature'
REGISTRY = 'input_signatures.json'
# Rows written before signatures existed and with no queue/reservation record
# get a per-row pseudo signature ``unsigned:<run_id>:<content hash>``, so that
# accepting them covers exactly the reviewed rows, never later unsigned ones.
UNSIGNED = 'unsigned'
_UNSIGNED_CONTENT = ('Efficiency', 'MassFlow', 'status')

_RELEASE = re.compile(r'v\d{3}', re.IGNORECASE)  # ANSYS install tree: ...\ANSYS Inc\v251\CFX\bin

_hash_cache: dict[tuple[Any, ...], str] = {}


class InputsChanged(ValueError):
    """Records were produced under other inputs and the change was not accepted."""


def _sha256(value: Any) -> str | None:
    path = Path(str(value or ''))
    if not str(value or '').strip() or not path.is_file():
        return None
    stat = path.stat()
    # ctime and inode change when a file is restored by a copy that keeps its mtime
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
    if key not in _hash_cache:
        _hash_cache[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _hash_cache[key]


def solver_identity(cfx_bin_dir: Any) -> dict[str, Any]:
    """The CFX release that solves, not where it is installed.

    Moving identical binaries keeps both the version folder and the solver
    launcher content; another release changes them. Both are ``None`` only
    when ``cfx5solve.exe`` is absent, and then the CFX stage refuses to solve.
    """
    text = str(cfx_bin_dir or '').strip()
    if not text:
        return {'release': None, 'cfx5solve': None}
    parts = [part for part in re.split(r'[\\/]', text) if part]
    release = next((part.lower() for part in reversed(parts) if _RELEASE.fullmatch(part)), None)
    return {'release': release, 'cfx5solve': _sha256(Path(text) / 'cfx5solve.exe')}


def physical_inputs(config: dict[str, Any]) -> dict[str, Any]:
    paths = config.get('paths', {})
    return {'variables': [v['name'] for v in config['variables']],
            'cfx_convergence': config.get('cfx_convergence', {}),
            'runtime': {k: config['runtime'].get(k) for k in RUNTIME_KEYS},
            'assets': {k: _sha256(paths.get(k)) for k in ASSET_KEYS},
            'solver': solver_identity(paths.get('cfx_bin_dir'))}


def physical_signature(config: dict[str, Any]) -> str:
    return digest(physical_inputs(config))


GEOMETRY_ASSETS = ('cft_batch_template', 'base_cft', 'turbogrid_template', 'geometry_script_path')


def geometry_signature(config: dict[str, Any]) -> str:
    """Inputs that shape the mesh of a given ``x``; a geometry receipt is valid only for these."""
    paths = config.get('paths', {})
    return digest({'variables': [v['name'] for v in config['variables']],
                   'n_blades': config['runtime'].get('n_blades'),
                   'assets': {k: _sha256(paths.get(k)) for k in GEOMETRY_ASSETS}})


PIN = '_physical_signature'


def pin(config: dict[str, Any]) -> str:
    """Fix the signature a run was checked under; later evaluations must still match it."""
    config[PIN] = remember(config)
    return config[PIN]


def ensure_pinned(config: dict[str, Any]) -> None:
    """Stop before new CFD when inputs changed on disk after the run was checked."""
    if config.get(PIN) and physical_signature(config) != config[PIN]:
        raise InputsChanged('Physical inputs changed on disk while this run was in progress '
                            '(template, geometry script or CFX installation); no new CFD was started. '
                            'Inspect the change, then rerun.')


def legacy_physical_signature(config: dict[str, Any]) -> str:
    """The formula used before 2026-10: equal only if every old input is unchanged."""
    assets = {}
    for key in ASSET_KEYS:
        path = Path(config['paths'].get(key, ''))
        assets[key] = {'path': str(path), 'sha256': _sha256(path) if path.is_file() else None}
    return digest({'variables': config['variables'], 'constraints': config.get('constraints'),
                   'search': config.get('search', {}), 'cfx_convergence': config.get('cfx_convergence', {}),
                   'runtime': {k: config['runtime'].get(k) for k in RUNTIME_KEYS}, 'assets': assets})


def _output(config: dict[str, Any]) -> Path | None:
    value = str(config.get('paths', {}).get('output_dir') or '').strip()
    return Path(value) if value else None


def _read(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def load_registry(config: dict[str, Any]) -> dict[str, Any]:
    out = _output(config)
    registry = _read(out / REGISTRY, None) if out else None
    if registry is None:
        return {'version': 1, 'signatures': {}, 'accepted': []}
    if registry.get('version') != 1:
        raise ValueError(f'Unknown {REGISTRY} version.')
    return registry


def remember(config: dict[str, Any]) -> str:
    """Return the current signature, recording its inputs for later mismatch messages."""
    signature = physical_signature(config)
    out = _output(config)
    if out is not None:
        registry = load_registry(config)
        if signature not in registry['signatures']:
            registry['signatures'][signature] = physical_inputs(config)
            atomic_json(out / REGISTRY, registry)
    return signature


def _closure(seeds: set[str], accepted: list[dict[str, Any]]) -> set[str]:
    allowed = set(seeds)
    grown = True
    while grown:
        grown = False
        for entry in accepted:
            if entry['to'] in allowed and not set(entry['from']) <= allowed:
                allowed |= set(entry['from'])
                grown = True
    return allowed


def _unresolved(current: dict[str, Any]) -> set[tuple[str, str]]:
    """Asset/solver fields this machine cannot read (no template copy, no CFX install)."""
    return {(group, key) for group in ('assets', 'solver') for key, value in current[group].items() if value is None}


def _matches(recorded: dict[str, Any], current: dict[str, Any], unknown: set[tuple[str, str]]) -> bool:
    for group in set(recorded) | set(current):
        before, after = recorded.get(group), current.get(group)
        if isinstance(before, dict) and isinstance(after, dict):
            if any(before.get(k) != after.get(k) for k in set(before) | set(after) if (group, k) not in unknown):
                return False
        elif before != after:
            return False
    return True


def compatible(config: dict[str, Any], *, lenient: bool = False) -> set[str]:
    """Current signature, the old formula for unchanged inputs, and accepted predecessors.

    ``lenient`` (read-only views only) also matches recorded inputs on every
    field this machine can read, so a viewer without the templates or CFX
    still recognises results produced where they exist.
    """
    registry = load_registry(config)
    seeds = {physical_signature(config), legacy_physical_signature(config)}
    if lenient:
        current = physical_inputs(config)
        unknown = _unresolved(current)
        if unknown:
            seeds |= {signature for signature, inputs in registry['signatures'].items()
                      if _matches(inputs, current, unknown)}
    return _closure(seeds, registry['accepted'])


def classify(config: dict[str, Any], signatures: Iterable[str]) -> list[str]:
    """For read-only views: 'current', 'other', or 'unknown' when this machine cannot decide.

    'unknown' is a signature without recorded inputs (older program version or
    unsigned) while some asset or solver cannot be read here.
    """
    allowed = compatible(config, lenient=True)
    recorded = load_registry(config)['signatures']
    undecidable = bool(_unresolved(physical_inputs(config)))
    return ['current' if signature in allowed else
            'unknown' if undecidable and signature not in recorded else 'other' for signature in signatures]


def row_signatures(config: dict[str, Any], frame: pd.DataFrame) -> pd.Series:
    """Stored signature; for older rows, the queue entry or reservation that produced them."""
    stored = frame[COLUMN] if COLUMN in frame.columns else pd.Series(index=frame.index, dtype=object)
    stored = stored.astype(object).where(stored.notna() & stored.astype(str).str.strip().ne(''), None)
    if stored.notna().all():
        return stored.astype(str)
    known: dict[str, str] = {}
    out = _output(config)
    if out is not None:
        for run_id, owner in _read(out / 'case_reservations.json', {}).items():
            if owner.get('signature'):
                known[run_id] = owner['signature']
        for entry in _read(out / 'pending_evaluations.json', {}).get('entries', []):
            if entry.get('signature'):
                known[entry['run_id']] = entry['signature']
    run_ids = frame['run_id'].astype(str) if 'run_id' in frame.columns else pd.Series('', index=frame.index)
    resolved = list(stored.fillna(run_ids.map(known)))
    for position, signature in enumerate(resolved):
        if pd.isna(signature):
            resolved[position] = _unsigned_signature(config, frame.iloc[position])
    return pd.Series(resolved, index=frame.index).astype(str)


def _unsigned_signature(config: dict[str, Any], row: pd.Series) -> str:
    def value(item: Any) -> Any:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return str(item)
        return repr(number) if math.isfinite(number) else None
    columns = [v['name'] for v in config['variables']] + list(_UNSIGNED_CONTENT)
    content = digest({column: value(row.get(column)) for column in columns})[:16]
    return f"{UNSIGNED}:{row.get('run_id', '')}:{content}"


def is_unsigned(signature: str) -> bool:
    return signature.startswith(UNSIGNED + ':')


def evidence_mask(config: dict[str, Any], frame: pd.DataFrame) -> pd.Series:
    """Rows produced under the current inputs (or an accepted predecessor)."""
    if frame.empty:
        return pd.Series(True, index=frame.index)
    return row_signatures(config, frame).isin(compatible(config))


def _describe(registry: dict[str, Any], old: str, current: dict[str, Any]) -> str:
    if old == UNSIGNED or is_unsigned(old):
        return 'written before signatures existed; inputs unknown'
    inputs = registry['signatures'].get(old)
    if inputs is None:
        return 'inputs were not recorded (signature from an older program version)'
    changes = []
    for group in sorted(set(inputs) | set(current)):
        before, after = inputs.get(group), current.get(group)
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(set(before) | set(after)):
                if before.get(key) != after.get(key):
                    detail = 'content changed' if group == 'assets' else f'{before.get(key)!r} -> {after.get(key)!r}'
                    changes.append(f'{group}.{key}: {detail}')
        elif before != after:
            changes.append(f'{group} changed')
    return '; '.join(changes) or 'no difference in recorded inputs'


def check(config: dict[str, Any], records: dict[str, Iterable[str]], accept: str | None) -> None:
    """Refuse incompatible records unless the user accepts them with a reason.

    ``records`` maps a signature to labels of the records carrying it (run_ids,
    a plan id). Acceptance is recorded once and makes those signatures
    compatible with the current inputs from then on.
    """
    allowed = compatible(config)
    found: dict[str, list[str]] = {}
    for signature, labels in records.items():
        if signature not in allowed:
            found.setdefault(signature, []).extend(labels)
    reason = (accept or '').strip()
    if not found:
        if reason:
            print('Input change: nothing to accept; all records match the current inputs.')
        return
    current = remember(config)
    registry = load_registry(config)
    inputs = physical_inputs(config)
    # Unsigned rows each carry their own pseudo signature; report them as one group.
    groups: dict[str, list[str]] = {}
    for signature, labels in sorted(found.items()):
        groups.setdefault(UNSIGNED if is_unsigned(signature) else signature, []).extend(labels)
    lines = [f'  {len(labels)} record(s) [{", ".join(labels[:5])}{", ..." if len(labels) > 5 else ""}]: '
             f'{_describe(registry, signature, inputs)}'
             for signature, labels in groups.items()]
    if not reason:
        raise InputsChanged(
            'Records in this output directory were produced under other physical inputs:\n' + '\n'.join(lines) +
            '\nUse a new output directory for a separate study, or rerun with '
            '--accept-input-change "<reason>" if these records stay valid under the current inputs.')
    registry['accepted'].append({'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                 'from': sorted(found), 'to': current, 'reason': reason,
                                 'records': {signature: labels for signature, labels in sorted(found.items())}})
    atomic_json(_output(config) / REGISTRY, registry)
    print('Input change accepted (' + reason + '):\n' + '\n'.join(lines))


def training_records(config: dict[str, Any], frame: pd.DataFrame) -> dict[str, list[str]]:
    records: dict[str, list[str]] = {}
    if frame.empty:
        return records
    for signature, run_id in zip(row_signatures(config, frame), frame['run_id'].astype(str)):
        records.setdefault(signature, []).append(run_id)
    return records


def merge(*groups: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for group in groups:
        for signature, labels in group.items():
            out.setdefault(signature, []).extend(labels)
    return out
