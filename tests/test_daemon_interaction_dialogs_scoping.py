"""DaemonInteractionDialogs scoping and reconciliation regression tests.

Covers the presenter half of the interaction-ownership fix:

* unbound presenters must never claim/display any interaction (no wildcard);
* ``set_session`` is race-safe — it reconciles interactions that were created
  before the frontend learned the scope ID;
* reconciliation is idempotent with event-driven delivery;
* concurrent presenters (File Manager SFTP, Authorized Keys SFTP, ssh-copy-id
  operation) never steal each other's interactions;
* host-key confirmations and FIDO presence flow through the same scoped
  presenter (presence stays a passive notification, never a secret prompt).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from sshpilot import daemon_interaction_dialogs as dialogs_mod
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import EventType
from sshpilot.api.interaction_identity import new_interaction_id
from sshpilot.api.models import (
    ChallengePrompt,
    HostKeyDecision,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PasswordPrompt,
    PresencePrompt,
    SecretDecision,
    SessionId,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.daemon_interaction_dialogs import DaemonInteractionDialogs


def _prompt_for(interaction_type: InteractionType):
    if interaction_type is InteractionType.HOST_KEY_CONFIRMATION:
        return HostKeyPrompt(
            hostname="example.test",
            port=22,
            key_type="ssh-ed25519",
            fingerprint="SHA256:scope-test-fingerprint",
            status=HostKeyStatus.UNKNOWN,
        )
    if interaction_type is InteractionType.PASSWORD:
        return PasswordPrompt(
            username="alice",
            hostname="example.test",
            port=22,
            attempt=1,
            can_remember=True,
            stored_secret_available=False,
        )
    if interaction_type is InteractionType.SECURITY_KEY_PRESENCE:
        return PresencePrompt(text="Touch your security key")
    if interaction_type is InteractionType.KEYBOARD_INTERACTIVE:
        return ChallengePrompt(text="Enter the 6-digit code", attempt=1)
    if interaction_type is InteractionType.PRIVATE_KEY_PASSPHRASE:
        from sshpilot.api.models import PassphrasePrompt

        return PassphrasePrompt(
            key_display_name="id_ed25519",
            key_fingerprint=None,
            attempt=1,
            can_remember=True,
            stored_secret_available=False,
        )
    raise AssertionError(f"no fixture prompt for {interaction_type}")


def _summary(interaction_type, session_id, *, interaction_id=None):
    now = datetime.now(timezone.utc)
    return InteractionSummary(
        id=interaction_id or new_interaction_id(),
        session_id=SessionId(session_id),
        connection_id=ConnectionId("conn-1"),
        type=interaction_type,
        state=InteractionState.PENDING,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        attempt=1,
        prompt=_prompt_for(interaction_type),
    )


class _Subscription:
    def __init__(self, client):
        self._client = client
        self.closed = False

    def close(self):
        self.closed = True


class _FakeClient:
    """Minimal daemon-client surface used by DaemonInteractionDialogs."""

    def __init__(self):
        self.pending = []
        self.claims = []
        self.responded = []
        self.cancelled = []
        self.secrets = []
        self._claimed_by = {}
        self._subscribers = []

    def subscribe_events(self, callback):
        sub = _Subscription(self)
        self._subscribers.append((callback, sub))
        return sub

    def list_interactions(self):
        return list(self.pending)

    def claim_interaction(self, interaction_id):
        if interaction_id in self._claimed_by:
            raise SshPilotError(
                ErrorCode.INTERACTION_CLAIM_CONFLICT,
                "Another client owns this interaction",
            )
        self._claimed_by[interaction_id] = True
        self.claims.append(interaction_id)
        return SimpleNamespace(nonce="ab" * 16)

    def respond_to_interaction(self, request):
        self.responded.append(request)

    def send_interaction_secret(self, interaction_id, nonce, secret):
        self.secrets.append((interaction_id, nonce, bytes(secret)))

    def cancel_interaction(self, interaction_id):
        self.cancelled.append(interaction_id)

    def release_interaction(self, interaction_id):
        self._claimed_by.pop(interaction_id, None)

    def emit(self, summary):
        event = SimpleNamespace(
            type=EventType.INTERACTION_CREATED, payload=summary
        )
        for callback, _sub in list(self._subscribers):
            callback(event)


class _SyncBridge:
    """Runs interaction operations immediately (no GTK main loop needed)."""

    def submit_interaction(self, operation, *, on_success, on_error, on_discard=None):
        try:
            result = operation()
        except BaseException as exc:
            on_error(exc)
        else:
            on_success(result)
        return None


class _DeferredBridge:
    """Hold submitted interaction work to expose in-flight deduplication."""

    def __init__(self):
        self.pending = []

    def submit_interaction(self, operation, *, on_success, on_error, on_discard=None):
        self.pending.append((operation, on_success, on_error))
        return None

    def complete_next(self):
        operation, on_success, on_error = self.pending.pop(0)
        try:
            result = operation()
        except BaseException as exc:
            on_error(exc)
        else:
            on_success(result)


class _RecordingDialogs(DaemonInteractionDialogs):
    """Presenter that records presentation instead of building GTK dialogs.

    Mirrors the production ``_present`` type dispatch (host-key vs the generic
    secret/passive dispatcher) so routing is observable without a display.
    """

    def __init__(self, client, bridge, parent=None):
        super().__init__(client, bridge, parent)
        self.presented = []
        self.presented_secret_types = []
        self.presented_host_keys = []

    def _present(self, summary):
        self.presented.append(summary)
        if summary.type is InteractionType.HOST_KEY_CONFIRMATION:
            self._present_host_key(summary, None)
        elif summary.type in {
            InteractionType.PASSWORD,
            InteractionType.PRIVATE_KEY_PASSPHRASE,
            InteractionType.KEYBOARD_INTERACTIVE,
            InteractionType.SECURITY_KEY_PRESENCE,
            InteractionType.CONFIRMATION,
        }:
            self._present_secret(summary, None)

    def _present_secret(self, summary, parent):
        self.presented_secret_types.append(summary.type)

    def _present_host_key(self, summary, parent):
        self.presented_host_keys.append(summary)


@pytest.fixture
def immediate_idle(monkeypatch):
    """Run GLib idle callbacks synchronously (tests have no main loop)."""
    monkeypatch.setattr(
        dialogs_mod.GLib,
        "idle_add",
        lambda fn, *args: fn(*args),
    )


# ---------------------------------------------------------------------------
# E. Unbound presenter isolation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blank-window regression: an embedded FM shell must never host the prompt
# ---------------------------------------------------------------------------


class _FakeWindow:
    """Minimal Gtk.Window stand-in exposing visibility + root."""

    def __init__(self, visible: bool):
        self._visible = visible
        self._root = self

    def get_visible(self):
        return self._visible

    def get_root(self):
        return self._root


def test_present_parent_skips_invisible_window_shell(monkeypatch):
    """An embedded FM window shell (content detached, never presented) is not
    a valid dialog parent — the live app window is resolved instead."""
    monkeypatch.setattr(dialogs_mod.Gtk, "Window", _FakeWindow)

    main_window = _FakeWindow(visible=True)
    shell = _FakeWindow(visible=False)

    resolved_from = []
    monkeypatch.setattr(
        "sshpilot.window_dialogs.resolve_app_modal_parent",
        lambda from_widget: (resolved_from.append(from_widget), main_window)[1],
    )

    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge(), parent=shell)

    parent = dialogs._resolve_present_parent()

    assert parent is main_window
    assert resolved_from == [shell]
    dialogs.close()


def test_present_parent_uses_visible_window_directly(monkeypatch):
    """A window that is actually on screen (e.g. a standalone FM window)
    keeps hosting its own prompts — no re-parenting to the main window."""
    monkeypatch.setattr(dialogs_mod.Gtk, "Window", _FakeWindow)

    visible_window = _FakeWindow(visible=True)

    def _must_not_resolve(_from_widget):
        raise AssertionError("visible window must be used directly")

    monkeypatch.setattr(
        "sshpilot.window_dialogs.resolve_app_modal_parent",
        _must_not_resolve,
    )

    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge(), parent=visible_window)

    assert dialogs._resolve_present_parent() is visible_window
    dialogs.close()


def test_present_passes_resolved_parent_not_invisible_shell(
    monkeypatch, immediate_idle
):
    """Full ``_present`` path: a password prompt whose presenter is bound to an
    invisible FM shell is presented over the resolved main window — the blank
    shell is never shown behind the dialog."""
    monkeypatch.setattr(dialogs_mod.Gtk, "Window", _FakeWindow)

    main_window = _FakeWindow(visible=True)
    shell = _FakeWindow(visible=False)

    monkeypatch.setattr(
        "sshpilot.window_dialogs.resolve_app_modal_parent",
        lambda from_widget: main_window,
    )
    monkeypatch.setattr(
        "sshpilot.window_dialogs.present_for_modal_dialog", lambda _window: None
    )

    client = _FakeClient()
    dialogs = DaemonInteractionDialogs(client, _SyncBridge(), parent=shell)
    dialogs.set_session(SessionId("operation-1"))

    presented_with = []
    monkeypatch.setattr(
        dialogs,
        "_present_secret",
        lambda summary, parent: presented_with.append(parent),
    )

    summary = _summary(InteractionType.PASSWORD, "operation-1")
    client.emit(summary)

    assert presented_with == [main_window]
    dialogs.close()


def test_unbound_presenter_ignores_all_interactions(immediate_idle):
    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge())
    assert dialogs._session_id is None

    client.emit(_summary(InteractionType.PASSWORD, "session-other"))

    assert client.claims == []
    assert dialogs.presented == []
    dialogs.close()
    assert dialogs._closed


# ---------------------------------------------------------------------------
# D. Event-after-bind path
# ---------------------------------------------------------------------------


def test_event_after_bind_is_presented_once(immediate_idle):
    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))

    summary = _summary(InteractionType.PASSWORD, "operation-1")
    client.emit(summary)

    assert client.claims == [summary.id]
    assert dialogs.presented == [summary]
    dialogs.close()


# ---------------------------------------------------------------------------
# C. Prompt-before-bind race: set_session reconciles pending interactions
# ---------------------------------------------------------------------------


def test_set_session_reconciles_prompt_created_before_bind(immediate_idle):
    client = _FakeClient()
    summary = _summary(InteractionType.PASSWORD, "operation-1")
    client.pending.append(summary)

    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))

    assert client.claims == [summary.id]
    assert dialogs.presented == [summary]
    dialogs.close()


def test_reconcile_skips_other_scopes(immediate_idle):
    client = _FakeClient()
    client.pending.append(_summary(InteractionType.PASSWORD, "sftp-1"))

    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))

    assert client.claims == []
    assert dialogs.presented == []
    dialogs.close()


# ---------------------------------------------------------------------------
# G. Reconciliation deduplication
# ---------------------------------------------------------------------------


def test_reconcile_and_event_present_exactly_once(immediate_idle):
    client = _FakeClient()
    summary = _summary(InteractionType.PASSWORD, "operation-1")
    client.pending.append(summary)

    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))
    assert dialogs.presented == [summary]

    # The INTERACTION_CREATED event arrives around the same time.
    client.pending.clear()
    client.emit(summary)

    assert dialogs.presented == [summary]
    assert client.claims == [summary.id]
    dialogs.close()


def test_duplicate_delivery_while_claim_pending_submits_one_claim():
    client = _FakeClient()
    bridge = _DeferredBridge()
    dialogs = _RecordingDialogs(client, bridge)
    dialogs._session_id = SessionId("operation-1")
    summary = _summary(InteractionType.PRIVATE_KEY_PASSPHRASE, "operation-1")

    dialogs._handle_event(summary)
    dialogs._handle_event(summary)

    assert len(bridge.pending) == 1
    bridge.complete_next()
    assert client.claims == [summary.id]
    assert dialogs.presented == [summary]
    dialogs.close()


def test_secret_entry_activation_emits_submit_response():
    class Dialog:
        def __init__(self):
            self.emitted = []

        def emit(self, signal, response):
            self.emitted.append((signal, response))

    dialog = Dialog()

    DaemonInteractionDialogs._activate_secret_dialog(dialog)

    assert dialog.emitted == [("response", "submit")]


class _ConfirmationEntry:
    def __init__(self, text=""):
        self.text = text
        self.css_classes = set()
        self.focused = False

    def get_text(self):
        return self.text

    def add_css_class(self, name):
        self.css_classes.add(name)

    def remove_css_class(self, name):
        self.css_classes.discard(name)

    def grab_focus(self):
        self.focused = True


class _ConfirmationDialog:
    def __init__(self):
        self.enabled = {}
        self.emitted = []

    def set_response_enabled(self, response, enabled):
        self.enabled[response] = enabled

    def emit(self, signal, response):
        self.emitted.append((signal, response))


class _ConfirmationLabel:
    def __init__(self):
        self.visible = False

    def set_visible(self, visible):
        self.visible = visible


def test_key_generation_passphrase_rows_reject_mismatch():
    dialog = _ConfirmationDialog()
    entry = _ConfirmationEntry("first-value")
    confirmation = _ConfirmationEntry("different-value")
    label = _ConfirmationLabel()

    DaemonInteractionDialogs._activate_confirmed_secret_dialog(
        dialog,
        entry,
        confirmation,
        label,
    )

    assert dialog.enabled["submit"] is False
    assert dialog.emitted == []
    assert label.visible is True
    assert "error" in confirmation.css_classes
    assert confirmation.focused is True


def test_key_generation_passphrase_rows_submit_one_matching_value():
    dialog = _ConfirmationDialog()
    entry = _ConfirmationEntry("matching-value")
    confirmation = _ConfirmationEntry("matching-value")
    label = _ConfirmationLabel()

    DaemonInteractionDialogs._activate_confirmed_secret_dialog(
        dialog,
        entry,
        confirmation,
        label,
    )

    assert dialog.enabled["submit"] is True
    assert dialog.emitted == [("response", "submit")]
    assert label.visible is False
    assert "error" not in confirmation.css_classes


def test_repeated_set_session_does_not_duplicate(immediate_idle):
    client = _FakeClient()
    summary = _summary(InteractionType.PASSWORD, "operation-1")
    client.pending.append(summary)

    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))
    dialogs.set_session(SessionId("operation-1"))  # no-op rebind
    dialogs.set_session(SessionId("operation-1"))

    assert dialogs.presented == [summary]
    assert client.claims == [summary.id]
    dialogs.close()


# ---------------------------------------------------------------------------
# F. Concurrent sessions under one frontend client
# ---------------------------------------------------------------------------


def test_concurrent_presenters_never_steal(immediate_idle):
    client = _FakeClient()
    fm = _RecordingDialogs(client, _SyncBridge())      # File Manager SFTP
    fm.set_session(SessionId("sftp-1"))
    authorized = _RecordingDialogs(client, _SyncBridge())  # Authorized Keys SFTP
    authorized.set_session(SessionId("sftp-2"))
    deploy = _RecordingDialogs(client, _SyncBridge())  # ssh-copy-id operation
    deploy.set_session(SessionId("operation-1"))

    fm_summary = _summary(InteractionType.PASSWORD, "sftp-1")
    ak_summary = _summary(InteractionType.KEYBOARD_INTERACTIVE, "sftp-2")
    deploy_summary = _summary(InteractionType.PASSWORD, "operation-1")

    client.emit(fm_summary)
    client.emit(ak_summary)
    client.emit(deploy_summary)

    assert fm.presented == [fm_summary]
    assert authorized.presented == [ak_summary]
    assert deploy.presented == [deploy_summary]
    assert set(client.claims) == {fm_summary.id, ak_summary.id, deploy_summary.id}
    for dialogs in (fm, authorized, deploy):
        dialogs.close()


def test_transfer_scope_routes_only_to_its_presenter(immediate_idle):
    """SCP transfer prompts (scope = public TransferId) route correctly.

    Mirrors the ssh-copy-id operation presenter: an unbound presenter ignores
    the prompt, and after ``set_session(transfer id)`` only the matching
    presenter claims it — an SFTP presenter never steals it.
    """
    client = _FakeClient()
    transfer = _RecordingDialogs(client, _SyncBridge())
    sftp = _RecordingDialogs(client, _SyncBridge())
    sftp.set_session(SessionId("sftp-1"))

    transfer_summary = _summary(InteractionType.PASSWORD, "transfer-3")

    # Unbound presenter must not claim the transfer prompt.
    client.pending.append(transfer_summary)
    client.emit(transfer_summary)
    assert transfer.presented == []
    assert sftp.presented == []

    # Binding to the transfer id picks it up (and reconciles the pending one).
    transfer.set_session(SessionId("transfer-3"))
    assert transfer.presented == [transfer_summary]
    assert set(client.claims) == {transfer_summary.id}
    assert sftp.presented == []

    # A second prompt for the same transfer routes only to the transfer
    # presenter, never to the SFTP one.
    second = _summary(InteractionType.PASSWORD, "transfer-3")
    client.emit(second)
    assert transfer.presented == [transfer_summary, second]
    assert sftp.presented == []
    transfer.close()
    sftp.close()


# ---------------------------------------------------------------------------
# I. Host-key confirmation through the same scoped presenter
# ---------------------------------------------------------------------------


def test_host_key_confirmation_routes_to_operation_presenter(immediate_idle):
    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))

    summary = _summary(InteractionType.HOST_KEY_CONFIRMATION, "operation-1")
    client.emit(summary)

    assert dialogs.presented == [summary]
    assert dialogs.presented_host_keys == [summary]
    dialog = SimpleNamespace(close=lambda: None)
    dialogs._host_key_response(summary, dialog, "accept")
    assert len(client.responded) == 1
    request = client.responded[0]
    assert request.host_key_decision is HostKeyDecision.ACCEPT
    assert request.secret_decision is None
    dialogs.close()


def test_host_key_confirmation_ignored_by_other_scope(immediate_idle):
    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))

    client.emit(_summary(InteractionType.HOST_KEY_CONFIRMATION, "sftp-1"))

    assert dialogs.presented == []
    assert client.claims == []
    dialogs.close()


# ---------------------------------------------------------------------------
# J. FIDO / security-key presence: passive notification, never a secret prompt
# ---------------------------------------------------------------------------


def test_security_key_presence_is_not_a_secret_prompt(immediate_idle):
    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))

    summary = _summary(InteractionType.SECURITY_KEY_PRESENCE, "operation-1")
    client.emit(summary)

    # Routed to the same presenter and classified through the generic
    # secret-presentation dispatcher's passive presence branch.
    assert dialogs.presented == [summary]
    assert dialogs.presented_secret_types == [InteractionType.SECURITY_KEY_PRESENCE]
    # No secret was ever submitted for the presence notification.
    assert client.responded == []
    assert client.secrets == []

    dialog = SimpleNamespace(close=lambda: None)
    dialogs._presence_closed(summary, dialog)
    assert client.cancelled == [summary.id]
    assert client.responded == []
    dialogs.close()


def test_password_and_passphrase_route_to_secret_dispatch(immediate_idle):
    client = _FakeClient()
    dialogs = _RecordingDialogs(client, _SyncBridge())
    dialogs.set_session(SessionId("operation-1"))

    password = _summary(InteractionType.PASSWORD, "operation-1")
    client.emit(password)
    assert dialogs.presented_secret_types == [InteractionType.PASSWORD]
    assert client.claims == [password.id]

    passphrase = _summary(
        InteractionType.PRIVATE_KEY_PASSPHRASE, "operation-1"
    )
    client.emit(passphrase)
    assert dialogs.presented_secret_types == [
        InteractionType.PASSWORD,
        InteractionType.PRIVATE_KEY_PASSPHRASE,
    ]
    dialogs.close()


# ---------------------------------------------------------------------------
# B. SshCopyIdRunner binds the presenter to the operation and secret flows
# ---------------------------------------------------------------------------


def _operation_summary(operation_id, state="running"):
    from sshpilot.api.models.operations import (
        OperationKind,
        OperationState,
        OperationSummary,
    )

    return OperationSummary(
        operation_id=operation_id,
        kind=OperationKind.KEY_DEPLOYMENT,
        state=OperationState(state),
        message="",
        created_at=datetime.now(timezone.utc),
    )


def test_runner_binds_presenter_and_password_flows(monkeypatch, immediate_idle):
    from sshpilot import sshcopyid_window as win_mod

    presented = []
    monkeypatch.setattr(
        dialogs_mod.DaemonInteractionDialogs,
        "_present",
        lambda self, summary: presented.append(summary) or None,
    )
    scheduled = []
    monkeypatch.setattr(
        win_mod.GLib,
        "timeout_add",
        lambda ms, fn, *args: scheduled.append((ms, fn)) or 1,
    )
    monkeypatch.setattr(win_mod.GLib, "source_remove", lambda _handle: True)

    client = _FakeClient()
    client.deploy_key = lambda _request: _operation_summary("operation-9")
    client.get_operation = lambda _op: _operation_summary(
        "operation-9", state="running"
    )
    window = SimpleNamespace(
        client=client,
        client_bridge=_SyncBridge(),
        _error_dialog=lambda *a, **k: None,
        _info_dialog=lambda *a, **k: None,
    )
    runner = win_mod.SshCopyIdRunner(window)
    runner.run(
        SimpleNamespace(nickname="HostAlias"),
        SimpleNamespace(key_id="key:daemon-1"),
    )

    assert runner._operation_id == "operation-9"
    dialogs = runner._interaction_dialogs
    assert dialogs is not None
    # Presenter is bound to the operation scope — the same scope the daemon
    # worker uses for its interactions.
    assert dialogs._session_id == SessionId("operation-9")

    # A password interaction created for the operation after the bind is
    # presented; the user's secret reaches the daemon client.
    summary = _summary(InteractionType.PASSWORD, "operation-9")
    client.emit(summary)
    assert presented == [summary]
    entry = SimpleNamespace(
        get_text=lambda: "hunter2", set_text=lambda _value: None
    )
    dialogs._secret_response(
        summary, SimpleNamespace(close=lambda: None), "submit", entry
    )
    assert client.responded
    assert client.responded[0].secret_decision is SecretDecision.SUBMIT
    assert client.secrets[0][2] == b"hunter2"

    # Terminal state disposes the presenter: no dangling event subscription.
    client.get_operation = lambda _op: _operation_summary(
        "operation-9", state="succeeded"
    )
    poll_fn = scheduled[0][1]
    poll_fn()
    assert runner._interaction_dialogs is None
    assert dialogs._closed
    runner.cancel()
