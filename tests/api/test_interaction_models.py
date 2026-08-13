from datetime import timedelta

import pytest

from sshpilot.api.models.common import InteractionId, RequestId, utc_now
from sshpilot.api.models.interactions import (
    InteractionKind,
    InteractionRequest,
    InteractionResponse,
    InteractionStatus,
)


def test_secret_response_value_is_excluded_from_repr():
    response = InteractionResponse(
        interaction_id=InteractionId("interaction-1"),
        status=InteractionStatus.ANSWERED,
        value="super-secret",
    )

    assert "super-secret" not in repr(response)


def test_completed_interaction_cannot_be_pending_or_carry_cancelled_answer():
    with pytest.raises(ValueError):
        InteractionResponse(
            interaction_id=InteractionId("interaction-1"),
            status=InteractionStatus.PENDING,
        )
    with pytest.raises(ValueError):
        InteractionResponse(
            interaction_id=InteractionId("interaction-1"),
            status=InteractionStatus.CANCELLED,
            value="must-not-exist",
        )


def test_interaction_expiry_must_follow_creation():
    created = utc_now()
    with pytest.raises(ValueError):
        InteractionRequest(
            id=InteractionId("interaction-1"),
            request_id=RequestId("request-1"),
            kind=InteractionKind.PASSWORD,
            message="Password:",
            secret=True,
            created_at=created,
            expires_at=created - timedelta(seconds=1),
        )

