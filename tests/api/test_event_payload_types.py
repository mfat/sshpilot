"""Every event type names a payload type that resolves (events.py is lazy)."""

import dataclasses

import pytest

from sshpilot.api import events
from sshpilot.api.events import EventType


def test_every_event_type_has_a_payload_entry():
    assert set(events._EVENT_PAYLOAD_TYPE_NAMES) == set(EventType)


@pytest.mark.parametrize("event_type", list(EventType), ids=lambda item: item.value)
def test_payload_types_resolve_to_api_model_dataclasses(event_type):
    resolved = events._payload_type(event_type)
    if event_type is EventType.ERROR_OCCURRED:
        assert resolved is None  # a validated mapping, not a DTO
        return
    assert isinstance(resolved, type)
    assert dataclasses.is_dataclass(resolved)
    assert resolved.__module__.startswith("sshpilot.api.models.")
