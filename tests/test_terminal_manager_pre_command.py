"""Regression tests for the per-connection pre-connection command.

The feature stores a shell command that must run locally *before* an SSH
session opens (typically a port knock -- fwknop -- or a VPN dial-up that
authorises a short access window). Every layer of it survived the move to the
daemon-backed session model except the one that actually ran the command:
the old TerminalWidget._connect_ssh_thread. The command could still be typed,
saved to ~/.ssh/config as `# sshpilot:PreCommand ...` and read back, but
nothing ever executed it, so knock-gated hosts stopped connecting.

These tests pin the execution back to every path that opens an SSH session.
"""

from types import SimpleNamespace
from unittest import mock

from sshpilot.terminal_manager import TerminalManager, _connection_pre_command


class _SyncThread:
    """Runs the thread target synchronously so tests don't need real threads."""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _sync(monkeypatch, on_run=None):
    """Run the pre-command inline and record every invocation.

    Patches the exact globals dict these methods resolve names through, rather
    than the module attributes. Another test in the same worker process can
    reload sshpilot.terminal_manager, after which `import ... as tm` hands back
    a different module object than the already-imported class's methods read
    from, and attribute patches silently miss. The secret-unlock suite
    documents the same hazard.

    ``on_run(cmd)`` may raise, to model a command that blows up.
    """
    g = TerminalManager._maybe_run_pre_command_then.__globals__
    monkeypatch.setitem(g, "threading", SimpleNamespace(Thread=_SyncThread))
    monkeypatch.setitem(
        g, "GLib", SimpleNamespace(idle_add=lambda cb, *a: (cb(*a), False)[1])
    )

    calls = []

    def _run(command):
        calls.append(command)
        if on_run is not None:
            on_run(command)

    monkeypatch.setitem(g, "run_pre_connection_command", _run)
    return calls


class _Conn:
    """A connection shaped like the real projection: the value lives in .data.

    A real class rather than SimpleNamespace because connections are used as
    dict keys (window.connection_to_terminals) and SimpleNamespace, having
    __eq__, is unhashable.
    """

    def __init__(self, pre_command):
        self.nickname = "Host"
        self.data = {"pre_command": pre_command}


def _connection(pre_command):
    return _Conn(pre_command)


_UNSET = object()


class _Summary:
    """What the GTK side actually holds: display fields only, no pre_command.

    The real ConnectionSummary has no such attribute, so the value has to be
    fetched from the daemon's ConnectionDetails.
    """

    def __init__(self, nickname="Host"):
        self.id = "host-id"
        self.nickname = nickname


def _manager(details=_UNSET):
    """A manager with no daemon transport by default.

    Passing ``details`` wires a bridge whose get_connection resolves to a
    ConnectionDetails-shaped object, modelling the daemon lookup.
    """
    window = mock.Mock()
    if details is _UNSET:
        window.client = None
        window.client_bridge = None
    else:
        client = mock.Mock()
        # ConnectionDetails deliberately answers with no pre_command here: the
        # field is part of the editable config and exists only on
        # ConnectionEditorDetails. Keeping this object field-less makes the
        # tests fail loudly if the code reaches for the wrong RPC, which is
        # otherwise a silent no-op.
        client.get_connection.return_value = SimpleNamespace()
        client.get_connection_editor.return_value = details
        bridge = mock.Mock()
        # Actually run the submitted operation, so which RPC was chosen matters.
        bridge.submit.side_effect = (
            lambda operation, on_success, on_error, **kw: on_success(operation())
        )
        window.client = client
        window.client_bridge = bridge
    return TerminalManager(window)


# --- reading the value -------------------------------------------------


def test_pre_command_read_from_data():
    """Connection is an ephemeral projection that does not promote this key to
    an attribute, so .data is the path that matters in production."""
    assert _connection_pre_command(_connection("fwknop -n app")) == "fwknop -n app"


