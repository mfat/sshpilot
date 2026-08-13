"""GTK interaction provider that turns core InteractionRequests into dialogs.

Daemon and headless code must depend on ``sshpilot.core.interaction`` models,
not this module. Wire via :func:`set_default_provider` from window startup.
"""
from __future__ import annotations

import logging
from typing import Optional

from sshpilot.core.interaction import (
    InteractionOutcome,
    InteractionRequest,
    InteractionResponse,
    PromptKind,
    ResponseType,
    decide_headless,
    validate_response,
)

logger = logging.getLogger(__name__)

_default_provider: Optional["GtkInteractionProvider"] = None


def get_default_provider() -> Optional["GtkInteractionProvider"]:
    return _default_provider


def set_default_provider(provider: Optional["GtkInteractionProvider"]) -> None:
    global _default_provider
    _default_provider = provider


class GtkInteractionProvider:
    """Maps typed interaction requests to existing GTK password/askpass dialogs."""

    def __init__(self, *, parent_widget=None, connection_manager=None):
        self.parent_widget = parent_widget
        self.connection_manager = connection_manager

    def handle(self, request: InteractionRequest) -> InteractionResponse:
        try:
            raw = self._prompt(request)
        except Exception as exc:
            logger.debug("GTK interaction provider failed: %s", exc, exc_info=True)
            return InteractionResponse(
                outcome=InteractionOutcome.UNAVAILABLE,
                message=str(exc),
            )
        return validate_response(request, raw)

    def _prompt(self, request: InteractionRequest) -> InteractionResponse:
        if request.kind in {PromptKind.PASSWORD, PromptKind.PASSPHRASE, PromptKind.VAULT_UNLOCK}:
            return self._secret_prompt(request)
        if request.kind == PromptKind.HOST_KEY:
            return InteractionResponse(
                outcome=InteractionOutcome.UNAVAILABLE,
                message="Host-key confirmation requires daemon broker or askpass",
            )
        return decide_headless(request)

    def _secret_prompt(self, request: InteractionRequest) -> InteractionResponse:
        parent = self.parent_widget
        if parent is None:
            return decide_headless(request)
        try:
            from sshpilot.window_dialogs import show_ssh_password_dialog
        except Exception:
            return decide_headless(request)

        password = show_ssh_password_dialog(
            from_widget=parent,
            connection=None,
            connection_manager=self.connection_manager,
            host=request.hostname or None,
            username=request.username or None,
            display_name=request.key_display or request.hostname or "",
        )
        if not password:
            return InteractionResponse(
                outcome=InteractionOutcome.CANCELLED,
                response_type=ResponseType.CANCEL,
            )
        return InteractionResponse(
            outcome=InteractionOutcome.ACCEPTED,
            response_type=ResponseType.SECRET,
            secret=password,
            remember=False,
        )
