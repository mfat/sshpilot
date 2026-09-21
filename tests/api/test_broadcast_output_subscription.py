"""``DaemonClient.subscribe_broadcast_output`` completion handling.

The subscriber ran ``summary.id`` on an ``OperationSummary``, which has
``operation_id``. Every operation event therefore raised AttributeError
*inside* the event subscriber, where the bus logs it and carries on — so
nothing failed loudly. What it cost: a streamed command never reported
completion, and because the finished flag was never set, each subscription
stayed live and threw again on every later operation event. Short commands
still worked, because the snapshot check after subscribing caught those.

These drive the real method against a stub transport, so the attribute has to
exist on the real model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.operations import (
    OperationKind,
    OperationState,
    OperationSummary,
)


def _summary(operation_id="op-1", state="succeeded"):
    return OperationSummary(
        operation_id=operation_id,
        kind=OperationKind.BROADCAST_COMMAND,
        state=OperationState(state),
        message="",
        created_at=datetime.fromtimestamp(0, tz=timezone.utc),
    )


class _Client:
    """Only what subscribe_broadcast_output touches."""

    def __init__(self, running=True):
        self.callbacks = []
        self.unsubscribed = 0
        self._running = running

    def _require_capability(self, _capability):
        return None

    def subscribe_events(self, callback):
        self.callbacks.append(callback)
        outer = self

        class _Subscription:
            def unsubscribe(self):
                outer.unsubscribed += 1

        return _Subscription()

    def get_broadcast_command(self, operation_id):
        state = "running" if self._running else "succeeded"
        return SimpleNamespace(
            operation=_summary(operation_id, state),
            targets=[SimpleNamespace(exit_code=0)] if not self._running else [],
        )

    def emit(self, event):
        for callback in list(self.callbacks):
            callback(event)


def _bind(client):
    client.subscribe_broadcast_output = (
        DaemonClient.subscribe_broadcast_output.__get__(client)
    )
    return client


def _state_event(operation_id="op-1", state="succeeded"):
    return CoreEvent(
        type=EventType.OPERATION_STATE_CHANGED,
        payload=_summary(operation_id, state),
        sequence=1,
    )


def test_a_command_that_finishes_after_subscribing_reports_completion():
    """The case the snapshot check cannot cover, and the one that was broken."""

    client = _bind(_Client(running=True))
    done = []
    client.subscribe_broadcast_output("op-1", lambda *a: None, done.append)

    client.emit(_state_event())

    assert done == [0]


def test_the_subscriber_never_raises_on_an_operation_event():
    """It raised inside the event bus, which logs and continues -- so this
    went unnoticed except as a repeating traceback in the user's log."""

    client = _bind(_Client(running=True))
    client.subscribe_broadcast_output("op-1", lambda *a: None, lambda code: None)

    client.emit(_state_event())  # must not raise


def test_another_operation_s_event_is_ignored():
    client = _bind(_Client(running=True))
    done = []
    client.subscribe_broadcast_output("op-1", lambda *a: None, done.append)

    client.emit(_state_event(operation_id="op-2"))

    assert done == []


def test_a_non_terminal_state_is_ignored():
    client = _bind(_Client(running=True))
    done = []
    client.subscribe_broadcast_output("op-1", lambda *a: None, done.append)

    client.emit(_state_event(state="running"))

    assert done == []


def test_completion_is_reported_once():
    client = _bind(_Client(running=True))
    done = []
    client.subscribe_broadcast_output("op-1", lambda *a: None, done.append)

    client.emit(_state_event())
    client.emit(_state_event())

    assert done == [0]


def test_a_command_that_already_finished_is_caught_by_the_snapshot():
    """This path kept short commands working while the event path was broken."""

    client = _bind(_Client(running=False))
    done = []

    client.subscribe_broadcast_output("op-1", lambda *a: None, done.append)

    assert done == [0]