def test_pre_command_prefers_attribute_when_present():
    conn = SimpleNamespace(pre_command="from-attr", data={"pre_command": "from-data"})
    assert _connection_pre_command(conn) == "from-attr"


def test_pre_command_absent_is_empty():
    assert _connection_pre_command(SimpleNamespace(data={})) == ""
    assert _connection_pre_command(None) == ""


def test_pre_command_ignores_non_string_values():
    """The value is handed to a shell. A stub/placeholder attribute must read
    as unset rather than be coerced into a command line and executed."""
    assert _connection_pre_command(mock.Mock()) == ""
    assert _connection_pre_command(SimpleNamespace(data={"pre_command": 42})) == ""


def test_pre_command_whitespace_only_is_empty():
    assert _connection_pre_command(_connection("   ")) == ""


# --- running it --------------------------------------------------------


def test_no_pre_command_proceeds_inline():
    manager = _manager()
    retried = []
    assert manager._maybe_run_pre_command_then(
        _connection(""), lambda: retried.append(1)
    ) is False
    assert retried == []


def test_pre_command_runs_then_retries(monkeypatch):
    calls = _sync(monkeypatch)
    manager = _manager()

    retried = []
    result = manager._maybe_run_pre_command_then(
        _connection("fwknop -n app"), lambda: retried.append(1)
    )

    assert result is True
    assert calls[0] == "fwknop -n app"
    assert retried == [1]


def test_pre_command_runs_before_the_retry(monkeypatch):
    """Ordering is the whole point: the knock must land before SSH dials out."""
    order = []
    _sync(monkeypatch, on_run=lambda cmd: order.append("pre"))
    manager = _manager()

    manager._maybe_run_pre_command_then(
        _connection("knock"), lambda: order.append("connect")
    )

    assert order == ["pre", "connect"]


def test_failing_pre_command_still_connects(monkeypatch):
    """A non-zero exit is logged, not fatal: SSH's own error tells the user far
    more than a pre-step veto, and that is how the feature shipped."""
    # A non-zero exit is swallowed inside run_pre_connection_command, so the
    # manager simply sees it return; the connection must still proceed.
    _sync(monkeypatch)
    manager = _manager()

    retried = []
    manager._maybe_run_pre_command_then(_connection("boom"), lambda: retried.append(1))
    assert retried == [1]


def test_timed_out_pre_command_still_connects(monkeypatch):
    _sync(monkeypatch)
    manager = _manager()

    retried = []
    manager._maybe_run_pre_command_then(_connection("hangs"), lambda: retried.append(1))
    assert retried == [1]


def test_crashing_pre_command_still_connects(monkeypatch):
    """The helper swallows its own failures, but if anything ever escaped it
    the worker thread would die and the connection would hang forever waiting
    for a resume that never comes. Guarantee the resume."""
    def _boom(cmd):
        raise OSError("no shell")

    _sync(monkeypatch, on_run=_boom)
    manager = _manager()

    retried = []
    manager._maybe_run_pre_command_then(_connection("nope"), lambda: retried.append(1))
    assert retried == [1]


def test_pre_command_does_not_block_the_main_thread(monkeypatch):
    """A 30s timeout on the GTK loop would freeze the UI for its full duration,
    so the command must be handed to a worker thread."""
    g = TerminalManager._maybe_run_pre_command_then.__globals__
    started = []

    class _RecordingThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            started.append(daemon)

        def start(self):
            pass  # deliberately never runs: proves the caller returned first

    monkeypatch.setitem(g, "threading", SimpleNamespace(Thread=_RecordingThread))

    manager = _manager()
    retried = []
    result = manager._maybe_run_pre_command_then(
        _connection("slow"), lambda: retried.append(1)
    )

    assert result is True
    assert started == [True]  # daemon thread
    assert retried == []  # resume is deferred to the worker, not run inline


# --- wiring into each connect path -------------------------------------


