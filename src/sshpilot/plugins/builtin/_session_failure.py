"""Structured launch failures emitted by SSH Pilot's built-in protocols."""

from __future__ import annotations

from typing import Mapping

from ..api import ProtocolError
from ...api.errors import ErrorCode
from ...api.models.sessions import (
    PluginSessionFailure,
    PluginSessionFailureCode,
)


class BuiltinProtocolError(ProtocolError):
    """Keep plugin compatibility text local while carrying stable metadata."""

    def __init__(
        self,
        code: PluginSessionFailureCode,
        legacy_message: str,
        *,
        parameters: Mapping[str, str] | None = None,
        diagnostic: str = "",
    ) -> None:
        super().__init__(legacy_message)
        self.failure = PluginSessionFailure(
            code=code,
            error_code=ErrorCode.SESSION_STARTUP_FAILED,
            parameters=parameters or {},
            diagnostic=diagnostic,
        )


__all__ = ["BuiltinProtocolError"]
