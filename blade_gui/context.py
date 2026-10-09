"""Shared application state and cross-page signals."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from .project import Project

ORG_NAME = "BladeShape"
APP_NAME = "ActiveLearningGUI"
#: point the GUI at another settings file (tests, portable installs)
SETTINGS_ENV = "BLADE_GUI_SETTINGS"


def open_settings(path: str | Path | None = None) -> QSettings:
    """A plain INI file the user can inspect or delete.

    Default location: ``%APPDATA%\\BladeShape\\ActiveLearningGUI.ini`` on Windows,
    ``~/.config/BladeShape/ActiveLearningGUI.ini`` elsewhere.
    """
    path = path or os.environ.get(SETTINGS_ENV)
    if path:
        return QSettings(str(path), QSettings.IniFormat)
    settings = QSettings(QSettings.IniFormat, QSettings.UserScope, ORG_NAME, APP_NAME)
    if not settings.contains("config_path"):
        # one-time import of the choice stored by earlier GUI versions (native format)
        legacy = QSettings(ORG_NAME, APP_NAME)
        for key in ("config_path", "data_dir"):
            value = legacy.value(key, "", type=str)
            if value:
                settings.setValue(key, value)
    return settings


class AppContext(QObject):
    """Owns the :class:`Project` and broadcasts every change to the pages."""

    project_reloaded = Signal()
    status_message = Signal(str)
    navigate = Signal(str)  # page key
    case_requested = Signal(str)  # run_id

    def __init__(self, config_path: str | Path | None = None, data_dir: str | Path | None = None,
                 *, settings_path: str | Path | None = None):
        super().__init__()
        self.command_runners = []
        try:
            self.settings = open_settings(settings_path)
        except Exception:  # noqa: BLE001
            self.settings = None
        stored_config = self.read_setting("config_path")
        stored_data = self.read_setting("data_dir")

        # no built-in default: the first launch starts empty until a config is chosen
        chosen = config_path or stored_config
        self.config_path = Path(chosen) if chosen else None
        override = data_dir if data_dir is not None else (stored_data or None)
        self.data_dir = Path(override) if override else None
        self.project = Project(self.config_path, self.data_dir)

    # QSettings can be unavailable (read-only preferences); never let that crash the GUI.
    def read_setting(self, key: str, default: str = "") -> str:
        try:
            value = self.settings.value(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else str(value)

    def write_setting(self, key: str, value) -> None:
        try:
            self.settings.setValue(key, value)
        except Exception:  # noqa: BLE001
            pass

    def read_bytes(self, key: str):
        """Binary blobs (window geometry, splitter state); ``None`` when absent."""
        try:
            value = self.settings.value(key)
        except Exception:  # noqa: BLE001
            return None
        return value if value is not None and not isinstance(value, str) else None

    def sync_settings(self) -> None:
        try:
            self.settings.sync()
        except Exception:  # noqa: BLE001
            pass

    def dialog_start(self, current: str = "") -> str:
        """Start file dialogs at the typed path, else where the last one was used."""
        return current or self.read_setting("dialog/last_dir", "")

    def remember_dialog(self, chosen: str | Path, *, is_dir: bool = False) -> None:
        path = Path(chosen)
        self.write_setting("dialog/last_dir", str(path if is_dir else path.parent))

    # ------------------------------------------------------------- state
    def set_config_path(self, path: str | Path) -> None:
        self.config_path = Path(path)
        self.write_setting("config_path", str(self.config_path))
        self.reload(keep_data_dir=True)

    def set_data_dir(self, path: str | Path | None) -> None:
        self.data_dir = Path(path) if path else None
        self.write_setting("data_dir", str(self.data_dir) if self.data_dir else "")
        self.reload(keep_data_dir=True)

    def reload(self, *, keep_data_dir: bool = False) -> None:
        self.project = Project(self.config_path, self.data_dir)
        self.project_reloaded.emit()
        if self.project.config_path is None:
            self.status_message.emit("尚未选择配置文件 · 点击侧栏「配置…」选择 JSON")
            return
        self.status_message.emit(
            f"已刷新 · 配置 {self.project.config_path.name} · 输出目录 {self.project.output_dir}"
        )

    def report(self, message: str) -> None:
        self.status_message.emit(message)
