"""Connection sharing (ControlMaster): read from OpenSSH's log, logged, published.

The log lines are OpenSSH 10.6's, captured against a real sshd for each
outcome. A launch whose host does not share must report nothing.
"""

from __future__ import annotations

import io
import logging
import os
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.events import CoreEvent, EventPublisher, EventType
from sshpilot.api.models.connection_sharing import (
    ConnectionSharingNotice,
    ConnectionSharingOutcome,
    ConnectionSharingScope,
)
from sshpilot.core.connection_sharing import (
    ConnectionSharingObservation,
    ConnectionSharingTracker,
)
from sshpilot.core.ssh_diagnostics import SshDiagnosticParser, SshDiagnosticState

SOCKET = "/run/user/1000/sshpilot/cm/35b51b69cd6b6926659231ffe7f53eb46ae9e8bd"
LOGIN = 'Authenticated to 192.168.8.1 ([192.168.8.1]:22) using "password".'
SESSION = "debug1: channel 1: new session [client-session] (inactive timeout: 0.000000)"
LOOKED = f"debug1: auto-mux: Trying existing master at '{SOCKET}'"
ABSENT = f'debug1: Control socket "{SOCKET}" does not exist'
NOISE = "debug1: pkcs11_del_provider: called, provider_id = (null)"
SETUP = "debug1: setting up multiplex master socket"

LOGS = {
    # ControlMaster auto, nothing open yet (ControlPersist: the master
    # backgrounds, and the launch reattaches to it as a client).
    ConnectionSharingOutcome.OPENED: [
        LOOKED, ABSENT, LOGIN, NOISE, SETUP,
        f"debug1: channel 0: new mux listener [{SOCKET}] (inactive timeout: 0.000000)",
        "debug1: control_persist_detach: backgrounding master process",
        "debug1: mux_client_request_session: master session id: 2",
    ],
    ConnectionSharingOutcome.JOINED: [
        LOOKED, "debug1: mux_client_request_session: master session id: 2",
    ],
    # ControlMaster yes with a master open: the router case.
    ConnectionSharingOutcome.REFUSED: [
        LOGIN, NOISE, SETUP,
        f"ControlSocket {SOCKET} already exists, disabling multiplexing",
        "debug1: channel 0: new session [client-session] (inactive timeout: 0.000000)",
    ],
    # ControlMaster no, nothing open.
    ConnectionSharingOutcome.UNAVAILABLE: [ABSENT, LOGIN, NOISE, SESSION],
    # The socket directory is missing.
    ConnectionSharingOutcome.FAILED: [
        LOOKED, ABSENT, LOGIN, NOISE, SETUP,
        f"unix_listener: cannot bind to path {SOCKET}.IE5ZVjw42yH7dxBU: "
        "No such file or directory",
    ],
}


def _track(lines):
    tracker = ConnectionSharingTracker()
    for line in lines:
        tracker.feed_line(line)
    return tracker


@pytest.mark.parametrize("outcome", list(LOGS))
def test_each_outcome_is_read_from_openssh_s_own_line(outcome):
    tracker = _track(LOGS[outcome])
    observation = tracker.take()
    assert observation is not None
    assert observation.outcome is outcome
    assert observation.control_path == SOCKET
    # Handed over once.
    assert tracker.take() is None


def test_a_host_that_does_not_share_has_no_outcome():
    tracker = _track([
        "debug1: Reading configuration data /home/u/.ssh/config", LOGIN, NOISE, SESSION,
    ])
    assert tracker.decided is None and tracker.take() is None


def test_a_socket_path_too_long_fails_before_connecting():
    tracker = _track([f"ControlPath too long ('{SOCKET}' >= 108 bytes)"])
    assert tracker.decided.outcome is ConnectionSharingOutcome.FAILED
    assert tracker.decided.control_path == SOCKET


def test_the_first_decisive_line_settles_it():
    lines = LOGS[ConnectionSharingOutcome.REFUSED] + [
        f"debug1: channel 3: new mux listener [{SOCKET}]",
    ]
    assert _track(lines).decided.outcome is ConnectionSharingOutcome.REFUSED


def test_the_diagnostics_parser_tracks_sharing_past_the_login_verdict():
    """The refusal comes after ``Authenticated to``, which the parser
    reports first and stops at for that read."""
    parser = SshDiagnosticParser()
    data = "\n".join(LOGS[ConnectionSharingOutcome.REFUSED]).encode() + b"\n"
    first = parser.feed(data)
    assert first.state is SshDiagnosticState.AUTHENTICATED
    assert parser.sharing.decided is None
    parser.feed(b"")
    assert parser.sharing.take().outcome is ConnectionSharingOutcome.REFUSED


def test_the_diagnostics_parser_sees_the_line_that_fails_the_session():
    parser = SshDiagnosticParser()
    data = "\n".join(LOGS[ConnectionSharingOutcome.FAILED]).encode() + b"\n"
    parser.feed(data)
    second = parser.feed(b"")
    assert second.state is SshDiagnosticState.FAILED
    assert parser.sharing.take().outcome is ConnectionSharingOutcome.FAILED


