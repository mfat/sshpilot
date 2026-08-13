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

from ..api.models import InteractionState, InteractionSummary
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
        self._bridge.submit_interaction(
            lambda: self._client.claim_interaction(summary.id),
            on_success=lambda claim: self._claimed_and_present(summary, claim),
            on_error=lambda _error: None,
        )
        return False
