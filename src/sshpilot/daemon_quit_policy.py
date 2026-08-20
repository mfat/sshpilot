"""Application quit policy for daemon-backed resources.

Quitting sshPilot ends everything it started: remote sessions, SFTP services,
transfers, forwards, the background daemon itself, and the ControlMasters the
daemon spawned. There is deliberately no "leave it running" outcome — the only
question a user is ever asked is whether to go through with the quit, and that
confirmation appears only when live work would be lost.

Ending it is not the same as claiming it ended. Exit is gated on
:func:`verify_quit_teardown`, which asks the daemon package's authoritative
verifier what is still alive; anything owned that survives keeps the window
open with a list of what it is. The alternative — exiting anyway — strands
processes with no owner left to reap them, which is exactly the residue the
next launch then has to fight.
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
    """User (or policy) choice when quitting with daemon work.

    Quit tears everything down; the only choice is whether to proceed.
    """

    TERMINATE_ALL = "terminate_all"
    CANCEL = "cancel"


def count_daemon_terminals(window) -> int:
    """How many mapped terminals are on the daemon SSH path."""
    total = 0
    for terms in getattr(window, "connection_to_terminals", {}).values():
        for term in terms:
            if getattr(term, "_daemon_mode", False) or (
                getattr(term, "_daemon_controller", None) is not None
            ):
                total += 1
    return total


def window_has_daemon_terminals(window) -> bool:
    """True when any mapped terminal is on the daemon SSH path."""
    return count_daemon_terminals(window) > 0


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
    """Whether quitting would end live daemon work, so it needs confirming."""
    if window_has_daemon_terminals(window):
        return True
    client = client if client is not None else getattr(window, "client", None)
    summary = daemon_active_work_summary(client)
    return any(summary.values())


def resolve_quit_decision_from_policy(config) -> Optional[DaemonQuitDecision]:
    """Map app-close policy to an automatic decision, or None when ASK.

    Quit always terminates; the policy only decides whether the user is asked
    to confirm first.
    """
    policy = resolve_app_close_policy(config)
    if policy == TerminalClosePolicy.TERMINATE:
        return DaemonQuitDecision.TERMINATE_ALL
    return None  # ASK


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
    daemon_identity=None,
    socket_path: Optional[os.PathLike] = None,
    timeout: float = _TERMINATE_WAIT_SECONDS,
    poll_interval: float = _TERMINATE_POLL_SECONDS,
) -> list[str]:
    """Block until the force-stopped daemon is confirmed gone.

    Success requires every known indicator to be satisfied:

    * an app-launched process handle has exited,
    * the Unix socket (when known) no longer exists, and
    * a captured daemon identity (when one was taken before teardown) no
      longer matches a live process.

    The identity check is what closes the gap between "the socket is gone"
    and "the daemon is gone": a daemon that closes its listener but keeps
    running has disappeared from the only place a socket-only wait would
    look, and would otherwise pass final verification. When no handle, socket
    or identity is known, probes ``get_daemon_status`` until the daemon is
    unreachable. Returns error messages on timeout.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    have_process = daemon_process is not None
    have_socket = socket_path is not None
    have_identity = daemon_identity is not None

    def _identity_gone() -> bool:
        if daemon_identity is None:
            return True
        return not daemon_identity.matches_live_process()

    while True:
        process_done = _process_has_exited(daemon_process)
        socket_gone = _socket_is_gone(socket_path)
        identity_gone = _identity_gone()

        if not have_process and not have_socket and not have_identity:
            # External / unknown layout: treat unreachable status as gone.
            if client is None:
                return []
            try:
                client.get_daemon_status()
            except Exception:
                return []
        elif process_done and socket_gone and identity_gone:
            return []

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, float(poll_interval)))

    details = []
    if have_process and not _process_has_exited(daemon_process):
        details.append("daemon process still running")
    if have_socket and not _socket_is_gone(socket_path):
        details.append(f"socket still present ({socket_path})")
    if have_identity and daemon_identity.matches_live_process():
        details.append(f"daemon process still running (pid={daemon_identity.pid})")
    if not have_process and not have_socket and not have_identity:
        details.append("daemon still responding to status probes")
    return [
        "daemon did not finish shutting down: "
        + (", ".join(details) if details else "timeout")
    ]


