from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from sshpilot import daemon_interaction_dialogs as dialogs_mod
from sshpilot.api import Capability
from sshpilot.api.events import EventType
from sshpilot.api.interaction_identity import new_interaction_id
from sshpilot.api.models import (
    InteractionState,
    InteractionSummary,
    InteractionType,
    PasswordPrompt,
    SessionId,
    TransferState,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.operations import SftpServiceState
from sshpilot.scp_window import ScpWindowController


class _Bridge:
    def __init__(self):
        self.calls = []

    def submit(self, operation, *, on_success, on_error, **_kwargs):
        self.calls.append((operation, on_success, on_error))
        return SimpleNamespace(cancel=lambda: None)


class _Client:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.started = []
        self.cancelled = []
        self.events = []

    def get_capabilities(self):
        return self.capabilities

    def start_scp_transfer(self, request):
        self.started.append(request)
        return SimpleNamespace(id="transfer-1", state=TransferState.QUEUED)

    def cancel_transfer(self, request):
        self.cancelled.append(request)

    def subscribe_events(self, callback):
        self.events.append(callback)
        return SimpleNamespace(unsubscribe=lambda: None)


def _controller(client):
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=_Bridge())
    controller._show_transfer_error = lambda message: setattr(controller, "error", message)
    return controller


def test_scp_start_uses_typed_client_and_never_local_process(monkeypatch):
    class Label:
        def set_wrap(self, _value):
            return None

        def set_halign(self, _value):
            return None

        def set_text(self, _value):
            return None

    class Dialog:
        def __init__(self, _title):
            self.content_box = SimpleNamespace(append=lambda _item: None)
            self.cancel_btn = SimpleNamespace()

        def connect(self, *_args):
            return None

        def present(self, *_args):
            return None

    monkeypatch.setattr("sshpilot.scp_window.ScpTransferDialog", Dialog)
    monkeypatch.setattr("sshpilot.scp_window.Gtk.Label", Label)
    client = _Client(SimpleNamespace(supports=lambda capability: capability is Capability.TRANSFERS_SCP))
    controller = _controller(client)
    controller.start_scp_transfer(
        SimpleNamespace(id="demo", nickname="demo"),
        ["/tmp/a file"],
        "/remote/drop",
        direction="upload",
    )

    operation, on_success, _on_error = controller.window.client_bridge.calls[0]
    summary = operation()
    on_success(summary)
    assert len(client.started) == 1
    assert client.started[0].sources == ("/tmp/a file",)
    assert client.started[0].destination == "/remote/drop"
    assert not hasattr(controller, "_show_scp_terminal_window")


def test_scp_dialog_observes_terminal_transfer_state(monkeypatch):
    class Label:
        def set_wrap(self, _value):
            return None

        def set_halign(self, _value):
            return None

        def set_text(self, value):
            self.value = value

    class Dialog:
        def __init__(self, _title):
            self.content_box = SimpleNamespace(append=lambda _item: None)

        def connect(self, *_args):
            return None

        def present(self, *_args):
            return None

    monkeypatch.setattr("sshpilot.scp_window.ScpTransferDialog", Dialog)
    monkeypatch.setattr("sshpilot.scp_window.Gtk.Label", Label)
    client = _Client(SimpleNamespace(supports=lambda capability: capability is Capability.TRANSFERS_SCP))
    controller = _controller(client)
    controller.start_scp_transfer(
        SimpleNamespace(id="demo", nickname="demo"),
        ["/tmp/file"],
        "/remote/drop",
        direction="upload",
    )
    _operation, on_started, _on_error = controller.window.client_bridge.calls[0]
    on_started(SimpleNamespace(id="transfer-1", state=TransferState.QUEUED))
    assert client.events


