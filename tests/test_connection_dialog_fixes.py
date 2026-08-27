import types
from sshpilot.connection_dialog import _editor_details_to_connection
from sshpilot.key_manager import SSHKey

def test_editor_details_to_connection_empty_hostname():
    details = types.SimpleNamespace(
        nickname='my-host',
        hostname='',
        host='my-host',
    )
    conn = _editor_details_to_connection(details)
    assert conn.hostname == ''  # Should not fallback to host


def test_editor_details_to_connection_preserves_display_name():
    details = types.SimpleNamespace(
        nickname='prod-server',
        display_name='Production Server',
        hostname='server.example',
    )
    conn = _editor_details_to_connection(details)
    assert conn.display_name == 'Production Server'

def test_editor_details_to_connection_plugin_data():
    details = types.SimpleNamespace(
        nickname='plugin-host',
        data={'plugin_field': 'value'}
    )
    conn = _editor_details_to_connection(details)
    assert conn.data == {'plugin_field': 'value'}

def test_editor_details_falsy_values():
    details = types.SimpleNamespace(
        nickname='my-host',
        port=0,
        x11_forwarding=False
    )
    conn = _editor_details_to_connection(details)
    assert conn.port == 0 or conn.port == 22
    assert conn.x11_forwarding is False


# ---------------------------------------------------------------------------
# Disk key discovery for the "Add keys" chooser
# ---------------------------------------------------------------------------
# Regression: ``_discover_disk_keys`` depended on the legacy
# ``ConnectionManager.load_ssh_keys`` surface, which was retired with the
# connection-store migration. The daemon-backed ``ConnectionPresentationStore``
# deliberately exposes no such method, so the guard silently returned an empty
# list and the chooser always showed "No private keys found in ~/.ssh".
# Discovery is daemon-owned: the dialog must use the parent's ``KeyManager``.


class _FakeKeyManager:
    def __init__(self, keys):
        self._keys = keys

    def discover_keys(self):
        return list(self._keys)


def _discover_disk_keys(parent):
    """Invoke the production method without constructing the GTK dialog."""
    from sshpilot.connection_dialog import ConnectionDialog

    method = ConnectionDialog.__dict__["_discover_disk_keys"]
    return types.MethodType(method, types.SimpleNamespace(parent_window=parent))()


def test_dialog_disk_key_discovery_uses_daemon_key_manager():
    manager = _FakeKeyManager([
        SSHKey("/home/alice/.ssh/id_ed25519"),
        SSHKey("/home/alice/.ssh/work_rsa", name="work_rsa"),
    ])
    parent = types.SimpleNamespace(key_manager=manager, client=None)

    assert _discover_disk_keys(parent) == [
        ("id_ed25519", "/home/alice/.ssh/id_ed25519"),
        ("work_rsa", "/home/alice/.ssh/work_rsa"),
    ]


def test_dialog_disk_key_discovery_empty_without_key_manager():
    parent = types.SimpleNamespace(key_manager=None, client=None)
    assert _discover_disk_keys(parent) == []


def test_dialog_disk_key_discovery_does_not_construct_before_mode_confirmation():
    """A client alone is insufficient to create a default-scope key manager."""
    parent = types.SimpleNamespace(
        key_manager=None,
        client=types.SimpleNamespace(),
        _key_scope="isolated",
    )

    assert _discover_disk_keys(parent) == []


def test_dialog_disk_key_discovery_swallows_failures():
    class _Boom:
        def discover_keys(self):
            raise OSError("boom")

    parent = types.SimpleNamespace(key_manager=_Boom(), client=None)
    assert _discover_disk_keys(parent) == []


# ---------------------------------------------------------------------------
# Browse / file chooser parentage (issue #1103)
# ---------------------------------------------------------------------------
# Regression: ``_browse_file`` parented the ``Gtk.FileDialog`` to
# ``self.get_transient_for()`` — the MainWindow — while the ConnectionDialog is
# a modal window stacked above it. On Wayland portal stacks the chooser then
# opens invisibly behind the modal dialog, so Browse appeared to do nothing.
# The chooser must be parented to the window the user is looking at (``self``).


def _browse_file(self, title="Pick a file", on_chosen=None, filters=None):
    """Invoke the production method without constructing the GTK dialog."""
    from sshpilot.connection_dialog import ConnectionDialog

    method = ConnectionDialog.__dict__["_browse_file"]
    return types.MethodType(method, self)(title, on_chosen, filters=filters)


