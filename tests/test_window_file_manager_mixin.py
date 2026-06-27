"""Guards for the WindowFileManagerMixin extraction.

The manage-files flow methods were moved verbatim out of window.py into
sshpilot/window_file_manager.py as a mixin. This ensures every method resolves
to the mixin module (so no stray copy in the window.py body silently shadows
it), the mixin precedes WindowActions in the MRO (so its on_manage_files_action
wins over the now-removed dead duplicate that used to live in WindowActions),
and that dead duplicate stays gone.
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


_FM_METHODS = (
    "on_manage_files_action",
    "_open_manage_files_for_connection",
    "_open_manage_files_now_for_connection",
    "_should_prompt_file_manager_choice",
    "_apply_file_manager_first_run_choice",
    "_should_prompt_operation_mode",
    "_prompt_backup_ssh_config",
    "_apply_operation_mode_choice",
    "_show_operation_mode_dialog",
    "_show_file_manager_first_run_dialog",
    "_track_internal_file_manager_window",
    "_create_file_manager_placeholder_tab",
    "_replace_placeholder_tab_content",
    "_register_file_manager_tab",
)


def test_fm_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _FM_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_file_manager", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_fm_mixin_precedes_window_actions_in_mro():
    wm = _window_module()
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowFileManagerMixin" in mro_names
    assert mro_names.index("WindowFileManagerMixin") < mro_names.index("WindowActions")


def test_dead_manage_files_copy_removed_from_window_actions():
    wm = _window_module()
    actions_cls = next(c for c in wm.MainWindow.__mro__ if c.__name__ == "WindowActions")
    assert "on_manage_files_action" not in vars(actions_cls)