def test_scp_start_rejects_missing_capability_without_fallback():
    client = _Client(SimpleNamespace(supports=lambda _capability: False))
    controller = _controller(client)
    controller.start_scp_transfer(
        SimpleNamespace(id="demo", nickname="demo"),
        ["/tmp/file"],
        "/remote/drop",
        direction="upload",
    )

    assert "unavailable" in controller.error.lower()
    assert controller.window.client_bridge.calls == []


def test_scp_controller_has_no_subprocess_or_vte_ownership():
    source = open("src/sshpilot/scp_window.py", encoding="utf-8").read()
    for forbidden in (
        "TerminalWidget",
        "spawn_async",
        "bash",
        "subprocess",
        "resolve_native_auth",
        "_build_scp_argv",
    ):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# SCP download browser: SFTP handshake presenter wiring
# ---------------------------------------------------------------------------


class _SftpSubscription:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _SftpBrowserClient:
    """Minimal daemon-client surface for the download-browser presenter."""

    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.events = []
        self.pending = []
        self.claims = []
        self._claimed_by = {}
        self._subscribers = []
        self.listed = []

    def sftp_list_directory(self, request):
        self.listed.append(request)
        return request

    def get_capabilities(self):
        return self.capabilities

    def subscribe_events(self, callback):
        sub = _SftpSubscription()
        self._subscribers.append((callback, sub))
        return sub

    def list_interactions(self):
        return list(self.pending)

    def open_sftp(self, request):
        self.open_request = request
        return SimpleNamespace(
            id="sftp-7",
            state=SftpServiceState.STARTING,
            connection_id=request.connection_id,
        )

    def claim_interaction(self, interaction_id):
        self._claimed_by[interaction_id] = True
        self.claims.append(interaction_id)
        return SimpleNamespace(nonce="ab" * 16)

    def release_interaction(self, interaction_id):
        self._claimed_by.pop(interaction_id, None)

    def emit(self, summary):
        event = SimpleNamespace(
            type=EventType.INTERACTION_CREATED, payload=summary
        )
        for callback, _sub in list(self._subscribers):
            callback(event)


class _SftpSyncBridge:
    """Runs both RPC and interaction operations synchronously."""

    def __init__(self):
        self.submitted = []
        self.interactions = []

    def submit(self, operation, *, on_success, on_error, **_kwargs):
        self.submitted.append((operation, on_success, on_error))
        return SimpleNamespace(cancel=lambda: None)

    def submit_interaction(self, operation, *, on_success, on_error, on_discard=None):
        self.interactions.append(operation)
        try:
            result = operation()
        except BaseException as exc:
            on_error(exc)
        else:
            on_success(result)
        return None


def _recording_dialogs(monkeypatch):
    """Patch the presenter to record presentations instead of GTK dialogs."""
    presented = []
    monkeypatch.setattr(
        dialogs_mod.DaemonInteractionDialogs,
        "_present",
        lambda self, summary: presented.append(summary) or None,
    )
    return presented


def _sftp_capabilities():
    supported = frozenset(
        {
            Capability.SFTP_READ,
            Capability.SFTP_WRITE,
            Capability.SFTP_EVENTS,
            Capability.SFTP_METADATA,
            Capability.SFTP_MUTATE,
            Capability.OPERATIONS_READ,
            Capability.OPERATIONS_CONTROL,
        }
    )
    return SimpleNamespace(
        supported=supported,
        supports=lambda capability: capability in supported,
    )


def _handshake_password(session_id, *, interaction_id=None):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return InteractionSummary(
        id=interaction_id or new_interaction_id(),
        session_id=SessionId(session_id),
        connection_id=ConnectionId("conn-1"),
        type=InteractionType.PASSWORD,
        state=InteractionState.PENDING,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        attempt=1,
        prompt=PasswordPrompt(
            username="root",
            hostname="192.168.8.1",
            port=22,
            attempt=1,
            can_remember=True,
            stored_secret_available=False,
        ),
    )


