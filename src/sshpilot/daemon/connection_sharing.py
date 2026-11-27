"""Log and publish what each launch did with the host's shared connection.

Terminal sessions read the outcome from their private ``-E`` diagnostics
(``SessionRuntime``), SFTP services from the stderr of their ``ssh -s sftp``
(``SubprocessSftpProcessRunner``); both hand it here, so ``daemon.log`` reads
the same for both and every client receives one ``connection.sharing`` event.

The log line is the diagnostic record: the outcome, the launch kind, the
socket, and OpenSSH's own line that settled it, tagged with the connection
and scope like every other launch trace. Outcomes where sharing was meant to
happen and did not are WARNINGs.
"""

from __future__ import annotations

import logging
from typing import Callable

from sshpilot.api.events import EventType
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.connection_sharing import (
    ConnectionSharingNotice,
    ConnectionSharingOutcome,
    ConnectionSharingScope,
)
from sshpilot.core.connection_sharing import ConnectionSharingObservation
from sshpilot.logging_support import log_context

logger = logging.getLogger(__name__)

#: Why each outcome matters, for someone reading the log.
_MEANING = {
    ConnectionSharingOutcome.OPENED: "opened the shared connection",
    ConnectionSharingOutcome.JOINED: "joined the shared connection",
    ConnectionSharingOutcome.REFUSED: (
        "logged in on its own: a shared connection is open but ControlMaster "
        "yes only ever opens one (auto joins it)"
    ),
    ConnectionSharingOutcome.UNAVAILABLE: (
        "logged in on its own: no shared connection was open and the host "
        "is not set to open one"
    ),
    ConnectionSharingOutcome.FAILED: "could not open the shared connection's socket",
}

Publish = Callable[..., object]


class ConnectionSharingReporter:
    """Turn one launch's observation into a log line and a public event."""

    def __init__(self, publish: Publish) -> None:
        if not callable(publish):
            raise TypeError("connection sharing publisher must be callable")
        self._publish = publish

    def report(
        self,
        connection_id: ConnectionId,
        scope_id: str,
        scope: ConnectionSharingScope,
        observation: ConnectionSharingObservation,
    ) -> None:
        outcome = observation.outcome
        context = (
            {"session": scope_id}
            if scope is ConnectionSharingScope.TERMINAL
            else {"sftp_service": scope_id}
        )
        level = logging.WARNING if outcome.problem else logging.INFO
        with log_context(connection=connection_id, **context):
            logger.log(
                level,
                "connection sharing %s kind=%s control_path=%s: %s (ssh: %s)",
                outcome.value,
                scope.value,
                observation.control_path or "-",
                _MEANING[outcome],
                observation.line,
            )
        try:
            notice = ConnectionSharingNotice(
                connection_id=connection_id,
                scope_id=str(scope_id),
                scope=scope,
                outcome=outcome,
                control_path=observation.control_path[
                    : ConnectionSharingNotice.MAX_CONTROL_PATH_CHARS
                ],
            )
            self._publish(
                EventType.CONNECTION_SHARING,
                notice,
                connection_id=connection_id,
                session_id=str(scope_id),
            )
        except RuntimeError:
            # The publisher closes during shutdown; the log line stays.
            logger.debug("connection sharing notice was not published")
        except (TypeError, ValueError):
            logger.debug("connection sharing notice is invalid", exc_info=True)
