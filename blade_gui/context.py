"""Shared application state and cross-page signals."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from .project import DEFAULT_CONFIG_PATH, Project

ORG_NAME = "BladeShape"
APP_NAME = "ActiveLearningGUI"


class AppContext(QObject):
    """Owns the :class:`Project` and broadcasts every change to the pages."""

    project_reloaded = Signal()
    status_message = Signal(str)
    navigate = Signal(str)  # page key
    case_requested = Signal(str)  # run_id

    def __init__(self, config_path: str | Path | None = None, data_dir: str | Path | None = None):
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        stored_config = self._read_setting("config_path")
        stored_data = self._read_setting("data_dir")

        self.config_path = Path(config_path or stored_config or DEFAULT_CONFIG_PATH)
        override = data_dir if data_dir is not None else (stored_data or None)
        self.data_dir = Path(override) if override else None
        self.project = Project(self.config_path, self.data_dir)

    # QSettings can be unavailable (read-only preferences); never let that crash the GUI.
    def _read_setting(self, key: str) -> str:
        try:
            return self.settings.value(key, "", type=str)
        except Exception:  # noqa: BLE001
            return ""

    def _write_setting(self, key: str, value: str) -> None:
        try:
            self.settings.setValue(key, value)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- state
    def set_config_path(self, path: str | Path) -> None:
        self.config_path = Path(path)
        self._write_setting("config_path", str(self.config_path))
        self.reload(keep_data_dir=True)

    def set_data_dir(self, path: str | Path | None) -> None:
        self.data_dir = Path(path) if path else None
        self._write_setting("data_dir", str(self.data_dir) if self.data_dir else "")
        self.reload(keep_data_dir=True)

    def reload(self, *, keep_data_dir: bool = False) -> None:
        self.project = Project(self.config_path, self.data_dir)
        self.project_reloaded.emit()
        self.status_message.emit(
            f"已刷新 · 配置 {self.project.config_path.name} · 输出目录 {self.project.output_dir}"
        )

    def report(self, message: str) -> None:
        self.status_message.emit(message)