def resolve_daemon_socket_path(client=None) -> Optional[os.PathLike]:
    """Return the daemon endpoint this app is bound to, or ``None``.

    Prefers the path the live client actually connected to so a session
    started against an explicit ``--socket`` is torn down at that path rather
    than the default one.
    """
    if client is not None:
        socket_path = getattr(client, "_socket_path", None)
        if socket_path is None:
            socket_path = getattr(client, "socket_path", None)
        if socket_path is not None:
            return socket_path
    try:
        from .daemon.lifecycle import resolve_socket_path

        return resolve_socket_path()
    except Exception:
        return None


def _resolve_terminate_context(window):
    """Return ``(client, daemon_process, socket_path)`` for Terminate everything."""
    client = getattr(window, "client", None)
    app = window.get_application() if hasattr(window, "get_application") else None
    selection = getattr(app, "_api_client_selection", None) if app is not None else None
    if selection is None:
        selection = getattr(window, "_api_client_selection", None)

    daemon_process = getattr(selection, "daemon_process", None) if selection else None

    return client, daemon_process, resolve_daemon_socket_path(client)


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


def capture_daemon_identity(socket_path: Optional[os.PathLike]):
    """Fingerprint the daemon before teardown, so it cannot vanish from view.

    A daemon that closes its socket but keeps running would otherwise pass
    final verification: the socket check stops finding it and nothing else
    knows what to look for. Taken from peer credentials rather than a
    ``Popen`` handle, so it works for a daemon this app merely connected to.
    """
    from .daemon.runtime_verification import capture_daemon_identity as _capture

    try:
        return _capture(socket_path)
    except Exception:
        logger.debug("Could not capture the daemon identity", exc_info=True)
        return None


def force_daemon_exit(
    socket_path: Optional[os.PathLike], *, daemon_identity=None
) -> list[str]:
    """Make the daemon exit when the graceful stop did not, then collect after it.

    Escalates SIGTERM (which the daemon handles as a normal shutdown, so it
    still tears down its own sessions and ControlMasters) and then SIGKILL,
    finally removing an orphaned socket file. A killed daemon cannot run its
    own cleanup, so its registered children and the ControlMasters it spawned
    are reaped here instead — the ControlMaster sweep under the same ownership
    rule the daemon's own shutdown uses, so an instance on an explicit
    ``--socket`` never touches the real daemon's masters.

    Returns an empty list only when nothing sshPilot owns is still running.
    """
    if socket_path is None:
        return []
    from .daemon.lifecycle import evict_socket_owner
    from .daemon.runtime_verification import terminate_owned_runtime

    errors: list[str] = []
    try:
        if not evict_socket_owner(Path(socket_path)):
            errors.append("the background service did not exit")
    except Exception as exc:
        errors.append(f"force daemon exit: {exc}")

    try:
        result = terminate_owned_runtime(
            socket_path=socket_path, daemon_identity=daemon_identity
        )
    except Exception as exc:
        return errors + [f"runtime teardown: {exc}"]
    return errors + list(result.messages())


def verify_quit_teardown(
    socket_path: Optional[os.PathLike],
    *,
    daemon_identity=None,
) -> list[str]:
    """Return what is still running, empty when quit may proceed.

    This is the gate on application exit. It asks the authoritative verifier
    rather than re-deriving the answer, so "nothing is left running" is a
    checked claim and not an assumption about what teardown probably did.
    """
    from .daemon.runtime_verification import verify_sshpilot_runtime_terminated

    try:
        result = verify_sshpilot_runtime_terminated(
            socket_path=socket_path, daemon_identity=daemon_identity
        )
    except Exception as exc:
        # A verification that cannot run has not proven anything, and quit is
        # gated on proof.
        return [f"could not verify shutdown: {exc}"]
    return list(result.messages())


def _clear_terminate_quit_decision(window) -> None:
    """Undo the quit intent so the window is usable again after a refusal."""
    window._daemon_quit_decision = None
    window._daemon_quit_close_policy = None
    window._daemon_shutdown_intent = None
    app = window.get_application() if hasattr(window, "get_application") else None
    if app is not None:
        app._daemon_quit_decision = None
        app._daemon_shutdown_intent = None


