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
from contextlib import contextmanager, ExitStack

from blade_shape_runtime import atomic_json, file_identity, output_lock

ATTEMPT = 'run_attempt.json'
RETRY = 'retry_journal.json'
OPERATION = 'active_operation.json'
TOKEN_ENV = 'BLADE_RUN_TOKEN'
STOP_ENV = 'BLADE_STOP_FILE'
#: Clock and file-time granularity when matching a recorded PID to its process.
PID_SLACK_S = 2.0
#: Files a cancelled CFX-Post leaves; a verified Solve beside them is kept.
POST_ARTIFACTS = ('CFX_Results.txt', 'cfx_post.log', 'active_process.json', ATTEMPT, '.stop_requests')
#: Evidence that retry-interrupted confirmed; it must not change before archiving.
REGISTERED_EVIDENCE = ('cfx_state.json', 'geometry_state.json', 'candidate.json')


class RunInterrupted(KeyboardInterrupt):
    """An operator stopped this attempt; keep the queue unfinished for --resume."""


def stop_environment(directory: Path) -> dict[str, str]:
    token = uuid.uuid4().hex
    return {TOKEN_ENV: token, STOP_ENV: str(directory.resolve() / '.stop_requests' / f'{token}.json')}


def request_stop(owner: dict, reason: str = 'operator stop', process_ids: list[int] | None = None,
                 process_started: dict[int, float] | None = None) -> None:
    path = Path(owner['stop_file'])
    # GUI and CLI can request a stop at once; serialize the read/merge/write.
    with ExitStack() as stack:
        for attempt in range(201):
            try:
                stack.enter_context(output_lock(path.parent))
                break
            except RuntimeError as exc:
                if attempt == 200:
                    raise OSError('Could not lock the stop receipt.') from exc
                time.sleep(.01)
        previous = json.loads(path.read_text()) if path.exists() else {}
        data = previous if previous.get('token') == owner['token'] else {}
        if data and process_ids is None:
            return
        data.update(token=owner['token'])
        data.setdefault('reason', reason)
        data.setdefault('requested_at', time.time())
        if process_ids is not None:
            data['process_ids'] = sorted(set(data.get('process_ids', [])) | set(process_ids))
            # A bare PID can be reused after exit; keep when it was seen and, if known, its start.
            records, now = data.setdefault('processes', {}), time.time()
            for pid in process_ids:
                previous = records.get(str(pid), {})
                records[str(pid)] = {'recorded_at': previous.get('recorded_at', now),
                                     'started_at': (process_started or {}).get(pid, previous.get('started_at'))}
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


def recorded_failure(case: Path) -> tuple[str, str] | None:
    """A stop request cannot override an already recorded tool/numerical failure."""
    geometry = case / 'geometry_state.json'
    cfx = case / 'cfx_state.json'
    stages = {'geometry': json.loads(geometry.read_text())} if geometry.exists() else {}
    if cfx.exists():
        stages.update(json.loads(cfx.read_text()).get('stages', {}))
    for name, stage in stages.items():
        if stage.get('status') == 'failed' or stage.get('exit_code') not in (None, 0):
            return name, stage.get('message', f'Recorded {name} failure (exit={stage.get("exit_code")}).')
    active = case / 'active_process.json'
    if active.exists():
        data = json.loads(active.read_text())
        if data.get('status') == 'finished' and data.get('cancelled') is False and data.get('exit_code') not in (None, 0):
            return 'environment', f'Recorded process failure (exit={data["exit_code"]}).'
    return None


def current_owner(case: Path) -> dict:
    for name in (OPERATION, ATTEMPT):
        path = case / name
        if path.exists():
            data = json.loads(path.read_text())
            if data.get('status') == 'running':
                return data.get('owner', {})
    return {}


