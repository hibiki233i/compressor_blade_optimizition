"""Thin :class:`QProcess` wrapper that streams CLI output line by line.

The GUI never imports the optimizer to execute it: every action spawns the
original command-line entry point in a child process, so a crash or a blocked
solver can never take the window down with it.
"""
from __future__ import annotations

import codecs
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Callable

import psutil
from blade_shape_interrupt import stop_environment, request_stop, TOKEN_ENV, STOP_ENV
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

#: CFX processes that can run outside the CLI's process tree (MPI workers may
#: be started by the MPI launcher rather than under cfx5solve). They are only
#: matched inside the run's directories and only when started after the run,
#: so other CFX jobs on the machine are left alone.
CFX_PROCESS_NAMES = frozenset({"solver-mpi.exe", "solver.exe", "cfx5solve.exe", "cfx5control.exe",
                               "solver-mpi", "solver", "cfx5solve", "cfx5control"})
#: Grace period before survivors of a stop are killed outright.
STOP_GRACE_MS = 2500
#: How often the processes a run has started are recorded while it runs.
TRACK_INTERVAL_MS = 5000
#: Tolerance between psutil start times and the GUI clock (Linux derives start
#: times from a boot time truncated to whole seconds).
CLOCK_SLACK_S = 1.0


class CommandRunner(QObject):
    """Runs one child process at a time and reports decoded lines."""

    started = Signal(list)
    line = Signal(str, str)  # (channel, text) - channel is always "out" (merged)
    finished = Signal(int, str)  # (exit code, reason)
    failed = Signal(str)

    def __init__(self, workdir: str | Path, parent: QObject | None = None):
        super().__init__(parent)
        self.workdir = Path(workdir)
        self._process: QProcess | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._command: list[str] = []
        self._scope: list[Path] = []
        self._started_at = 0.0
        self._root_pid = 0
        self._stopping = False
        self._stop_owner: dict = {}
        # (pid, start time) -> process: identities outlive the parent links
        self._known: dict[tuple[int, float], psutil.Process] = {}
        self._tracker = QTimer(self)
        self._tracker.setInterval(TRACK_INTERVAL_MS)
        self._tracker.timeout.connect(self._track)

    # ------------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    @property
    def command(self) -> list[str]:
        return list(self._command)

    # -------------------------------------------------------------- start
    def start(self, argv: list[str], extra_env: dict[str, str] | None = None, *,
              scope: list[str | Path] = ()) -> bool:
        """Start ``argv``; ``scope`` lists the directories its CFX runs work in."""
        if self.running:
            self.failed.emit("已有任务正在运行。")
            return False
        if not argv:
            self.failed.emit("命令为空。")
            return False

        self._command = list(argv)
        self._decoder.reset()
        self._buffer = ""
        # relative paths are resolved like the CLI does, from its working directory
        self._scope = [(self.workdir / Path(directory)).resolve() for directory in scope]
        self._known = {}
        self._stopping = False
        self._started_at = time.time()

        process = QProcess(self)
        process.setWorkingDirectory(str(self.workdir))
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.setProgram(argv[0])
        process.setArguments(argv[1:])

        environment = QProcessEnvironment.systemEnvironment()
        self._stop_owner = {}
        if self._scope:
            stop_env = stop_environment(self._scope[0])
            self._stop_owner = {'token': stop_env[TOKEN_ENV], 'stop_file': stop_env[STOP_ENV]}
            for key, value in stop_env.items():
                environment.insert(key, value)
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("PYTHONUTF8", "1")
        for key, value in (extra_env or {}).items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)

        process.readyReadStandardOutput.connect(self._on_ready)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        self.started.emit(self._command)
        process.start()
        self._root_pid = int(process.processId())
        self._tracker.start()
        return True

    def stop(self, *, wait: bool = False) -> None:
        """End the child and every process it started.

        The CLI runs PowerShell/CFturbo/TurboGrid and cfx5solve, and CFX starts
        its MPI workers (solver-mpi.exe) below those. None of them exits with
        the CLI: stopping only the CLI left the solver running and writing into
        the case after the output lock had died with the CLI.

        ``wait`` blocks until they are gone, for closing the window; the Stop
        button must not block the event loop.
        """
        process = self._process
        if process is None or process.state() == QProcess.NotRunning:
            return
        if self._stop_owner:
            try:
                request_stop(self._stop_owner)
            except OSError as exc:
                self.failed.emit(f'无法记录停止请求，尚未终止进程：{exc}')
                return
        self._stopping = True
        self._track()
        root = self._root_pid
        targets = sorted(self._known.values(), key=lambda proc: proc.pid == root)  # the CLI last
        if self._stop_owner:
            try:
                request_stop(self._stop_owner, process_ids=[proc.pid for proc in targets])
            except OSError as exc:
                self.failed.emit(f'无法记录进程停止范围，尚未终止进程：{exc}')
                return
        names = Counter(_name(proc) for proc in targets)
        self.line.emit("out", "[GUI] 停止：结束本次运行启动的 {} 个进程（{}）".format(
            len(targets), "，".join(f"{name}×{count}" for name, count in sorted(names.items()))))
        # Let the CLI observe its token and finish its owned-child cleanup first.
        # A fresh tree snapshot before forced termination also covers a child
        # spawned while the first stop snapshot was being collected.
        owner = dict(self._stop_owner)
        if owner and not wait:
            QTimer.singleShot(500, lambda: self._terminate_owned(process, root, targets, owner, wait=False))
            return
        if owner:
            process.waitForFinished(500)
        self._terminate_owned(process, root, targets, owner, wait=wait)

    def _terminate_owned(self, process: QProcess, root: int, targets: list[psutil.Process],
                         owner: dict, *, wait: bool) -> None:
        if self._process is process:
            self._track()
            targets = sorted(self._known.values(), key=lambda proc: proc.pid == root)
            if owner:
                try:
                    request_stop(owner, process_ids=[proc.pid for proc in targets])
                except OSError as exc:
                    self.failed.emit(f'无法更新停止范围：{exc}')
                    return
        # terminate() is TerminateProcess on Windows; console programs ignore
        # QProcess.terminate() there, which only posts WM_CLOSE.
        self._signal(targets, kill=False)
        # Never wait on the CLI through psutil: reaping it would hide its exit from QProcess.
        others = [proc for proc in targets if proc.pid != root]
        if wait:
            psutil.wait_procs(others, timeout=STOP_GRACE_MS / 1000)
            if not process.waitForFinished(STOP_GRACE_MS):
                process.kill()
                process.waitForFinished(STOP_GRACE_MS)
            self._signal(others, kill=True)
            psutil.wait_procs(others, timeout=1)
            self._report_survivors(others)
        else:
            QTimer.singleShot(STOP_GRACE_MS, lambda: self._kill_if_running(process, others))

    def _kill_if_running(self, process: QProcess, others: list[psutil.Process]) -> None:
        """Kill what outlived the grace period; report a second later, without blocking."""
        try:
            if process.state() != QProcess.NotRunning:
                process.kill()
            self._signal(others, kill=True)
            QTimer.singleShot(1000, lambda: self._report_survivors(others))
        except RuntimeError:  # the runner or QProcess was already deleted
            pass

    def _report_survivors(self, others: list[psutil.Process]) -> None:
        try:
            alive = [proc for proc in others if _alive(proc)]
            if alive:
                self.line.emit("out", "[GUI] 仍未结束的进程：" + _listing(alive) + "。请在任务管理器中结束后再续跑。")
            else:
                self.line.emit("out", "[GUI] 本次运行启动的进程已全部结束。")
        except RuntimeError:  # the runner was already deleted
            pass

    def _signal(self, targets: list[psutil.Process], *, kill: bool) -> None:
        for proc in targets:
            try:
                proc.kill() if kill else proc.terminate()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                if kill:
                    self.line.emit("out", f"[GUI] 无权结束 {_name(proc)} PID={proc.pid}。")

    def _track(self) -> None:
        """Record the processes this run has started while the parent links show them.

        A process stays recorded after its parent exits, and psutil refuses to
        signal a PID that has been reused since. A parent link only counts for a
        process started after the run and not before its parent: on Windows a
        process keeps the PID of a parent that exited long ago, and that PID may
        since belong to this run. CFX processes outside the tree are picked up
        by name inside the run's directories.

        This runs on the GUI thread every few seconds, so it reads parent links
        from one system snapshot and start times only for processes that may be
        this run's. Asking psutil every process for ppid/create_time took one
        system snapshot per process on Windows: minutes for the first pass
        with ~460 processes and CFX on every core, about a second for each later one.
        """
        if self._process is None or not self._root_pid:
            return
        since = self._started_at - CLOCK_SLACK_S
        parents = ppid_map()
        # psutil keeps these objects between calls and caches name and start time in them
        table = {proc.pid: proc for proc in psutil.process_iter(["name"]) if proc.pid in parents}

        def created(pid: int) -> float | None:
            try:
                return table[pid].create_time()
            except (KeyError, psutil.Error):
                return None

        self._known = {(pid, start): table[pid] for (pid, start), _ in self._known.items()
                       if pid in table and created(pid) == start}
        roots = {pid for pid, _ in self._known}
        if self._root_pid in table and (created(self._root_pid) or 0) >= since:
            roots.add(self._root_pid)
        roots |= {pid for pid, proc in table.items() if pid not in roots and self._is_scoped_cfx(proc, since)}

        def started_by(pid: int, parent: int) -> bool:
            start = created(pid)
            return start is not None and start >= since and start >= (created(parent) or 0)

        pairs = list(parents.items())
        found = set(roots)
        for pid in roots:
            found.update(descendants(pid, pairs, accept=started_by))
        for pid in found:
            start = created(pid)
            if start is not None:
                self._known[(pid, start)] = table[pid]

    def _is_scoped_cfx(self, proc: psutil.Process, since: float) -> bool:
        # the name is cached; start time and working directory are read only for CFX names
        if not self._scope or _name(proc).lower() not in CFX_PROCESS_NAMES:
            return False
        try:
            if proc.create_time() < since:
                return False
            directory = os.path.normcase(os.path.realpath(proc.cwd()))
        except (psutil.Error, OSError):
            return False
        return any(directory == scope or directory.startswith(scope.rstrip(os.sep) + os.sep)
                   for scope in (os.path.normcase(str(path)) for path in self._scope))

    # ------------------------------------------------------------ signals
    def _on_ready(self) -> None:
        process = self._process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput().data())
        text = self._decoder.decode(chunk)
        if not text:
            return
        self._buffer += text
        while "\n" in self._buffer:
            row, self._buffer = self._buffer.split("\n", 1)
            self.line.emit("out", row.rstrip("\r"))
        if len(self._buffer) > 8192:  # a solver writing one giant line
            self.line.emit("out", self._buffer)
            self._buffer = ""

    def _flush(self) -> None:
        tail = self._buffer + self._decoder.decode(b"", final=True)
        self._buffer = ""
        if tail.strip():
            for row in tail.splitlines():
                self.line.emit("out", row.rstrip("\r"))

    def _on_finished(self, exit_code: int, status) -> None:
        self._tracker.stop()
        self._flush()
        if not self._stopping:
            # The CLI ended by itself (possibly crashed): Stop is no longer
            # available, so say if anything it started is still running.
            self._track()
            alive = [proc for proc in self._known.values() if proc.pid != self._root_pid and _alive(proc)]
            if alive:
                self.line.emit("out", "[GUI] 命令已结束，但它启动的进程仍在运行：" + _listing(alive)
                               + "。请确认后在任务管理器中结束，再续跑。")
        reason = "normal"
        if self._stopping:
            reason = "stopped"
        elif status == QProcess.CrashExit:
            reason = "crashed"
        elif self._process is not None and self._process.error() == QProcess.Timedout:
            reason = "timeout"
        self.finished.emit(int(exit_code), reason)
        self._process = None

    def _on_error(self, error) -> None:
        if error == QProcess.FailedToStart:
            self._tracker.stop()
            self.failed.emit(f"无法启动进程：{self._command[0] if self._command else ''}")
            self._process = None


