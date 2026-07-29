"""Experimental GTK dialogs for daemon-owned typed SSH interactions."""

from __future__ import annotations

from typing import Optional

from gi.repository import Adw, GLib, Gtk

from .api.events import EventType
from .api.models import (
    HostKeyDecision,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionDecisionRequest,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PassphrasePrompt,
    PasswordPrompt,
    RememberPolicy,
    SecretDecision,
    SessionId,
)


class DaemonInteractionDialogs:
    """Own at most one dialog per visible daemon interaction."""

    def __init__(self, client, bridge, parent: Gtk.Widget) -> None:
        self._client = client
        self._bridge = bridge
        self._parent = parent
        self._session_id: Optional[SessionId] = None
        self._dialogs = {}
        self._pending_secrets = {}
        self._claimed = set()
        self._claims = {}
        self._closed = False
        self._subscription = client.subscribe_events(self._on_transport_event)

    def set_session(self, session_id: SessionId) -> None:
        self._session_id = session_id

    def _on_transport_event(self, event) -> None:
        if event.type not in {
            EventType.INTERACTION_CREATED,
            EventType.INTERACTION_STATE_CHANGED,
        }:
            return
        GLib.idle_add(self._handle_event, event.payload)

    def _handle_event(self, summary: InteractionSummary) -> bool:
        if self._closed:
            return False
        if self._session_id is not None and summary.session_id != self._session_id:
            return False
        if summary.state in {
            InteractionState.ANSWERED,
            InteractionState.CANCELLED,
            InteractionState.EXPIRED,
            InteractionState.FAILED,
        }:
            self._dismiss(summary.id)
            return False
        if summary.id in self._dialogs or summary.id in self._claimed:
            return False
        self._bridge.submit_interaction(
            lambda: self._client.claim_interaction(summary.id),
            on_success=lambda claim: self._claimed_and_present(summary, claim),
            on_error=lambda _error: None,
        )
        return False

    def _claimed_and_present(self, summary: InteractionSummary, claim) -> None:
        if self._closed:
            try:
                self._bridge.submit_interaction(
                    lambda: self._client.release_interaction(summary.id),
                    on_success=lambda _value: None,
                    on_error=lambda _error: None,
                )
            except RuntimeError:
                pass
            return
        self._claimed.add(summary.id)
        self._claims[summary.id] = claim.nonce
        self._present(summary)

    def _present(self, summary: InteractionSummary) -> None:
        if self._closed or summary.id in self._dialogs:
            return
        parent = self._parent.get_root()
        if not isinstance(parent, Gtk.Window):
            parent = self._parent
        if summary.type is InteractionType.HOST_KEY_CONFIRMATION:
            self._present_host_key(summary, parent)
        elif summary.type in {
            InteractionType.PASSWORD,
            InteractionType.PRIVATE_KEY_PASSPHRASE,
        }:
            self._present_secret(summary, parent)

    def _present_host_key(
        self,
        summary: InteractionSummary,
        parent: Gtk.Widget,
    ) -> None:
        prompt = summary.prompt
        if not isinstance(prompt, HostKeyPrompt):
            return
        changed = prompt.status in {HostKeyStatus.CHANGED, HostKeyStatus.REVOKED}
        heading = (
            "SSH host key changed"
            if changed
            else "Verify this SSH host"
        )
        body = (
            f"{prompt.hostname}:{prompt.port}\n"
            f"{prompt.key_type}\n{prompt.fingerprint}"
        )
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("reject", "Reject")
        if not changed:
            dialog.add_response("once", "Accept Once")
            dialog.add_response("store", "Save and Accept")
            dialog.set_response_appearance(
                "store",
                Adw.ResponseAppearance.SUGGESTED,
            )
        dialog.set_default_response("reject")
        dialog.set_close_response("reject")
        dialog.connect(
            "response",
            lambda item, response: self._host_key_response(
                summary,
                item,
                response,
            ),
        )
        self._dialogs[summary.id] = dialog
        dialog.present(parent)

    def _host_key_response(self, summary, dialog, response: str) -> None:
        self._dialogs.pop(summary.id, None)
        decision = {
            "once": HostKeyDecision.ACCEPT_ONCE,
            "store": HostKeyDecision.ACCEPT_AND_STORE,
        }.get(response, HostKeyDecision.REJECT)
        self._bridge.submit_interaction(
            lambda: self._client.respond_to_interaction(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    host_key_decision=decision,
                )
            ),
            on_success=lambda _value: None,
            on_error=lambda _error: None,
        )
        dialog.close()

    def _present_secret(
        self,
        summary: InteractionSummary,
        parent: Gtk.Widget,
    ) -> None:
        prompt = summary.prompt
        if isinstance(prompt, PasswordPrompt):
            heading = f"Password for {prompt.username}@{prompt.hostname}"
            can_remember = prompt.can_remember
        elif isinstance(prompt, PassphrasePrompt):
            heading = f"Passphrase for {prompt.key_display_name}"
            can_remember = prompt.can_remember
        else:
            return
        dialog = Adw.AlertDialog(
            heading=heading,
            body=f"Authentication attempt {summary.attempt}",
        )
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        entry = Gtk.PasswordEntry(show_peek_icon=True)
        entry.connect("activate", lambda _entry: dialog.response("submit"))
        content.append(entry)
        remember = Gtk.CheckButton(label="Remember after authentication succeeds")
        remember.set_visible(can_remember)
        content.append(remember)
        dialog.set_extra_child(content)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("submit", "Continue")
        dialog.set_response_appearance(
            "submit",
            Adw.ResponseAppearance.SUGGESTED,
        )
        dialog.set_default_response("submit")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda item, response: self._secret_response(
                summary,
                item,
                response,
                entry,
                remember,
            ),
        )
        self._dialogs[summary.id] = dialog
        dialog.present(parent)

    def _secret_response(
        self,
        summary,
        dialog,
        response: str,
        entry: Gtk.PasswordEntry,
        remember: Gtk.CheckButton,
    ) -> None:
        self._dialogs.pop(summary.id, None)
        if response != "submit":
            self._bridge.submit_interaction(
                lambda: self._client.respond_to_interaction(
                    InteractionDecisionRequest(
                        interaction_id=summary.id,
                        secret_decision=SecretDecision.CANCEL,
                    )
                ),
                on_success=lambda _value: None,
                on_error=lambda _error: None,
            )
            dialog.close()
            return
        secret = bytearray(entry.get_text().encode("utf-8"))
        entry.set_text("")
        self._pending_secrets[summary.id] = secret
        remember_policy = (
            RememberPolicy.STORE_AFTER_SUCCESS
            if remember.get_active()
            else RememberPolicy.DO_NOT_STORE
        )

        def _send() -> None:
            nonce = self._claims.get(summary.id)
            if nonce is None:
                claim = self._client.claim_interaction(summary.id)
                nonce = claim.nonce
                self._claims[summary.id] = nonce
            self._client.respond_to_interaction(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    secret_decision=SecretDecision.SUBMIT,
                    remember_policy=remember_policy,
                )
            )
            self._client.send_interaction_secret(
                summary.id,
                nonce,
                secret,
            )

        self._bridge.submit_interaction(
            _send,
            on_success=lambda _value: self._finish_secret(summary.id, secret),
            on_error=lambda _error: self._finish_secret(summary.id, secret),
        )
        dialog.close()

    def _finish_secret(self, interaction_id, secret: bytearray) -> None:
        self._pending_secrets.pop(interaction_id, None)
        self._clear_secret(secret)

    @staticmethod
    def _clear_secret(secret: bytearray) -> None:
        secret[:] = b"\0" * len(secret)
        secret.clear()

    def _dismiss(self, interaction_id) -> None:
        self._claimed.discard(interaction_id)
        self._claims.pop(interaction_id, None)
        dialog = self._dialogs.pop(interaction_id, None)
        if dialog is not None:
            dialog.close()
        secret = self._pending_secrets.pop(interaction_id, None)
        if secret is not None:
            self._clear_secret(secret)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._subscription.close()
        claimed = tuple(self._claimed)
        self._claimed.clear()
        self._claims.clear()
        dialogs = tuple(self._dialogs.values())
        self._dialogs.clear()
        secrets = tuple(self._pending_secrets.values())
        self._pending_secrets.clear()
        for dialog in dialogs:
            dialog.close()
        for secret in secrets:
            self._clear_secret(secret)
        for interaction_id in claimed:
            try:
                self._bridge.submit_interaction(
                    lambda value=interaction_id: self._client.release_interaction(
                        value
                    ),
                    on_success=lambda _value: None,
                    on_error=lambda _error: None,
                )
            except RuntimeError:
                break