@contextmanager
def operation(case: Path, name: str):
    """Post reassessment has its own cancellation lifetime, never a new CFD attempt."""
    env = stop_environment(case)
    owner = {'token': os.environ.get(TOKEN_ENV, env[TOKEN_ENV]),
             'stop_file': os.environ.get(STOP_ENV, env[STOP_ENV])}
    record = {'operation': name, 'owner': owner, 'status': 'running'}
    atomic_json(case / OPERATION, record)
    try:
        check_stop(case)
        yield
    except KeyboardInterrupt:
        record['status'] = 'interrupted'
        raise
    finally:
        if record['status'] == 'running':
            record['status'] = 'complete'
        atomic_json(case / OPERATION, record)


def process_started_at(pid: int) -> float | None:
    """Creation time (epoch seconds) of the process now holding ``pid``; None if unknown."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return psutil.Process(pid).create_time()
        except psutil.Error:
            return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        dll = ctypes.WinDLL('kernel32', use_last_error=True)
        dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        dll.OpenProcess.restype = wintypes.HANDLE
        dll.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = dll.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not dll.GetProcessTimes(handle, *map(ctypes.byref, times)):
                return None
            return (times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) / 1e7 - 11644473600
        finally:
            dll.CloseHandle(handle)
    try:
        ticks = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
        boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                    if line.startswith('btime '))
        return boot + ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _pid_exists(pid: int) -> bool:
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


def pid_alive(pid: int, *, started_at: float | None = None, not_after: float | None = None) -> bool:
    """Whether the recorded process may still run.

    ``started_at`` is its recorded creation time; ``not_after`` is when the PID
    was recorded, so the original process started no later. A process holding
    the PID that started otherwise is an unrelated reuse. An unreadable start
    time stays live: it is not evidence that the original exited.
    """
    if not _pid_exists(pid):
        return False
    if started_at is None and not_after is None:
        return True
    current = process_started_at(pid)
    if current is None:
        return True
    if started_at is not None:
        return abs(current - started_at) <= PID_SLACK_S
    return current <= not_after + PID_SLACK_S


def assert_idle(case: Path) -> None:
    # (pid, recorded start, recorded-at bound) from every receipt naming a process.
    records = []
    for name in (ATTEMPT, OPERATION):
        attempt = case / name
        if not attempt.exists():
            continue
        owner = json.loads(attempt.read_text()).get('owner', {})
        stop_file = Path(owner.get('stop_file', ''))
        if stop_file.is_file():
            stop = json.loads(stop_file.read_text())
            if stop.get('token') == owner.get('token'):
                # Receipts without per-process records were last written at their mtime.
                written, processes = stop_file.stat().st_mtime, stop.get('processes', {})
                for pid in stop.get('process_ids', []):
                    record = processes.get(str(pid), {})
                    records.append((int(pid), record.get('started_at'), record.get('recorded_at', written)))
    active = case / 'active_process.json'
    if active.exists():
        data = json.loads(active.read_text(encoding='utf-8'))
        if data.get('status') == 'running':
            records.append((int(data['pid']), data.get('started_at'),
                            data.get('recorded_at', active.stat().st_mtime)))
    for path in case.glob('*.dir/pids'):
        # CFX lists a process only after starting it, so none started after the file was written.
        written = path.stat().st_mtime
        records.extend((int(pid), None, written) for pid in re.findall(
            r'^\s*\|[^|]*\|\s*\d+\s*\|\s*(\d+)\s*\|', path.read_text(errors='replace'), re.M))
    alive = sorted({pid for pid, started, written in records if pid_alive(pid, started_at=started, not_after=written)})
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


def stage_boundary_ready(case: Path) -> bool:
    """Only continue a never-started stage after a verified predecessor finished."""
    geometry_path = case / 'geometry_state.json'
    if not geometry_path.exists():
        return False
    geometry = json.loads(geometry_path.read_text())
    if (geometry.get('status') != 'complete' or not geometry.get('mesh')
            or file_identity(case / 'Impeller_Mesh.gtm') != geometry['mesh']):
        return False
    cfx_path = case / 'cfx_state.json'
    stages = json.loads(cfx_path.read_text()).get('stages', {}) if cfx_path.exists() else {}
    if not stages:
        return True
    if stages.get('pre', {}).get('status') != 'complete':
        return False
    if 'solve' not in stages:
        return True
    return stages['solve'].get('status') in {'complete', 'review'} and 'post' not in stages


def verified_solve_available(case: Path) -> bool:
    """Solve finished with accepted, unchanged evidence and only Post is unfinished."""
    from blade_shape_cfx_runner import final_solve_recorded
    geometry_path, state_path = case / 'geometry_state.json', case / 'cfx_state.json'
    if not geometry_path.exists() or not state_path.exists():
        return False
    geometry, state = json.loads(geometry_path.read_text()), json.loads(state_path.read_text())
    stages, inputs = state.get('stages', {}), state.get('inputs', {})
    pre, solve = stages.get('pre', {}), stages.get('solve', {})
    evidence = [('Impeller_Mesh.gtm', geometry.get('mesh')), ('Impeller_Mesh.gtm', inputs.get('mesh')),
                ('candidate.json', inputs.get('candidate')),
                ('Impeller.def', pre.get('definition_identity')), ('update_bc.ccl', pre.get('ccl_identity')),
                ('convergence.ccl', state.get('convergence_control_identity')),
                (solve.get('result_file'), solve.get('result_identity')), (solve.get('out_file'), solve.get('out_identity'))]
    if state.get('restart'):
        evidence.append(('restart_convergence.ccl', state['restart'].get('ccl_identity')))
    accepted = solve.get('review') if solve.get('status') == 'review' else solve.get('convergence', {}).get('converged')
    return (geometry.get('status') == 'complete' and pre.get('status') == 'complete'
            and solve.get('status') in {'complete', 'review'} and solve.get('exit_code') == 0 and bool(accepted)
            and stages.get('post', {}).get('status') in {None, 'running', 'returned'} and final_solve_recorded(state)
            and all(isinstance(name, str) and name and Path(name).name == name and identity
                    and file_identity(case / name) == identity for name, identity in evidence))


def archive_interrupted(case: Path, files: tuple[str, ...] | None = None) -> None:
    """Journal each move, preserving all prior artifacts and the OS lock inode.

    ``files`` limits a new archive to those entries (a Post-only retry keeps the
    verified Solve in place); a journaled archive replays its recorded list.
    """
    case = case.resolve()
    assert_idle(case)
    journal_path = case / RETRY
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {'attempts': []}
    pending = next((a for a in journal['attempts'] if a['status'] == 'archiving'), None)
    if pending is None:
        if recorded_failure(case):
            raise ValueError('A recorded failed stage cannot be archived as an interrupted attempt.')
        attempt = json.loads((case/ATTEMPT).read_text()) if (case/ATTEMPT).exists() else {}
        pending = {'directory': f'attempts/interrupted_{len(journal["attempts"])+1:04d}', 'status': 'archiving',
                   'files': [p.name for p in case.iterdir() if p.name not in {'.optimizer.lock', 'attempts', RETRY}
                             and (files is None or p.name in files)],
                   'scope': 'all' if files is None else 'post', 'attempt': attempt}
        journal['attempts'].append(pending)
        atomic_json(journal_path, journal)
    dest = (case / pending['directory']).resolve()
    if (dest.parent != case / 'attempts' or not re.fullmatch(r'interrupted_\d{4,}', dest.name)
            or not dest.is_relative_to(case)):
        raise ValueError('Retry archive escapes the case directory.')
    if dest.exists():
        assert_idle(dest)
    if recorded_failure(case) or (dest.exists() and recorded_failure(dest)):
        raise ValueError('A recorded failed stage cannot be archived as an interrupted attempt.')
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


def begin_case(case: Path, run_id: str, signature: str, x: list[float],
               accepted: set[str] | None = None) -> dict:
    """``accepted`` are signatures still compatible with ``signature`` (--accept-input-change)."""
    case.mkdir(parents=True, exist_ok=True)
    path = case / ATTEMPT
    prior = json.loads(path.read_text()) if path.exists() else None
    journal_path = case / RETRY
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
    archiving = next((a for a in journal.get('attempts', []) if a['status'] == 'archiving'), None)
    if prior is None and archiving:
        prior = archiving.get('attempt')
    if retry_requested(case) or any(a['status'] == 'archiving' for a in journal.get('attempts', [])):
        if archiving and prior != archiving.get('attempt'):
            raise ValueError('Interrupted archive attempt identity changed.')
        same_vector = (not prior or (len(prior['x']) == len(x) and all(
            math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-10) for a, b in zip(prior['x'], x))))
        if prior and (prior['signature'] not in (accepted or set()) | {signature}
                      or not same_vector or prior['run_id'] != run_id):
            raise ValueError('Interrupted inputs changed; refusing automatic retry.')
        registered = (prior or {}).get('registered_evidence')
        if registered is None and prior and 'original_receipt_identity' in prior:
            registered = {'cfx_state.json': prior['original_receipt_identity']}
        if registered is not None and not archiving:
            changed = sorted(name for name, identity in registered.items() if file_identity(case / name) != identity)
            if changed:
                raise ValueError(f'Evidence confirmed by retry-interrupted changed ({", ".join(changed)}); '
                                 'inspect the case before retrying.')
        if recorded_failure(case):
            raise ValueError('A recorded failed stage is not an interrupted case; inspect the failure instead.')
        state_path = case / 'cfx_state.json'
        stages = json.loads(state_path.read_text()).get('stages', {}) if state_path.exists() else {}
        if (stages.get('solve', {}).get('status') in {'complete', 'review'}
                and stages.get('post', {}).get('status') == 'complete' and not completed_result_available(case)):
            raise ValueError('Completed result evidence changed; refusing to archive it as interrupted.')
        if not completed_result_available(case):
            # Unchanged inputs and a verified Solve: only the stopped Post reruns.
            post_only = bool(prior) and prior['signature'] == signature and verified_solve_available(case)
            archive_interrupted(case, POST_ARTIFACTS if post_only else None)
        prior = None
    if prior and prior.get('status') == 'running':
        # A partial stage cannot be retried without an explicit operator stop.
        # A verified stage boundary can hand off the next, never-started stage;
        # its original inputs are still checked before any external invocation.
        if not stage_boundary_ready(case):
            raise ValueError('Unmarked running attempt requires inspection and retry-interrupted confirmation.')
        assert_idle(case)
        if (prior.get('run_id') != run_id or len(prior.get('x', [])) != len(x)
                or not all(math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-10) for a, b in zip(prior['x'], x))):
            raise ValueError('Stage handoff inputs differ from the recorded attempt.')
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
    if stopped(current_owner(case)):
        raise RunInterrupted('Operator stopped this case; --resume will recompute it with the same run_id.')


def run_logged(command: list[str], case: Path, log_path: Path) -> int:
    check_stop(case)
    with log_path.open('w', encoding='utf-8', errors='replace') as log:
        log.write('COMMAND: ' + ' '.join(map(str, command)) + '\n\n')
        proc = None
        active = {'status': 'starting', 'command': list(map(str, command)), 'owner': current_owner(case), 'cancelled': False}
        try:
            proc = subprocess.Popen(command, cwd=case, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=0x08000000 if os.name == 'nt' else 0,
                                    start_new_session=os.name != 'nt')
            active.update(pid=proc.pid, status='running', recorded_at=time.time(),
                          started_at=process_started_at(proc.pid))
            atomic_json(case / 'active_process.json', active)
            while proc.poll() is None:
                check_stop(case)
                time.sleep(.2)
        except KeyboardInterrupt:
            active['cancelled'] = proc is None or proc.poll() is None
            owner = active['owner']
            try:
                if owner:
                    request_stop(owner, 'operator interrupt')
            finally:
                if proc is not None and proc.poll() is None:
                    _terminate(proc)
            raise
        except BaseException:
            if proc is not None and proc.poll() is None:
                _terminate(proc)
            raise
        finally:
            if proc is not None:
                active.update(status='finished' if proc.poll() is not None else 'running', exit_code=proc.poll())
                atomic_json(case / 'active_process.json', active)
    # A returned process reaches its stage receipt first: a natural error stays a
    # failure, and a finished Solve stays verified evidence. A late stop is
    # honoured by the next stage's check instead.
    return int(proc.returncode)


def _terminate(proc) -> None:
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
