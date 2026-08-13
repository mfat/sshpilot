"""Application quit policy for daemon-backed resources.

Presents Keep running / Terminate everything / Cancel when active daemon work
exists, and applies the chosen decision without duplicating daemon state
machines in GTK.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from enum import Enum
from gettext import gettext as _
from pathlib import Path
from typing import Any, Callable, Optional

from .daemon_terminal_policy import TerminalClosePolicy, resolve_app_close_policy

logger = logging.getLogger(__name__)

# How long Terminate everything waits for process exit / socket removal after
# the daemon accepts force-stop. Kept short enough for quit UX, long enough for
# drain of a few local sessions/SFTP children.
_TERMINATE_WAIT_SECONDS = 10.0
_TERMINATE_POLL_SECONDS = 0.05


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


def _client_supports_daemon_control(client) -> Optional[bool]:
    """Return True/False when capabilities are known, else None."""
    get_caps = getattr(client, "get_capabilities", None)
    if not callable(get_caps):
        return None
    try:
        from .api.capabilities import Capability

        supported = getattr(get_caps(), "supported", None) or frozenset()
        return Capability.DAEMON_CONTROL in supported
    except Exception:
        return None


def terminate_all_daemon_work(client) -> list[str]:
    """Request administrative teardown of all daemon resources.

    Prefer ``daemon.stop(force=true)`` so orphaned SFTP/sessions/forwards are
    torn down regardless of which client originally owned them.

    An empty return means the stop request was *accepted*. Callers that need
    the stronger "daemon process and socket are gone" guarantee must also call
    :func:`wait_for_daemon_termination`.
    """
    if client is None:
        return ["no daemon client"]

    stop = getattr(client, "stop_daemon", None)
    if callable(stop):
        try:
            from .api.models.daemon import StopDaemonRequest

            result = stop(StopDaemonRequest(force=True))
            if getattr(result, "accepted", False):
                return []
            message = getattr(result, "message", None) or "force stop was not accepted"
            return [f"stop_daemon: {message}"]
        except Exception as exc:
            return [f"stop_daemon force: {exc}"]

    # Real daemon clients advertise DAEMON_CONTROL and must expose stop_daemon.
    # Quietly falling back would leave orphaned SFTP services alive.
    if _client_supports_daemon_control(client) is True:
        return [
            "stop_daemon unavailable despite DAEMON_CONTROL capability; "
            "refusing incomplete per-resource terminate"
        ]

    # Incomplete mocks / legacy doubles without stop_daemon.
    return _terminate_all_per_resource(client)


def _terminate_all_per_resource(client) -> list[str]:
    """Fallback when ``stop_daemon`` is unavailable (incomplete test doubles)."""
    errors: list[str] = []

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


def _socket_is_gone(socket_path: Optional[os.PathLike]) -> bool:
    if socket_path is None:
        return True
    path = Path(socket_path)
    try:
        return not path.exists()
    except OSError:
        return True


def _process_has_exited(daemon_process) -> bool:
    if daemon_process is None:
        return True
    process = getattr(daemon_process, "process", daemon_process)
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return True
    try:
        return poll() is not None
    except Exception:
        return True


def wait_for_daemon_termination(
    *,
    client=None,
    daemon_process=None,
    socket_path: Optional[os.PathLike] = None,
    timeout: float = _TERMINATE_WAIT_SECONDS,
    poll_interval: float = _TERMINATE_POLL_SECONDS,
) -> list[str]:
    """Block until the force-stopped daemon is confirmed gone.

    Success when:
    * an app-launched process handle has exited, and
    * the Unix socket (when known) no longer exists.

    When neither handle nor socket is known, probes ``get_daemon_status`` until
    the daemon is unreachable. Returns error messages on timeout.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    have_process = daemon_process is not None
    have_socket = socket_path is not None

    while True:
        process_done = _process_has_exited(daemon_process)
        socket_gone = _socket_is_gone(socket_path)

        if have_process and have_socket:
            if process_done and socket_gone:
                return []
        elif have_process:
            if process_done:
                return []
        elif have_socket:
            if socket_gone:
                return []
        else:
            # External / unknown layout: treat unreachable status as gone.
            if client is None:
                return []
            try:
                client.get_daemon_status()
            except Exception:
                return []

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, float(poll_interval)))

    details = []
    if have_process and not _process_has_exited(daemon_process):
        details.append("daemon process still running")
    if have_socket and not _socket_is_gone(socket_path):
        details.append(f"socket still present ({socket_path})")
    if not have_process and not have_socket:
        details.append("daemon still responding to status probes")
    return [
        "daemon did not finish shutting down: "
        + (", ".join(details) if details else "timeout")
    ]


