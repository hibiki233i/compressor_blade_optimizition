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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blade_gui",
        description="Blade-shape active-learning desktop console.",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（省略时使用上次选择的配置；首次启动为空）")
    parser.add_argument("--data-dir", default=None, help="覆盖 paths.output_dir，只读已有结果")
    return parser


def configure_application(app: QApplication) -> None:
    """Apply style, fonts and the style sheet (shared by main() and screenshots)."""
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationDisplayName("Blade Shape · 主动学习控制台")
    app.setWindowIcon(icons.brand_icon(64))
    app.setStyle("Fusion")
    app.setPalette(theme.application_palette())
    families = theme.available_families(theme.UI_FAMILIES)
    font = QFont()
    if families:
        font.setFamilies(families)
    font.setPixelSize(13)
    app.setFont(font)
    app.setStyleSheet(theme.build_stylesheet(theme.ensure_assets(), families or None))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    QApplication.setAttribute(Qt.AA_DontShowIconsInMenus, False)
    app = QApplication(sys.argv[:1])
    configure_application(app)

    config_path = Path(args.config) if args.config else None
    data_dir = Path(args.data_dir) if args.data_dir else None
    context = AppContext(config_path, data_dir)
    window = MainWindow(context)
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
