"""Machine-specific paths live in an untracked INI next to the config JSON.

``blade_shape_config.json`` keeps shareable settings and leaves ``paths.*``
empty. Every empty (or absent) path is filled from the ``[paths]`` section of
``blade_shape_local.ini`` beside it; a non-empty JSON value always wins, so an
explicit config is never silently redirected. ``BLADE_SHAPE_LOCAL_INI`` points
at another file. Values are used verbatim (relative paths stay cwd-relative,
like JSON values); copy ``blade_shape_local.ini.example`` to start.

``cfx_bin_dir`` and ``turbogrid_exe`` left empty in both files are derived from
the ANSYS installer's ``AWP_ROOT<version>`` variable (``[ansys] version = 251``,
or the only installed version when none is set). Several installed versions and
no ``version`` derive nothing: ``cfx_bin_dir`` is part of the CFX resume
signature, so a second install must never switch the solver silently.
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
ANSYS_SECTION = 'ansys'
ANSYS_ROOT_ENV = re.compile(r'AWP_ROOT(\d{3})', re.IGNORECASE)
#: tools inside an ANSYS installation root, as path parts below AWP_ROOT<version>
ANSYS_TOOLS = {'cfx_bin_dir': ('CFX', 'bin'), 'turbogrid_exe': ('TurboGrid', 'bin', 'cfxtg.exe')}
#: config metadata: which path keys are owned by the INI (dropped from saved JSON)
META_KEY = '_local_paths'


def local_ini_path(config_path: str | Path) -> Path:
    override = os.environ.get(LOCAL_INI_ENV)
    return Path(override) if override else Path(config_path).resolve().parent / LOCAL_INI_NAME


def _read_ini(ini: Path) -> configparser.ConfigParser | None:
    if not ini.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None, delimiters=('=',))
    parser.optionxform = str  # keep the JSON key spelling
    parser.read(ini, encoding='utf-8-sig')
    return parser


def read_local_paths(ini: Path) -> dict[str, str]:
    """Non-empty ``[paths]`` values; a missing file means no local paths."""
    parser = _read_ini(ini)
    if parser is None or not parser.has_section(SECTION):
        return {}
    return {key: value.strip() for key, value in parser.items(SECTION) if value.strip()}


def read_ansys_version(ini: Path) -> str:
    """``[ansys] version`` (``251`` or ``v251``), empty when not set."""
    parser = _read_ini(ini)
    if parser is None:
        return ''
    return parser.get(ANSYS_SECTION, 'version', fallback='').strip().lower().removeprefix('v')


def ansys_installs(environ: dict[str, str] | None = None) -> dict[str, str]:
    """``{version: root}`` from every non-empty ``AWP_ROOT<version>`` variable."""
    environ = os.environ if environ is None else environ
    return {match[1]: value.strip() for key, value in environ.items()
            if (match := ANSYS_ROOT_ENV.fullmatch(key)) and value.strip()}


def ansys_root(version: str = '', environ: dict[str, str] | None = None) -> tuple[str, str, str]:
    """``(version, root, problem)`` of the ANSYS install to derive tool paths from."""
    installs = ansys_installs(environ)
    if version:
        root = installs.get(version, '')
        found = '、'.join(f'AWP_ROOT{v}' for v in sorted(installs)) or '无'
        return version, root, '' if root else f'[ansys] version = {version}，但没有环境变量 AWP_ROOT{version}（本机：{found}）'
    if len(installs) == 1:
        (only, root), = installs.items()
        return only, root, ''
    if installs:
        return '', '', (f'本机装有多个 ANSYS 版本（{"、".join(sorted(installs))}），'
                        f'请在 [ansys] version 指定其一')
    return '', '', ''


def apply_local_paths(config: dict[str, Any], config_path: str | Path) -> dict[str, Any]:
    """Fill empty ``paths`` entries from the local INI (or ``AWP_ROOT``) and record where each came from."""
    paths = config.get('paths')
    if not isinstance(paths, dict):
        return config
    ini = local_ini_path(config_path)
    values = read_local_paths(ini)
    owned = [key for key, value in paths.items() if value in ('', None)]
    owned += [key for key in values if key not in paths]
    derive = [key for key in ANSYS_TOOLS if key in owned and key not in values]
    version, root, problem = ansys_root(read_ansys_version(ini)) if derive else ('', '', '')
    sources = {}
    for key in owned:
        if key in values:
            paths[key], sources[key] = values[key], 'ini'
        elif key in derive and root:
            paths[key], sources[key] = os.path.join(root, *ANSYS_TOOLS[key]), f'AWP_ROOT{version}'
        else:
            paths[key] = ''
    misnamed = ini.with_name(ini.name + '.txt')
    config[META_KEY] = {
        'ini': str(ini), 'keys': owned, 'found': ini.is_file(),
        'loaded': {key: paths[key] for key in owned}, 'sources': sources,
        # INI entries that a non-empty JSON value overrides
        'ignored': [key for key in values if key not in owned],
        'ansys_problem': problem,
        'misnamed': str(misnamed) if not ini.is_file() and misnamed.is_file() else '',
    }
    return config


def split_local_paths(config: dict[str, Any], ini: str | Path | None = None
                      ) -> tuple[dict[str, Any], dict[str, str]]:
    """Shareable JSON payload (INI-owned paths emptied) and the values to write to ``ini``.

    Saving back to the INI the config was read from writes only edited values, so
    hand-written entries and derived ``AWP_ROOT`` paths stay as they are; another
    INI ("save as") gets every non-empty value it cannot derive itself.
    """
    meta = config.get(META_KEY)
    paths = config.get('paths')
    if not isinstance(meta, dict) or not isinstance(paths, dict):
        return config, {}
    same = ini is None or str(ini) == meta.get('ini')
    loaded, sources = meta.get('loaded', {}), meta.get('sources', {})
    payload = dict(config, paths=dict(paths))
    local = {}
    for key in meta.get('keys', []):
        if key not in paths:
            continue
        value = str(paths[key] or '')
        payload['paths'][key] = ''
        edited = key not in loaded or value != loaded[key]
        if edited if same else (value and (edited or sources.get(key) == 'ini')):
            local[key] = value
    return payload, local


def describe_local_paths(config: dict[str, Any]) -> list[tuple[str, str]]:
    """``(level, message)`` lines explaining where the machine paths came from."""
    meta = config.get(META_KEY)
    if not isinstance(meta, dict):
        return []
    ini, lines = Path(meta.get('ini', LOCAL_INI_NAME)), []
    if meta.get('found'):
        lines.append(('info', f'本机路径文件：{ini}'))
    elif meta.get('misnamed'):
        lines.append(('warning', f'找到 {Path(meta["misnamed"]).name} 而不是 {ini.name}：'
                                 f'Windows 隐藏了扩展名，请把它改名为 {ini.name}'))
    else:
        lines.append(('warning', f'没有找到本机路径文件 {ini}（INI 须与所用配置 JSON 在同一目录）'))
    derived = {}
    for key, source in meta.get('sources', {}).items():
        if source != 'ini':
            derived.setdefault(source, []).append(key)
    lines += [('info', f'{"、".join(keys)} 由 {source} 推导') for source, keys in derived.items()]
    if meta.get('ignored'):
        lines.append(('warning', f'INI 中的 {"、".join(meta["ignored"])} 未生效：JSON 中已有非空值，JSON 优先；'
                                 '要用 INI 的值请把 JSON 里对应项清空'))
    if meta.get('ansys_problem'):
        lines.append(('warning', meta['ansys_problem']))
    return lines


def main(argv: list[str] | None = None) -> int:
    """Print how ``paths.*`` resolve for a config file (read-only)."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('config', nargs='?', default='blade_shape_config.json', type=Path)
    args = parser.parse_args(argv)
    config = apply_local_paths(json.loads(args.config.read_text(encoding='utf-8')), args.config)
    print(f'config: {args.config.resolve()}')
    for level, message in describe_local_paths(config):
        print(f'[{level}] {message}')
    sources = config.get(META_KEY, {}).get('sources', {})
    for key, value in config.get('paths', {}).items():
        source = sources.get(key) or ('json' if value else '-')
        print(f'  {key:<22} {source:<12} {value}')
    installs = ansys_installs()
    print('AWP_ROOT: ' + ('; '.join(f'{v} = {root}' for v, root in sorted(installs.items())) or 'none'))
    return 0


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


if __name__ == '__main__':
    raise SystemExit(main())
