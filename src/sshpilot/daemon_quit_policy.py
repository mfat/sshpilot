"""Application quit policy for daemon-backed resources.

Presents Keep running / Terminate everything / Cancel when active daemon work
exists, and applies the chosen decision without duplicating daemon state
machines in GTK.
"""

from __future__ import annotations

import logging
from enum import Enum
from gettext import gettext as _
from typing import Any, Optional

from .daemon_terminal_policy import TerminalClosePolicy, resolve_app_close_policy

logger = logging.getLogger(__name__)


class DaemonQuitDecision(str, Enum):
    """User (or policy) choice when quitting with daemon work."""

    KEEP_RUNNING = "keep_running"
    TERMINATE_ALL = "terminate_all"
    CANCEL = "cancel"


def window_has_daemon_terminals(window) -> bool:
    """True when any mapped terminal is on the daemon SSH path."""
    for terms in getattr(window, "connection_to_terminals", {}).values():
        for term in terms:
            if getattr(term, "_daemon_mode", False):
                return True
            if getattr(term, "_daemon_controller", None) is not None:
                return True
    return False


def daemon_active_work_summary(client) -> dict[str, int]:
    """Best-effort counts of live daemon resources via public status API."""
    empty = {
        "sessions_active": 0,
        "sftp_active": 0,
        "transfers_running": 0,
        "forwards_active": 0,
        "interactions_pending": 0,
    }
    if client is None:
        return empty
    try:
        status = client.get_daemon_status()
        resources = getattr(status, "resources", None)
        if resources is None:
            return empty
        return {
            "sessions_active": int(getattr(resources, "sessions_active", 0) or 0),
            "sftp_active": int(getattr(resources, "sftp_active", 0) or 0),
            "transfers_running": int(getattr(resources, "transfers_running", 0) or 0),
            "forwards_active": int(getattr(resources, "forwards_active", 0) or 0),
            "interactions_pending": int(
                getattr(resources, "interactions_pending", 0) or 0
            ),
        }
    except Exception:
        logger.debug("daemon status probe failed during quit", exc_info=True)
        return empty


def has_daemon_active_work(window, client=None) -> bool:
    """Whether quit should offer daemon keep-running / terminate choices."""
    if window_has_daemon_terminals(window):
        return True
    client = client if client is not None else getattr(window, "client", None)
    summary = daemon_active_work_summary(client)
    return any(summary.values())


def resolve_quit_decision_from_policy(config) -> Optional[DaemonQuitDecision]:
    """Map app-close policy to an automatic decision, or None when ASK."""
    policy = resolve_app_close_policy(config)
    if policy == TerminalClosePolicy.DETACH:
        return DaemonQuitDecision.KEEP_RUNNING
    if policy == TerminalClosePolicy.TERMINATE:
        return DaemonQuitDecision.TERMINATE_ALL
    return None  # ASK


def apply_keep_running(window) -> None:
    """Detach GTK views; leave daemon resources running."""
    window._daemon_quit_decision = DaemonQuitDecision.KEEP_RUNNING
    window._daemon_quit_close_policy = TerminalClosePolicy.DETACH
    app = window.get_application() if hasattr(window, "get_application") else None
    if app is not None:
        app._daemon_quit_decision = DaemonQuitDecision.KEEP_RUNNING

    from . import shutdown

    shutdown.cleanup_and_quit(window)


