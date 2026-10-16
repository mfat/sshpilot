import queue
import threading

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import SessionId
from sshpilot.api.models.terminal import TerminalOutput
from sshpilot.api.terminal_events import TerminalSubscription
from sshpilot.gtk_client_bridge import GtkClientBridge


def _run_dispatched(dispatches):
    callback, args = dispatches.get(timeout=2)
    callback(*args)


def test_bridge_runs_work_off_caller_and_hands_result_back_to_dispatcher():
    dispatches = queue.Queue()
    worker_thread = []
    callback_thread = []
    caller_thread = threading.get_ident()
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args: dispatches.put((callback, args))
    )
    try:
        bridge.submit(
            lambda: worker_thread.append(threading.get_ident()) or "done",
            on_success=lambda result: callback_thread.append(
                (threading.get_ident(), result)
            ),
            on_error=lambda error: (_ for _ in ()).throw(error),
        )
        _run_dispatched(dispatches)
    finally:
        bridge.shutdown()

    assert worker_thread[0] != caller_thread
    assert callback_thread == [(caller_thread, "done")]


def test_bridge_serializes_operations_on_one_bounded_worker():
    dispatches = queue.Queue()
    release = threading.Event()
    order = []
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args: dispatches.put((callback, args))
    )

    def _first():
        release.wait(2)
        order.append("first")

    try:
        bridge.submit(
            _first,
            on_success=lambda _result: None,
            on_error=lambda _error: None,
        )
        bridge.submit(
            lambda: order.append("second"),
            on_success=lambda _result: None,
            on_error=lambda _error: None,
        )
        assert order == []
        release.set()
        _run_dispatched(dispatches)
        _run_dispatched(dispatches)
    finally:
        bridge.shutdown()

    assert order == ["first", "second"]


def test_interaction_lane_remains_responsive_while_session_open_waits():
    dispatches = queue.Queue()
    open_started = threading.Event()
    release_open = threading.Event()
    interaction_ran = threading.Event()
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args: dispatches.put((callback, args))
    )

    def _blocked_open():
        open_started.set()
        assert release_open.wait(2)
        return "opened"

    try:
        bridge.submit(
            _blocked_open,
            on_success=lambda _result: None,
            on_error=lambda _error: None,
        )
        assert open_started.wait(1)
        bridge.submit_interaction(
            lambda: interaction_ran.set(),
            on_success=lambda _result: None,
            on_error=lambda _error: None,
        )
        assert interaction_ran.wait(1)
        _run_dispatched(dispatches)
        release_open.set()
        _run_dispatched(dispatches)
    finally:
        release_open.set()
        bridge.shutdown()


def test_terminal_input_lane_is_fifo_and_bounded():
    dispatches = queue.Queue()
    first_started = threading.Event()
    release = threading.Event()
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args: dispatches.put((callback, args)),
        max_pending_terminal_inputs=1,
    )

    def _blocked_input():
        first_started.set()
        assert release.wait(2)

    try:
        bridge.submit_terminal_input(
            _blocked_input,
            on_success=lambda _result: None,
            on_error=lambda _error: None,
        )
        assert first_started.wait(1)
        with pytest.raises(SshPilotError) as raised:
            bridge.submit_terminal_input(
                lambda: None,
                on_success=lambda _result: None,
                on_error=lambda _error: None,
            )
        assert raised.value.code is ErrorCode.TERMINAL_INPUT_BACKPRESSURE
    finally:
        release.set()
        bridge.shutdown(wait=True)


def test_cancelled_request_discards_late_result_without_ui_callback():
    dispatches = queue.Queue()
    release = threading.Event()
    callbacks = []
    discarded = []
    discard_done = threading.Event()
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args: dispatches.put((callback, args))
    )

    def _late_result():
        release.wait(2)
        return "late"

    try:
        request = bridge.submit(
            _late_result,
            on_success=callbacks.append,
            on_error=callbacks.append,
            on_discard=lambda result: (
                discarded.append(result),
                discard_done.set(),
            ),
        )
        request.cancel()
        release.set()
        assert discard_done.wait(2)
    finally:
        bridge.shutdown()

    assert callbacks == []
    assert discarded == ["late"]


