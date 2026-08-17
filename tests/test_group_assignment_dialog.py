"""Headless coverage for the "Move to Group" / "Copy to Group" dialog.

``_open_group_assignment_dialog`` (window_dialogs.py) is the shared method
behind both sidebar actions and, before this file, had zero test coverage.
It builds real Adw/Gtk widgets inline (not a separate dialog class), so it
can't be exercised under the suite's generic ``gi`` stub (every widget
construction there returns a bare, featureless ``object()``).

Instead, this file monkeypatches the small set of Adw/Gtk/Gdk symbols the
function actually touches with lightweight fakes that behave like the real
widgets closely enough to drive it end to end: ``connect``/``emit`` records
and replays signal handlers, entry rows track their text, color buttons
track their RGBA. That lets the tests click through the real production
code path (row selection, entry typing, Confirm) and assert on a recording
fake daemon client, proving:

* Move replaces the connection's group membership (assign_connection_to_group).
* Copy adds a membership while preserving the existing one
  (copy_connection_to_group).
* Both support creating a brand-new group inline (create_group, then the
  assign/copy step reusing the returned group id).
* Multiple selected connections are all moved/copied in one sequence.
* Typing an existing group's name (instead of picking its row) reuses that
  group rather than creating a duplicate.
"""

from types import SimpleNamespace

from sshpilot import window_dialogs


class _FakeRGBA:
    def __init__(self):
        self.red = self.green = self.blue = self.alpha = 0.0

    def to_string(self):
        return f"rgba({self.red},{self.green},{self.blue},{self.alpha})"


class _FakeWidget:
    """Generic stand-in for a Gtk/Adw widget.

    Records signal handlers (``connect``/``emit``) and swallows any
    layout/styling call it doesn't specifically model (via ``__getattr__``),
    so the dialog's many purely-cosmetic Gtk calls (set_valign,
    set_content_width, add_top_bar, ...) don't need individual stubs.
    """

    def __init__(self, *args, **kwargs):
        self._signals = {}
        self._css_classes = []
        self._sensitive = True
        self._visible = True
        self._title = kwargs.get("title", "")
        self._label = kwargs.get("label")

    def connect(self, signal, callback):
        self._signals.setdefault(signal, []).append(callback)
        return len(self._signals[signal])

    def emit(self, signal, *args):
        for callback in list(self._signals.get(signal, [])):
            callback(self, *args)

    def add_css_class(self, name):
        self._css_classes.append(name)

    def get_css_classes(self):
        return list(self._css_classes)

    def set_sensitive(self, value):
        self._sensitive = value

    def get_sensitive(self):
        return self._sensitive

    def set_visible(self, value):
        self._visible = value

    def get_visible(self):
        return self._visible

    def set_title(self, title):
        self._title = title

    def get_title(self):
        return self._title

    def get_label(self):
        return self._label

    def __getattr__(self, name):
        def _noop(*_args, **_kwargs):
            return None
        return _noop


