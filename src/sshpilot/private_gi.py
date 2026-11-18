"""Load a GObject Introspection library installed privately for sshPilot.

The Linux packages install gtk4-frdp (RDP tab) and the gtkghostty terminal
widget into <libdir>/sshpilot rather than the shared library paths, so they
never clash with a system copy. GI does not look there by itself.
"""

from __future__ import annotations

import os
from typing import Sequence

import gi


def use_private_libdir(libdir: str, libraries: Sequence[str]) -> None:
    """Load ``libraries`` from ``libdir`` and put it ahead in GI's paths.

    The libraries load in order, so a dependency listed first (libghostty-vt
    before the widget) is the copy the next one links to. Raises OSError when
    one cannot load (a missing system dependency): GI would merely warn and
    hand back a namespace whose types fail on first use.
    """

    import ctypes

    for library in libraries:
        ctypes.CDLL(os.path.join(libdir, library))
    typelibs = os.path.join(libdir, "girepository-1.0")
    repository = gi._gi.Repository.get_default()
    if hasattr(repository, "prepend_search_path"):  # PyGObject >= 3.52
        repository.prepend_search_path(typelibs)
        repository.prepend_library_path(libdir)
        return
    # Older PyGObject is built on libgirepository-1.0, where these are static.
    gi.require_version("GIRepository", "2.0")
    from gi.repository import GIRepository

    GIRepository.Repository.prepend_search_path(typelibs)
    GIRepository.Repository.prepend_library_path(libdir)
