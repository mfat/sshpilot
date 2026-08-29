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
    SecretPromptKind,
)
from ..daemon.secret_backend_service import (
    MASTER_PASSWORD_PROMPT_TITLE,
    is_secret_service_session,
)
from ..daemon_interaction_dialogs import DaemonInteractionDialogs
from ..i18n import N_


_SECRET_PROMPT_MESSAGES = {
    SecretPromptKind.BITWARDEN_SIGN_IN: (
        N_("Bitwarden sign-in"),
        N_("Enter the Bitwarden master password for {email}"),
    ),
    SecretPromptKind.BITWARDEN_AUTHENTICATION_CHALLENGE: (
        N_("Authentication challenge"),
        N_(
            "Enter the Bitwarden API client secret to complete the "
            "authentication challenge"
        ),
    ),
    SecretPromptKind.BITWARDEN_TWO_STEP_LOGIN: (
        N_("Two-step login code"),
        N_("Enter the two-step login code for {email}"),
    ),
    SecretPromptKind.BITWARDEN_API_KEY: (
        N_("Bitwarden API key"),
        N_("Enter the API key client secret for {client_id}"),
    ),
    SecretPromptKind.BITWARDEN_UNLOCK: (
        N_("Unlock Bitwarden"),
        N_("Enter the Bitwarden master password to unlock the vault"),
    ),
    SecretPromptKind.KEEPASS_DATABASE_CREATE: (
        N_("New KeePass database"),
        N_("Enter a master password for the new KeePass database"),
    ),
    SecretPromptKind.KEEPASS_UNLOCK: (
        N_("Unlock KeePass"),
        N_("Enter the master password to unlock the KeePass database"),
    ),
    SecretPromptKind.REMEMBER_MASTER_PASSWORD: (
        N_("Remember master password"),
        N_("Enter the master password to remember for {name}"),
    ),
    SecretPromptKind.BACKUP_ENCRYPT: (
        N_("Encrypt backup"),
        N_("Enter a passphrase to encrypt the backup"),
    ),
    SecretPromptKind.BACKUP_DECRYPT: (
        N_("Decrypt backup"),
        N_("Enter the passphrase to decrypt the backup"),
    ),
}


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
            if prompt.secret_prompt_kind is not None:
                self._present_titled_secret(summary, prompt, parent)
            elif prompt.username == MASTER_PASSWORD_PROMPT_TITLE:
                self._present_master_password(summary, prompt, parent)
            else:
                super()._present_secret(summary, parent)
            return
        super()._present_secret(summary, parent)

    def _present_titled_secret(self, summary, prompt: PasswordPrompt, parent) -> None:
        """Any other protected secret the daemon asks for: a two-step login
        code, an API client secret, a backup passphrase.

        The daemon sends only a stable prompt kind and safe dynamic parameters.
        This frontend maps that code to gettext msgids, translates at display
        time, and only then formats the parameters. No remember checkbox is
        shown, since none of these values are stored.
        """
        from ..window_dialogs import present_for_modal_dialog, show_ssh_password_dialog

        kind = prompt.secret_prompt_kind
        if kind is None:
            raise ValueError("structured secret prompt kind is required")
        present_for_modal_dialog(parent)
        self._dialogs[summary.id] = None
        title_msgid, body_msgid = _SECRET_PROMPT_MESSAGES[kind]
        parameters = dict(prompt.secret_prompt_parameters)
        heading = _(title_msgid).format(**parameters)
        body = _(body_msgid).format(**parameters)
        if body and not body.endswith((".", "?", "!")):
            body = f"{body}."
        try:
            value = show_ssh_password_dialog(
                parent_window=parent,
                display_name=heading,
                heading=heading,
                body=body,
                allow_store=False,
            )
        finally:
            self._dialogs.pop(summary.id, None)
        if value:
            self._submit_secret(
                summary,
                bytearray(value.encode("utf-8")),
                remember_policy=RememberPolicy.DO_NOT_STORE,
            )
        else:
            self._cancel_secret(summary)

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
