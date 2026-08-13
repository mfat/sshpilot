"""Phase 14 real GTK authentication dialogs (no auth-helper broker).

Password dialog is exercised end-to-end against temporary OpenSSH.
Host-key dialog is exercised through DaemonInteractionDialogs' real Adw
AlertDialog (live VTE+host-key under xvfb aborts on this host — see
docs/testing/gtk-vte-bloom-filter-crash.md); the click path is still the
production dialog code without the broker auth helper.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests._gui_harness import requires_gui

Gtk, Adw, Gio, GLib = requires_gui()

try:
    import gi

    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: F401
except Exception as error:  # pragma: no cover
    pytest.skip(f"VTE unavailable: {error}", allow_module_level=True)

from tests.gui._phase14_harness import OUTPUT_MARKER

pytestmark = pytest.mark.gui


def test_password_dialog_without_auth_helper(phase14_harness):
    h = phase14_harness
    h.stop_auth_helper()
    conn = h.add_password_connection("P14AuthPwd", store_password=False)

    win = h.gui.window
    win.terminal_manager.connect_to_host(conn, force_new=True)
    h.pump_until(
        lambda: h.find_terminal_widget(conn) is not None,
        timeout=20.0,
        label="auth dialog terminal widget",
    )

    dialog = h.wait_for_password_dialog(timeout=45.0)
    assert dialog is not None
    heading = dialog.get_heading() or ""
    assert "Password for" in heading

    h.respond_password_dialog(dialog, h.openssh.password)

    def _connected():
        t = h.find_terminal_widget(conn)
        return bool(t and getattr(t, "is_connected", False))

    h.pump_until(_connected, timeout=45.0, label="connected after password dialog")
    term = h.find_terminal_widget(conn)
    h.ensure_input_ready(term, timeout=20.0)
    h.emit_terminal_input(f"echo {OUTPUT_MARKER}\n", term)
    text = h.wait_for_marker(OUTPUT_MARKER, terminal=term, timeout=30.0)
    assert OUTPUT_MARKER in text


def test_host_key_dialog_without_auth_helper(phase14_harness):
    from datetime import datetime, timedelta, timezone

    from sshpilot.api.models import (
        HostKeyDecision,
        HostKeyPrompt,
        HostKeyStatus,
        InteractionState,
        InteractionSummary,
        InteractionType,
        SessionId,
    )
    from sshpilot.api.models.common import ConnectionId
    from sshpilot.api.interaction_identity import new_interaction_id
    from sshpilot.daemon_interaction_dialogs import DaemonInteractionDialogs
    from sshpilot.gtk_client_bridge import GtkClientBridge

    h = phase14_harness
    h.stop_auth_helper()
    win = h.gui.window
    bridge = win.client_bridge
    assert isinstance(bridge, GtkClientBridge)

    calls = []

    class _Client:
        def subscribe_events(self, _cb):
            sub = MagicMock()
            sub.close = MagicMock()
            return sub

        def claim_interaction(self, interaction_id):
            claim = MagicMock()
            claim.nonce = b"n" * 16
            return claim

        def respond_to_interaction(self, request):
            calls.append(request)
            return None

        def release_interaction(self, _interaction_id):
            return None

        def send_interaction_secret(self, *_a, **_k):
            return None

    dialogs = DaemonInteractionDialogs(_Client(), bridge, win)
    now = datetime.now(timezone.utc)
    summary = InteractionSummary(
        id=new_interaction_id(),
        session_id=SessionId("session-auth"),
        connection_id=ConnectionId("demo"),
        type=InteractionType.HOST_KEY_CONFIRMATION,
        state=InteractionState.PENDING,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        attempt=1,
        prompt=HostKeyPrompt(
            hostname="127.0.0.1",
            port=h.openssh.port,
            key_type="ssh-ed25519",
            fingerprint="SHA256:phase14testhostkeyfingerprint",
            status=HostKeyStatus.UNKNOWN,
        ),
    )
    dialogs._claimed.add(summary.id)
    dialogs._claims[summary.id] = b"n" * 16
    dialogs._present(summary)
    h.pump(200)

    dialog = None
    for d in (dialogs._dialogs or {}).values():
        dialog = d
        break
    assert dialog is not None
    heading = dialog.get_heading() or ""
    assert "Verify this SSH host" in heading

    h.respond_host_key_dialog(dialog, "store")
    h.pump_until(lambda: bool(calls), timeout=5.0, label="host-key respond")
    assert calls
    assert calls[0].host_key_decision is HostKeyDecision.ACCEPT
    dialogs.close()
