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


def test_dialog_disk_key_discovery_builds_manager_from_client(monkeypatch):
    """Without a pre-built manager, a daemon-backed one is constructed."""
    from sshpilot import key_manager as key_manager_mod

    manager = _FakeKeyManager([SSHKey("/home/alice/.ssh/id_ed25519")])
    created = []

    def _factory(client, **kwargs):
        created.append((client, kwargs))
        return manager

    monkeypatch.setattr(key_manager_mod, "KeyManager", _factory)
    parent = types.SimpleNamespace(
        key_manager=None,
        client=types.SimpleNamespace(),
        _key_scope="isolated",
    )

    assert _discover_disk_keys(parent) == [
        ("id_ed25519", "/home/alice/.ssh/id_ed25519")
    ]
    assert len(created) == 1
    assert created[0][1]["scope"] == "isolated"


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
