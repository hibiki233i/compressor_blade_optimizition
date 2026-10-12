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

import psutil
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from .commands import format_command

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
        self._stopping = False
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

    def command_text(self) -> str:
        return format_command(self._command)

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
        self._stopping = True
        self._track()
        root = int(process.processId())
        targets = sorted(self._known.values(), key=lambda proc: proc.pid == root)  # the CLI last
        names = Counter(_name(proc) for proc in targets)
        self.line.emit("out", "[GUI] 停止：结束本次运行启动的 {} 个进程（{}）".format(
            len(targets), "，".join(f"{name}×{count}" for name, count in sorted(names.items()))))
        # terminate() is TerminateProcess on Windows; console programs ignore
        # QProcess.terminate() there, which only posts WM_CLOSE.
        self._signal(targets, kill=False)
        if wait:
            # Never wait on the CLI through psutil: reaping it would hide its
            # exit from QProcess.
            others = [proc for proc in targets if proc.pid != root]
            psutil.wait_procs(others, timeout=STOP_GRACE_MS / 1000)
            if not process.waitForFinished(STOP_GRACE_MS):
                process.kill()
                process.waitForFinished(STOP_GRACE_MS)
            self._finish_stop(targets, root)
        else:
            QTimer.singleShot(STOP_GRACE_MS, lambda: self._kill_if_running(process, targets, root))

    def _kill_if_running(self, process: QProcess, targets: list[psutil.Process], root: int) -> None:
        try:
            if process.state() != QProcess.NotRunning:
                process.kill()
            self._finish_stop(targets, root)
        except RuntimeError:  # the runner or QProcess was already deleted
            pass

    def _finish_stop(self, targets: list[psutil.Process], root: int) -> None:
        """Kill what outlived the grace period and say whether anything survived."""
        others = [proc for proc in targets if proc.pid != root]
        self._signal(others, kill=True)
        _, alive = psutil.wait_procs(others, timeout=1)
        if alive:
            self.line.emit("out", "[GUI] 仍未结束的进程：" + "，".join(
                f"{_name(proc)} PID={proc.pid}" for proc in alive) + "。请在任务管理器中结束后再续跑。")
        else:
            self.line.emit("out", "[GUI] 本次运行启动的进程已全部结束。")

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
        signal a PID that has been reused since. CFX processes outside the tree
        are picked up by name inside the run's directories.
        """
        process = self._process
        if process is None or process.state() == QProcess.NotRunning:
            return
        table = {proc.pid: proc for proc in psutil.process_iter(["ppid", "name", "create_time"])}
        pairs = [(pid, proc.info["ppid"]) for pid, proc in table.items() if proc.info["ppid"] is not None]
        self._known = {(pid, created): proc for (pid, created), proc in self._known.items()
                       if pid in table and table[pid].info["create_time"] == created}
        roots = {int(process.processId())} | {pid for pid, _ in self._known}
        roots |= {pid for pid, proc in table.items() if self._is_scoped_cfx(proc)}
        found = set(roots)
        for pid in roots:
            found.update(descendants(pid, pairs))
        for pid in found:
            proc = table.get(pid)
            if proc is not None and proc.info["create_time"] is not None:
                self._known[(pid, proc.info["create_time"])] = proc

    def _is_scoped_cfx(self, proc: psutil.Process) -> bool:
        if not self._scope or _name(proc).lower() not in CFX_PROCESS_NAMES:
            return False
        if (proc.info["create_time"] or 0) < self._started_at:
            return False
        try:
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
            self.failed.emit(f"无法启动进程：{self._command[0] if self._command else ''}")
            self._process = None


# --------------------------------------------------------------------------
# process tree
# --------------------------------------------------------------------------
def descendants(root: int, pairs: list[tuple[int, int]]) -> list[int]:
    """PIDs started directly or indirectly by ``root``, from ``(pid, ppid)`` pairs."""
    children: dict[int, list[int]] = {}
    for pid, ppid in pairs:
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    seen = {root}
    frontier = [root]
    while frontier:
        for pid in children.get(frontier.pop(), []):
            if pid not in seen:
                seen.add(pid)
                found.append(pid)
                frontier.append(pid)
    return found


def _name(proc: psutil.Process) -> str:
    return (getattr(proc, "info", {}) or {}).get("name") or "?"


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------
def default_python() -> str:
    """Interpreter used for child processes (same one running the GUI)."""
    return sys.executable or "python"


def python_argv(entry_script: str, arguments: list[str]) -> list[str]:
    return [default_python(), "-u", entry_script, *arguments]


def child_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()}
    env["PYTHONIOENCODING"] = "utf-8"
    return env
