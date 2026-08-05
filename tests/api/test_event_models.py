import threading

import pytest

from sshpilot.api import EventType
from sshpilot.api.events import CoreEvent, EventPublisher
from sshpilot.api.models import (
    ConnectionId,
    ConnectionSummary,
    SessionExitInfo,
)


def _connection(name):
    return ConnectionSummary(
        id=ConnectionId(name),
        nickname=name,
        host=name,
        hostname=f"{name}.example",
        username="user",
        port=22,
    )


def test_event_delivery_is_ordered_and_subscriptions_cleanup():
    publisher = EventPublisher()
    received = []
    subscription = publisher.subscribe(received.append)

    publisher.publish(EventType.CONNECTION_CREATED, _connection("one"))
    publisher.publish(EventType.CONNECTION_UPDATED, _connection("two"))
    subscription.unsubscribe()
    publisher.publish(EventType.CONNECTION_DELETED, _connection("three"))

    assert [event.sequence for event in received] == [0, 1]
    assert [event.type.value for event in received] == [
        "connection.created",
        "connection.updated",
    ]
    assert subscription.active is False


def test_existing_daemon_sequence_is_preserved_and_advances_local_sequence():
    publisher = EventPublisher()
    received = []
    publisher.subscribe(received.append)

    publisher.publish_existing(
        CoreEvent(
            type=EventType.CONNECTION_CREATED,
            payload=_connection("remote"),
            sequence=41,
        )
    )
    local = publisher.publish(
        EventType.CONNECTION_UPDATED,
        _connection("local"),
    )

    assert [event.sequence for event in received] == [41, 42]
    assert local.sequence == 42
    with pytest.raises(ValueError, match="not monotonic"):
        publisher.publish_existing(received[0])


def test_subscriber_failure_does_not_block_other_subscribers(caplog):
    publisher = EventPublisher()
    received = []

    def fail(_event):
        raise RuntimeError("subscriber failed")

    publisher.subscribe(fail)
    publisher.subscribe(received.append)
    event = publisher.publish(EventType.CONNECTION_CREATED, _connection("demo"))

    assert received == [event]
    assert "subscriber failed" in caplog.text


