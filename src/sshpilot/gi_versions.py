"""The GI namespace versions the GUI is built against, pinned in one place.

Called before anything imports ``gi.repository`` modules: by ``sshpilot.main``
and by the early daemon start, which reads the language preference through
GLib before ``main`` is imported.
"""

import sys


def require_gui_versions() -> None:
    if sys.platform == "win32":
        # Must run before ``import gi``: gvsbuild's PyGObject only add_dll_directory
        # for girepository-1.0-1.dll, which the frozen bundle does not ship.
        from .platform.windows.gtk_runtime import configure

        configure()
    import gi

    gi.require_version('Adw', '1')
    gi.require_version('Gtk', '4.0')
    try:
        gi.require_version('Vte', '3.91')
    except ValueError:  # Windows: no VTE; the pyte backend is used instead
        pass
