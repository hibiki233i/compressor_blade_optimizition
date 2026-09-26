"""Qt application bootstrap."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from . import icons, theme
from .context import APP_NAME, ORG_NAME, AppContext
from .main_window import MainWindow
from .project import DEFAULT_CONFIG_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blade_gui",
        description="Blade-shape active-learning desktop console.",
    )
    parser.add_argument("--config", default=None, help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）")
    parser.add_argument("--data-dir", default=None, help="覆盖 paths.output_dir，只读已有结果")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    QApplication.setAttribute(Qt.AA_DontShowIconsInMenus, False)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationDisplayName("Blade Shape · 主动学习控制台")
    app.setWindowIcon(icons.icon("target", theme.PALETTE["accent"], 64))
    app.setStyle("Fusion")
    app.setStyleSheet(theme.build_stylesheet())

    font = QFont()
    font.setPixelSize(13)
    app.setFont(font)

    config_path = Path(args.config) if args.config else None
    data_dir = Path(args.data_dir) if args.data_dir else None
    context = AppContext(config_path, data_dir)
    window = MainWindow(context)
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
