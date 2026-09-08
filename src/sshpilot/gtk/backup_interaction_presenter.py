"""GTK presenter for the SSH prompts an SSH-server backup raises.

Exporting to, listing on, or importing from an SSH server connects from
*inside* the daemon: it opens the backup's transport itself -- the SFTP
service the file manager uses, or the one-shot command service behind Host
Info when a host has no ``sftp`` subsystem. Those connects raise the
connection's own prompts (login password, key passphrase, unknown host key)
under a scope id that never reaches the frontend, so unlike the file manager
or Host Info there is no id to hand to ``set_session()``.

What the frontend does know is which connection it asked the daemon to back up
to, and that a backup call against it is in flight. That pair is the scope
here. Everything else -- claiming, presenting, submitting the secret -- is the
shared machinery in :class:`~sshpilot.daemon_interaction_dialogs.DaemonInteractionDialogs`.
"""
from __future__ import annotations

from typing import Iterable

from ..api.models import InteractionSummary
from ..daemon_interaction_dialogs import DaemonInteractionDialogs


class BackupServerInteractionPresenter(DaemonInteractionDialogs):
    """Present a backup's SSH prompts for the duration of one backup call.

    Scoped to a connection rather than to a session id. The daemon shows an
    interaction only to the client that owns its scope, so the reach of that
    rule is one frontend's own prompts for one host, while this presenter is
    open. If the user starts a terminal or file-manager connection to that same
    host mid-backup, this presenter may claim that connect's password prompt
    first; the user still answers one prompt for the host and both flows
    proceed. Open it immediately before the backup call and close it as soon as
    the call returns, so the window stays that narrow.
    """

    def __init__(
        self,
        client,
        bridge,
        parent,
        *,
        connection_ids: Iterable[str],
    ) -> None:
        # Set before subscribing: the base constructor starts delivering
        # events, and the ownership test below reads this.
        self._connection_ids = {
            str(value) for value in connection_ids if str(value or "").strip()
        }
        super().__init__(client, bridge, parent)
        # A prompt raised between the backup call starting and this presenter
        # existing is still pending in the daemon; reconcile picks it up.
        #
        # Past the base constructor this object is subscribed to the event
        # stream, so it cannot simply be abandoned: the caller wraps
        # construction and treats a failure as "no presenter", which would
        # leave a live one claiming and presenting every prompt for this
        # connection for the rest of the session. Close it before the failure
        # escapes.
        try:
            self._reconcile()
        except BaseException:
            self.close()
            raise

    def _scope_is_bound(self) -> bool:
        """Bound at construction: the connection being backed up is the scope."""
        return bool(self._connection_ids)

    def _is_ours(self, summary: InteractionSummary) -> bool:
        """Prompts for the backup's own connection, and nothing else.

        The backup's *passphrase* prompt is not one of these: the secret
        backend service raises it in the reserved ``secret-session`` namespace,
        where :class:`~sshpilot.gtk.secrets_interaction_presenter.SecretsInteractionPresenter`
        already owns it.
        """
        if self._is_secret_backend_scope(summary):
            return False
        return str(summary.connection_id) in self._connection_ids
