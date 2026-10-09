"""Machine-specific paths live in an untracked INI next to the config JSON.

``blade_shape_config.json`` keeps shareable settings and leaves ``paths.*``
empty. Every empty (or absent) path is filled from the ``[paths]`` section of
``blade_shape_local.ini`` beside it; a non-empty JSON value always wins, so an
explicit config is never silently redirected. ``BLADE_SHAPE_LOCAL_INI`` points
at another file. Values are used verbatim (relative paths stay cwd-relative,
like JSON values); copy ``blade_shape_local.ini.example`` to start.
"""
from __future__ import annotations

import configparser
import os
import re
from pathlib import Path
from typing import Any

LOCAL_INI_NAME = 'blade_shape_local.ini'
LOCAL_INI_ENV = 'BLADE_SHAPE_LOCAL_INI'
SECTION = 'paths'
#: config metadata: which path keys are owned by the INI (dropped from saved JSON)
META_KEY = '_local_paths'


def local_ini_path(config_path: str | Path) -> Path:
    override = os.environ.get(LOCAL_INI_ENV)
    return Path(override) if override else Path(config_path).resolve().parent / LOCAL_INI_NAME


def read_local_paths(ini: Path) -> dict[str, str]:
    """Non-empty ``[paths]`` values; a missing file means no local paths."""
    if not ini.is_file():
        return {}
    parser = configparser.ConfigParser(interpolation=None, delimiters=('=',))
    parser.optionxform = str  # keep the JSON key spelling
    parser.read(ini, encoding='utf-8-sig')
    if not parser.has_section(SECTION):
        return {}
    return {key: value.strip() for key, value in parser.items(SECTION) if value.strip()}


def apply_local_paths(config: dict[str, Any], config_path: str | Path) -> dict[str, Any]:
    """Fill empty ``paths`` entries from the local INI and record which keys it owns."""
    paths = config.get('paths')
    if not isinstance(paths, dict):
        return config
    ini = local_ini_path(config_path)
    values = read_local_paths(ini)
    owned = [key for key, value in paths.items() if value in ('', None)]
    owned += [key for key in values if key not in paths]
    for key in owned:
        paths[key] = values.get(key, '')
    config[META_KEY] = {'ini': str(ini), 'keys': owned, 'found': ini.is_file()}
    return config


def split_local_paths(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Shareable JSON payload (INI-owned paths emptied) and the values for the INI."""
    meta = config.get(META_KEY)
    paths = config.get('paths')
    if not isinstance(meta, dict) or not isinstance(paths, dict):
        return config, {}
    payload = dict(config, paths=dict(paths))
    local = {}
    for key in meta.get('keys', []):
        if key in paths:
            local[key] = str(paths[key] or '')
            payload['paths'][key] = ''
    return payload, local


def write_local_paths(ini: Path, values: dict[str, str]) -> None:
    """Update ``[paths]`` in place, keeping comments and other sections."""
    lines = ini.read_text(encoding='utf-8-sig').splitlines() if ini.is_file() else []
    header = re.compile(r'^\s*\[(?P<name>[^\]]+)\]\s*$')
    start = next((i for i, line in enumerate(lines)
                  if (m := header.match(line)) and m['name'].strip() == SECTION), None)
    if start is None:
        lines += ([''] if lines else []) + [f'[{SECTION}]']
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
    pending = dict(values)
    for i in range(start + 1, end):
        key = lines[i].split('=', 1)[0].strip() if '=' in lines[i] else ''
        if key in pending and not lines[i].lstrip().startswith(('#', ';')):
            lines[i] = f'{key} = {pending.pop(key)}'
    insert = end
    while insert > start + 1 and not lines[insert - 1].strip():
        insert -= 1
    lines[insert:insert] = [f'{key} = {value}' for key, value in pending.items()]
    ini.parent.mkdir(parents=True, exist_ok=True)
    temporary = ini.with_name(ini.name + '.tmp')
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temporary.replace(ini)
