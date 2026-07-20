"""Shared, backend-agnostic primitives for the file manager.

Used by the OpenSSH SFTP backend (``openssh_backend``) and the window/pane UI.
Importing this module pulls in no SSH library — it is just dataclasses and the
GLib main-thread dispatcher.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Optional

from gi.repository import GLib


@dataclasses.dataclass
class FileEntry:
    """Light weight description of a directory entry."""

    name: str
    is_dir: bool
    size: int
    modified: float
    item_count: Optional[int] = None  # Number of items in directory (for folders only)


class _MainThreadDispatcher:
    """Helper that marshals callbacks back to the GTK main loop."""

    @staticmethod
    def dispatch(func: Callable, *args, **kwargs) -> None:
        GLib.idle_add(lambda: func(*args, **kwargs))
