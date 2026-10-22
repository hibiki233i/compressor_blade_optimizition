"""Explicit cancellation receipts and reversible retries of the same design point."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid

from blade_shape_runtime import atomic_json, file_identity

ATTEMPT = 'run_attempt.json'
RETRY = 'retry_journal.json'
TOKEN_ENV = 'BLADE_RUN_TOKEN'
STOP_ENV = 'BLADE_STOP_FILE'


class RunInterrupted(KeyboardInterrupt):
    """An operator stopped this attempt; keep the queue unfinished for --resume."""


def stop_environment(directory: Path) -> dict[str, str]:
    token = uuid.uuid4().hex
    return {TOKEN_ENV: token, STOP_ENV: str(directory.resolve() / '.stop_requests' / f'{token}.json')}


def request_stop(owner: dict, reason: str = 'operator stop', process_ids: list[int] | None = None) -> None:
    path = Path(owner['stop_file'])
    previous = json.loads(path.read_text()) if path.exists() else {}
    data = previous if previous.get('token') == owner['token'] else {}
    data.update(token=owner['token'])
    data.setdefault('reason', reason)
    data.setdefault('requested_at', time.time())
    if process_ids is not None:
        data['process_ids'] = sorted(set(data.get('process_ids', [])) | set(process_ids))
    atomic_json(path, data)


def stopped(owner: dict) -> bool:
    path = Path(owner.get('stop_file', ''))
    if not path.is_file():
        return False
    data = json.loads(path.read_text(encoding='utf-8'))
    return bool(owner.get('token') and data.get('token') == owner['token'])


def retry_requested(case: Path) -> bool:
    path = case / ATTEMPT
    if not path.is_file():
        return False
    data = json.loads(path.read_text(encoding='utf-8'))
    return data.get('status') == 'interrupted' or (data.get('status') == 'running' and stopped(data.get('owner', {})))


def pid_alive(pid: int) -> bool:
    """Never use os.kill(pid, 0) on Windows: it terminates the process there."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        dll = ctypes.WinDLL('kernel32', use_last_error=True)
        dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        dll.OpenProcess.restype = wintypes.HANDLE
        dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = dll.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # denied/unknown is not evidence of exit
        try:
            return dll.WaitForSingleObject(handle, 0) != 0
        finally:
            dll.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def assert_idle(case: Path) -> None:
    pids = set()
    attempt = case / ATTEMPT
    if attempt.exists():
        owner = json.loads(attempt.read_text()).get('owner', {})
        stop_file = Path(owner.get('stop_file', ''))
        if stop_file.is_file():
            stop = json.loads(stop_file.read_text())
            if stop.get('token') == owner.get('token'):
                pids.update(int(pid) for pid in stop.get('process_ids', []))
    active = case / 'active_process.json'
    if active.exists():
        data = json.loads(active.read_text(encoding='utf-8'))
        if data.get('status') == 'running':
            pids.add(int(data['pid']))
    for path in case.glob('*.dir/pids'):
        pids.update(int(pid) for pid in re.findall(
            r'^\s*\|[^|]*\|\s*\d+\s*\|\s*(\d+)\s*\|', path.read_text(errors='replace'), re.M))
    alive = sorted(pid for pid in pids if pid_alive(pid))
    if alive:
        raise RuntimeError(f'Interrupted case still has live or unverifiable process IDs {alive}; stop them before retrying.')


def completed_result_available(case: Path) -> bool:
    path = case / 'cfx_state.json'
    if not path.exists():
        return False
    stages = json.loads(path.read_text()).get('stages', {})
    solve, post = stages.get('solve', {}), stages.get('post', {})
    return (solve.get('status') in {'complete', 'review'} and solve.get('exit_code') == 0
            and post.get('status') == 'complete'
            and file_identity(case / solve.get('result_file', '')) == solve.get('result_identity')
            and bool(solve.get('result_identity'))
            and file_identity(case / solve.get('out_file', '')) == solve.get('out_identity')
            and file_identity(case / 'CFX_Results.txt') == post.get('result_identity'))


