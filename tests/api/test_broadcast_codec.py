from sshpilot.api.models.broadcast import (
    BroadcastCommandRequest,
    BroadcastExecutionPolicy,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.interactions import ExecutionInteractionMode
from sshpilot.api.transport.codec import (
    broadcast_command_request_from_wire,
    broadcast_command_request_to_wire,
)


def _old_style_request_wire():
    return {
        "connection_ids": ["conn-1"],
        "command": "hostname",
        "policy": {
            "failure_policy": "continue",
            "concurrency_limit": 1,
            "timeout_seconds": None,
            "capture_stdout": True,
            "capture_stderr": True,
            "output_limit_bytes": 1024,
        },
    }


def test_old_style_broadcast_policy_defaults_to_interactive():
    request = broadcast_command_request_from_wire(_old_style_request_wire())

    assert request.policy.interaction_mode is ExecutionInteractionMode.INTERACTIVE


def test_interactive_broadcast_serialization_omits_interaction_mode():
    request = BroadcastCommandRequest(
        (ConnectionId("conn-1"),),
        "hostname",
        BroadcastExecutionPolicy(
            interaction_mode=ExecutionInteractionMode.INTERACTIVE,
        ),
    )

    wire = broadcast_command_request_to_wire(request)

    assert "interaction_mode" not in wire["policy"]


def test_autofill_only_broadcast_serialization_round_trips_mode():
    request = BroadcastCommandRequest(
        (ConnectionId("conn-1"),),
        "cat .bash_history",
        BroadcastExecutionPolicy(
            interaction_mode=ExecutionInteractionMode.AUTOFILL_ONLY,
        ),
    )

    wire = broadcast_command_request_to_wire(request)
    restored = broadcast_command_request_from_wire(wire)

    assert wire["policy"]["interaction_mode"] == "autofill_only"
    assert (
        restored.policy.interaction_mode
        is ExecutionInteractionMode.AUTOFILL_ONLY
    )
