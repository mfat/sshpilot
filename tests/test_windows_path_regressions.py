"""Windows path regressions: remote paths stay POSIX, local paths follow Windows.

Every test runs on every platform. Where the bug was an ``os.path`` call on a
remote path, ``ntpath`` stands in for the module's ``os.path``; where it was a
POSIX-only directory, ``sys.platform`` is set to ``win32`` for the resolver.
"""

import json
import ntpath
import os
import types
from pathlib import Path
from types import SimpleNamespace

from sshpilot.file_manager import properties_dialog
from sshpilot.file_manager.common import FileEntry
from sshpilot.file_manager.properties_dialog import PropertiesDialog
from sshpilot.platform import paths


def _windows_os_path(monkeypatch, module):
    """Give ``module`` an ``os`` whose ``path`` and separators are Windows'."""
    fake_os = types.ModuleType("os")
    fake_os.__dict__.update(os.__dict__)
    fake_os.path = ntpath
    fake_os.sep = ntpath.sep
    fake_os.altsep = ntpath.altsep
    monkeypatch.setattr(module, "os", fake_os)


def _windows_dirs(monkeypatch, tmp_path):
    """Resolve sshPilot's directories as on Windows, under ``tmp_path``."""
    monkeypatch.setattr(paths, "sys", SimpleNamespace(platform="win32"))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))


def _pane_module(module):
    return __import__(module.FilePane.__module__, fromlist=["_"])


def _remote_pane(module, monkeypatch, entries):
    _windows_os_path(monkeypatch, _pane_module(module))
    pane = module.FilePane.__new__(module.FilePane)
    pane._is_remote = True
    pane._current_path = "/srv/data"
    pane._entries = entries
    pane.emitted = []
    pane.emit = lambda *args: pane.emitted.append(args)
    pane.show_toast = lambda *args, **kwargs: None
    return pane


# --- file manager: remote paths ------------------------------------------