# --------------------------------------------------------------------------
# process tree
# --------------------------------------------------------------------------
def descendants(root: int, pairs: list[tuple[int, int]],
                accept: Callable[[int, int], bool] | None = None) -> list[int]:
    """PIDs started directly or indirectly by ``root``, from ``(pid, ppid)`` pairs.

    ``accept(pid, parent)`` can refuse a parent link; it is asked only for the
    processes reached from ``root``.
    """
    children: dict[int, list[int]] = {}
    for pid, ppid in pairs:
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    seen = {root}
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for pid in children.get(parent, []):
            if pid not in seen and (accept is None or accept(pid, parent)):
                seen.add(pid)
                found.append(pid)
                frontier.append(pid)
    return found


def ppid_map() -> dict[int, int]:
    """PID -> parent PID of every process, from one system snapshot.

    On Windows psutil's per-process ``ppid()`` takes a snapshot of all
    processes each time; ``_ppid_map`` is what ``Process.children()`` uses.
    """
    snapshot = getattr(psutil, "_ppid_map", None)
    if snapshot is not None:
        return dict(snapshot())
    return {proc.pid: proc.info["ppid"] for proc in psutil.process_iter(["ppid"])}


def _name(proc: psutil.Process) -> str:
    return (getattr(proc, "info", {}) or {}).get("name") or "?"


def _alive(proc: psutil.Process) -> bool:
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _listing(processes: list[psutil.Process]) -> str:
    return "，".join(f"{_name(proc)} PID={proc.pid}" for proc in processes)


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------
def default_python() -> str:
    """Interpreter used for child processes (same one running the GUI)."""
    return sys.executable or "python"