def test_scp_download_browser_presents_sftp_handshake_password(monkeypatch):
    """The download browser's SFTP handshake prompt is presented.

    The SCP download browser opens the remote-path picker through a bare
    ``DaemonSftpServiceController`` (unlike the file manager's
    ``DaemonSftpManager``, which already wires a presenter). Previously the
    password interaction — scoped to the SFTP service id, e.g.
    ``session=sftp-1`` in the daemon log — was created daemon-side but no
    GTK presenter was attached, so the operation stalled with no prompt.
    """
    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _patch_browser_widgets(monkeypatch)
    presented = _recording_dialogs(monkeypatch)
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    # The handshake password prompt is created before the frontend learns the
    # service id (prompt-before-bind race, exactly like the operation case).
    summary = _handshake_password("sftp-7")
    client.pending.append(summary)

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1")
    )

    # A presenter is attached to the browser's SFTP controller and starts
    # unbound — it must not claim anything yet.
    dialogs = controller._sftp_browser_dialogs_holder["value"]
    assert dialogs is not None
    assert dialogs._session_id is None

    # The open_sftp RPC lands; the controller's state change binds the
    # presenter to the SFTP service id and reconciles the pending prompt.
    operation, on_success, _on_error = bridge.submitted[0]
    assert operation() is not None  # constructs OpenSftpRequest without error
    on_success(
        SimpleNamespace(
            id="sftp-7",
            state=SftpServiceState.STARTING,
            connection_id=ConnectionId("conn-1"),
        )
    )

    assert dialogs._session_id == SessionId("sftp-7")
    assert presented == [summary]
    assert client.claims == [summary.id]

    # Closing the browser disposes the presenter: no dangling subscriptions.
    controller._close_sftp_browser_controller()
    assert controller._sftp_browser_dialogs_holder is None
    assert dialogs._closed


def test_scp_download_browser_handshake_prompt_event_path(monkeypatch):
    """Interactions created after the bind still route through the event path."""
    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _patch_browser_widgets(monkeypatch)
    presented = _recording_dialogs(monkeypatch)
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1")
    )
    dialogs = controller._sftp_browser_dialogs_holder["value"]
    _operation, on_success, _on_error = bridge.submitted[0]
    # STARTING (not READY) keeps the test hermetic: READY would trigger
    # _on_ready, which re-enters _prompt_scp_download and builds the real
    # ScpDownloadWindow GTK UI. The bind + event path is identical either way.
    on_success(
        SimpleNamespace(
            id="sftp-7",
            state=SftpServiceState.STARTING,
            connection_id=ConnectionId("conn-1"),
        )
    )

    summary = _handshake_password("sftp-7")
    client.emit(summary)

    assert presented == [summary]
    assert client.claims == [summary.id]
    controller._close_sftp_browser_controller()
    assert dialogs._closed


# ---------------------------------------------------------------------------
# SCP download browser: Enter in the remote-path row reloads the listing
# ---------------------------------------------------------------------------


class _Widget:
    """Permissive stand-in for any Gtk/Adw widget in the browser dialog."""

    def __init__(self, **kwargs):
        del kwargs
        self._handlers = {}
        self._text = ""

    def __getattr__(self, _name):
        return lambda *args, **_kwargs: None

    def connect(self, signal, handler):
        self._handlers.setdefault(signal, []).append(handler)
        return None

    def set_text(self, text):
        self._text = text

    def get_text(self):
        return self._text

    def set_sensitive(self, _value):
        return None


class _EntryRow(_Widget):
    instances = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        _EntryRow.instances.append(self)

    def get_editable(self):
        return None


class _Dialog:
    instances = []

    def __init__(self, parent, subtitle=""):
        del parent, subtitle
        self.content_box = _Widget()
        self.download_button = _Widget()
        self.presented = False
        _Dialog.instances.append(self)

    def connect(self, *_args):
        return None

    def present(self, *_args):
        self.presented = True
        return None

    def set_application(self, *_args):
        return None