def test_opening_a_remote_folder_joins_with_a_slash(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    pane = _remote_pane(module, monkeypatch, [FileEntry("docs", True, 0, 0)])

    pane._navigate_to_entry(0)

    assert pane.emitted == [("path-changed", "/srv/data/docs")]


def test_remote_drag_payload_carries_posix_paths(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    entries = [FileEntry("a.txt", False, 0, 0), FileEntry("b.txt", False, 0, 0)]
    pane = _remote_pane(module, monkeypatch, entries)
    pane._selection_model = SimpleNamespace(is_selected=lambda index: True)
    monkeypatch.setattr(
        _pane_module(module).Gdk,
        "ContentProvider",
        SimpleNamespace(new_for_value=lambda value: value),
        raising=False,
    )
    widget = SimpleNamespace(drag_position=0)

    value = pane._on_drag_prepare(SimpleNamespace(get_widget=lambda: widget), 0.0, 0.0)

    payload = json.loads(value)["payload"]
    assert payload["entry_path"] == "/srv/data/a.txt"
    assert [item["entry_path"] for item in payload["entries"]] == [
        "/srv/data/a.txt",
        "/srv/data/b.txt",
    ]


def test_remote_to_local_drop_downloads_a_posix_source(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    entry = FileEntry("a.txt", False, 0, 0)
    source_pane = _remote_pane(module, monkeypatch, [entry])

    target_pane = module.FilePane.__new__(module.FilePane)
    target_pane._is_remote = False
    target_pane._current_path = "C:\\Users\\me"
    target_pane.show_toast = lambda *args, **kwargs: None
    target_pane._resolve_drop_target_folder = lambda *args, **kwargs: None
    downloads = []
    target_pane._handle_download_from_drag = (
        lambda items, target_folder=None: downloads.append(items)
    )

    DummyWindow = type("DummyWindow", (), {})
    monkeypatch.setattr(module, "FileManagerWindow", DummyWindow)
    window = DummyWindow()
    window._left_pane = target_pane
    window._right_pane = source_pane
    target_pane._get_file_manager_window = lambda: window

    # No entry_path: the drop rebuilds the source from the pane's directory.
    payload = {
        "pane_id": id(source_pane),
        "path": "/srv/data",
        "position": 0,
        "entry_name": "a.txt",
    }
    value = json.dumps({"format": "sshpilot_drag", "payload": payload})

    assert target_pane._on_drop_string(None, value, 0.0, 0.0) is True
    assert [(source, e.name) for source, e in downloads[0]] == [("/srv/data/a.txt", "a.txt")]


def test_remote_properties_location_joins_with_a_slash(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    entry = FileEntry("a.txt", False, 10, 0)
    pane = _remote_pane(module, monkeypatch, [entry])
    pane._format_size = lambda size: f"{size} B"

    details = pane._build_properties_details(entry)

    assert details["location"] == "/srv/data/a.txt"


# --- file manager: local Properties --------------------------------------


def _local_dialog(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    dialog = PropertiesDialog.__new__(PropertiesDialog)
    dialog._entry = FileEntry("notes.txt", False, 1, 0.0)
    dialog._entries = [dialog._entry]
    dialog._current_path = str(tmp_path)
    dialog._sftp_manager = None
    return dialog


def test_local_free_space_is_shown_without_statvfs(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "statvfs", raising=False)
    dialog = _local_dialog(tmp_path)
    shown = []
    dialog._show_free_space = shown.append

    dialog._fill_free_space()

    assert len(shown) == 1
    assert isinstance(shown[0], int) and shown[0] >= 0


def test_open_parent_launches_a_well_formed_file_uri(tmp_path, monkeypatch):
    dialog = _local_dialog(tmp_path)
    launched = []
    monkeypatch.setattr(
        properties_dialog.Gio,
        "File",
        SimpleNamespace(
            new_for_path=lambda path: SimpleNamespace(get_uri=lambda: Path(path).as_uri())
        ),
        raising=False,
    )
    monkeypatch.setattr(
        properties_dialog.Gio,
        "AppInfo",
        SimpleNamespace(launch_default_for_uri=lambda uri, _ctx: launched.append(uri)),
        raising=False,
    )

    dialog._on_open_parent()

    assert launched == [tmp_path.as_uri()]
    assert launched[0].startswith("file:///")


# --- per-user directories -------------------------------------------------


def test_askpass_reads_settings_from_the_app_config_dir(tmp_path, monkeypatch):
    from sshpilot import askpass_utils

    _windows_dirs(monkeypatch, tmp_path)
    config_dir = tmp_path / "Roaming" / "sshpilot"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({"use-askpass": False}))

    assert askpass_utils._askpass_enabled() is False


def test_user_plugins_live_in_the_local_app_data_dir(tmp_path, monkeypatch):
    from sshpilot.plugins.api import PluginContext
    from sshpilot.plugins.loader import _user_plugin_dir

    _windows_dirs(monkeypatch, tmp_path)
    context = PluginContext.__new__(PluginContext)
    context.plugin_id = "acme-vps"

    assert _user_plugin_dir() == tmp_path / "Local" / "sshpilot" / "plugins"
    assert Path(context.data_dir) == tmp_path / "Local" / "sshpilot" / "plugin-data" / "acme-vps"


# --- transfer progress ----------------------------------------------------


def _progress_dialog(monkeypatch, *, home):
    from sshpilot.file_manager import progress_dialog

    _windows_os_path(monkeypatch, progress_dialog)
    monkeypatch.setenv("USERPROFILE", home)
    dialog = progress_dialog.SFTPProgressDialog.__new__(progress_dialog.SFTPProgressDialog)
    dialog.current_file = "a.txt"
    return dialog


def test_progress_shortens_a_windows_home_to_tilde(monkeypatch):
    dialog = _progress_dialog(monkeypatch, home=r"C:\Users\me")

    shown = dialog._display_location(r"C:\Users\me\Downloads\a.txt", local=True)

    assert shown == r"~\Downloads"


def test_progress_keeps_remote_locations_posix(monkeypatch):
    dialog = _progress_dialog(monkeypatch, home=r"C:\Users\me")

    assert dialog._display_location("/srv/data/a.txt", local=False) == "/srv/data"


# --- backup restore -------------------------------------------------------


def _windows_backup_manager(monkeypatch):
    from sshpilot import backup_manager

    _windows_os_path(monkeypatch, backup_manager)
    return backup_manager


def test_linux_backup_config_is_rebased_onto_a_windows_home(monkeypatch):
    bm = _windows_backup_manager(monkeypatch)
    text = (
        "Host web\n"
        "    IdentityFile /home/alice/.ssh/id_ed25519\n"
        "    RemoteCommand tail -f /home/alice/app.log\n"
    )

    out = bm._rebase_home_in_text(text, "/home/alice", r"C:\Users\bob")

    assert "IdentityFile C:/Users/bob/.ssh/id_ed25519\n" in out
    assert "RemoteCommand tail -f /home/alice/app.log\n" in out


def test_windows_backup_config_is_rebased_with_either_separator(monkeypatch):
    bm = _windows_backup_manager(monkeypatch)
    text = (
        "Host web\n"
        r"    IdentityFile C:\Users\alice\.ssh\id_ed25519" "\n"
        "    Include C:/Users/alice/.ssh/extra.conf\n"
    )

    out = bm._rebase_home_in_text(text, r"C:\Users\alice", r"C:\Users\bob")

    assert r"IdentityFile C:\Users\bob\.ssh\id_ed25519" "\n" in out
    assert "Include C:/Users/bob/.ssh/extra.conf\n" in out
