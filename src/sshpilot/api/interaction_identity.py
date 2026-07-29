"""Strict daemon-lifetime interaction identifier helpers."""

from uuid import UUID, uuid4

from .models.common import InteractionId

_PREFIX = "interaction:"
_MAX_LENGTH = len(_PREFIX) + 36


def new_interaction_id() -> InteractionId:
    return interaction_id_from_uuid(uuid4())


def interaction_id_from_uuid(value: UUID) -> InteractionId:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError("interaction UUID must be non-nil")
    return InteractionId(f"{_PREFIX}{value!s}")


def interaction_uuid_from_id(value: InteractionId) -> UUID:
    if type(value) is not str or len(value) != _MAX_LENGTH:
        raise ValueError("interaction ID is malformed")
    if not value.startswith(_PREFIX):
        raise ValueError("interaction ID has the wrong prefix")
    try:
        parsed = UUID(value[len(_PREFIX) :])
    except (AttributeError, ValueError):
        raise ValueError("interaction ID is malformed") from None
    if parsed.int == 0 or str(parsed) != value[len(_PREFIX) :]:
        raise ValueError("interaction ID must contain a canonical non-nil UUID")
    return parsed
