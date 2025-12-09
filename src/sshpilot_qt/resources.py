"""Helpers for loading icons and styles for the Qt preview."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

try:  # Optional dependency: PyQt6
    from PyQt6.QtGui import QIcon
except ImportError:  # pragma: no cover - PyQt may not be installed in CI
    QIcon = None  # type: ignore

LOGGER = logging.getLogger(__name__)


def _resource_roots() -> Iterable[Path]:
    """Return possible base paths that contain shipped assets."""
    here = Path(__file__).resolve().parent
    yield here / ".." / "sshpilot" / "resources"
    yield Path.cwd() / "sshpilot" / "resources"


def resolve_asset_path(*parts: str) -> Optional[str]:
    """Return the first existing asset path for the requested file."""
    relative = Path(*parts)
    for root in _resource_roots():
        candidate = (root / relative).resolve()
        if candidate.exists():
            return str(candidate)
    LOGGER.warning("Asset not found: %s", relative)
    return None


def load_icon(name: str) -> Optional["QIcon"]:
    """Load an icon from shipped assets if PyQt6 is available."""
    if QIcon is None:
        LOGGER.debug("PyQt6 is not available; skipping icon load for %s", name)
        return None

    icon_path = resolve_asset_path("icons", name) or resolve_asset_path(name)
    if icon_path:
        return QIcon(icon_path)
    return None


def load_stylesheet(name: str) -> str:
    """Load a stylesheet from disk if present."""
    stylesheet_path = resolve_asset_path(name)
    if stylesheet_path is None:
        return ""

    try:
        return Path(stylesheet_path).read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem errors are non-critical
        LOGGER.warning("Failed to read stylesheet %s: %s", stylesheet_path, exc)
        return ""
