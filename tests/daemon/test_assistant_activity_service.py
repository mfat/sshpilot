"""Tests for the AI assistant activity ring buffer."""

from sshpilot.api.models.assistant_activity import (
    AssistantActivityState,
    RecordAssistantActivityRequest,
)
from sshpilot.daemon.assistant_activity_service import AssistantActivityService


def test_record_and_list_updates_same_id():
    seen = []
    service = AssistantActivityService(capacity=10, on_entry=seen.append)
    started = service.record(
        RecordAssistantActivityRequest(
            tool="run_command",
            state=AssistantActivityState.STARTED,
            summary="echo hi",
            activity_id="asst-1",
            connection_id="host-a",
            level="operate",
        )
    )
    assert started.activity_id == "asst-1"
    finished = service.record(
        RecordAssistantActivityRequest(
            tool="run_command",
            state=AssistantActivityState.SUCCEEDED,
            summary="echo hi",
            activity_id="asst-1",
            connection_id="host-a",
            level="operate",
        )
    )
    assert finished.state is AssistantActivityState.SUCCEEDED
    listing = service.list()
    assert len(listing.entries) == 1
    assert listing.entries[0].state is AssistantActivityState.SUCCEEDED
    assert len(seen) == 2
