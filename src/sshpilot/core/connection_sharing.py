"""Read one launch's connection-sharing outcome from OpenSSH's own log (GTK-free).

OpenSSH says, at ``LogLevel DEBUG1``, what it did with the ControlPath, and
each outcome has a line of its own (OpenSSH 10)::

    debug1: auto-mux: Trying existing master at '<path>'
    debug1: Control socket "<path>" does not exist        # looked, none there
    debug1: mux_client_request_session: master session id: 2   # joined one
    Authenticated to host ([addr]:22) using "publickey".
    debug1: setting up multiplex master socket
    debug1: channel 0: new mux listener [<path>] ...      # opened one
    ControlSocket <path> already exists, disabling multiplexing  # refused
    unix_listener: cannot bind to path <path>.XXXX: ...   # could not listen
    debug1: channel 1: new session [client-session] ...   # the session starts

The session's own channel opens only after all of that, so a launch that
looked for a shared connection and reaches it without any of the decisive
lines did not get one and was not set to open one. A launch with none of the
control-socket lines does not share at all and has no outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sshpilot.api.models.connection_sharing import ConnectionSharingOutcome

_LOOKED_RE = re.compile(
    r"auto-mux: Trying existing master at '([^']*)'"
    r"|Control socket \"([^\"]*)\" does not exist",
    re.IGNORECASE,
)
_JOINED_RE = re.compile(r"master session id:\s*\d+", re.IGNORECASE)
_AUTHENTICATED_RE = re.compile(r"^authenticated to ", re.IGNORECASE)
_LISTENER_RE = re.compile(r"new mux listener \[([^\]]*)\]", re.IGNORECASE)
_REFUSED_RE = re.compile(
    r"ControlSocket (.+?) already exists, disabling multiplexing", re.IGNORECASE
)
_FAILED_RE = re.compile(
    r"unix_listener: (?:cannot bind to path (\S+?)(?:\.[A-Za-z0-9]{16})?:"
    r"|path \"([^\"]*)\" too long)"
    r"|ControlPath too long \('([^']*)'",
    re.IGNORECASE,
)
_SESSION_START_RE = re.compile(
    r"new session \[client-session\]|entering interactive session",
    re.IGNORECASE,
)
_DEBUG_PREFIX_RE = re.compile(r"^debug\d*:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class ConnectionSharingObservation:
    """The outcome, the ssh line that settled it, and the socket path."""

    outcome: ConnectionSharingOutcome
    line: str
    control_path: str = ""


class ConnectionSharingTracker:
    """Feed OpenSSH log lines; :meth:`take` hands the outcome over once.

    Lines are fed whole, debug prefix and all. The outcome is settled by the
    first decisive line; everything after it is ignored.
    """

    def __init__(self) -> None:
        self._looked = False
        self._authenticated = False
        self._path = ""
        self._decided: Optional[ConnectionSharingObservation] = None
        self._taken = False

    @property
    def decided(self) -> Optional[ConnectionSharingObservation]:
        return self._decided

    def feed_line(self, line: str) -> Optional[ConnectionSharingObservation]:
        """Consume one line; the observation when this line settled it."""
        if self._decided is not None or not line:
            return None
        text = _DEBUG_PREFIX_RE.sub("", line.strip())
        looked = _LOOKED_RE.search(text)
        if looked:
            self._looked = True
            self._path = looked.group(1) or looked.group(2) or self._path
            return None
        if _AUTHENTICATED_RE.search(text):
            self._authenticated = True
            return None
        if _JOINED_RE.search(text):
            # After a login it is the client half of a master this launch
            # opened and backgrounded (ControlPersist), whose listener line
            # already settled it.
            if not self._authenticated:
                return self._decide(ConnectionSharingOutcome.JOINED, text)
            return None
        match = _LISTENER_RE.search(text)
        if match:
            return self._decide(ConnectionSharingOutcome.OPENED, text, match.group(1))
        match = _REFUSED_RE.search(text)
        if match:
            return self._decide(ConnectionSharingOutcome.REFUSED, text, match.group(1))
        match = _FAILED_RE.search(text)
        if match:
            path = next((group for group in match.groups() if group), "")
            return self._decide(ConnectionSharingOutcome.FAILED, text, path)
        if _SESSION_START_RE.search(text) and self._looked:
            return self._decide(ConnectionSharingOutcome.UNAVAILABLE, text)
        return None

    def take(self) -> Optional[ConnectionSharingObservation]:
        """The settled observation, the first time it is asked for."""
        if self._decided is None or self._taken:
            return None
        self._taken = True
        return self._decided

    def _decide(
        self, outcome: ConnectionSharingOutcome, line: str, path: str = ""
    ) -> ConnectionSharingObservation:
        self._decided = ConnectionSharingObservation(
            outcome=outcome, line=line, control_path=path or self._path
        )
        return self._decided