@pytest.mark.parametrize("_iteration", range(25))
def test_concurrent_publishers_deliver_globally_ordered_sequences(_iteration):
    publisher = EventPublisher()
    observed = []
    returned = []
    barrier = threading.Barrier(3)

    publisher.subscribe(lambda event: observed.append(event.sequence))

    def publish(name):
        barrier.wait()
        returned.append(
            publisher.publish(EventType.CONNECTION_UPDATED, _connection(name))
        )

    threads = [
        threading.Thread(target=publish, args=("one",)),
        threading.Thread(target=publish, args=("two",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert observed == [0, 1]
    assert sorted(event.sequence for event in returned) == [0, 1]


def test_reentrant_publication_runs_after_current_event_reaches_all_subscribers():
    publisher = EventPublisher()
    first_observed = []
    second_observed = []

    def publish_followup(event):
        first_observed.append(event.sequence)
        if event.sequence == 0:
            publisher.publish(
                EventType.CONNECTION_UPDATED,
                _connection("followup"),
            )

    publisher.subscribe(publish_followup)
    publisher.subscribe(lambda event: second_observed.append(event.sequence))

    publisher.publish(EventType.CONNECTION_CREATED, _connection("initial"))

    assert first_observed == [0, 1]
    assert second_observed == [0, 1]


def test_slow_subscriber_preserves_order_for_other_subscribers():
    publisher = EventPublisher()
    slow_entered = threading.Event()
    release_slow = threading.Event()
    observed = []
    callback_threads = []

    def slow_first_event(event):
        if event.sequence == 0:
            slow_entered.set()
            assert release_slow.wait(timeout=2)

    def observe(event):
        observed.append(event.sequence)
        callback_threads.append(threading.get_ident())

    publisher.subscribe(slow_first_event)
    publisher.subscribe(observe)

    first = threading.Thread(
        target=lambda: publisher.publish(
            EventType.CONNECTION_CREATED,
            _connection("one"),
        )
    )
    second = threading.Thread(
        target=lambda: publisher.publish(
            EventType.CONNECTION_UPDATED,
            _connection("two"),
        )
    )
    first.start()
    assert slow_entered.wait(timeout=2)
    second.start()
    second.join(timeout=0.05)
    assert second.is_alive()

    release_slow.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert observed == [0, 1]
    assert callback_threads == [first.ident, first.ident]


def test_unsubscription_during_delivery_affects_only_later_events():
    publisher = EventPublisher()
    first_observed = []
    second_observed = []
    second_subscription = None

    def unsubscribe_second(event):
        first_observed.append(event.sequence)
        second_subscription.unsubscribe()

    publisher.subscribe(unsubscribe_second)
    second_subscription = publisher.subscribe(
        lambda event: second_observed.append(event.sequence)
    )

    publisher.publish(EventType.CONNECTION_CREATED, _connection("one"))
    publisher.publish(EventType.CONNECTION_UPDATED, _connection("two"))

    assert first_observed == [0, 1]
    assert second_observed == [0]
    assert second_subscription.active is False


def test_close_during_delivery_finishes_current_snapshot_and_rejects_new_events():
    publisher = EventPublisher()
    observed = []
    rejected = []
    first_subscription = None
    second_subscription = None

    def close_publisher(event):
        observed.append(("first", event.sequence))
        publisher.close()
        with pytest.raises(RuntimeError) as caught:
            publisher.publish(
                EventType.CONNECTION_UPDATED,
                _connection("rejected"),
            )
        rejected.append(str(caught.value))

    first_subscription = publisher.subscribe(close_publisher)
    second_subscription = publisher.subscribe(
        lambda event: observed.append(("second", event.sequence))
    )

    publisher.publish(EventType.CONNECTION_CREATED, _connection("accepted"))

    assert observed == [("first", 0), ("second", 0)]
    assert rejected == ["event publisher is closed"]
    assert first_subscription.active is False
    assert second_subscription.active is False


def test_close_after_publication_is_idempotent_and_publish_after_close_fails():
    publisher = EventPublisher()
    subscription = publisher.subscribe(lambda _event: None)

    publisher.publish(EventType.CONNECTION_CREATED, _connection("one"))
    publisher.close()
    publisher.close()

    assert subscription.active is False
    with pytest.raises(RuntimeError, match="event publisher is closed"):
        publisher.publish(EventType.CONNECTION_UPDATED, _connection("two"))


@pytest.mark.parametrize(
    "unsafe",
    [
        RuntimeError("must not cross the boundary"),
        lambda: None,
        object(),
        {"nickname": "raw-persistence-record"},
    ],
)
def test_generic_public_events_reject_unsafe_payload_objects(unsafe):
    publisher = EventPublisher()

    with pytest.raises((TypeError, ValueError)) as caught:
        publisher.publish(EventType.ERROR_OCCURRED, unsafe)

    assert repr(unsafe) not in str(caught.value)


def test_event_payload_type_is_bound_to_event_identifier():
    publisher = EventPublisher()

    with pytest.raises(TypeError, match="session.exited requires payload type"):
        publisher.publish(
            EventType.SESSION_EXITED,
            _connection("wrong-type"),
        )

    event = publisher.publish(
        EventType.SESSION_EXITED,
        SessionExitInfo(exit_code=0),
    )
    assert event.sequence == 0
    assert event.payload.exit_code == 0


def test_event_repr_excludes_payload_content():
    event = EventPublisher().publish(
        EventType.ERROR_OCCURRED,
        {
            "code": "internal_error",
            "message": "Operation failed",
            "details": {"reason": "potentially-sensitive-context"},
        },
    )

    assert "potentially-sensitive-context" not in repr(event)
    with pytest.raises(TypeError):
        event.payload["details"] = {}
    with pytest.raises(TypeError):
        event.payload["details"]["reason"] = "mutated"


def test_event_envelope_rejects_internal_objects_outside_payload():
    unsafe = object()

    with pytest.raises(TypeError) as caught:
        CoreEvent(
            type=EventType.CONNECTION_CREATED,
            payload=_connection("safe"),
            sequence=0,
            timestamp=unsafe,
        )

    assert repr(unsafe) not in str(caught.value)


@pytest.mark.parametrize(
    "signal_name,event_type",
    [
        ("connection-added", EventType.CONNECTION_CREATED),
        ("connection-updated", EventType.CONNECTION_UPDATED),
        ("connection-removed", EventType.CONNECTION_DELETED),
    ],
)
def test_connection_events_use_typed_dtos_across_clients(
    fake_repo,
    client_factory,
    signal_name,
    event_type,
):
    client = client_factory(fake_repo)
    received = []
    delivered = threading.Event()

    def _receive(event):
        received.append(event)
        delivered.set()

    subscription = client.subscribe_events(_receive)

    if signal_name == "connection-added":
        created = fake_repo.create_connection(
            {"nickname": "other", "hostname": "other.example", "username": "user", "port": 22}
        )
        expected_id = created.id
    elif signal_name == "connection-updated":
        fake_repo.update_connection("demo", {"hostname": "updated.example"})
        expected_id = client.list_connections()[0].id
    else:
        expected_id = client.list_connections()[0].id
        fake_repo.delete_connection(str(expected_id))

    assert delivered.wait(2)
    assert received[0].type is event_type
    assert received[0].connection_id == expected_id
    assert received[0].connection_id is not None
    subscription.close()
