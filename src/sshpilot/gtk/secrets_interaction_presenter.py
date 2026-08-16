"""GTK presenter for daemon-owned secret-backend interactions.

Presents the protected interactions raised by the daemon secret backend service
(master-password unlock, Bitwarden 2FA / API-key / SSO challenges, backup
passphrases). These interactions use the reserved ``secret-session`` namespace
and are intentionally ignored by the session-scoped ``DaemonInteractionDialogs``.

Secret values never cross the wire: the user-entered value is handed to the
daemon as a one-use ``binary-secret-v2`` frame through the client's
``send_interaction_secret``, and cleared from memory after submission.
"""
from __future__ import annotations

from gettext import gettext as _

from ..api.models import (
    InteractionState,
    InteractionSummary,
    PasswordPrompt,
    RememberPolicy,
)
from ..daemon.secret_backend_service import is_secret_service_session
from ..daemon_interaction_dialogs import DaemonInteractionDialogs


class SecretsInteractionPresenter(DaemonInteractionDialogs):
    """Present secret-backend interactions to the frontend.

    Not session-scoped: secret-session interactions carry synthetic session ids
    under ``secret-session`` and are filtered in from that namespace only.
    """

    def _handle_event(self, summary: InteractionSummary) -> bool:
        if self._closed:
            return False
        if not is_secret_service_session(summary.session_id):
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
        # Reserve the interaction before the asynchronous claim starts (see
        # the base class). Regression: this reservation was missing, so
        # _claimed_and_present always found summary.id absent from
        # self._claimed, took its "not ours" branch, and released the
        # interaction right back — no dialog was ever presented for any
        # secret-session interaction (master-password unlock, Bitwarden
        # 2FA/API-key/SSO, backup passphrases), even though the claim RPC
        # itself succeeded.
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

    def _present_secret(self, summary, parent) -> None:
        prompt = summary.prompt
        if isinstance(prompt, PasswordPrompt):
            self._present_master_password(summary, prompt, parent)
            return
        super()._present_secret(summary, parent)

    def _present_master_password(self, summary, prompt: PasswordPrompt, parent) -> None:
        """A vault master-password unlock — its own dialog, not the SSH host
        login one. The base class's ``_present_shared_password`` reuses
        ``show_ssh_password_dialog`` with ``username="Secret backend"`` /
        ``hostname=<message>``, which renders as a garbled
        "Enter your password for Secret backend@Enter the master password to
        unlock keepassxc." That read as broken rather than a real prompt.

        ``prompt.hostname`` here is the bare backend name (see
        ``SecretBackendService._prompt_for_master_password``). The "Remember
        master password" checkbox flows through ``RememberPolicy`` on the
        same interaction response — no second prompt to actually store it.
        """
        from ..secret_unlock_dialog import _friendly_backend_name
        from ..window_dialogs import present_for_modal_dialog, show_ssh_password_dialog

        present_for_modal_dialog(parent)
        self._dialogs[summary.id] = None
        friendly = _friendly_backend_name(prompt.hostname)
        remember = [False]
        try:
            value = show_ssh_password_dialog(
                parent_window=parent,
                display_name=friendly,
                heading=_("Unlock {backend}").format(backend=friendly),
                body=_("Enter the master password for {backend}.").format(
                    backend=friendly
                ),
                allow_store=bool(prompt.can_remember),
                store_label=_("Remember master password"),
                on_store=lambda _password: remember.__setitem__(0, True),
            )
        finally:
            self._dialogs.pop(summary.id, None)
        if value:
            policy = (
                RememberPolicy.STORE_AFTER_SUCCESS
                if remember[0]
                else RememberPolicy.DO_NOT_STORE
            )
            self._submit_secret(
                summary, bytearray(value.encode("utf-8")), remember_policy=policy
            )
        else:
            self._cancel_secret(summary)
