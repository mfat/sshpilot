"""The frontend controller starts probes, awaits events, and never polls."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sshpilot.api.events import EventType
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.host_info import (
    HostInfoProbe,
    HostInfoSnapshot,
    HostInfoSummary,
)
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
)
from sshpilot.gtk.host_info_controller import HostInfoController, HostInfoProbeBusy

CONNECTION = ConnectionId("conn-1")


def _operation(state: OperationState, operation_id: str = "op-1") -> OperationSummary:
    return OperationSummary(
        OperationId(operation_id),
        OperationKind.BROADCAST_COMMAND,
        state,
        "probe",
        datetime.now(timezone.utc),
    )


class _Event:
    def __init__(self, payload) -> None:
        self.type = EventType.OPERATION_STATE_CHANGED
        self.payload = payload


class _StatePayload:
    """Mirrors the ``OperationSummary`` the daemon puts in the event."""

    def __init__(self, operation_id: str, state: OperationState) -> None:
        self.operation_id = OperationId(operation_id)
        self.state = state


class _PayloadWithoutOperationId:
    """An event payload that does not name an operation must be ignored."""

    def __init__(self, state: OperationState) -> None:
        self.state = state


class _Subscription:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """Records calls and lets a test drive completion events by hand."""

    def __init__(self, *, start_state=OperationState.RUNNING) -> None:
        self.started = []
        self.get_calls = 0
        self.cancelled = []
        self.subscription = _Subscription()
        self._callback = None
        self._start_state = start_state
        self.result = HostInfoSummary(
            _operation(OperationState.SUCCEEDED),
            HostInfoProbe.FULL,
            HostInfoSnapshot(hostname="router"),
        )

    def subscribe_events(self, callback):
        self._callback = callback
        return self.subscription

    def start_host_info(self, request):
        self.started.append(request)
        return HostInfoSummary(_operation(self._start_state), request.probe)

    def get_host_info(self, operation_id):
        self.get_calls += 1
        return self.result

    def cancel_host_info(self, operation_id):
        self.cancelled.append(operation_id)
        return self.result

    def emit_done(self, operation_id="op-1", state=OperationState.SUCCEEDED):
        self._callback(_Event(_StatePayload(operation_id, state)))


def _collect():
    results, errors = [], []
    return results, errors, results.append, errors.append


def _drain(controller):
    """Wait for the controller's worker to finish everything queued so far."""

    controller._executor.submit(lambda: None).result(timeout=5)


def test_completion_arrives_by_event_without_polling():
    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()

    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)
    assert results == [] and errors == []
    assert controller.is_running(HostInfoProbe.FULL)
    assert client.get_calls == 0

    client.emit_done()
    _drain(controller)

    assert errors == []
    assert results[0].snapshot.hostname == "router"
    assert client.get_calls == 1
    assert not controller.is_running(HostInfoProbe.FULL)


def test_a_probe_finished_before_subscription_still_settles():
    client = FakeClient(start_state=OperationState.SUCCEEDED)
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()

    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)

    assert len(results) == 1
    assert errors == []


def test_a_second_probe_of_the_same_kind_is_refused_not_queued():
    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()
    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)

    with pytest.raises(HostInfoProbeBusy):
        controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)


def test_probes_of_different_kinds_run_independently():
    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()

    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    controller.start(CONNECTION, HostInfoProbe.NETWORK_COUNTERS, on_result, on_error)
    _drain(controller)

    assert [request.probe for request in client.started] == [
        HostInfoProbe.FULL,
        HostInfoProbe.NETWORK_COUNTERS,
    ]


def test_a_failed_start_reports_an_error_and_frees_the_slot():
    class Failing(FakeClient):
        def start_host_info(self, request):
            raise RuntimeError("daemon is gone")

    controller = HostInfoController(Failing())
    results, errors, on_result, on_error = _collect()

    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)

    assert results == []
    assert str(errors[0]) == "daemon is gone"
    assert not controller.is_running(HostInfoProbe.FULL)


