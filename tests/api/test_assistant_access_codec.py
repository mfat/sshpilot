"""Wire codec round-trips for assistant-access settings and activity."""

from sshpilot.api.models.assistant_activity import (
    AssistantActivityEntry,
    AssistantActivityList,
    AssistantActivityState,
    RecordAssistantActivityRequest,
)
from sshpilot.api.models.common import utc_now
from sshpilot.api.models.settings import (
    AssistantAccessSettings,
    UpdateAssistantAccessRequest,
)
from sshpilot.api.transport.codec import (
    assistant_access_from_wire,
    assistant_access_to_wire,
    assistant_activity_entry_from_wire,
    assistant_activity_entry_to_wire,
    assistant_activity_list_from_wire,
    assistant_activity_list_to_wire,
    record_assistant_activity_request_from_wire,
    record_assistant_activity_request_to_wire,
    update_assistant_access_request_from_wire,
    update_assistant_access_request_to_wire,
)


def test_assistant_access_round_trip():
    snapshot = AssistantAccessSettings(
        revision="abcdef123456",
        enabled=True,
        allowed_connection_ids=("a", "b"),
        operate=True,
        mutate=False,
        content=True,
        http_listen=True,
        http_port=0,
        http_token="tok-example",
        http_listening=True,
        http_bound_port=8765,
        http_url="http://127.0.0.1:8765/mcp",
    )
    restored = assistant_access_from_wire(assistant_access_to_wire(snapshot))
    assert restored == snapshot


def test_update_assistant_access_request_round_trip():
    request = UpdateAssistantAccessRequest(
        enabled=True,
        allowed_connection_ids=("host-1",),
        operate=True,
        mutate=True,
        content=False,
        http_listen=True,
        http_port=9123,
        regenerate_token=True,
        expected_revision="abcdef123456",
    )
    restored = update_assistant_access_request_from_wire(
        update_assistant_access_request_to_wire(request)
    )
    assert restored == request


def test_assistant_activity_round_trip():
    entry = AssistantActivityEntry(
        activity_id="asst-abc",
        tool="run_command",
        state=AssistantActivityState.SUCCEEDED,
        summary="ps aux",
        timestamp=utc_now(),
        connection_id="MyGermanServer",
        detail="ps aux",
        level="operate",
    )
    restored = assistant_activity_entry_from_wire(
        assistant_activity_entry_to_wire(entry)
    )
    assert restored == entry
    listing = AssistantActivityList(entries=(entry,))
    assert (
        assistant_activity_list_from_wire(assistant_activity_list_to_wire(listing))
        == listing
    )


def test_record_assistant_activity_request_round_trip():
    request = RecordAssistantActivityRequest(
        tool="run_command",
        state=AssistantActivityState.STARTED,
        summary="uptime",
        connection_id="host-1",
        level="operate",
    )
    restored = record_assistant_activity_request_from_wire(
        record_assistant_activity_request_to_wire(request)
    )
    assert restored == request