def _present_incomplete_teardown(window, survivors: list[str]) -> None:
    """List what is still running, and stay open.

    The heading carries the outcome; the body is just the list. Why teardown
    fell short is a log concern.
    """
    from gi.repository import Adw

    body = "\n".join(f"• {message}" for message in survivors[:8])
    if len(survivors) > 8:
        body += "\n" + _("…and {n} more").format(n=len(survivors) - 8)

    dialog = Adw.AlertDialog.new(
        _("Could not quit completely"), body or _("Something is still running.")
    )
    dialog.add_response("ok", _("OK"))
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    dialog.present(window)


def apply_terminate_all(window) -> None:
    """Tear the runtime down, verify it is gone, and only then quit.

    The stop RPC, escalation and verification run on a background thread so
    the GTK main loop is not blocked. A daemon that does not answer or does
    not exit is escalated to signals rather than cancelling the quit — but
    the quit itself is conditional on the final verification: if anything
    sshPilot owns is still running, the application stays open and says what
    survived, because exiting would strand it with no owner left to reap it.
    """
    # Intent first — before stop_daemon — so transport_closed cannot reconnect.
    begin_terminate_shutdown_intent(window)

    client, daemon_process, socket_path = _resolve_terminate_context(window)
    # Captured before anything is asked to stop: teardown is exactly when the
    # daemon stops being findable through its socket.
    daemon_identity = capture_daemon_identity(socket_path)

    def _worker() -> None:
        errors = terminate_all_daemon_work(client)
        if not errors:
            errors = wait_for_daemon_termination(
                client=client,
                daemon_process=daemon_process,
                daemon_identity=daemon_identity,
                socket_path=socket_path,
            )
        if errors:
            for message in errors:
                logger.warning(
                    "terminate-all during quit: %s; escalating", message
                )
            force_daemon_exit(socket_path, daemon_identity=daemon_identity)

        # Exit is gated on proof, not on the teardown steps having been
        # attempted. Whatever the escalation above believed it accomplished,
        # the verifier is what decides: it re-checks the socket, the daemon's
        # registered children, and sshPilot's own ControlMasters.
        survivors = verify_quit_teardown(
            socket_path, daemon_identity=daemon_identity
        )

        def _finish() -> bool:
            if survivors:
                for message in survivors:
                    logger.error("quit refused: %s is still running", message)
                _clear_terminate_quit_decision(window)
                _present_incomplete_teardown(window, survivors)
                return False

            from . import shutdown

            shutdown.cleanup_and_quit(window)
            return False

        _invoke_on_main(_finish)

    _run_in_background(_worker)


def present_daemon_quit_dialog(window, *, on_decision) -> Any:
    """Confirm a quit that will end live remote work, and invoke callback.

    The body is only what the user has running. How teardown is carried out
    and verified is this module's problem, not something to explain in a
    dialog the user is trying to get past.

    Uses ``Adw.AlertDialog`` (libadwaita) per project dialog rules.
    """
    from gi.repository import Adw

    summary = daemon_active_work_summary(getattr(window, "client", None))
    sessions = summary["sessions_active"]
    if not sessions:
        # The status probe can come back empty (a daemon mid-shutdown, a
        # transport hiccup) while tabs are plainly open. Count what the window
        # is showing rather than telling the user they have nothing running.
        sessions = count_daemon_terminals(window)
    body = _("Number of running sessions: {n}").format(n=sessions)

    dialog = Adw.AlertDialog.new(_("Quit SSH Pilot?"), body)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("terminate", _("Quit"))
    dialog.set_response_appearance(
        "terminate", Adw.ResponseAppearance.DESTRUCTIVE
    )
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def _on_response(_dialog, response: str) -> None:
        if response == "terminate":
            on_decision(DaemonQuitDecision.TERMINATE_ALL)
        else:
            on_decision(DaemonQuitDecision.CANCEL)

    dialog.connect("response", _on_response)
    dialog.present(window)
    return dialog


__all__ = [
    "DaemonQuitDecision",
    "apply_terminate_all",
    "begin_terminate_shutdown_intent",
    "daemon_active_work_summary",
    "capture_daemon_identity",
    "count_daemon_terminals",
    "force_daemon_exit",
    "resolve_daemon_socket_path",
    "verify_quit_teardown",
    "has_daemon_active_work",
    "present_daemon_quit_dialog",
    "resolve_quit_decision_from_policy",
    "terminate_all_daemon_work",
    "wait_for_daemon_termination",
    "window_has_daemon_terminals",
]
