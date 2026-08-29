"""Daemon-owned identity operations: agent keys, deployment, authorized keys.

This service orchestrates native OpenSSH — it never recreates it:

* agent key listing/loading/removal run the native ``ssh-add`` against the
  selected provider's agent environment;
* public-key deployment runs the native ``ssh-copy-id`` (OpenSSH owns remote
  ``.ssh``/``authorized_keys`` creation, duplicate detection, permissions and
  shell quoting);
* authorized-key listing/removal — the one management surface OpenSSH does
  not provide — uses ordinary ``ssh`` transport, preserves unknown lines
  byte-for-byte, and derives fingerprints with ``ssh-keygen``;
* key metadata and fingerprints come from ``ssh-keygen``, not from custom
  key-format parsing.

Passphrases, passwords and host-key decisions raised by those children are
bridged through the interaction broker's askpass transport as typed
interactions.  Private keys never enter daemon storage; ``ssh-add`` reads
them from the user's own key store in place.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId, SessionId
from sshpilot.api.models.identity import (
    AGENT_UNAVAILABLE,
    AUTHORIZED_KEYS_LINE_NOT_FOUND,
    AUTHORIZED_KEYS_STALE,
    SSH_COPY_ID_UNAVAILABLE,
    AgentKey,
    AgentKeyList,
    AgentKeyMutationRequest,
    AuthorizedKeyEntry,
    AuthorizedKeyLineKind,
    AuthorizedKeyList,
    DeployKeyRequest,
    ListAuthorizedKeysRequest,
    RemoveAuthorizedKeyRequest,
)
from sshpilot.api.models.keys import KeyStoreScope, ListKeysRequest, ReadPublicKeyRequest
from sshpilot.api.models.operations import (
    IdentityFailure,
    IdentityFailureCode,
    OperationKind,
    OperationSummary,
)
from sshpilot.authorized_keys_parser import (
    AuthorizedKeyEntry as ParsedKeyEntry,
)
from sshpilot.authorized_keys_parser import (
    parse_file,
)
from sshpilot.core.identity_service import IdentityStateService
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.operation_runtime import OperationHandle, OperationRuntime
from sshpilot.runtime_identity import new_operation_id

logger = logging.getLogger(__name__)

# Remote commands (ordinary native ssh transport; single shell strings).
_REMOTE_READ_COMMAND = (
    "if [ -f ~/.ssh/authorized_keys ]; then cat ~/.ssh/authorized_keys; fi"
)
_REMOTE_WRITE_COMMAND = (
    "umask 077; "
    'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && '
    't=$(mktemp "$HOME/.ssh/.authorized_keys.XXXXXX") && '
    'cat > "$t" && chmod 600 "$t" && '
    'mv -f "$t" "$HOME/.ssh/authorized_keys"'
)

# ssh(1) exits 255 on its own errors (connect/auth); the remote command's exit
# status is returned otherwise.
_SSH_ERROR_EXIT = 255

_BASE_ENV_ALLOWLIST = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "LANG", "SSH_AUTH_SOCK", "SSH_AGENT_PID"}
)

_REMOTE_COMMAND_TIMEOUT = 120.0


class DaemonIdentityService:
    """Authoritative daemon service for identity operations.

    ``popen`` is the single process seam: production passes
    ``subprocess.Popen``; tests inject a deterministic fake runner.  No
    private key, password, or passphrase ever appears in argv, results,
    messages, or logs.
    """

    def __init__(
        self,
        state_service: IdentityStateService,
        key_service: DaemonKeyService,
        operation_runtime: OperationRuntime,
        *,
        launch_provider=None,
        interaction_broker=None,
        popen: Optional[Callable[..., object]] = None,
        environ: Optional[Dict[str, str]] = None,
    ) -> None:
        self._state = state_service
        self._keys = key_service
        self._operations = operation_runtime
        self._launch_provider = launch_provider
        self._broker = interaction_broker
        self._popen = popen if popen is not None else subprocess.Popen
        self._environ = dict(os.environ if environ is None else environ)

    def attach_interaction_broker(self, broker) -> None:
        """Attach the daemon interaction broker (post-composition hook).

        The broker is constructed after the core services; askpass bridging
        for agent key loading, deployment, and remote commands activates once
        attached.  Without a broker, operations still run but interactive
        prompts fail closed instead of hanging on a missing TTY.
        """
        self._broker = broker

    # ------------------------------------------------------------------
    # Provider state (delegated to the settings-backed state service)
    # ------------------------------------------------------------------

    def get_providers(self):
        return self._state.get_providers()

    def get_state(self):
        return self._state.get_state()

    def update_selection(self, request):
        return self._state.update_selection(request)

    def update_configuration(self, request):
        return self._state.update_configuration(request)

    # ------------------------------------------------------------------
    # Agent keys (native ssh-add)
    # ------------------------------------------------------------------

    def list_agent_keys(self) -> AgentKeyList:
        """List the keys loaded in the selected agent (``ssh-add -l``)."""
        return self._list_agent_keys(None)

    def list_provider_agent_keys(self, provider_id: str) -> AgentKeyList:
        """List the keys loaded in one named provider's agent (``ssh-add -l``).

        Scoped to an explicit registry provider id (e.g. ``'auto'`` for the
        system ssh-agent) so the caller observes that provider regardless of
        which provider is currently selected.  Raises ``AGENT_UNAVAILABLE``
        when the named provider's agent cannot be reached.
        """
        if type(provider_id) is not str or not provider_id.strip():
            raise TypeError("a non-empty provider id is required")
        return self._list_agent_keys(provider_id.strip().lower())

    def _list_agent_keys(self, provider_id: Optional[str]) -> AgentKeyList:
        returncode, stdout, _stderr = self._run_ssh_add(
            ["-l"], askpass=False, provider_id=provider_id
        )
        if returncode == 1:
            # "The agent has no identities."
            return AgentKeyList(keys=())
        if returncode != 0:
            raise self._agent_unavailable()
        keys: List[AgentKey] = []
        for line in stdout.splitlines():
            key = _parse_ssh_add_line(line)
            if key is not None:
                keys.append(key)
        return AgentKeyList(keys=tuple(keys))

    def add_agent_key(self, request: AgentKeyMutationRequest) -> AgentKeyList:
        """Load one daemon-known key into the selected agent (``ssh-add``)."""
        if type(request) is not AgentKeyMutationRequest:
            raise TypeError("an AgentKeyMutationRequest is required")
        private_path, _public_path = self._resolve_key_paths(
            request.key_id, request.scope
        )
        returncode, _stdout, _stderr = self._run_ssh_add(
            [private_path], askpass=True
        )
        if returncode != 0:
            raise SshPilotError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "The key could not be loaded into the agent",
            )
        return self.list_agent_keys()

    def remove_agent_key(self, request: AgentKeyMutationRequest) -> AgentKeyList:
        """Remove one daemon-known key from the selected agent (``ssh-add -d``)."""
        if type(request) is not AgentKeyMutationRequest:
            raise TypeError("an AgentKeyMutationRequest is required")
        private_path, _public_path = self._resolve_key_paths(
            request.key_id, request.scope
        )
        returncode, _stdout, _stderr = self._run_ssh_add(
            ["-d", private_path], askpass=False
        )
        if returncode != 0:
            raise SshPilotError(
                ErrorCode.KEY_NOT_FOUND,
                "The key is not loaded in the agent",
            )
        return self.list_agent_keys()

    def _run_ssh_add(
        self,
        args: Sequence[str],
        *,
        askpass: bool,
        provider_id: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        if provider_id is None:
            env = self._state.agent_environment(self._base_env())
        else:
            env = self._state.provider_agent_environment(
                provider_id, self._base_env()
            )
        argv = ["ssh-add", *[str(arg) for arg in args]]
        scope_id: Optional[SessionId] = None
        if askpass and self._broker is not None:
            scope_id = SessionId(new_operation_id())
            argv, env = self._broker.prepare_operation_launch(
                argv,
                env,
                scope_id=scope_id,
                connection_id=None,
                hostname="",
            )
        try:
            # Askpass runs block on broker interactions (which carry their own
            # deadlines); plain runs get a bounded wait so a wedged agent
            # cannot stall the executor lane.
            completed = self._run_capture(argv, env, None, 30.0)
        finally:
            if scope_id is not None and self._broker is not None:
                self._broker.cancel_session(scope_id)
        return completed

    # ------------------------------------------------------------------
    # Public-key deployment (native ssh-copy-id)
    # ------------------------------------------------------------------

    def deploy_key(
        self,
        request: DeployKeyRequest,
        *,
        owner_client_id: Optional[ClientId] = None,
    ) -> OperationSummary:
        """Start a native ``ssh-copy-id`` deployment operation."""
        if type(request) is not DeployKeyRequest:
            raise TypeError("a DeployKeyRequest is required")
        state = self._state.get_state()
        if not state.ssh_copy_id_available:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "ssh-copy-id is not installed; install it to deploy keys",
                details={"code": SSH_COPY_ID_UNAVAILABLE},
            )
        self._require_launch_provider()
        _private_path, public_path = self._resolve_key_paths(
            request.key_id, request.scope
        )
        self._keys.read_public_key(
            ReadPublicKeyRequest(key_id=request.key_id, scope=request.scope)
        )
        connection_id = request.connection_id

        def _body(handle: OperationHandle) -> str:
            try:
                provider = self._require_launch_provider()
            except SshPilotError as error:
                raise _identity_error(
                    IdentityFailureCode.LAUNCH_PREPARATION_UNAVAILABLE,
                    error.code,
                ) from error
            try:
                argv, env = provider.prepare_copy_id_launch(
                    request.connection_id, public_path, force=request.force
                )
            except SshPilotError as error:
                raise _identity_error(
                    _identity_launch_failure_code(error.code),
                    error.code,
                ) from error
            return self._run_deploy(handle, argv, env, connection_id)

        return self._operations.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            _body,
            connection_id=connection_id,
            owner_client_id=owner_client_id,
            message="Deploying the public key",
            failure_mapper=_identity_failure,
        )

    def _run_deploy(
        self,
        handle: OperationHandle,
        argv: Sequence[str],
        env: Dict[str, str],
        connection_id: ConnectionId,
    ) -> str:
        scope_id: Optional[SessionId] = None
        final_env = dict(env)
        final_argv = tuple(argv)
        if self._broker is not None:
            # One public scope per daemon resource: the interaction scope of a
            # key-deployment operation IS its public OperationId. The frontend
            # learns that ID from OperationSummary and binds its interaction
            # presenter to it; a private random scope would be unknowable.
            scope_id = SessionId(str(handle.operation_id))
            final_argv, final_env = self._broker.prepare_operation_launch(
                final_argv,
                final_env,
                scope_id=scope_id,
                connection_id=connection_id,
            )
        try:
            try:
                process = self._popen(
                    list(final_argv),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=final_env,
                    text=True,
                    errors="replace",
                    start_new_session=True,
                )
                try:
                    process._sshpilot_process_group = True
                except Exception:
                    pass
            except OSError as exc:
                raise _identity_error(
                    IdentityFailureCode.PROCESS_START_FAILED,
                    ErrorCode.SESSION_STARTUP_FAILED,
                    diagnostic=str(exc),
                ) from exc
            handle.set_process(process)
            last_line = ""
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    text = line.rstrip("\r\n")
                    if text:
                        last_line = text
                        handle.report(text)
                returncode = process.wait()
            finally:
                handle.clear_process()
            handle.raise_if_cancelled()
            if returncode != 0:
                raise _identity_error(
                    _deploy_failure_code(last_line),
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    diagnostic=last_line,
                )
            if scope_id is not None and self._broker is not None:
                # Commit secrets the user chose to remember after a successful
                # run — BEFORE the interaction scope is torn down.
                # ``cancel_session`` destroys the askpass context and clears its
                # pending remembered secrets, so ``mark_authenticated`` must
                # come first. On failure/cancellation the raise below skips it
                # and the ``finally`` still cleans the scope up.
                self._broker.mark_authenticated(scope_id)
            return "The public key was installed on the server"
        finally:
            if scope_id is not None and self._broker is not None:
                self._broker.cancel_session(scope_id)

    # ------------------------------------------------------------------
    # Remote authorized-keys management (ordinary ssh transport)
    # ------------------------------------------------------------------

    def list_authorized_keys(
        self, request: ListAuthorizedKeysRequest
    ) -> AuthorizedKeyList:
        """Read and parse the remote ``authorized_keys`` via native ssh."""
        if type(request) is not ListAuthorizedKeysRequest:
            raise TypeError("a ListAuthorizedKeysRequest is required")
        content = self._run_remote_text(
            request.connection_id, _REMOTE_READ_COMMAND, None
        )
        return self._build_authorized_key_list(request.connection_id, content)

    def remove_authorized_key(
        self,
        request: RemoveAuthorizedKeyRequest,
        *,
        owner_client_id: Optional[ClientId] = None,
    ) -> OperationSummary:
        """Start an exact-line remote ``authorized_keys`` removal operation."""
        if type(request) is not RemoveAuthorizedKeyRequest:
            raise TypeError("a RemoveAuthorizedKeyRequest is required")
        provider = self._require_launch_provider()
        # Validate the connection eagerly so a missing connection fails the
        # RPC instead of surfacing as an operation failure.
        provider.prepare_remote_command_launch(
            request.connection_id, _REMOTE_READ_COMMAND
        )
        connection_id = request.connection_id

        def _body(handle: OperationHandle) -> str:
            return self._run_authorized_key_removal(handle, request)

        return self._operations.start_operation(
            OperationKind.AUTHORIZED_KEY_REMOVAL,
            _body,
            connection_id=connection_id,
            owner_client_id=owner_client_id,
            message="Removing the authorized key",
        )

    def _run_authorized_key_removal(
        self,
        handle: OperationHandle,
        request: RemoveAuthorizedKeyRequest,
    ) -> str:
        handle.report("Reading the remote authorized_keys")
        content = self._run_remote_text(
            request.connection_id, _REMOTE_READ_COMMAND, None, handle=handle
        )
        handle.raise_if_cancelled()
        if _file_revision(content) != request.file_revision:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The remote authorized_keys changed since it was read",
                details={"code": AUTHORIZED_KEYS_STALE},
            )
        lines = content.split("\n")
        remaining: List[str] = []
        removed = False
        for line in lines:
            if not removed and _line_id(line) == request.line_id:
                removed = True
                continue
            remaining.append(line)
        if not removed:
            raise SshPilotError(
                ErrorCode.KEY_NOT_FOUND,
                "The authorized key line no longer exists",
                details={"code": AUTHORIZED_KEYS_LINE_NOT_FOUND},
            )
        new_content = "\n".join(remaining)
        handle.report("Writing the updated authorized_keys")
        handle.raise_if_cancelled()
        self._run_remote_text(
            request.connection_id, _REMOTE_WRITE_COMMAND, new_content, handle=handle
        )
        handle.raise_if_cancelled()
        return "The authorized key was removed"

    # ------------------------------------------------------------------
    # Authorized-keys parsing (ssh-keygen-derived metadata)
    # ------------------------------------------------------------------

    def _build_authorized_key_list(
        self, connection_id: ConnectionId, content: str
    ) -> AuthorizedKeyList:
        items = parse_file(content)
        # parse_file appends a synthetic trailing "" item to preserve a final
        # newline; it is not a real line and must not become an entry.
        if content.endswith("\n") and items and items[-1] == "":
            items = items[:-1]
        entries: List[AuthorizedKeyEntry] = []
        for item in items:
            if isinstance(item, ParsedKeyEntry):
                entries.append(
                    self._key_entry(
                        item,
                        self._native_fingerprint(item),
                    )
                )
                continue
            line = item
            stripped = line.strip()
            if not stripped:
                kind = AuthorizedKeyLineKind.BLANK
            elif stripped.startswith("#"):
                kind = AuthorizedKeyLineKind.COMMENT
            else:
                kind = AuthorizedKeyLineKind.UNKNOWN
            entries.append(
                AuthorizedKeyEntry(
                    line_id=_line_id(line),
                    kind=kind,
                    raw_line=line,
                )
            )
        return AuthorizedKeyList(
            connection_id=connection_id,
            entries=tuple(entries),
            file_revision=_file_revision(content),
        )

    @staticmethod
    def _key_entry(
        parsed: ParsedKeyEntry,
        native: Optional[Tuple[str, str]],
    ) -> AuthorizedKeyEntry:
        key_type, fingerprint = (
            native
            if native is not None
            # The parser's pure-python SHA256 fingerprint is the documented
            # fallback when ssh-keygen cannot process the line.
            else (parsed.keytype, parsed.fingerprint_sha256)
        )
        return AuthorizedKeyEntry(
            line_id=_line_id(parsed.raw_line),
            kind=AuthorizedKeyLineKind.KEY,
            raw_line=parsed.raw_line,
            key_type=key_type,
            fingerprint=fingerprint,
            comment=parsed.comment,
            disabled=parsed.disabled,
        )

    def _native_fingerprint(
        self, parsed: ParsedKeyEntry
    ) -> Optional[Tuple[str, str]]:
        """Derive ``(key_type, fingerprint)`` via native ``ssh-keygen -lf -``.

        Returns None when ssh-keygen is unavailable or cannot process the
        key — the caller keeps the parser's fallback metadata, so a malformed
        key stays representable and is never silently dropped.
        """
        key_line = f"{parsed.keytype} {parsed.key_b64} {parsed.comment}".rstrip()
        try:
            process = self._popen(
                ["ssh-keygen", "-lf", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._base_env(),
                text=True,
                errors="replace",
                start_new_session=True,
            )
            try:
                stdout, _stderr = process.communicate(key_line + "\n", timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                return None
        except Exception:
            logger.debug("ssh-keygen fingerprint failed", exc_info=True)
            return None
        if process.returncode != 0 or not stdout:
            return None
        return _parse_ssh_keygen_line(stdout.splitlines()[0])

    # ------------------------------------------------------------------
    # Remote execution
    # ------------------------------------------------------------------

    def _run_remote_text(
        self,
        connection_id: ConnectionId,
        remote_command: str,
        input_text: Optional[str],
        *,
        handle: Optional[OperationHandle] = None,
    ) -> str:
        provider = self._require_launch_provider()
        argv, env = provider.prepare_remote_command_launch(
            connection_id, remote_command
        )
        scope_id: Optional[SessionId] = None
        if self._broker is not None:
            if handle is not None:
                # Long-running authorized-key operations scope their prompts to
                # their public OperationId — the same one the frontend learns
                # from OperationSummary (one public scope per resource).
                scope_id = SessionId(str(handle.operation_id))
            else:
                # Direct RPC (list_authorized_keys): no public resource ID is
                # visible to the frontend, so there is still no presenter to
                # bind; keep a private scope rather than inventing one.
                scope_id = SessionId(new_operation_id())
            argv, env = self._broker.prepare_operation_launch(
                argv,
                env,
                scope_id=scope_id,
                connection_id=connection_id,
            )
        try:
            returncode, stdout, stderr = self._run_capture(
                argv, env, input_text, _REMOTE_COMMAND_TIMEOUT, handle=handle
            )
        finally:
            if scope_id is not None and self._broker is not None:
                self._broker.cancel_session(scope_id)
        if returncode == 0:
            return stdout
        if returncode == _SSH_ERROR_EXIT:
            raise SshPilotError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "The SSH connection could not run the remote command",
            )
        raise SshPilotError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            _remote_failure_message(stderr),
        )

    def _run_capture(
        self,
        argv: Sequence[str],
        env: Dict[str, str],
        input_text: Optional[str],
        timeout: Optional[float],
        *,
        handle: Optional[OperationHandle] = None,
    ) -> Tuple[int, str, str]:
        try:
            process = self._popen(
                list(argv),
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                errors="replace",
                start_new_session=True,
            )
            try:
                process._sshpilot_process_group = True
            except Exception:
                pass
        except OSError as exc:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The native command could not be started",
            ) from exc
        if handle is not None:
            handle.set_process(process)
        try:
            try:
                stdout, stderr = process.communicate(input_text, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise SshPilotError(
                    ErrorCode.OPERATION_TIMED_OUT,
                    "The remote command timed out",
                ) from exc
            return process.returncode, stdout or "", stderr or ""
        finally:
            if handle is not None:
                handle.clear_process()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _require_launch_provider(self):
        if self._launch_provider is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "SSH launch preparation is unavailable",
            )
        return self._launch_provider

    def _resolve_key_paths(
        self, key_id: str, scope: KeyStoreScope
    ) -> Tuple[str, str]:
        key_list = self._keys.list_keys(ListKeysRequest(scope=scope))
        for summary in key_list.keys:
            if summary.key_id == key_id:
                return summary.private_path, summary.public_path
        raise SshPilotError(
            ErrorCode.KEY_NOT_FOUND,
            "The requested SSH key was not found",
        )

    def _base_env(self) -> Dict[str, str]:
        env = {
            key: value
            for key, value in self._environ.items()
            if key in _BASE_ENV_ALLOWLIST or key.startswith("LC_")
        }
        env["TERM"] = "xterm-256color"
        return env

    @staticmethod
    def _agent_unavailable() -> SshPilotError:
        return SshPilotError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "The selected SSH agent is unavailable",
            details={"code": AGENT_UNAVAILABLE},
        )


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def _parse_ssh_add_line(line: str) -> Optional[AgentKey]:
    """Parse one ``ssh-add -l`` line: ``<bits> <fingerprint> <comment> (<type>)``."""
    parts = line.split()
    if len(parts) < 3:
        return None
    fingerprint = parts[1]
    has_type = parts[-1].startswith("(") and parts[-1].endswith(")")
    key_type = parts[-1].strip("()") if has_type else ""
    comment = " ".join(parts[2:-1] if has_type else parts[2:])
    return AgentKey(
        fingerprint=fingerprint,
        comment=comment or key_type or fingerprint,
        key_type=key_type,
    )


def _line_id(raw_line: str) -> str:
    """Stable opaque identifier of one exact authorized_keys line."""
    digest = hashlib.sha256(raw_line.encode("utf-8", "surrogateescape")).hexdigest()
    return f"akl-{digest[:24]}"


def _file_revision(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8", "surrogateescape")).hexdigest()
    return f"akf-{digest[:24]}"


def _parse_ssh_keygen_line(line: str) -> Optional[Tuple[str, str]]:
    """Parse ``ssh-keygen -lf`` output: ``<bits> <fingerprint> <comment> (<type>)``."""
    parts = line.split()
    if len(parts) < 2:
        return None
    fingerprint = parts[1]
    key_type = ""
    if parts[-1].startswith("(") and parts[-1].endswith(")"):
        key_type = parts[-1].strip("()")
    return key_type, fingerprint


def _deploy_failure_code(last_line: str) -> IdentityFailureCode:
    """Classify opaque ssh-copy-id output into a stable presentation code."""
    text = (last_line or "").strip()
    lowered = text.lower()
    if "permission denied" in lowered:
        return IdentityFailureCode.AUTHENTICATION_FAILED
    if "connection refused" in lowered:
        return IdentityFailureCode.CONNECTION_REFUSED
    if "no route to host" in lowered or "name or service not known" in lowered:
        return IdentityFailureCode.SERVER_UNREACHABLE
    if "timed out" in lowered or "timeout" in lowered:
        return IdentityFailureCode.CONNECTION_TIMED_OUT
    return IdentityFailureCode.INSTALLATION_FAILED


def _identity_launch_failure_code(error_code: ErrorCode) -> IdentityFailureCode:
    return {
        ErrorCode.CONNECTION_NOT_FOUND: IdentityFailureCode.CONNECTION_NOT_FOUND,
        ErrorCode.UNSUPPORTED_SESSION_PROTOCOL: (
            IdentityFailureCode.SSH_CONNECTION_REQUIRED
        ),
        ErrorCode.VALIDATION_FAILED: IdentityFailureCode.HOST_IDENTIFIER_MISSING,
        ErrorCode.UNSUPPORTED_CAPABILITY: IdentityFailureCode.SSH_COPY_ID_UNAVAILABLE,
    }.get(error_code, IdentityFailureCode.LAUNCH_PREPARATION_UNAVAILABLE)


def _identity_error(
    code: IdentityFailureCode,
    error_code: ErrorCode,
    *,
    diagnostic: str = "",
) -> SshPilotError:
    return SshPilotError(
        error_code,
        code.value,
        details={
            "identity_failure_code": code.value,
            "diagnostic": diagnostic.replace("\x00", ""),
        },
    )


def _identity_failure(error: BaseException) -> IdentityFailure:
    if isinstance(error, SshPilotError):
        detail_code = error.details.get("identity_failure_code")
        try:
            code = IdentityFailureCode(detail_code)
        except (TypeError, ValueError):
            code = _identity_launch_failure_code(error.code)
        diagnostic = error.details.get("diagnostic", "")
        if type(diagnostic) is not str:
            diagnostic = ""
        return IdentityFailure(
            code=code,
            error_code=error.code,
            diagnostic=diagnostic.replace("\x00", ""),
        )
    return IdentityFailure(
        code=IdentityFailureCode.OPERATION_FAILED_UNEXPECTEDLY,
        error_code=ErrorCode.INTERNAL_ERROR,
    )


def _remote_failure_message(stderr: str) -> str:
    text = (stderr or "").strip()
    lowered = text.lower()
    if "permission denied" in lowered:
        return "Authentication failed while running the remote command"
    return "The remote command could not complete"