def _patch_browser_widgets(monkeypatch):
    import sshpilot.scp_window as scp_window_mod
    from sshpilot import icon_utils

    class _ActionRow(_Widget):
        instances = []

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.title = kwargs.get("title", "")
            self.subtitle = None
            self.remote_name = ""
            self.remote_is_dir = False
            self.remote_selectable = True
            _ActionRow.instances.append(self)

        def set_title(self, title):
            self.title = title

        def set_subtitle(self, subtitle):
            self.subtitle = subtitle

        def add_prefix(self, _widget):
            self.prefix = _widget

        def set_selectable(self, selectable):
            self.remote_selectable = selectable

    class _ListBox(_Widget):
        instances = []

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.children = []
            self.selected = None
            _ListBox.instances.append(self)

        def append(self, child):
            if self.children:
                self.children[-1]._next = child
            child._next = None
            self.children.append(child)

        def remove(self, child):
            self.children.remove(child)
            for index, current in enumerate(self.children):
                current._next = (
                    self.children[index + 1] if index + 1 < len(self.children) else None
                )
            if child is self.selected:
                self.selected = None

        def get_first_child(self):
            return self.children[0] if self.children else None

        def get_selected_row(self):
            return self.selected

        def select_row(self, row):
            self.selected = row
            for handler in self._handlers.get("row-selected", []):
                handler(self, row)

    def _get_next_sibling(self):
        return getattr(self, "_next", None)

    _Widget.get_next_sibling = _get_next_sibling

    _EntryRow.instances = []
    _ActionRow.instances = []
    _ListBox.instances = []
    monkeypatch.setattr(scp_window_mod, "ScpDownloadWindow", _Dialog)
    monkeypatch.setattr(scp_window_mod.Adw, "EntryRow", _EntryRow)
    monkeypatch.setattr(scp_window_mod.Adw, "ActionRow", _ActionRow)
    monkeypatch.setattr(scp_window_mod.Adw, "PreferencesGroup", _Widget)
    monkeypatch.setattr(scp_window_mod.Adw, "Clamp", _Widget)
    monkeypatch.setattr(scp_window_mod.Gtk, "Box", _Widget)
    monkeypatch.setattr(scp_window_mod.Gtk, "Label", _Widget)
    monkeypatch.setattr(scp_window_mod.Gtk, "ScrolledWindow", _Widget)
    monkeypatch.setattr(scp_window_mod.Gtk, "ListBox", _ListBox)
    monkeypatch.setattr(
        icon_utils, "new_button_from_icon_name", lambda *args, **_kwargs: _Widget()
    )
    return _ListBox, _ActionRow


def test_scp_browser_remote_path_row_reloads_on_entry_activated(monkeypatch):
    """Enter in the remote-path row reloads the remote listing.

    Regression: ``remote_row`` is an ``Adw.EntryRow`` whose ``activate``
    signal is the inherited ``GtkListBoxRow::activate`` (fired only when the
    *row* is activated, e.g. double-click). Pressing Enter inside the
    embedded entry emits ``entry-activated`` instead, so connecting
    ``activate`` left Enter dead.
    """
    monkeypatch.setattr(dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args))
    _patch_browser_widgets(monkeypatch)
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1"),
        "sftp-7",
    )

    remote_row = _EntryRow.instances[0]
    # Enter must be wired to entry-activated; the dead row-level activate
    # signal must not be the reload trigger.
    assert "entry-activated" in remote_row._handlers
    assert "activate" not in remote_row._handlers

    submitted_before = len(bridge.submitted)
    remote_row._handlers["entry-activated"][0]()

    assert len(bridge.submitted) == submitted_before + 1
    operation, _on_success, _on_error = bridge.submitted[-1]
    request = operation()
    assert request.path == remote_row.get_text()
    assert client.listed[-1] is request


def test_scp_download_browser_is_presented_before_sftp_ready(monkeypatch):
    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _patch_browser_widgets(monkeypatch)
    _Dialog.instances = []
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1")
    )

    assert _Dialog.instances[-1].presented is True
    assert len(bridge.submitted) == 1
    assert controller._sftp_browser_controller is not None


