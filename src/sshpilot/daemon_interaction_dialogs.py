"""Experimental GTK dialogs for daemon-owned typed SSH interactions."""

from __future__ import annotations

from typing import Optional

from gi.repository import Adw, GLib, Gtk

from .api.events import EventType
from .api.models import (
    ChallengePrompt,
    ConfirmationPrompt,
    HostKeyDecision,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionDecisionRequest,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PassphrasePrompt,
    PasswordPrompt,
    PresencePrompt,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from .daemon.secret_backend_service import is_secret_service_session


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

    def set_parent(self, parent: Gtk.Widget) -> None:
        """Reparent future dialogs (e.g. once the real window exists)."""
        self._parent = parent

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
        if is_secret_service_session(summary.session_id):
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
        from .window_dialogs import present_for_modal_dialog, resolve_app_modal_parent

        parent = self._parent
        if not isinstance(parent, Gtk.Window):
            try:
                parent = resolve_app_modal_parent(self._parent)
            except RuntimeError:
                parent = self._parent.get_root()
                if not isinstance(parent, Gtk.Window):
                    parent = self._parent
        if isinstance(parent, Gtk.Window):
            # Raise the parent so the modal child stacks above it (Wayland).
            present_for_modal_dialog(parent)
        if summary.type is InteractionType.HOST_KEY_CONFIRMATION:
            self._present_host_key(summary, parent)
        elif summary.type in {
            InteractionType.PASSWORD,
            InteractionType.PRIVATE_KEY_PASSPHRASE,
            InteractionType.KEYBOARD_INTERACTIVE,
            InteractionType.SECURITY_KEY_PRESENCE,
            InteractionType.CONFIRMATION,
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
            dialog.add_response("accept", "Accept")
            dialog.set_response_appearance(
                "accept",
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
            "accept": HostKeyDecision.ACCEPT,
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
            self._present_shared_password(summary, prompt, parent)
            return
        if isinstance(prompt, PassphrasePrompt):
            heading = f"Passphrase for {prompt.key_display_name}"
        elif isinstance(prompt, ChallengePrompt):
            heading = "SSH authentication challenge"
        elif isinstance(prompt, PresencePrompt):
            heading = "Security key required"
        elif isinstance(prompt, ConfirmationPrompt):
            heading = "Confirm SSH operation"
        else:
            return
        dialog = Adw.AlertDialog(
            heading=heading,
            body=(
                prompt.text
                if isinstance(
                    prompt, (ChallengePrompt, PresencePrompt, ConfirmationPrompt)
                )
                else f"Authentication attempt {summary.attempt}"
            ),
        )
        if isinstance(prompt, PresencePrompt):
            dialog.add_response("close", "Close")
            dialog.set_close_response("close")
            dialog.connect(
                "response",
                lambda item, _response: self._presence_closed(summary, item),
            )
            self._dialogs[summary.id] = dialog
            dialog.present(parent)
            return
        if isinstance(prompt, ConfirmationPrompt):
            dialog.add_response("no", "No")
            dialog.add_response("yes", "Yes")
            dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("yes")
            dialog.set_close_response("no")
            dialog.connect(
                "response",
                lambda item, response: self._confirmation_response(
                    summary, item, response
                ),
            )
            self._dialogs[summary.id] = dialog
            dialog.present(parent)
            return


        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        entry = Gtk.PasswordEntry(show_peek_icon=True)
        # AlertDialog exposes the response signal and virtual handler, but not
        # the Adw.MessageDialog-style ``response()`` convenience method.
        entry.connect("activate", lambda _entry: dialog.do_response("submit"))
        content.append(entry)
        dialog.set_extra_child(content)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response(
            "submit",
            "Yes" if isinstance(prompt, ConfirmationPrompt) else "Continue",
        )
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
            ),
        )
        self._dialogs[summary.id] = dialog
        dialog.present(parent)

        # AlertDialog maps its extra child during presentation; defer the
        # focus request until then so typing starts in the password field.
        def _focus_entry() -> bool:
            if summary.id not in self._dialogs:
                return GLib.SOURCE_REMOVE
            entry.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_focus_entry)

    def _present_shared_password(
        self,
        summary: InteractionSummary,
        prompt: PasswordPrompt,
        parent: Gtk.Widget,
    ) -> None:
        """Use the standard password UI, then submit through the broker."""
        from .window_dialogs import present_for_modal_dialog, show_ssh_password_dialog

        # ``parent`` is resolved from the app window in _present().  Explicitly
        # prepare it for modal presentation because this call uses the shared
        # helper's parent_window escape hatch.
        present_for_modal_dialog(parent)
        self._dialogs[summary.id] = None
        try:
            value = show_ssh_password_dialog(
                parent_window=parent,
                display_name=f"{prompt.username}@{prompt.hostname}",
                host=prompt.hostname,
                username=prompt.username,
                allow_store=False,
            )
        finally:
            self._dialogs.pop(summary.id, None)
        if value:
            self._submit_secret(summary, bytearray(value.encode("utf-8")))
        else:
            self._cancel_secret(summary)

    def _presence_closed(self, summary, dialog) -> None:
        self._dialogs.pop(summary.id, None)
        self._bridge.submit_interaction(
            lambda: self._client.cancel_interaction(summary.id),
            on_success=lambda _value: None,
            on_error=lambda _error: None,
        )
        dialog.close()

    def _confirmation_response(self, summary, dialog, response: str) -> None:
        self._dialogs.pop(summary.id, None)
        if response != "yes":
            self._cancel_secret(summary)
        else:
            self._submit_secret(summary, bytearray(b"yes"))
        dialog.close()

    def _cancel_secret(self, summary) -> None:
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


    def _secret_response(
        self,
        summary,
        dialog,
        response: str,
        entry: Gtk.PasswordEntry,
    ) -> None:
        self._dialogs.pop(summary.id, None)
        if response != "submit":
            self._cancel_secret(summary)
            dialog.close()
            return
        secret = bytearray(entry.get_text().encode("utf-8"))
        entry.set_text("")
        self._submit_secret(summary, secret)
        dialog.close()

    def _submit_secret(self, summary, secret: bytearray) -> None:
        self._pending_secrets[summary.id] = secret

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
                    remember_policy=RememberPolicy.DO_NOT_STORE,
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
            if dialog is not None:
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