def test_browse_file_parents_dialog_to_self(monkeypatch, tmp_path):
    """The chooser is parented to the dialog itself, never its transient-for
    window (the reach-across that hid the chooser behind the modal dialog)."""
    from sshpilot import connection_dialog as cd

    opened = []
    initial_folders = []

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            pass

        def set_initial_folder(self, gfile):
            initial_folders.append(gfile)

        def set_filters(self, filters):
            pass

        def open(self, parent, _cancellable, _callback):
            opened.append(parent)

    monkeypatch.setattr(cd.Gtk, "FileDialog", _FakeDialog)
    monkeypatch.setattr(cd, "get_ssh_dir", lambda: str(tmp_path))

    # The transient-for window is what the old code reached across to.
    main_window = cd.Gtk.Window.__new__(cd.Gtk.Window)
    self = types.SimpleNamespace(get_transient_for=lambda: main_window)

    _browse_file(self, on_chosen=lambda path: None)

    assert opened == [self], (
        "File chooser must be parented to the dialog itself, not to "
        "get_transient_for()"
    )
    assert initial_folders, "initial folder should still be the SSH directory"


def test_browse_file_delivers_chosen_path(monkeypatch, tmp_path):
    """A successful portal selection still flows through to ``on_chosen``."""
    from sshpilot import connection_dialog as cd

    chosen = []
    holder = {}

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            pass

        def set_initial_folder(self, gfile):
            pass

        def set_filters(self, filters):
            pass

        def open(self, parent, _cancellable, callback):
            holder["dialog"] = self
            holder["callback"] = callback

        def open_finish(self, _result):
            return types.SimpleNamespace(
                get_path=lambda: "/home/alice/.ssh/id_ed25519")

    monkeypatch.setattr(cd.Gtk, "FileDialog", _FakeDialog)
    monkeypatch.setattr(cd, "get_ssh_dir", lambda: str(tmp_path))
    self = types.SimpleNamespace(
        get_transient_for=lambda: cd.Gtk.Window.__new__(cd.Gtk.Window))

    _browse_file(self, on_chosen=chosen.append)
    holder["callback"](holder["dialog"], object())

    assert chosen == ["/home/alice/.ssh/id_ed25519"]


def test_browse_key_uses_explicit_parent_when_given(monkeypatch, tmp_path):
    """``_browse_key``/``_browse_file`` must parent the chooser to an explicit
    ``parent`` when one is supplied, not fall back to ``self`` (ConnectionDialog).

    This is the regression from issue #1103: the key-selection flow opens
    ``KeyChooserDialog`` *above* ConnectionDialog, and its "Browse..." row
    calls back into ConnectionDialog._browse_key. Always parenting to
    ConnectionDialog reopens the file chooser one window layer below the
    modal the user is actually looking at, so it appears hidden again."""
    from sshpilot import connection_dialog as cd
    from sshpilot.connection_dialog import ConnectionDialog

    opened = []

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            pass

        def set_initial_folder(self, gfile):
            pass

        def set_filters(self, filters):
            pass

        def open(self, parent, _cancellable, _callback):
            opened.append(parent)

    monkeypatch.setattr(cd.Gtk, "FileDialog", _FakeDialog)
    monkeypatch.setattr(cd, "get_ssh_dir", lambda: str(tmp_path))

    connection_dialog_self = types.SimpleNamespace(
        get_transient_for=lambda: cd.Gtk.Window.__new__(cd.Gtk.Window))
    connection_dialog_self._browse_file = types.MethodType(
        ConnectionDialog.__dict__["_browse_file"], connection_dialog_self)
    key_chooser_dialog = types.SimpleNamespace(name="key-chooser")

    browse_key = types.MethodType(
        ConnectionDialog.__dict__["_browse_key"], connection_dialog_self)
    browse_key(lambda path: None, key_chooser_dialog)

    assert opened == [key_chooser_dialog], (
        "Browsing from within KeyChooserDialog must parent the file "
        "chooser to KeyChooserDialog (the visible topmost window), not "
        "to ConnectionDialog underneath it"
    )


def test_key_chooser_dialog_passes_itself_as_browse_parent():
    """KeyChooserDialog._on_browse_clicked must hand itself to the on_browse
    callback so the eventual file chooser parents to the dialog the user is
    actually looking at."""
    from sshpilot.connection_dialog import KeyChooserDialog

    calls = []

    def fake_on_browse(chosen, parent):
        calls.append(parent)

    self = types.SimpleNamespace(
        _on_add=lambda path: None,
        _on_browse=fake_on_browse,
        close=lambda: None,
    )

    method = KeyChooserDialog.__dict__["_on_browse_clicked"]
    types.MethodType(method, self)()

    assert calls == [self], (
        "KeyChooserDialog must pass itself as the parent for the browse "
        "callback, not rely on the callback defaulting to some other window"
    )