def test_unrelated_operations_do_not_settle_a_probe():
    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()
    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)

    client.emit_done(operation_id="someone-elses-operation")
    _drain(controller)

    assert results == [] and errors == []
    assert controller.is_running(HostInfoProbe.FULL)


def test_close_unsubscribes_and_drops_waiters():
    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()
    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)

    controller.close()
    client.emit_done()

    assert client.subscription.closed is True
    assert results == [] and errors == []
    controller.close()


def test_cancel_forwards_the_operation_id():
    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()
    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)

    controller.cancel(HostInfoProbe.FULL)

    assert client.cancelled == [OperationId("op-1")]
    assert not controller.is_running(HostInfoProbe.FULL)


def test_a_second_start_is_refused_before_the_worker_even_runs():
    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()

    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    with pytest.raises(HostInfoProbeBusy):
        controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)
    assert len(client.started) == 1


def test_an_event_payload_without_an_operation_id_is_ignored():
    """The daemon sends an OperationSummary, whose field is ``operation_id``."""

    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()
    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)

    client._callback(_Event(_PayloadWithoutOperationId(OperationState.SUCCEEDED)))
    _drain(controller)

    assert results == [] and errors == []
    assert controller.is_running(HostInfoProbe.FULL)


def test_the_event_thread_is_never_blocked_by_the_follow_up_read():
    """``get_host_info`` must run on the worker, not the event dispatcher."""

    import threading

    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()
    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    _drain(controller)

    reading_threads = []
    original = client.get_host_info

    def _record(operation_id):
        reading_threads.append(threading.current_thread().name)
        return original(operation_id)

    client.get_host_info = _record
    emitting_thread = threading.current_thread().name
    client.emit_done()
    _drain(controller)

    assert results
    assert reading_threads and reading_threads[0] != emitting_thread
    assert reading_threads[0].startswith("sshpilot-host-info")


def test_the_operation_id_is_surfaced_as_soon_as_the_probe_starts():
    """A frontend needs it to bind an interaction presenter to the scope."""

    client = FakeClient()
    controller = HostInfoController(client)
    results, errors, on_result, on_error = _collect()
    started = []

    controller.start(
        CONNECTION,
        HostInfoProbe.FULL,
        on_result,
        on_error,
        on_started=started.append,
    )
    _drain(controller)

    # Reported while the probe is still running, not after it completes.
    assert started == [OperationId("op-1")]
    assert results == []
    assert controller.is_running(HostInfoProbe.FULL)


def test_a_probe_that_never_starts_reports_no_operation_id():
    class Failing(FakeClient):
        def start_host_info(self, request):
            raise RuntimeError("daemon is gone")

    controller = HostInfoController(Failing())
    results, errors, on_result, on_error = _collect()
    started = []

    controller.start(
        CONNECTION,
        HostInfoProbe.FULL,
        on_result,
        on_error,
        on_started=started.append,
    )
    _drain(controller)

    assert started == []
    assert errors


def test_a_live_sample_runs_alongside_an_outstanding_gather():
    """The dialog keeps sampling while the first gather is still going, so the
    two probe kinds must not compete for one slot."""

    client = FakeClient()
    controller = HostInfoController(client)
    _results, _errors, on_result, on_error = _collect()

    controller.start(CONNECTION, HostInfoProbe.FULL, on_result, on_error)
    controller.start(CONNECTION, HostInfoProbe.LIVE, on_result, on_error)
    _drain(controller)

    assert [request.probe for request in client.started] == [
        HostInfoProbe.FULL,
        HostInfoProbe.LIVE,
    ]


def test_a_second_live_sample_is_refused_while_one_is_outstanding():
    """This is what stops a slow link building a queue of probes: the tick is
    skipped rather than stacked."""

    client = FakeClient()
    controller = HostInfoController(client)
    _results, _errors, on_result, on_error = _collect()

    controller.start(CONNECTION, HostInfoProbe.LIVE, on_result, on_error)
    with pytest.raises(HostInfoProbeBusy):
        controller.start(CONNECTION, HostInfoProbe.LIVE, on_result, on_error)