def archive_interrupted(case: Path) -> None:
    """Journal each move, preserving all prior artifacts and the OS lock inode."""
    case = case.resolve()
    assert_idle(case)
    journal_path = case / RETRY
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {'attempts': []}
    pending = next((a for a in journal['attempts'] if a['status'] == 'archiving'), None)
    if pending is None:
        attempt = json.loads((case/ATTEMPT).read_text()) if (case/ATTEMPT).exists() else {}
        pending = {'directory': f'attempts/interrupted_{len(journal["attempts"])+1:04d}', 'status': 'archiving',
                   'files': [p.name for p in case.iterdir() if p.name not in {'.optimizer.lock', 'attempts', RETRY}],
                   'attempt': attempt}
        journal['attempts'].append(pending)
        atomic_json(journal_path, journal)
    dest = (case / pending['directory']).resolve()
    if not dest.is_relative_to(case) or dest == case:
        raise ValueError('Retry archive escapes the case directory.')
    dest.mkdir(parents=True, exist_ok=True)
    for name in pending['files']:
        source, target = case / name, dest / name
        if source.parent != case or not target.resolve().is_relative_to(dest):
            raise ValueError('Invalid retry archive entry.')
        if source.exists() and not target.exists():
            shutil.move(str(source), str(target))
        elif source.exists() or not target.exists():
            raise ValueError(f'Ambiguous interrupted archive entry: {name}')
    pending['status'] = 'archived'
    atomic_json(journal_path, journal)


def begin_case(case: Path, run_id: str, signature: str, x: list[float]) -> dict:
    case.mkdir(parents=True, exist_ok=True)
    path = case / ATTEMPT
    prior = json.loads(path.read_text()) if path.exists() else None
    journal_path = case / RETRY
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
    archiving = next((a for a in journal.get('attempts', []) if a['status'] == 'archiving'), None)
    if prior is None and archiving:
        prior = archiving.get('attempt')
    if retry_requested(case) or any(a['status'] == 'archiving' for a in journal.get('attempts', [])):
        same_vector = (not prior or (len(prior['x']) == len(x) and all(
            math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-10) for a, b in zip(prior['x'], x))))
        if prior and (prior['signature'] != signature or not same_vector or prior['run_id'] != run_id):
            raise ValueError('Interrupted inputs changed; refusing automatic retry.')
        if not completed_result_available(case):
            archive_interrupted(case)
        prior = None
    if prior and prior.get('status') == 'running':
        # An unmarked crash is not an operator cancellation.
        return prior
    env = stop_environment(case)
    owner = {'token': os.environ.get(TOKEN_ENV, env[TOKEN_ENV]),
             'stop_file': os.environ.get(STOP_ENV, env[STOP_ENV])}
    data = dict(run_id=run_id, signature=signature, x=x, owner=owner, status='running')
    atomic_json(path, data)
    return data


def finish_case(case: Path, status: str) -> None:
    data = json.loads((case / ATTEMPT).read_text())
    data['status'] = status
    atomic_json(case / ATTEMPT, data)


def check_stop(case: Path) -> None:
    path = case / ATTEMPT
    if path.exists() and stopped(json.loads(path.read_text()).get('owner', {})):
        raise RunInterrupted('Operator stopped this case; --resume will recompute it with the same run_id.')


def run_logged(command: list[str], case: Path, log_path: Path) -> int:
    check_stop(case)
    with log_path.open('w', encoding='utf-8', errors='replace') as log:
        log.write('COMMAND: ' + ' '.join(map(str, command)) + '\n\n')
        proc = subprocess.Popen(command, cwd=case, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=0x08000000 if os.name == 'nt' else 0,
                                start_new_session=os.name != 'nt')
        active = {'pid': proc.pid, 'status': 'running', 'command': list(map(str, command))}
        atomic_json(case / 'active_process.json', active)
        try:
            while proc.poll() is None:
                check_stop(case)
                time.sleep(.2)
        except KeyboardInterrupt:
            attempt = case / ATTEMPT
            if attempt.exists():
                request_stop(json.loads(attempt.read_text())['owner'], 'operator interrupt')
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=0x08000000, check=False)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait(timeout=5)
            raise
        finally:
            active.update(status='finished' if proc.poll() is not None else 'running', exit_code=proc.poll())
            atomic_json(case / 'active_process.json', active)
    check_stop(case)
    return int(proc.returncode)