def test_shutdown_suppresses_result_already_queued_for_glib_delivery():
    dispatches = queue.Queue()
    callbacks = []
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args: dispatches.put((callback, args))
    )
    bridge.submit(
        lambda: "queued",
        on_success=callbacks.append,
        on_error=callbacks.append,
    )
    callback, args = dispatches.get(timeout=2)

    bridge.shutdown()
    callback(*args)

    assert callbacks == []


class _TerminalClient:
    def subscribe_terminal(
        self,
        _session_id,
        on_output,
        *,
        on_continuity_lost=None,
        on_eof=None,
        on_error=None,
    ):
        self.on_output = on_output
        self.on_continuity_lost = on_continuity_lost
        self.on_eof = on_eof
        self.on_error = on_error
        return TerminalSubscription(lambda: setattr(self, "closed", True))


def test_terminal_binding_coalesces_output_and_suppresses_after_close():
    dispatches = queue.Queue()
    client = _TerminalClient()
    delivered = []
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args: dispatches.put((callback, args))
    )
    session_id = SessionId(
        "session-1"
    )
    binding = bridge.bind_terminal(
        client,
        session_id,
        on_output=delivered.append,
    )
    try:
        client.on_output(
            TerminalOutput(session_id=session_id, sequence=0, data=b"a")
        )
        client.on_output(
            TerminalOutput(session_id=session_id, sequence=1, data=b"b")
        )

        assert dispatches.qsize() == 1
        _run_dispatched(dispatches)
        assert [item.data for item in delivered] == [b"a", b"b"]

        binding.close()
        client.on_output(
            TerminalOutput(session_id=session_id, sequence=2, data=b"late")
        )
        assert dispatches.empty()
        assert client.closed is True
    finally:
        bridge.shutdown()


def test_bridge_delivers_at_the_requested_priority_only_when_asked():
    dispatches = queue.Queue()
    bridge = GtkClientBridge(
        dispatcher=lambda callback, *args, **kwargs: dispatches.put(
            (callback, args, kwargs)
        )
    )
    results = []
    try:
        bridge.submit(
            lambda: "plain",
            on_success=results.append,
            on_error=lambda error: (_ for _ in ()).throw(error),
        )
        bridge.submit(
            lambda: "urgent",
            on_success=results.append,
            on_error=lambda error: (_ for _ in ()).throw(error),
            priority=100,
        )

        def _failing():
            raise RuntimeError("boom")

        bridge.submit(
            _failing,
            on_success=results.append,
            on_error=lambda error: results.append(str(error)),
            priority=100,
        )
        for _ in range(3):
            callback, args, kwargs = dispatches.get(timeout=2)
            callback(*args)
            results.append(kwargs)
    finally:
        bridge.shutdown()

    assert results == ["plain", {}, "urgent", {"priority": 100}, "boom", {"priority": 100}]


def test_default_dispatcher_queues_at_default_priority(monkeypatch):
    """Daemon results must not queue at ``PRIORITY_DEFAULT_IDLE``.

    That is below GDK's redraw priority: while a frame takes longer than the
    frame interval (Cairo repainting a large window under the connecting
    spinner) the redraw source is always ready and idle-priority results never
    run. ``tests/gui/test_gtk_client_bridge_dispatch.py`` runs it on real GLib.
    """
    from sshpilot import gtk_client_bridge

    calls = []
    monkeypatch.setattr(
        gtk_client_bridge.GLib,
        "idle_add",
        lambda callback, *args, **kwargs: calls.append((callback, args, kwargs)),
    )

    def callback(*_args):
        return False

    gtk_client_bridge.gtk_thread_idle(callback, "a")
    gtk_client_bridge.gtk_thread_idle(callback, "b", priority=100)

    assert calls == [
        (callback, ("a",), {"priority": gtk_client_bridge.GLib.PRIORITY_DEFAULT}),
        (callback, ("b",), {"priority": 100}),
    ]
    bridge = GtkClientBridge()
    try:
        assert bridge._dispatcher is gtk_client_bridge.gtk_thread_idle
    finally:
        bridge.shutdown()