def test_reconnect_runs_pre_command_first(monkeypatch):
    """A knock-gated host refuses a reconnect once the earlier access window
    has closed, so the reconnect path needs the command as much as a first
    connect -- and it does not route through connect_to_host."""
    order = []
    _sync(monkeypatch, on_run=lambda cmd: order.append("pre"))

    manager = _manager()
    manager.window.secrets_controller = None
    manager.reconnect_terminal = mock.Mock(
        side_effect=lambda t: (order.append("reconnect"), True)[1]
    )

    terminal = mock.Mock()
    terminal.connection = _connection("fwknop -n app")

    assert manager._reconnect_terminal_gated(terminal) is True
    assert order == ["pre", "reconnect"]


def test_reconnect_runs_pre_command_after_vault_unlock(monkeypatch):
    """Unlock first, knock second: a modal unlock prompt sitting between the
    knock and the connect can outlast the window the knock opened."""
    order = []
    _sync(monkeypatch, on_run=lambda cmd: order.append("pre"))

    manager = _manager()
    captured = []
    manager._maybe_unlock_secrets_then = mock.Mock(
        side_effect=lambda retry: (captured.append(retry), True)[1]
    )
    manager.reconnect_terminal = mock.Mock(
        side_effect=lambda t: (order.append("reconnect"), True)[1]
    )

    terminal = mock.Mock()
    terminal.connection = _connection("fwknop -n app")

    assert manager._reconnect_terminal_gated(terminal) is True
    assert order == []  # gated on the unlock, nothing ran yet

    order.append("unlock")
    captured[0]()
    assert order == ["unlock", "pre", "reconnect"]


def test_reconnect_without_pre_command_is_unchanged(monkeypatch):
    _sync(monkeypatch)
    manager = _manager()
    manager.window.secrets_controller = None
    manager.reconnect_terminal = mock.Mock(return_value=True)

    terminal = mock.Mock()
    terminal.connection = _connection("")

    assert manager._reconnect_terminal_gated(terminal) is True
    manager.reconnect_terminal.assert_called_once_with(terminal)


def test_pane_runs_pre_command_before_session(monkeypatch):
    """Split-pane creation opens its own SSH session, bypassing connect_to_host."""
    order = []
    _sync(monkeypatch, on_run=lambda cmd: order.append("pre"))

    manager = _manager()
    window = manager.window
    window.config = mock.Mock()
    window.connection_manager = mock.Mock()
    window.connection_to_terminals = {}
    window.terminal_to_connection = {}
    window.active_terminals = {}
    window.group_manager = None

    monkeypatch.setattr(
        manager, "_ensure_daemon_terminal_ready", lambda: mock.Mock(ready=True)
    )
    monkeypatch.setattr(manager, "_maybe_unlock_secrets_then", lambda retry: False)

    fake_terminal = mock.Mock()
    fake_terminal.start_daemon_session.side_effect = lambda *a, **k: order.append("session")
    monkeypatch.setitem(
        TerminalManager.create_terminal_for_pane.__globals__,
        "TerminalWidget", mock.Mock(return_value=fake_terminal),
    )

    manager.create_terminal_for_pane(_connection("fwknop -n app"))

    assert order == ["pre", "session"]


def test_daemon_connect_runs_pre_command_before_session(monkeypatch):
    """The ordinary connect path: the command must land before the daemon
    session opens, not after."""
    order = []
    _sync(monkeypatch, on_run=lambda cmd: order.append("pre"))

    manager = _manager()
    monkeypatch.setattr(
        manager, "_ensure_daemon_terminal_ready", lambda: mock.Mock(ready=True)
    )
    monkeypatch.setattr(manager, "_maybe_unlock_secrets_then", lambda retry: False)
    monkeypatch.setattr(
        "sshpilot.daemon_terminal_policy.resolve_daemon_terminal_readiness",
        lambda *a, **k: mock.Mock(ready=True),
    )

    fake_terminal = mock.Mock()
    fake_terminal.start_daemon_session.side_effect = lambda *a, **k: order.append("session")
    monkeypatch.setattr(
        manager, "_create_internal_terminal_tab",
        lambda *a, **k: (fake_terminal, mock.Mock()),
    )
    monkeypatch.setitem(
        TerminalManager._open_daemon_ssh.__globals__,
        "connection_id_for", lambda c: "host-id",
    )

    manager._open_daemon_ssh(_connection("fwknop -n app"))

    assert order == ["pre", "session"]


