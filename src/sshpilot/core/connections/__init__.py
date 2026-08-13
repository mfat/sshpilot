"""Connection domain package (GTK-free)."""
from .models import (
    ConnectionRecord,
    GroupRecord,
    MutationEvent,
    MutationKind,
    generate_duplicate_nickname,
)
from .service import ConnectionService

__all__ = [
    "ConnectionRecord",
    "ConnectionService",
    "GroupRecord",
    "MutationEvent",
    "MutationKind",
    "generate_duplicate_nickname",
]
