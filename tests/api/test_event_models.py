import pytest

from sshpilot.api import EventType
from sshpilot.api.events import EventPublisher


def test_event_delivery_is_ordered_and_subscriptions_cleanup():
    publisher = EventPublisher()
    received = []
    subscription = publisher.subscribe(received.append)

    publisher.publish(EventType.CONNECTION_CREATED, {"name": "one"})
    publisher.publish(EventType.CONNECTION_UPDATED, {"name": "two"})
    subscription.unsubscribe()
    publisher.publish(EventType.CONNECTION_DELETED, {"name": "three"})

    assert [event.sequence for event in received] == [0, 1]
    assert [event.type.value for event in received] == [
        "connection.created",
        "connection.updated",
    ]
    assert subscription.active is False


def test_subscriber_failure_does_not_block_other_subscribers(caplog):
    publisher = EventPublisher()
    received = []

    def fail(_event):
        raise RuntimeError("subscriber failed")

    publisher.subscribe(fail)
    publisher.subscribe(received.append)
    event = publisher.publish(EventType.CONNECTION_CREATED, {"name": "demo"})

    assert received == [event]
    assert "subscriber failed" in caplog.text


@pytest.mark.parametrize(
    "signal_name,event_type",
    [
        ("connection-added", EventType.CONNECTION_CREATED),
        ("connection-updated", EventType.CONNECTION_UPDATED),
        ("connection-removed", EventType.CONNECTION_DELETED),
    ],
)
def test_in_process_connection_events_use_typed_dtos(
    fake_manager,
    fake_connection,
    client_factory,
    signal_name,
    event_type,
):
    client = client_factory(fake_manager)
    received = []
    subscription = client.subscribe_events(received.append)

    fake_manager.emit(signal_name, fake_connection)

    assert received[0].type is event_type
    assert received[0].connection_id == client.list_connections()[0].id
    assert received[0].payload.nickname == "demo"
    subscription.close()
