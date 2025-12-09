"""Entry point for the Qt preview application."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

try:
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover - PyQt may not be installed in CI
    raise SystemExit(
        "PyQt6 is required for the Qt preview. Please install PyQt6 before continuing."
    ) from exc

from sshpilot import __version__
from sshpilot.platform_utils import get_data_dir

from .main_window import MainWindow
from .resources import load_icon

LOGGER = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Mirror the GTK logging strategy for the Qt entrypoint."""
    log_dir = get_data_dir()
    os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "sshpilot.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler()

    logging.getLogger().handlers.clear()
    root = logging.getLogger()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    level = logging.DEBUG if verbose else logging.INFO
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    console_handler.setLevel(level)
    root.setLevel(level)


def load_config() -> Optional[object]:
    try:
        from sshpilot.config import Config
    except Exception as exc:  # pragma: no cover - config is optional here
        LOGGER.warning("Could not load configuration backend: %s", exc)
        return None

    try:
        return Config()
    except Exception as exc:  # pragma: no cover - best-effort load
        LOGGER.warning("Config initialization failed: %s", exc)
        return None


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sshPilot Qt preview")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Placeholder flag for parity with the GTK entrypoint",
    )
    parser.add_argument(
        "--native-connect",
        action="store_true",
        help="Reserved for future parity with GTK backend",
    )
    return parser.parse_args(argv)


def bootstrap(argv: Optional[list[str]] = None) -> QApplication:
    args = parse_args(argv)
    setup_logging(verbose=args.verbose)

    QCoreApplication.setOrganizationName("mFat")
    QCoreApplication.setApplicationName("sshPilot")
    QCoreApplication.setApplicationVersion(__version__)

    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])

    icon = load_icon("sshpilot.png")
    if icon and isinstance(icon, QIcon):
        app.setWindowIcon(icon)

    return app


def main(argv: Optional[list[str]] = None) -> int:
    app = bootstrap(argv)
    window = MainWindow(config=load_config())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
