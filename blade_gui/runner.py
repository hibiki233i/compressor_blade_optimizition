"""Thin :class:`QProcess` wrapper that streams CLI output line by line.

The GUI never imports the optimizer to execute it: every action spawns the
original command-line entry point in a child process, so a crash or a blocked
solver can never take the window down with it.
"""
from __future__ import annotations

import codecs
import os
import shlex
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


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

    # ------------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    @property
    def command(self) -> list[str]:
        return list(self._command)

    def command_text(self) -> str:
        return " ".join(shlex.quote(part) for part in self._command)

    # -------------------------------------------------------------- start
    def start(self, argv: list[str], extra_env: dict[str, str] | None = None) -> bool:
        if self.running:
            self.failed.emit("已有任务正在运行。")
            return False
        if not argv:
            self.failed.emit("命令为空。")
            return False

        self._command = list(argv)
        self._decoder.reset()
        self._buffer = ""

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
        return True

    def stop(self) -> None:
        process = self._process
        if process is None or process.state() == QProcess.NotRunning:
            return
        process.terminate()
        # Console children ignore terminate() on Windows. Escalate later instead
        # of blocking the event loop (the old waitForFinished froze the window).
        QTimer.singleShot(2500, lambda: self._kill_if_running(process))

    @staticmethod
    def _kill_if_running(process: QProcess) -> None:
        try:
            if process.state() != QProcess.NotRunning:
                process.kill()
        except RuntimeError:  # the QProcess was already deleted
            pass

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
        self._flush()
        reason = "normal"
        if status == QProcess.CrashExit:
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