# -- the daemon --------------------------------------------------------------


def _observation(outcome, path=SOCKET):
    return ConnectionSharingObservation(outcome=outcome, line="ssh line", control_path=path)


def _reporter():
    from sshpilot.daemon.connection_sharing import ConnectionSharingReporter

    publisher = EventPublisher()
    events = []
    publisher.subscribe(events.append)
    return ConnectionSharingReporter(publisher.publish), publisher, events


@pytest.mark.parametrize(
    ("outcome", "level"),
    [
        (ConnectionSharingOutcome.OPENED, logging.INFO),
        (ConnectionSharingOutcome.JOINED, logging.INFO),
        (ConnectionSharingOutcome.UNAVAILABLE, logging.INFO),
        (ConnectionSharingOutcome.REFUSED, logging.WARNING),
        (ConnectionSharingOutcome.FAILED, logging.WARNING),
    ],
)
def test_every_outcome_is_logged_and_published(outcome, level, caplog):
    reporter, _publisher, events = _reporter()
    with caplog.at_level(logging.INFO, logger="sshpilot.daemon.connection_sharing"):
        reporter.report("router", "session-7", ConnectionSharingScope.TERMINAL,
                        _observation(outcome))
    [record] = caplog.records
    assert record.levelno == level
    message = record.getMessage()
    assert f"connection sharing {outcome.value} kind=terminal" in message
    assert SOCKET in message and "(ssh: ssh line)" in message
    [event] = events
    assert event.type is EventType.CONNECTION_SHARING
    assert event.connection_id == "router" and event.session_id == "session-7"
    assert event.payload == ConnectionSharingNotice(
        connection_id="router", scope_id="session-7",
        scope=ConnectionSharingScope.TERMINAL, outcome=outcome, control_path=SOCKET,
    )


def test_the_log_line_names_the_connection_and_the_sftp_service():
    from sshpilot.logging_support import SanitizingFormatter

    class _Formatted(logging.Handler):
        # The context is read while the record is formatted, at emit time.
        def __init__(self):
            super().__init__(logging.INFO)
            self.setFormatter(SanitizingFormatter("%(message)s"))
            self.lines = []

        def emit(self, record):
            self.lines.append(self.format(record))

    handler = _Formatted()
    logger = logging.getLogger("sshpilot.daemon.connection_sharing")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        reporter, _publisher, _events = _reporter()
        reporter.report("router", "sftp-3", ConnectionSharingScope.SFTP,
                        _observation(ConnectionSharingOutcome.JOINED))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    [line] = handler.lines
    assert line.endswith("[connection=router sftp_service=sftp-3]")


def test_a_closed_publisher_keeps_the_log_line(caplog):
    reporter, publisher, events = _reporter()
    publisher.close()
    with caplog.at_level(logging.INFO, logger="sshpilot.daemon.connection_sharing"):
        reporter.report("router", "session-7", ConnectionSharingScope.TERMINAL,
                        _observation(ConnectionSharingOutcome.REFUSED))
    assert events == []
    assert any("connection sharing refused" in r.getMessage() for r in caplog.records)


def test_the_notice_crosses_the_wire_intact():
    from sshpilot.api.transport.codec import (
        public_event_from_envelope,
        public_event_to_envelope,
    )

    notice = ConnectionSharingNotice(
        connection_id="router", scope_id="sftp-3", scope=ConnectionSharingScope.SFTP,
        outcome=ConnectionSharingOutcome.REFUSED, control_path=SOCKET,
    )
    event = CoreEvent(type=EventType.CONNECTION_SHARING, payload=notice,
                      sequence=4, connection_id="router", session_id="sftp-3")
    envelope = public_event_to_envelope(event, sequence=4, protocol_version="1.0")
    assert envelope.payload == {
        "connection_id": "router", "scope_id": "sftp-3", "scope": "sftp",
        "outcome": "refused", "control_path": SOCKET,
    }
    decoded = public_event_from_envelope(envelope)
    assert decoded.payload == notice
    assert decoded.connection_id == "router" and decoded.session_id == "sftp-3"


def test_an_unknown_outcome_on_the_wire_is_refused():
    from sshpilot.api.transport.codec import connection_sharing_notice_from_wire

    with pytest.raises(ValueError, match="unknown outcome"):
        connection_sharing_notice_from_wire({
            "connection_id": "router", "scope_id": "s", "scope": "terminal",
            "outcome": "maybe", "control_path": "",
        })


def test_sftp_reads_sharing_from_the_stderr_it_drains():
    from sshpilot.daemon.sftp_runtime import _drain_ssh_stderr_for_banner

    stream = io.BytesIO(
        ("\n".join([
            "debug1: Remote protocol version 2.0, remote software version OpenSSH_9.6",
            *LOGS[ConnectionSharingOutcome.REFUSED],
        ]) + "\n").encode()
    )
    holder = {"software": None}
    seen = []
    _drain_ssh_stderr_for_banner(stream, holder, seen.append)
    assert holder["software"]
    assert [o.outcome for o in seen] == [ConnectionSharingOutcome.REFUSED]


