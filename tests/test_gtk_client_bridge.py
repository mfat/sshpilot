import queue
import threading

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