class _FakeDialog(_FakeWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.presented = False
        self.closed = False

    def present(self, *_args, **_kwargs):
        self.presented = True

    def close(self):
        self.closed = True


class _FakeEntryRow(_FakeWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._text = ""

    def get_text(self):
        return self._text

    def set_text(self, value):
        self._text = value
        self.emit('changed')


class _FakeColorButton(_FakeWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rgba = _FakeRGBA()

    def set_rgba(self, rgba):
        self._rgba = rgba

    def get_rgba(self):
        return self._rgba


class _FakeImage(_FakeWidget):
    @staticmethod
    def new_from_icon_name(_name):
        return _FakeWidget()


class _AutoNamespace(SimpleNamespace):
    """SimpleNamespace that auto-vivifies unknown attributes.

    Covers enum-like lookups the dialog makes in passing (``Gtk.Align.CENTER``,
    ``Gtk.PolicyType.AUTOMATIC``) without hand-listing every constant — the
    dialog only ever hands these values back into no-op layout setters, so
    their concrete identity doesn't matter.
    """

    def __getattr__(self, name):
        value = _AutoNamespace()
        setattr(self, name, value)
        return value


class _FakeAdw(_AutoNamespace):
    pass


class _FakeGtk(_AutoNamespace):
    pass


def _make_fake_gi():
    fake_gtk = _FakeGtk(
        Button=_FakeWidget,
        ScrolledWindow=_FakeWidget,
        ColorButton=_FakeColorButton,
        Image=_FakeImage,
        PolicyType=SimpleNamespace(NEVER='never', AUTOMATIC='automatic'),
    )
    fake_adw = _FakeAdw(
        Dialog=_FakeDialog,
        ToolbarView=_FakeWidget,
        HeaderBar=_FakeWidget,
        PreferencesPage=_FakeWidget,
        PreferencesGroup=_FakeWidget,
        EntryRow=_FakeEntryRow,
        ActionRow=_FakeWidget,
        AlertDialog=_FakeDialog,
    )
    fake_gdk = SimpleNamespace(RGBA=_FakeRGBA)
    return fake_gtk, fake_adw, fake_gdk


def _patch_widgets(monkeypatch):
    fake_gtk, fake_adw, fake_gdk = _make_fake_gi()
    monkeypatch.setattr(window_dialogs, 'Gtk', fake_gtk)
    monkeypatch.setattr(window_dialogs, 'Adw', fake_adw)
    monkeypatch.setattr(window_dialogs, 'Gdk', fake_gdk)


class _RecordingGroupClient:
    """Fake daemon client mirroring the repository's real move/copy semantics.

    ``memberships`` is maintained the way the real repository would: assign
    replaces a connection's groups with a single one, copy only adds.
    """

    def __init__(self):
        self.calls = []
        self.memberships = {}
        self._next_group_id = 1

    def assign_connection_to_group(self, connection_id, group_id=""):
        self.calls.append(("assign_connection_to_group", connection_id, group_id))
        self.memberships[connection_id] = {group_id} if group_id else set()
        return True

    def copy_connection_to_group(self, request):
        self.calls.append(
            ("copy_connection_to_group", request.connection_id, request.group_id)
        )
        self.memberships.setdefault(request.connection_id, set()).add(request.group_id)
        return True

    def create_group(self, name, parent_id="", color=""):
        group_id = f"new-group-{self._next_group_id}"
        self._next_group_id += 1
        self.calls.append(("create_group", name, parent_id, color))
        return group_id


class _SyncController:
    """Runs run_sequence steps synchronously — no mainloop needed headlessly."""

    def __init__(self, client):
        self.client = client

    def run_sequence(self, steps, *, on_success, on_error):
        try:
            previous = None
            for step in steps:
                previous = step(previous)
        except Exception as exc:  # pragma: no cover - failure path
            on_error(exc)
            return
        on_success(previous)


class _FakeGroupManager:
    def __init__(self, controller, groups=None):
        self.controller = controller
        self._groups = groups or {}

    def get_all_groups(self):
        return list(self._groups.values())


class _DialogHost(window_dialogs.WindowConfigDialogsMixin):
    """Minimal stand-in for MainWindow: only what the dialog method touches."""

    def __init__(self, client, groups=None, connections=()):
        self.group_manager = _FakeGroupManager(_SyncController(client), groups)
        self._connections = list(connections)
        self.rebuild_calls = 0
        self.simple_dialogs = []

    def get_available_groups(self):
        return self.group_manager.get_all_groups()

    def _get_target_connections(self, prefer_context=False):
        return self._connections

    def _simple_dialog(self, heading, body):
        self.simple_dialogs.append((heading, body))

    def rebuild_connection_list(self):
        self.rebuild_calls += 1


def _group(gid, name):
    return {
        'id': gid, 'name': name, 'parent_id': None, 'children': [],
        'connections': [], 'expanded': True, 'order': 0, 'color': None,
    }


def _open(monkeypatch, host, *, is_copy):
    """Build the dialog and return (dialog, entry_row, action_rows, confirm_button).

    ``_open_group_assignment_dialog`` never exposes its widgets, so this
    stitches the created dialog together by intercepting the fake widget
    constructors that matter, in the order the production code builds them.
    """
    _patch_widgets(monkeypatch)

    created = {'entry': None, 'rows': [], 'buttons': [], 'dialog': None}

    real_entry_row = window_dialogs.Adw.EntryRow
    def entry_row(*a, **k):
        w = real_entry_row(*a, **k)
        created['entry'] = w
        return w
    window_dialogs.Adw.EntryRow = entry_row

    real_action_row = window_dialogs.Adw.ActionRow
    def action_row(*a, **k):
        w = real_action_row(*a, **k)
        created['rows'].append(w)
        return w
    window_dialogs.Adw.ActionRow = action_row

    real_button = window_dialogs.Gtk.Button
    def button(*a, **k):
        w = real_button(*a, **k)
        created['buttons'].append(w)
        return w
    window_dialogs.Gtk.Button = button

    real_dialog = window_dialogs.Adw.Dialog
    def dialog_ctor(*a, **k):
        w = real_dialog(*a, **k)
        created['dialog'] = w
        return w
    window_dialogs.Adw.Dialog = dialog_ctor

    if is_copy:
        host.on_copy_to_group_action(None, None)
    else:
        host.on_move_to_group_action(None, None)

    dialog = created['dialog']
    assert dialog is not None, 'dialog was not built'
    assert dialog.presented, 'dialog was not presented'

    # Three plain-labelled buttons exist (Cancel, Confirm, Clear-color); only
    # the real Move/Copy confirm button carries suggested-action.
    confirm = next(
        b for b in created['buttons'] if 'suggested-action' in b.get_css_classes()
    )
    return dialog, created['entry'], created['rows'], confirm


def test_move_to_existing_group_replaces_membership(monkeypatch):
    client = _RecordingGroupClient()
    client.memberships['demo'] = {'g0'}
    host = _DialogHost(
        client,
        groups={'g0': _group('g0', 'Source'), 'g1': _group('g1', 'Target')},
        connections=[SimpleNamespace(nickname='demo')],
    )

    dialog, _entry, rows, confirm = _open(monkeypatch, host, is_copy=False)
    target_row = next(r for r in rows if r.get_title() == 'Target')
    target_row.emit('activated')
    assert confirm.get_sensitive()

    confirm.emit('clicked')

    assert client.calls == [("assign_connection_to_group", "demo", "g1")]
    assert client.memberships['demo'] == {'g1'}, 'move must replace prior membership'
    assert dialog.closed
    assert host.rebuild_calls == 1


def test_copy_to_existing_group_preserves_membership(monkeypatch):
    client = _RecordingGroupClient()
    client.memberships['demo'] = {'g0'}
    host = _DialogHost(
        client,
        groups={'g0': _group('g0', 'Source'), 'g1': _group('g1', 'Target')},
        connections=[SimpleNamespace(nickname='demo')],
    )

    dialog, _entry, rows, confirm = _open(monkeypatch, host, is_copy=True)
    target_row = next(r for r in rows if r.get_title() == 'Target')
    target_row.emit('activated')

    confirm.emit('clicked')

    assert client.calls == [("copy_connection_to_group", "demo", "g1")]
    assert client.memberships['demo'] == {'g0', 'g1'}, 'copy must keep prior membership'
    assert dialog.closed


def test_move_to_new_group_creates_then_assigns(monkeypatch):
    client = _RecordingGroupClient()
    client.memberships['demo'] = {'g0'}
    host = _DialogHost(
        client,
        groups={'g0': _group('g0', 'Source')},
        connections=[SimpleNamespace(nickname='demo')],
    )

    dialog, entry, _rows, confirm = _open(monkeypatch, host, is_copy=False)
    entry.set_text('Brand New Group')
    assert confirm.get_sensitive()

    confirm.emit('clicked')

    assert client.calls == [
        ("create_group", "Brand New Group", "", ""),
        ("assign_connection_to_group", "demo", "new-group-1"),
    ]
    assert client.memberships['demo'] == {'new-group-1'}
    assert dialog.closed


def test_copy_to_new_group_creates_then_copies(monkeypatch):
    client = _RecordingGroupClient()
    client.memberships['demo'] = {'g0'}
    host = _DialogHost(
        client,
        groups={'g0': _group('g0', 'Source')},
        connections=[SimpleNamespace(nickname='demo')],
    )

    dialog, entry, _rows, confirm = _open(monkeypatch, host, is_copy=True)
    entry.set_text('Brand New Group')

    confirm.emit('clicked')

    assert client.calls == [
        ("create_group", "Brand New Group", "", ""),
        ("copy_connection_to_group", "demo", "new-group-1"),
    ]
    assert client.memberships['demo'] == {'g0', 'new-group-1'}


def test_move_multiple_selected_connections_between_groups(monkeypatch):
    client = _RecordingGroupClient()
    client.memberships['alpha'] = {'g0'}
    client.memberships['beta'] = {'g0'}
    host = _DialogHost(
        client,
        groups={'g0': _group('g0', 'Source'), 'g1': _group('g1', 'Target')},
        connections=[SimpleNamespace(nickname='alpha'), SimpleNamespace(nickname='beta')],
    )

    dialog, _entry, rows, confirm = _open(monkeypatch, host, is_copy=False)
    target_row = next(r for r in rows if r.get_title() == 'Target')
    target_row.emit('activated')

    confirm.emit('clicked')

    assert client.calls == [
        ("assign_connection_to_group", "alpha", "g1"),
        ("assign_connection_to_group", "beta", "g1"),
    ]
    assert client.memberships == {'alpha': {'g1'}, 'beta': {'g1'}}
    assert dialog.closed


def test_typing_an_existing_group_name_reuses_it_instead_of_creating_a_duplicate(monkeypatch):
    client = _RecordingGroupClient()
    client.memberships['demo'] = {'g0'}
    host = _DialogHost(
        client,
        groups={'g0': _group('g0', 'Source'), 'g1': _group('g1', 'Target')},
        connections=[SimpleNamespace(nickname='demo')],
    )

    dialog, entry, _rows, confirm = _open(monkeypatch, host, is_copy=False)
    # Case-insensitive match against the existing "Target" group.
    entry.set_text('target')

    confirm.emit('clicked')

    assert client.calls == [("assign_connection_to_group", "demo", "g1")]
    assert not any(call[0] == "create_group" for call in client.calls)
    assert dialog.closed