def test_daemon_connect_without_pre_command_is_unchanged(monkeypatch):
    """No command configured must not defer or skip the session."""
    _sync(monkeypatch)
    manager = _manager()

    monkeypatch.setattr(
        manager, "_ensure_daemon_terminal_ready", lambda: mock.Mock(ready=True)
    )
    monkeypatch.setattr(manager, "_maybe_unlock_secrets_then", lambda retry: False)
    monkeypatch.setattr(
        "sshpilot.daemon_terminal_policy.resolve_daemon_terminal_readiness",
        lambda *a, **k: mock.Mock(ready=True),
    )

    fake_terminal = mock.Mock()
    monkeypatch.setattr(
        manager, "_create_internal_terminal_tab",
        lambda *a, **k: (fake_terminal, mock.Mock()),
    )
    monkeypatch.setitem(
        TerminalManager._open_daemon_ssh.__globals__,
        "connection_id_for", lambda c: "host-id",
    )

    manager._open_daemon_ssh(_connection(""))

    fake_terminal.start_daemon_session.assert_called_once()


def test_daemon_connect_runs_pre_command_once(monkeypatch):
    """The resume re-enters _open_daemon_ssh; the guard must stop it knocking
    again (and looping) on the second pass."""
    calls = _sync(monkeypatch)

    manager = _manager()
    monkeypatch.setattr(
        manager, "_ensure_daemon_terminal_ready", lambda: mock.Mock(ready=True)
    )
    monkeypatch.setattr(manager, "_maybe_unlock_secrets_then", lambda retry: False)
    monkeypatch.setattr(
        "sshpilot.daemon_terminal_policy.resolve_daemon_terminal_readiness",
        lambda *a, **k: mock.Mock(ready=True),
    )

    fake_terminal = mock.Mock()
    monkeypatch.setattr(
        manager, "_create_internal_terminal_tab",
        lambda *a, **k: (fake_terminal, mock.Mock()),
    )
    monkeypatch.setitem(
        TerminalManager._open_daemon_ssh.__globals__,
        "connection_id_for", lambda c: "host-id",
    )

    manager._open_daemon_ssh(_connection("fwknop -n app"))

    assert len(calls) == 1
    fake_terminal.start_daemon_session.assert_called_once()


def test_external_terminal_runs_pre_command(monkeypatch):
    """The external-terminal route spawns SSH in another process; it needs the
    knock just the same."""
    order = []
    _sync(monkeypatch, on_run=lambda cmd: order.append("pre"))

    manager = _manager()
    manager.window.secrets_controller = None
    manager.window._open_connection_in_external_terminal.side_effect = (
        lambda c: order.append("external")
    )

    manager._open_external_ssh(_connection("fwknop -n app"))

    assert order == ["pre", "external"]


# --- the daemon lookup (what the regression actually needed) ------------


def test_summary_without_details_looks_up_the_daemon(monkeypatch):
    """The GTK side holds a ConnectionSummary, which has no pre_command at all.
    Reading only the in-process object is why the restored feature still did
    nothing: the value has to come from the daemon's ConnectionDetails."""
    calls = _sync(monkeypatch)
    manager = _manager(details=SimpleNamespace(pre_command="fwknop -n app"))

    retried = []
    result = manager._maybe_run_pre_command_then(_Summary(), lambda: retried.append(1))

    assert result is True
    assert calls[0] == "fwknop -n app"
    assert retried == [1]


def test_summary_with_no_configured_command_just_connects(monkeypatch):
    calls = _sync(monkeypatch)
    manager = _manager(details=SimpleNamespace(pre_command=""))

    retried = []
    result = manager._maybe_run_pre_command_then(_Summary(), lambda: retried.append(1))

    assert result is True
    assert calls == []
    assert retried == [1]