def test_sftp_without_a_reporter_drains_as_before():
    from sshpilot.daemon.sftp_runtime import _drain_ssh_stderr_for_banner

    holder = {"software": None}
    _drain_ssh_stderr_for_banner(
        io.BytesIO("\n".join(LOGS[ConnectionSharingOutcome.JOINED]).encode()), holder
    )
    assert holder == {"software": None}


@pytest.mark.skipif(
    not __import__("sshpilot.daemon.ssh_diagnostics_monitor", fromlist=["x"]).inotify_available(),
    reason="needs inotify",
)
def test_the_readiness_manager_hands_a_terminal_s_outcome_over_once(tmp_path, monkeypatch):
    from tests.daemon.test_ssh_readiness_runtime import _await, _manager

    manager = _manager(tmp_path, monkeypatch)
    results, sharing = [], []
    manager.subscribe(
        "session-1", lambda sid, result: results.append(result.state),
        on_sharing=lambda sid, observation: sharing.append((sid, observation.outcome)),
    )
    path = manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {})
    assert path is not None
    try:
        with open(path, "a") as log:
            log.write("\n".join(LOGS[ConnectionSharingOutcome.REFUSED]) + "\n")
        assert _await(lambda: sharing)
        assert sharing == [("session-1", ConnectionSharingOutcome.REFUSED)]
        assert SshDiagnosticState.AUTHENTICATED in results
        with open(path, "a") as log:
            log.write(f"debug1: channel 3: new mux listener [{SOCKET}]\n")
        time.sleep(0.2)
        assert len(sharing) == 1
    finally:
        manager.finish("session-1")
        manager.close()


def test_a_terminal_session_reports_under_its_connection():
    from sshpilot.daemon.session_runtime import SessionRuntime

    reported = []
    runtime = SimpleNamespace(
        _sharing_reporter=SimpleNamespace(report=lambda *args: reported.append(args)),
        _lock=__import__("threading").RLock(),
        _records={"session-1": SimpleNamespace(connection_id="router")},
    )
    observation = _observation(ConnectionSharingOutcome.JOINED)
    SessionRuntime._on_ssh_sharing(runtime, "session-1", observation)
    # A session already gone reports nothing.
    SessionRuntime._on_ssh_sharing(runtime, "session-gone", observation)
    assert reported == [
        ("router", "session-1", ConnectionSharingScope.TERMINAL, observation)
    ]


# -- the app -----------------------------------------------------------------


class _Overlay:
    def __init__(self):
        self.toasts = []

    def add_toast(self, toast):
        self.toasts.append(toast)


class _Toast:
    def __init__(self, title):
        self.title = title

    @classmethod
    def new(cls, title):
        return cls(title)

    def set_timeout(self, seconds):
        self.timeout = seconds


@pytest.fixture
def application(monkeypatch):
    import sshpilot.main as main
    from sshpilot.main import SshPilotApplication

    monkeypatch.setattr(main, "Adw", SimpleNamespace(Toast=_Toast))
    overlay = _Overlay()
    connection = SimpleNamespace(display_name="", nickname="router", host="192.168.8.1")
    app = SimpleNamespace(
        window=SimpleNamespace(
            _is_quitting=False,
            toast_overlay=overlay,
            connection_manager=SimpleNamespace(get_connection_by_id=lambda cid: connection),
        ),
        get_windows=list,
        get_active_window=lambda: None,
        overlay=overlay,
    )
    for name in (
        "_handle_connection_sharing_event",
        "_pre_command_toast_overlay",
        "_connection_display_name",
    ):
        setattr(app, name, getattr(SshPilotApplication, name).__get__(app))
    return app


def _notice(outcome):
    return ConnectionSharingNotice(
        connection_id="router", scope_id="session-1",
        scope=ConnectionSharingScope.TERMINAL, outcome=outcome, control_path=SOCKET,
    )


@pytest.mark.parametrize(
    "outcome", [ConnectionSharingOutcome.REFUSED, ConnectionSharingOutcome.FAILED]
)
def test_sharing_that_did_not_work_raises_a_toast(application, outcome):
    application._handle_connection_sharing_event(_notice(outcome))
    [toast] = application.overlay.toasts
    assert "“router”" in toast.title


def test_the_refusal_toast_says_what_to_change(application):
    application._handle_connection_sharing_event(_notice(ConnectionSharingOutcome.REFUSED))
    assert "ControlMaster to auto" in application.overlay.toasts[0].title


@pytest.mark.parametrize(
    "outcome",
    [
        ConnectionSharingOutcome.OPENED,
        ConnectionSharingOutcome.JOINED,
        ConnectionSharingOutcome.UNAVAILABLE,
    ],
)
def test_sharing_that_worked_or_was_not_asked_for_is_silent(application, outcome):
    application._handle_connection_sharing_event(_notice(outcome))
    assert application.overlay.toasts == []


def test_no_toast_while_the_window_is_quitting(application):
    application.window._is_quitting = True
    application._handle_connection_sharing_event(_notice(ConnectionSharingOutcome.REFUSED))
    assert application.overlay.toasts == []