def terminate_all_daemon_work(client) -> list[str]:
    """Close/cancel daemon resources via public APIs. Returns error messages."""
    errors: list[str] = []
    if client is None:
        return ["no daemon client"]

    try:
        from .api.models.sessions import CloseSessionRequest, SessionState
        from .api.models.operations import (
            ClaimForwardRequest,
            CloseForwardRequest,
            CloseSftpRequest,
            ForwardState,
            SftpServiceState,
        )
        from .api.models.transfers import CancelTransferRequest, TransferState
    except Exception as exc:
        return [f"import failed: {exc}"]

    try:
        for session in list(client.list_sessions() or []):
            state = getattr(session, "state", None)
            if state in {SessionState.CLOSED, SessionState.FAILED}:
                continue
            try:
                client.close_session(CloseSessionRequest(session_id=session.id))
            except Exception as exc:
                errors.append(f"close_session {session.id}: {exc}")
    except Exception as exc:
        errors.append(f"list_sessions: {exc}")

    try:
        for service in list(client.list_sftp_services() or []):
            state = getattr(service, "state", None)
            if state in {SftpServiceState.CLOSED, SftpServiceState.FAILED}:
                continue
            try:
                client.close_sftp(CloseSftpRequest(service_id=service.id))
            except Exception as exc:
                errors.append(f"close_sftp {service.id}: {exc}")
    except Exception as exc:
        errors.append(f"list_sftp_services: {exc}")

    try:
        for transfer in list(client.list_transfers() or []):
            state = getattr(transfer, "state", None)
            if state in {
                TransferState.COMPLETED,
                TransferState.CANCELLED,
                TransferState.FAILED,
            }:
                continue
            try:
                client.cancel_transfer(CancelTransferRequest(transfer_id=transfer.id))
            except Exception as exc:
                errors.append(f"cancel_transfer {transfer.id}: {exc}")
    except Exception as exc:
        errors.append(f"list_transfers: {exc}")

    try:
        for forward in list(client.list_forwards() or []):
            state = getattr(forward, "state", None)
            if state in {ForwardState.CLOSED, ForwardState.FAILED}:
                continue
            try:
                try:
                    client.claim_forward(ClaimForwardRequest(forward_id=forward.id))
                except Exception:
                    pass
                client.close_forward(CloseForwardRequest(forward_id=forward.id))
            except Exception as exc:
                errors.append(f"close_forward {forward.id}: {exc}")
    except Exception as exc:
        errors.append(f"list_forwards: {exc}")

    try:
        cancel = getattr(client, "cancel_interaction", None)
        list_interactions = getattr(client, "list_interactions", None)
        if callable(list_interactions) and callable(cancel):
            from .api.models.interactions import InteractionState

            for interaction in list(list_interactions() or []):
                state = getattr(interaction, "state", None)
                if state not in {
                    InteractionState.PENDING,
                    InteractionState.CLAIMED,
                }:
                    continue
                try:
                    cancel(interaction.id)
                except Exception as exc:
                    errors.append(f"cancel_interaction {interaction.id}: {exc}")
    except Exception as exc:
        errors.append(f"interactions: {exc}")

    return errors


def apply_terminate_all(window) -> None:
    """Terminate daemon work then quit the GTK application."""
    window._daemon_quit_decision = DaemonQuitDecision.TERMINATE_ALL
    window._daemon_quit_close_policy = TerminalClosePolicy.TERMINATE
    app = window.get_application() if hasattr(window, "get_application") else None
    if app is not None:
        app._daemon_quit_decision = DaemonQuitDecision.TERMINATE_ALL

    client = getattr(window, "client", None)
    errors = terminate_all_daemon_work(client)
    for message in errors:
        logger.warning("terminate-all during quit: %s", message)

    from . import shutdown

    shutdown.cleanup_and_quit(window)


def present_daemon_quit_dialog(window, *, on_decision) -> Any:
    """Show Keep running / Terminate everything / Cancel and invoke callback.

    Uses ``Adw.AlertDialog`` (libadwaita) per project dialog rules.
    """
    from gi.repository import Adw

    summary = daemon_active_work_summary(getattr(window, "client", None))
    parts = []
    if summary["sessions_active"]:
        parts.append(_("Sessions: {n}").format(n=summary["sessions_active"]))
    if summary["sftp_active"]:
        parts.append(_("File managers: {n}").format(n=summary["sftp_active"]))
    if summary["transfers_running"]:
        parts.append(_("Transfers: {n}").format(n=summary["transfers_running"]))
    if summary["forwards_active"]:
        parts.append(_("Forwards: {n}").format(n=summary["forwards_active"]))
    if summary["interactions_pending"]:
        parts.append(
            _("Pending prompts: {n}").format(n=summary["interactions_pending"])
        )
    if window_has_daemon_terminals(window) and not parts:
        parts.append(_("Daemon-backed terminal tabs are open."))

    detail = "\n".join(parts) if parts else _(
        "Daemon-backed connections are active."
    )
    body = _(
        "Choose whether to leave remote work running in the daemon, "
        "or terminate everything and quit.\n\n{detail}"
    ).format(detail=detail)

    dialog = Adw.AlertDialog.new(_("Quit SSH Pilot?"), body)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("keep", _("Keep connections running"))
    dialog.add_response("terminate", _("Terminate everything and quit"))
    dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_response_appearance(
        "terminate", Adw.ResponseAppearance.DESTRUCTIVE
    )
    dialog.set_default_response("keep")
    dialog.set_close_response("cancel")

    def _on_response(_dialog, response: str) -> None:
        if response == "keep":
            on_decision(DaemonQuitDecision.KEEP_RUNNING)
        elif response == "terminate":
            on_decision(DaemonQuitDecision.TERMINATE_ALL)
        else:
            on_decision(DaemonQuitDecision.CANCEL)

    dialog.connect("response", _on_response)
    dialog.present(window)
    return dialog


__all__ = [
    "DaemonQuitDecision",
    "apply_keep_running",
    "apply_terminate_all",
    "daemon_active_work_summary",
    "has_daemon_active_work",
    "present_daemon_quit_dialog",
    "resolve_quit_decision_from_policy",
    "terminate_all_daemon_work",
    "window_has_daemon_terminals",
]
