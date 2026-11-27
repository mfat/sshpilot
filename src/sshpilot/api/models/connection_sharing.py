"""Frontend-neutral notices for OpenSSH connection sharing (ControlMaster).

A host set to share one connection (``ControlMaster`` with a ``ControlPath``,
in its Host block or through Settings ▸ SSH Options) should open the shared
connection once and have every later terminal and file browser join it. When
that does not happen, ssh connects anyway -- with ``ControlMaster yes`` and a
master already running, every new launch logs in again -- so nothing failed
that a user could see, except a second password prompt.

The daemon reads the outcome from OpenSSH's own log of each launch it owns
(the private ``-E`` stream of a terminal, the stderr of an SFTP service) and
publishes one :class:`ConnectionSharingNotice` per launch that involved
sharing. A launch whose host does not share publishes nothing.

What travels is the outcome code and the socket path. The wording belongs to
``sshpilot.gtk.connection_sharing_messages``, which owns translation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .common import ConnectionId, require_identifier


class ConnectionSharingOutcome(str, Enum):
    """What one launch did with the host's shared connection."""

    #: It opened the shared connection: later launches can join it.
    OPENED = "opened"
    #: It joined a shared connection already open: no new login.
    JOINED = "joined"
    #: A shared connection was open but this launch would not join it, and
    #: logged in on its own (``ControlMaster yes``: ssh only ever opens one).
    REFUSED = "refused"
    #: It looked for a shared connection, found none, and was not set to open
    #: one (``ControlMaster no``): it logged in on its own.
    UNAVAILABLE = "unavailable"
    #: It could not open the shared connection's socket (a missing directory,
    #: a path too long); ssh exits, so the launch fails with it.
    FAILED = "failed"

    @property
    def shared(self) -> bool:
        """Whether the launch rides a shared connection."""
        return self in (ConnectionSharingOutcome.OPENED, ConnectionSharingOutcome.JOINED)

    @property
    def problem(self) -> bool:
        """Whether sharing was meant to happen and did not."""
        return self in (ConnectionSharingOutcome.REFUSED, ConnectionSharingOutcome.FAILED)


class ConnectionSharingScope(str, Enum):
    """Which kind of launch the notice is about."""

    TERMINAL = "terminal"
    SFTP = "sftp"


@dataclass(frozen=True)
class ConnectionSharingNotice:
    """One launch's connection-sharing outcome.

    :attr:`scope_id` is the session id of a terminal or the service id of an
    SFTP service. :attr:`control_path` is the socket OpenSSH used, expanded,
    when its log named it.
    """

    connection_id: ConnectionId
    scope_id: str
    scope: ConnectionSharingScope
    outcome: ConnectionSharingOutcome
    control_path: str = ""

    MAX_CONTROL_PATH_CHARS = 4096

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.scope_id, "connection sharing scope id")
        if not isinstance(self.scope, ConnectionSharingScope):
            raise TypeError("scope must be a ConnectionSharingScope")
        if not isinstance(self.outcome, ConnectionSharingOutcome):
            raise TypeError("outcome must be a ConnectionSharingOutcome")
        if type(self.control_path) is not str:
            raise TypeError("control path must be a string")
        if len(self.control_path) > self.MAX_CONTROL_PATH_CHARS:
            raise ValueError("control path is too long")
