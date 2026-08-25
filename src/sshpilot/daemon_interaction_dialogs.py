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
        """Bind this presenter to exactly one daemon interaction scope.

        Binding is race-safe: an interaction created before the frontend
        learned the scope ID (a prompt raised between ``deploy_key()``
        starting and the OperationSummary arriving, or during an SFTP
        handshake) is reconciled from the daemon's live interaction snapshot.
        Reconciliation is idempotent: event-driven delivery and the snapshot
        are deduplicated by interaction id, so nothing is presented twice.
        Repeated binds to the same scope are no-ops.
        """
        if session_id == self._session_id:
            return
        self._session_id = session_id
        self._reconcile()

    def _reconcile(self) -> None:
        """Pull currently pending interactions for the bound scope."""
        if self._closed or self._session_id is None:
            return
        try:
            self._bridge.submit_interaction(
                lambda: self._client.list_interactions(),
                on_success=lambda summaries: self._reconcile_present(summaries),
                on_error=lambda _error: None,
            )
        except RuntimeError:
            # Bridge already closed while shutting down; nothing to present.
            pass

    def _reconcile_present(self, summaries) -> None:
        """Feed only our own scope's interactions through the normal path."""
        if self._closed or self._session_id is None:
            return
        for summary in summaries or ():
            if summary.session_id != self._session_id:
                continue
            self._handle_event(summary)

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
        # An unbound presenter must never claim or display an unrelated
        # prompt. With no scope set, no interaction is ours — in particular
        # the File Manager, Authorized Keys, a terminal and ssh-copy-id can
        # all operate concurrently under one frontend client, and an unbound
        # presenter must not act as a wildcard for any of them.
        if self._session_id is None:
            return False
        if summary.session_id != self._session_id:
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
        # Reserve the interaction before the asynchronous claim starts.  The
        # event stream and set_session() reconciliation can report the same
        # pending interaction concurrently; waiting until claim completion to
        # mark it would submit two claim requests.
        self._claimed.add(summary.id)
        try:
            self._bridge.submit_interaction(
                lambda: self._client.claim_interaction(summary.id),
                on_success=lambda claim: self._claimed_and_present(summary, claim),
                on_error=lambda _error: self._claim_failed(summary.id),
            )
        except RuntimeError:
            self._claim_failed(summary.id)
        return False

    def _claim_failed(self, interaction_id) -> None:
        """Release an in-flight claim reservation after submission failure."""
        self._claimed.discard(interaction_id)
        self._claims.pop(interaction_id, None)

    def _claimed_and_present(self, summary: InteractionSummary, claim) -> None:
        if self._closed or summary.id not in self._claimed:
            try:
                self._bridge.submit_interaction(
                    lambda: self._client.release_interaction(summary.id),
                    on_success=lambda _value: None,
                    on_error=lambda _error: None,
                )
            except RuntimeError:
                pass
            return
        self._claims[summary.id] = claim.nonce
        self._present(summary)

    def _resolve_present_parent(self):
        """Return the window the interaction dialog should be presented on.

        A visible explicit secondary window keeps hosting its own prompts.
        When the configured parent is the main window, a visible modal
        secondary window takes precedence so prompts cannot appear behind it.

        The configured parent is used only when it is a *visible* window. A
        window that is not on screen must never be force-presented behind the
        prompt: an embedded FileManagerWindow whose content was detached into a
        tab (and which is removed from the application) is a blank,
        decoration-less shell with no window controls — presenting it shows
        exactly that empty box behind the password dialog. In that case the
        live application window is resolved instead.
        """
        parent = self._parent

        if self._parent_window_is_visible(parent):
            return self._resolve_topmost_if_main_window(parent)

        from .window_dialogs import resolve_app_modal_parent

        try:
            resolved = resolve_app_modal_parent(self._parent)
        except RuntimeError:
            try:
                root = self._parent.get_root()
            except Exception:
                root = None

            if self._parent_window_is_visible(root):
                return self._resolve_topmost_if_main_window(root)

            return self._parent

        return self._resolve_topmost_if_main_window(resolved)

    @staticmethod
    def _resolve_topmost_if_main_window(window):
        """Prefer a blocking modal secondary window over the app main window."""
        try:
            app = window.get_application()
        except Exception:
            try:
                app = Gtk.Application.get_default()
            except Exception:
                app = None

        if app is None:
            return window

        try:
            windows = list(app.get_windows())
        except Exception:
            windows = []

        main_window = getattr(app, "window", None)
        if main_window is None:
            main_window = next(
                (
                    candidate
                    for candidate in windows
                    if candidate.__class__.__name__ == "MainWindow"
                ),
                None,
            )

        if window is not main_window:
            return window

        try:
            active_window = app.get_active_window()
        except Exception:
            active_window = None

        from .window_dialogs import resolve_topmost_prompt_parent

        return resolve_topmost_prompt_parent(
            windows,
            active_window,
            main_window,
        )

    @staticmethod
    def _parent_window_is_visible(widget) -> bool:
        """True when *widget* is a Gtk.Window currently shown on screen.

        The probe is guarded: a window destroyed mid-race (e.g. the owning
        window closed while an interaction event is still in flight, before
        the presenter is disposed) must not raise out of the idle callback —
        it just reads as not visible and falls back to the resolved parent.
        """
        if not isinstance(widget, Gtk.Window):
            return False
        try:
            return bool(widget.get_visible())
        except Exception:
            return False

    def _present(self, summary: InteractionSummary) -> None:
        if self._closed or summary.id in self._dialogs:
            return
        from .window_dialogs import present_for_modal_dialog

        parent = self._resolve_present_parent()
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
                else "Enter and confirm a new passphrase."
                if isinstance(prompt, PassphrasePrompt)
                and prompt.confirmation_required
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
        confirmation_entry = None
        mismatch_label = None
        if isinstance(prompt, PassphrasePrompt) and prompt.confirmation_required:
            entry = Adw.PasswordEntryRow(title="Passphrase")
            confirmation_entry = Adw.PasswordEntryRow(title="Confirm passphrase")
            group = Adw.PreferencesGroup()
            group.add(entry)
            group.add(confirmation_entry)
            content.append(group)

            mismatch_label = Gtk.Label(
                label="Passphrases do not match",
                xalign=0,
                visible=False,
            )
            mismatch_label.add_css_class("error")
            mismatch_label.add_css_class("caption")
            content.append(mismatch_label)
        else:
            entry = Gtk.PasswordEntry(show_peek_icon=True)
            # AlertDialog exposes a response signal, but no public response()
            # convenience method.  do_response() is its language-binding virtual
            # handler and cannot be invoked as the signal emitter.
            entry.connect(
                "activate",
                lambda _entry: self._activate_secret_dialog(dialog),
            )
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
        if confirmation_entry is not None:
            dialog.set_response_enabled("submit", False)
            entry.connect(
                "notify::text",
                lambda *_args: self._sync_passphrase_confirmation(
                    dialog,
                    entry,
                    confirmation_entry,
                    mismatch_label,
                ),
            )
            confirmation_entry.connect(
                "notify::text",
                lambda *_args: self._sync_passphrase_confirmation(
                    dialog,
                    entry,
                    confirmation_entry,
                    mismatch_label,
                ),
            )
            entry.connect(
                "entry-activated",
                lambda _entry: confirmation_entry.grab_focus(),
            )
            confirmation_entry.connect(
                "entry-activated",
                lambda _entry: self._activate_confirmed_secret_dialog(
                    dialog,
                    entry,
                    confirmation_entry,
                    mismatch_label,
                ),
            )
        dialog.connect(
            "response",
            lambda item, response: self._secret_response(
                summary,
                item,
                response,
                entry,
                confirmation_entry,
                mismatch_label,
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

    @staticmethod
    def _activate_secret_dialog(dialog) -> None:
        """Submit the default secret response from the password entry."""
        dialog.emit("response", "submit")

    @staticmethod
    def _passphrase_confirmation_is_valid(entry, confirmation_entry) -> bool:
        """Return whether both key-generation passphrase rows match."""
        passphrase = entry.get_text()
        return bool(passphrase) and passphrase == confirmation_entry.get_text()

    @classmethod
    def _sync_passphrase_confirmation(
        cls,
        dialog,
        entry,
        confirmation_entry,
        mismatch_label,
    ) -> bool:
        """Keep the key-generation submit response and mismatch state in sync."""
        valid = cls._passphrase_confirmation_is_valid(entry, confirmation_entry)
        confirmation = confirmation_entry.get_text()
        mismatch = bool(confirmation) and entry.get_text() != confirmation
        dialog.set_response_enabled("submit", valid)
        mismatch_label.set_visible(mismatch)
        if mismatch:
            confirmation_entry.add_css_class("error")
        else:
            confirmation_entry.remove_css_class("error")
        return valid

    @classmethod
    def _activate_confirmed_secret_dialog(
        cls,
        dialog,
        entry,
        confirmation_entry,
        mismatch_label,
    ) -> None:
        """Submit from the confirmation row only after both values match."""
        if cls._sync_passphrase_confirmation(
            dialog,
            entry,
            confirmation_entry,
            mismatch_label,
        ):
            dialog.emit("response", "submit")
        else:
            confirmation_entry.grab_focus()

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
        entry,
        confirmation_entry=None,
        mismatch_label=None,
    ) -> None:
        if response == "submit" and confirmation_entry is not None:
            if not self._sync_passphrase_confirmation(
                dialog,
                entry,
                confirmation_entry,
                mismatch_label,
            ):
                confirmation_entry.grab_focus()
                return
        self._dialogs.pop(summary.id, None)
        if response != "submit":
            entry.set_text("")
            if confirmation_entry is not None:
                confirmation_entry.set_text("")
            self._cancel_secret(summary)
            dialog.close()
            return
        secret = bytearray(entry.get_text().encode("utf-8"))
        entry.set_text("")
        if confirmation_entry is not None:
            confirmation_entry.set_text("")
        self._submit_secret(summary, secret)
        dialog.close()

    def _submit_secret(
        self,
        summary,
        secret: bytearray,
        remember_policy: RememberPolicy = RememberPolicy.DO_NOT_STORE,
    ) -> None:
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