def test_scp_download_browser_gates_on_vault_unlock(monkeypatch):
    """A locked session-backed vault must be unlocked before the SCP
    download browser opens its SFTP listing session - mirrors the gate
    used before starting the actual transfer (start_scp_transfer)."""
    from unittest import mock

    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _patch_browser_widgets(monkeypatch)
    _Dialog.instances = []
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    captured_retry = []
    terminal_manager = mock.Mock()
    terminal_manager._maybe_unlock_secrets_then = mock.Mock(
        side_effect=lambda retry: (captured_retry.append(retry), True)[1]
    )
    controller.window.terminal_manager = terminal_manager

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1")
    )

    # Gated: no SFTP browse session must have started yet.
    assert bridge.submitted == []
    assert len(captured_retry) == 1

    # Once the vault is unlocked, the retry must proceed without re-gating.
    captured_retry[0]()

    assert len(bridge.submitted) == 1
    assert controller._sftp_browser_controller is not None
    terminal_manager._maybe_unlock_secrets_then.assert_called_once()


def test_scp_download_browser_populates_large_listing_incrementally(monkeypatch):
    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _list_box_cls, action_row_cls = _patch_browser_widgets(monkeypatch)
    import sshpilot.scp_window as scp_window_mod

    pending = []
    monkeypatch.setattr(
        scp_window_mod.GLib, "idle_add", lambda callback, *args: pending.append(callback)
    )
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1"),
        "sftp-7",
    )
    _operation, on_success, _on_error = bridge.submitted[-1]
    entries = [
        SimpleNamespace(
            name=f"file-{index}",
            file_type=SimpleNamespace(value="file"),
        )
        for index in range(2000)
    ]
    on_success(SimpleNamespace(entries=entries))

    # The original Adw.ActionRow appearance is retained, but rows are created
    # in bounded idle batches instead of blocking GTK on one large append.
    assert len(action_row_cls.instances) == 0
    assert pending
    callback = pending.pop(0)
    if callback() == scp_window_mod.GLib.SOURCE_CONTINUE:
        pending.append(callback)
    assert len(action_row_cls.instances) == 50

    while pending:
        callback = pending.pop(0)
        if callback() == scp_window_mod.GLib.SOURCE_CONTINUE:
            pending.append(callback)

    assert len(action_row_cls.instances) == 2001


def test_scp_download_browser_uses_file_manager_icons(monkeypatch):
    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _list_box_cls, _action_row_cls = _patch_browser_widgets(monkeypatch)
    from sshpilot import icon_utils

    monkeypatch.setattr(
        icon_utils,
        "new_image_from_icon_name",
        lambda name, **_kwargs: SimpleNamespace(name=name),
    )
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1"),
        "sftp-7",
    )
    _operation, on_success, _on_error = bridge.submitted[-1]
    on_success(
        SimpleNamespace(
            entries=[
                SimpleNamespace(
                    name="src", file_type=SimpleNamespace(value="directory")
                ),
                SimpleNamespace(
                    name="build.py", file_type=SimpleNamespace(value="file")
                ),
                SimpleNamespace(
                    name="photo.png", file_type=SimpleNamespace(value="file")
                ),
            ]
        )
    )

    rows = _list_box_cls.instances[0].children
    assert rows[0].prefix.name == "go-up-symbolic"
    assert rows[1].prefix.name == "inode-directory"
    assert rows[2].prefix.name == "text-x-script"
    assert rows[3].prefix.name == "image-x-generic"