def _resolve_terminate_context(window):
    """Return ``(client, daemon_process, socket_path)`` for Terminate everything."""
    client = getattr(window, "client", None)
    app = window.get_application() if hasattr(window, "get_application") else None
    selection = getattr(app, "_api_client_selection", None) if app is not None else None
    if selection is None:
        selection = getattr(window, "_api_client_selection", None)

    daemon_process = getattr(selection, "daemon_process", None) if selection else None

    socket_path = None
    if client is not None:
        socket_path = getattr(client, "_socket_path", None)
        if socket_path is None:
            socket_path = getattr(client, "socket_path", None)
    if socket_path is None:
        try:
            from .daemon.lifecycle import resolve_socket_path

            socket_path = resolve_socket_path()
        except Exception:
            socket_path = None

    return client, daemon_process, socket_path


def _run_in_background(fn: Callable[[], None]) -> None:
    """Run ``fn`` off the GTK main thread (overridable in tests)."""
    threading.Thread(target=fn, name="sshpilot-terminate-all", daemon=True).start()


def _invoke_on_main(fn: Callable[[], Any]) -> None:
    """Schedule ``fn`` on the GTK main loop (overridable in tests)."""
    try:
        from gi.repository import GLib

        GLib.idle_add(fn)
    except Exception:
        fn()


def begin_terminate_shutdown_intent(window) -> None:
    """Mark intentional Terminate everything before any stop_daemon call.

    Suppresses daemon auto-reconnect so transport_closed from the force-stop
    cannot ``connect_or_start`` a replacement daemon while we wait for exit.
    """
    window._daemon_quit_decision = DaemonQuitDecision.TERMINATE_ALL
    window._daemon_quit_close_policy = TerminalClosePolicy.TERMINATE
    window._daemon_shutdown_intent = "terminate"
    app = window.get_application() if hasattr(window, "get_application") else None
    if app is not None:
        app._daemon_quit_decision = DaemonQuitDecision.TERMINATE_ALL
        app._daemon_shutdown_intent = "terminate"
        cancel = getattr(app, "cancel_daemon_reconnect", None)
        if callable(cancel):
            try:
                cancel(reason="terminate_all")
            except Exception:
                logger.debug("cancel_daemon_reconnect failed", exc_info=True)


def _clear_terminate_quit_decision(window) -> None:
    window._daemon_quit_decision = None
    window._daemon_quit_close_policy = None
    window._daemon_shutdown_intent = None
    app = window.get_application() if hasattr(window, "get_application") else None
    if app is not None:
        app._daemon_quit_decision = None
        app._daemon_shutdown_intent = None


def _present_terminate_failed(window, errors: list[str]) -> None:
    """Keep the window open and report that Terminate everything failed."""
    from gi.repository import Adw

    detail = "\n".join(f"• {message}" for message in errors[:8])
    if len(errors) > 8:
        detail += "\n" + _("…and {n} more").format(n=len(errors) - 8)
    body = _(
        "Could not terminate all daemon work. The application was not quit "
        "so remote sessions and file transfers are not left half-closed.\n\n"
        "{detail}"
    ).format(detail=detail or _("Unknown error"))

    dialog = Adw.AlertDialog.new(_("Terminate everything failed"), body)
    dialog.add_response("ok", _("OK"))
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    dialog.present(window)


def apply_terminate_all(window) -> None:
    """Force-stop the daemon, wait for confirmed exit, then quit.

    The stop RPC and process/socket wait run on a background thread so the GTK
    main loop is not blocked for the full RPC or drain timeout. Quit proceeds
    only after termination is confirmed (or fails with an error dialog).
    """
    # Intent first — before stop_daemon — so transport_closed cannot reconnect.
    begin_terminate_shutdown_intent(window)

    client, daemon_process, socket_path = _resolve_terminate_context(window)

    def _worker() -> None:
        errors = terminate_all_daemon_work(client)
        if not errors:
            errors = wait_for_daemon_termination(
                client=client,
                daemon_process=daemon_process,
                socket_path=socket_path,
            )

        def _finish() -> bool:
            if errors:
                for message in errors:
                    logger.warning("terminate-all during quit: %s", message)
                _clear_terminate_quit_decision(window)
                _present_terminate_failed(window, errors)
                return False

            from . import shutdown

            shutdown.cleanup_and_quit(window)
            return False

        _invoke_on_main(_finish)

    _run_in_background(_worker)


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
    "begin_terminate_shutdown_intent",
    "daemon_active_work_summary",
    "has_daemon_active_work",
    "present_daemon_quit_dialog",
    "resolve_quit_decision_from_policy",
    "terminate_all_daemon_work",
    "wait_for_daemon_termination",
    "window_has_daemon_terminals",
]
