"""Guards for the WindowConfigDialogsMixin extraction.

The known-hosts editor, preferences launcher, and config export/import methods
were moved verbatim out of window.py into sshpilot/window_dialogs.py as a mixin.
These checks ensure every method resolves to the mixin module (so no stray copy
in the window.py body silently shadows it) and the mixin is wired into the MRO.
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


_DIALOG_METHODS = (
    "show_known_hosts_editor",
    "show_preferences",
    "show_export_dialog",
    "show_import_dialog",
    "_show_import_mode_dialog",
    "_perform_import",
)


def test_config_dialog_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _DIALOG_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_dialogs", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_config_dialogs_mixin_in_mro():
    wm = _window_module()
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowConfigDialogsMixin" in mro_names