def test_scp_download_browser_ignores_stale_directory_results(monkeypatch):
    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _list_box_cls, _action_row_cls = _patch_browser_widgets(monkeypatch)
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1"),
        "sftp-7",
    )
    remote_row = _EntryRow.instances[0]
    remote_row._handlers["entry-activated"][0]()
    assert len(bridge.submitted) == 2

    stale_result = SimpleNamespace(
        entries=[
            SimpleNamespace(
                name="stale.txt", file_type=SimpleNamespace(value="file")
            )
        ]
    )
    fresh_result = SimpleNamespace(
        entries=[
            SimpleNamespace(
                name="fresh.txt", file_type=SimpleNamespace(value="file")
            )
        ]
    )
    _old_operation, old_success, _old_error = bridge.submitted[0]
    _new_operation, new_success, _new_error = bridge.submitted[1]
    old_success(stale_result)
    new_success(fresh_result)
    old_success(stale_result)

    list_box = _list_box_cls.instances[0]
    assert [row.remote_name for row in list_box.children] == ["..", "fresh.txt"]


def test_scp_download_browser_presenter_never_steals_other_scopes(monkeypatch):
    """The browser presenter only claims its own SFTP service interactions."""
    monkeypatch.setattr(
        dialogs_mod.GLib, "idle_add", lambda fn, *args: fn(*args)
    )
    _patch_browser_widgets(monkeypatch)
    presented = _recording_dialogs(monkeypatch)
    client = _SftpBrowserClient(_sftp_capabilities())
    bridge = _SftpSyncBridge()
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=bridge)
    controller._show_transfer_error = lambda message: setattr(
        controller, "error", message
    )

    controller._prompt_scp_download(
        SimpleNamespace(id="conn-1", nickname="Router", host="192.168.8.1")
    )
    dialogs = controller._sftp_browser_dialogs_holder["value"]
    _operation, on_success, _on_error = bridge.submitted[0]
    on_success(
        SimpleNamespace(
            id="sftp-7",
            state=SftpServiceState.STARTING,
            connection_id=ConnectionId("conn-1"),
        )
    )

    # A sibling terminal/session prompt must not be claimed or displayed.
    other = _handshake_password("session-other", interaction_id="inter-other")
    client.emit(other)
    assert presented == []
    assert client.claims == []
    controller._close_sftp_browser_controller()
    assert dialogs._closed


def test_scp_transfer_gates_on_vault_unlock(monkeypatch):
    """A locked session-backed vault must be unlocked before SCP starts -
    mirrors the gate used before opening a terminal/file-manager connection
    (TerminalManager._maybe_unlock_secrets_then)."""
    from unittest import mock

    class Label:
        def set_wrap(self, _value):
            return None

        def set_halign(self, _value):
            return None

        def set_text(self, _value):
            return None

    class Dialog:
        def __init__(self, _title):
            self.content_box = SimpleNamespace(append=lambda _item: None)
            self.cancel_btn = SimpleNamespace()

        def connect(self, *_args):
            return None

        def present(self, *_args):
            return None

    monkeypatch.setattr("sshpilot.scp_window.ScpTransferDialog", Dialog)
    monkeypatch.setattr("sshpilot.scp_window.Gtk.Label", Label)
    client = _Client(SimpleNamespace(supports=lambda capability: capability is Capability.TRANSFERS_SCP))
    controller = _controller(client)

    captured_retry = []
    terminal_manager = mock.Mock()
    terminal_manager._maybe_unlock_secrets_then = mock.Mock(
        side_effect=lambda retry: (captured_retry.append(retry), True)[1]
    )
    controller.window.terminal_manager = terminal_manager

    controller.start_scp_transfer(
        SimpleNamespace(id="demo", nickname="demo"),
        ["/tmp/a file"],
        "/remote/drop",
        direction="upload",
    )

    # Gated: the transfer must not have started yet.
    assert client.started == []
    assert len(captured_retry) == 1

    # Once the vault is unlocked, the retry must proceed without re-gating.
    captured_retry[0]()

    operation, on_success, _on_error = controller.window.client_bridge.calls[0]
    on_success(operation())
    assert len(client.started) == 1
    terminal_manager._maybe_unlock_secrets_then.assert_called_once()