# ---------------------------------------------------------------------------
# "Browse…" row must be clickable on libadwaita < 1.6 (issue #1103)
# ---------------------------------------------------------------------------
# Regression: the key chooser's Browse row prefers ``Adw.ButtonRow``
# (libadwaita >= 1.6) and falls back to ``Adw.ActionRow`` on older runtimes such
# as Ubuntu 24.04 (libadwaita 1.5). ``Adw.ActionRow`` defaults to
# ``activatable=False``, and GtkListBox skips ::row-activated for
# non-activatable rows — so the "activated" handler never fired and Browse did
# nothing at all, silently, no matter how the file chooser was parented.

import pytest


class _FakeRow:
    """Minimal stand-in for Adw.ActionRow recording activation wiring."""

    def __init__(self, title=None, **_kwargs):
        self.title = title
        self.activatable = False
        self.handlers = {}
        self.prefixes = []

    def set_activatable(self, value):
        self.activatable = bool(value)

    def add_prefix(self, widget):
        self.prefixes.append(widget)

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def get_title(self):
        return self.title


class _FakeGroup:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def _adw_stub_without_button_row(monkeypatch):
    """Patch ``connection_dialog.Adw`` to emulate libadwaita < 1.6."""
    from sshpilot import connection_dialog

    class _Adw:
        PreferencesGroup = _FakeGroup
        ActionRow = _FakeRow

        def __getattr__(self, name):  # pragma: no cover - defensive
            raise AttributeError(name)

    stub = _Adw()
    monkeypatch.setattr(connection_dialog, 'Adw', stub)
    return stub


def _build_disk_page_rows(monkeypatch, on_browse):
    from sshpilot import connection_dialog
    from sshpilot.connection_dialog import KeyChooserDialog

    _adw_stub_without_button_row(monkeypatch)
    monkeypatch.setattr(
        connection_dialog.Gtk, 'Image',
        types.SimpleNamespace(new_from_icon_name=lambda name: name),
        raising=False,
    )

    captured = {}

    self = types.SimpleNamespace(
        _on_browse=on_browse,
        _existing=set(),
        _checks=[],
        _placeholder_row=lambda text: _FakeRow(title=text),
        _wrap_group=lambda group: captured.setdefault('group', group),
        _on_browse_clicked=lambda *_a: None,
    )
    types.MethodType(KeyChooserDialog.__dict__['_build_disk_page'], self)([])
    return captured['group'].rows


def _find_browse_row(rows):
    for row in rows:
        if 'Browse' in (row.get_title() or ''):
            return row
    return None


def test_browse_row_is_activatable_on_old_libadwaita(monkeypatch):
    """Without Adw.ButtonRow the AdwActionRow fallback must be made
    activatable, or clicking Browse is a silent no-op."""
    rows = _build_disk_page_rows(monkeypatch, lambda chosen, parent: None)
    browse = _find_browse_row(rows)

    assert browse is not None, "Browse row missing from the disk page"
    assert browse.activatable, (
        "The Adw.ActionRow fallback must set activatable=True; GtkListBox "
        "never emits ::row-activated for a non-activatable row, so Browse "
        "silently does nothing on libadwaita < 1.6 (Ubuntu 24.04)"
    )
    assert 'activated' in browse.handlers


@pytest.mark.gui
def test_browse_row_activation_reaches_callback_with_real_gtk(monkeypatch):
    """End-to-end signal path on real widgets: activating the fallback row in
    its GtkListBox must invoke the browse callback."""
    from gi.repository import Adw, Gtk

    if not getattr(Adw.ActionRow, '__module__', '').startswith('gi.repository'):
        pytest.skip('GTK is stubbed (headless/CI); real PyGObject not loaded')

    from sshpilot import connection_dialog
    from sshpilot.connection_dialog import KeyChooserDialog

    class _AdwWithoutButtonRow:
        def __getattr__(self, name):
            if name == 'ButtonRow':
                raise AttributeError(name)
            return getattr(Adw, name)

    monkeypatch.setattr(connection_dialog, 'Adw', _AdwWithoutButtonRow())

    parents = []
    self = types.SimpleNamespace(
        _on_browse=lambda chosen, parent: parents.append(parent),
        _on_add=lambda path: None,
        close=lambda: None,
        _existing=set(),
        _checks=[],
    )
    for name in ('_build_disk_page', '_wrap_group', '_placeholder_row',
                 '_on_browse_clicked'):
        setattr(self, name, types.MethodType(KeyChooserDialog.__dict__[name], self))

    page = self._build_disk_page([])

    def _walk(widget):
        child = widget.get_first_child()
        while child is not None:
            yield child
            yield from _walk(child)
            child = child.get_next_sibling()

    listbox = next(w for w in _walk(page) if isinstance(w, Gtk.ListBox))
    row = next(w for w in _walk(page)
               if isinstance(w, Adw.PreferencesRow) and 'Browse' in (w.get_title() or ''))

    assert row.get_activatable()
    listbox.emit('row-activated', row)

    assert parents == [self], "Activating the Browse row must call on_browse"