def test_details_lookup_failure_still_connects(monkeypatch):
    """A daemon hiccup reading an optional pre-step must not strand a valid
    connection."""
    _sync(monkeypatch)
    manager = _manager(details=None)
    manager.window.client_bridge.submit.side_effect = (
        lambda operation, on_success, on_error, **kw: on_error(RuntimeError("daemon down"))
    )

    retried = []
    result = manager._maybe_run_pre_command_then(_Summary(), lambda: retried.append(1))

    assert result is True
    assert retried == [1]


def test_bridge_raising_falls_through_to_inline_connect(monkeypatch):
    """submit() itself failing means we never drive the retry, so the caller
    must be told to proceed on its own rather than hang."""
    _sync(monkeypatch)
    manager = _manager(details=None)
    manager.window.client_bridge.submit.side_effect = RuntimeError("bridge closed")

    retried = []
    result = manager._maybe_run_pre_command_then(_Summary(), lambda: retried.append(1))

    assert result is False
    assert retried == []


def test_no_transport_proceeds_inline(monkeypatch):
    """Without a client/bridge there is nowhere to look the command up."""
    _sync(monkeypatch)
    manager = _manager()  # client and bridge are None

    retried = []
    assert manager._maybe_run_pre_command_then(_Summary(), lambda: retried.append(1)) is False
    assert retried == []


def test_inline_value_skips_the_daemon_round_trip(monkeypatch):
    """A projection that already carries the value (the CLI builds one) must
    not pay for a lookup."""
    calls = _sync(monkeypatch)
    manager = _manager(details=SimpleNamespace(pre_command="from-daemon"))

    retried = []
    manager._maybe_run_pre_command_then(_connection("from-data"), lambda: retried.append(1))

    assert calls[0] == "from-data"
    manager.window.client_bridge.submit.assert_not_called()
    assert retried == [1]


def test_daemon_connect_looks_up_and_runs_before_session(monkeypatch):
    """End to end on the ordinary connect path, with the summary object the
    real UI passes rather than a projection carrying its own config."""
    order = []
    _sync(monkeypatch, on_run=lambda cmd: order.append("pre"))

    manager = _manager(details=SimpleNamespace(pre_command="fwknop -n app"))
    monkeypatch.setattr(
        manager, "_ensure_daemon_terminal_ready", lambda: mock.Mock(ready=True)
    )
    monkeypatch.setattr(manager, "_maybe_unlock_secrets_then", lambda retry: False)
    monkeypatch.setattr(
        "sshpilot.daemon_terminal_policy.resolve_daemon_terminal_readiness",
        lambda *a, **k: mock.Mock(ready=True),
    )

    fake_terminal = mock.Mock()
    fake_terminal.start_daemon_session.side_effect = lambda *a, **k: order.append("session")
    monkeypatch.setattr(
        manager, "_create_internal_terminal_tab",
        lambda *a, **k: (fake_terminal, mock.Mock()),
    )
    monkeypatch.setitem(
        TerminalManager._open_daemon_ssh.__globals__,
        "connection_id_for", lambda c: "host-id",
    )

    manager._open_daemon_ssh(_Summary())

    assert order == ["pre", "session"]


def test_lookup_uses_the_editor_rpc_which_is_the_one_carrying_the_field(monkeypatch):
    """ConnectionDetails has no pre_command; only ConnectionEditorDetails does.
    Calling the wrong one fails silently -- the command simply never runs --
    so pin the choice."""
    calls = _sync(monkeypatch)
    manager = _manager(details=SimpleNamespace(pre_command="fwknop -n app"))

    manager._maybe_run_pre_command_then(_Summary(), lambda: None)

    manager.window.client.get_connection_editor.assert_called_once_with("Host")
    manager.window.client.get_connection.assert_not_called()
    assert calls[0] == "fwknop -n app"
