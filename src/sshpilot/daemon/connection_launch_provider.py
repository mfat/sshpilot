"""Daemon connection launch provider.

The core ``ConnectionApplicationService`` delegates launch preparation to an
injected launch provider; this module is the daemon's implementation. It
resolves connections through a headless ``ConnectionRecord`` resolver and
uses the shared OpenSSH/plugin launch builders with daemon-owned settings and
credential seams.

The provider never receives a filesystem path from the client; the record's
``source``/``config_root`` fields (daemon-owned persistence metadata) feed the
daemon launch view so OpenSSH resolves the correct root config.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.connections import ConnectionId
from ..api.models.sessions import PluginSessionFailure
from ..core.connections.models import ConnectionRecord

logger = logging.getLogger(__name__)


class _PluginSessionLaunchError(SshPilotError):
    """Carry one built-in plugin presentation failure to SessionRuntime."""

    def __init__(
        self,
        failure: PluginSessionFailure,
        *,
        connection_id: ConnectionId,
    ) -> None:
        if type(failure) is not PluginSessionFailure:
            raise TypeError("a plugin session failure is required")
        super().__init__(
            failure.error_code,
            failure.code.value,
            connection_id=connection_id,
        )
        self.session_failure = failure


def _string(value: Any) -> str:
    return str(value or "").strip()


class HeadlessConnectionView:
    """Headless compatibility view over a ``ConnectionRecord``.

    Exposes only the attributes and pure helpers the legacy launch machinery
    reads (``ssh_connection_builder`` + plugin registry). It never persists
    state and never touches the filesystem beyond existing config lookups.
    """

    def __init__(
        self,
        record: ConnectionRecord,
        *,
        manager: Optional[Any] = None,
    ) -> None:
        self._record = record
        self._manager = manager
        self.data = dict(record.data or {})
        self._status = None
        self.is_connected = False
        self.resolved_identity_files: List[str] = []
        # IdentityAgent policy is daemon-loaded SSH configuration, not frontend
        # mutation: ``none`` disables agent usage; any other non-empty value is
        # a custom agent socket/directive.
        identity_agent = _string(self.data.get("identity_agent"))
        if identity_agent.lower() == "none":
            self.identity_agent_disabled = True
            self.identity_agent_directive = ""
        else:
            self.identity_agent_disabled = False
            self.identity_agent_directive = identity_agent
        self._password: Optional[str] = None
        self._effective_username: Optional[str] = None

    # -- plain attribute passthrough ----------------------------------------

    @property
    def id(self) -> str:
        return self._record.id

    @property
    def nickname(self) -> str:
        return self._record.nickname

    @property
    def host(self) -> str:
        return _string(self._record.host) or self._record.nickname

    @property
    def hostname(self) -> str:
        return _string(self._record.hostname)

    @property
    def username(self) -> str:
        return _string(self._record.username)

    @property
    def effective_username(self) -> str:
        """The account OpenSSH resolves for this host.

        Equals :attr:`username` whenever the Host block authored ``User``. When
        it did not, the account is inherited — from a global block or from
        OpenSSH's own local-user default — and only ``ssh -G`` knows it. Secret
        lookup and user-facing labels must use this, never the authored value,
        which is empty in exactly that case.

        Resolved once per view; failures fall back to the authored value rather
        than guessing a local username.
        """
        if self._effective_username is not None:
            return self._effective_username
        resolved = self.username
        if not resolved:
            host_label = self.resolve_host_identifier() or self.get_effective_host()
            if host_label:
                try:
                    from ..core.ssh_config_effective import get_effective_ssh_config

                    config_override = self._resolve_config_override_path()
                    effective = get_effective_ssh_config(
                        host_label, config_file=config_override
                    )
                    resolved = _string(effective.get("user"))
                except Exception:
                    logger.debug(
                        "Could not resolve effective username", exc_info=True
                    )
                    resolved = ""
        self._effective_username = resolved
        return resolved

    @property
    def port(self) -> int:
        try:
            return int(self._record.port)
        except (TypeError, ValueError):
            return 22

    @property
    def protocol(self) -> str:
        return _string(self._record.protocol) or "ssh"

    @property
    def source(self) -> str:
        return _string(self._record.source) or _string(self.data.get("source"))

    @property
    def generation(self) -> int:
        return int(getattr(self._record, "generation", 0) or 0)

    @property
    def auth_method(self) -> int:
        try:
            return int(self.data.get("auth_method", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def authentication_method(self) -> Any:
        raw = self.auth_method
        return 1 if raw == 1 else 0

    @property
    def keyfile(self) -> str:
        return _string(self.data.get("keyfile"))

    @property
    def key_select_mode(self) -> int:
        try:
            return int(self.data.get("key_select_mode", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def identity_files(self) -> List[str]:
        raw = self.data.get("identity_files") or ()
        if isinstance(raw, str):
            return [f.strip() for f in raw.splitlines() if f.strip()]
        return [str(f) for f in raw if f]

    @property
    def local_command(self) -> str:
        return _string(self.data.get("local_command"))

    @property
    def password(self) -> Optional[str]:
        return self._password

    @password.setter
    def password(self, value: Optional[str]) -> None:
        self._password = value

    # -- pure helpers used by the launch machinery --------------------------

    def get_effective_host(self) -> str:
        if self.hostname:
            return self.hostname
        if self.host:
            return self.host
        return self.nickname

    def resolve_host_identifier(self) -> str:
        candidates: List[str] = []
        tokens = self.data.get("__host_tokens")
        if isinstance(tokens, (list, tuple)):
            for token in tokens:
                token_str = _string(token)
                if not token_str or token_str.startswith("!"):
                    continue
                if any(ch in token_str for ch in ("*", "?")):
                    continue
                candidates.append(token_str)
        for key in ("host", "nickname", "hostname"):
            value = _string(self.data.get(key))
            if value:
                candidates.append(value)
        for attr in ("host", "nickname", "hostname"):
            value = _string(getattr(self, attr, ""))
            if value:
                candidates.append(value)
        for candidate in candidates:
            if candidate:
                return candidate
        return ""

    def _resolve_config_override_path(self) -> Optional[str]:
        def _existing(path: str) -> Optional[str]:
            if not path:
                return None
            expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
            return expanded if os.path.exists(expanded) else None

        root = _existing(_string(self.data.get("config_root")))
        if root:
            return root
        source = _existing(self.source)
        if source:
            return source
        return None

    def collect_identity_file_candidates(
        self,
        effective_cfg: Optional[Dict[str, Union[str, List[str]]]] = None,
    ) -> List[str]:
        candidates: List[str] = []
        seen = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if os.path.isfile(expanded) and expanded not in seen:
                candidates.append(expanded)
                seen.add(expanded)

        key_mode = self.key_select_mode
        keyfile_value = self.keyfile
        if keyfile_value.lower().startswith("select key file"):
            keyfile_value = ""
        if key_mode in (1, 2):
            _add(keyfile_value)
            return candidates

        if effective_cfg is None:
            host_label = self.resolve_host_identifier() or self.get_effective_host()
            if host_label:
                try:
                    from ..core.ssh_config_effective import get_effective_ssh_config

                    config_override = self._resolve_config_override_path()
                    if config_override:
                        effective_cfg = get_effective_ssh_config(
                            host_label, config_file=config_override
                        )
                    else:
                        effective_cfg = get_effective_ssh_config(host_label)
                except Exception:
                    effective_cfg = {}
            else:
                effective_cfg = {}

        cfg = effective_cfg or {}
        cfg_ids = cfg.get("identityfile")
        if isinstance(cfg_ids, list):
            for value in cfg_ids:
                _add(str(value))
        elif isinstance(cfg_ids, str):
            _add(cfg_ids)
        _add(keyfile_value)
        return candidates

    # -- best-effort preload (mirrors classic VTE; never raises) ------------

    def _preload_keys_into_agent(
        self,
        app_config=None,
        credential_lookup=None,
    ) -> None:
        """Best-effort keyring-gated agent preload; never aborts launch.

        Restored daemon-owned policy: only keys whose passphrase is already
        stored are force-added to ssh-agent, and only when the app setting
        enables preloading and the host's IdentityAgent policy allows it.
        Mirrors the retired frontend ``ConnectionManager`` gates.
        """
        try:
            # Password auth never preloads keys.
            if self.auth_method == 1:
                return
            if self.identity_agent_disabled:
                return
            if _string(self.identity_agent_directive):
                return

            preload_enabled = True
            lifetime = 0
            if app_config is not None:
                try:
                    getter = getattr(app_config, "get_setting", None)
                    if callable(getter):
                        preload_enabled = bool(
                            getter("ssh.agent_preload_keys", True)
                        )
                        lifetime = getter("ssh.agent_preload_lifetime", 0)
                except Exception:
                    pass
            if not preload_enabled:
                return
            try:
                lifetime = int(lifetime)
            except (TypeError, ValueError):
                lifetime = 0
            if lifetime < 0:
                lifetime = 0

            candidates = list(self.resolved_identity_files or [])
            if not candidates:
                candidates = list(self.collect_identity_file_candidates() or [])
                # Share the candidate set with the auth resolver / preload path.
                self.resolved_identity_files = candidates
            if not candidates:
                return

            from ..askpass_utils import ensure_key_in_agent

            for key_path in candidates:
                try:
                    passphrase = None
                    if credential_lookup is not None:
                        getter = getattr(
                            credential_lookup, "get_key_passphrase", None
                        )
                        if callable(getter):
                            passphrase = getter(key_path)
                        if not passphrase:
                            alt = getattr(
                                credential_lookup, "lookup_key_passphrase", None
                            )
                            if callable(alt):
                                passphrase = alt(key_path)
                    if not passphrase:
                        # No stored passphrase -> never silently add the key.
                        continue
                    preparer = (
                        getattr(
                            credential_lookup, "prepare_key_for_connection", None
                        )
                        if credential_lookup is not None
                        else None
                    )
                    if callable(preparer):
                        preparer(key_path, force=True, lifetime=lifetime)
                    else:
                        ensure_key_in_agent(key_path, force=True, lifetime=lifetime)
                except Exception:
                    continue
        except Exception:
            return


class DaemonConnectionLaunchProvider:
    """Launch contract implementation for the daemon process owner."""

    def __init__(
        self,
        resolver: Callable[[ConnectionId], Optional[ConnectionRecord]],
        *,
        secret_provider: Any = None,
        app_config: Any = None,
        headless_settings: Any = None,
        identity_env: Optional[Callable[[Dict[str, str]], Dict[str, str]]] = None,
    ) -> None:
        if resolver is None:
            raise ValueError("a connection resolver is required")
        self._resolver = resolver
        self._secret_provider = secret_provider
        self._app_config_view = app_config
        self._headless_settings = headless_settings
        # Daemon-owned identity seam: applies the selected provider's agent
        # environment (effective ``SSH_AUTH_SOCK``) to every spawned OpenSSH
        # command.  Optional so legacy tests/compositions keep working.
        self._identity_env = identity_env

    def _apply_identity_env(self, environment: Dict[str, str]) -> Dict[str, str]:
        if self._identity_env is None:
            return environment
        try:
            return self._identity_env(environment)
        except Exception:
            logger.debug("identity agent environment unavailable", exc_info=True)
            return environment

    def _resolve(self, connection_id: ConnectionId) -> ConnectionRecord:
        record = self._resolver(connection_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        return record

    def _manager_shim(self, connection: HeadlessConnectionView) -> Any:
        """Daemon credential seam consumed by the shared launch builder.

        Only the secret-lookup and passphrase methods are required by the
        native auth resolution; everything else is optional via ``getattr``.
        It is created inside the daemon and never exposes a frontend manager.
        """
        secret_provider = self._secret_provider

        class _ManagerShim:
            secret_lookup_authoritative = True

            def get_connection_password(self, _conn):
                if secret_provider is None:
                    return None
                getter = getattr(secret_provider, "lookup_connection_password", None)
                if callable(getter):
                    try:
                        return getter(connection.id)
                    except Exception:
                        return None
                return None

            def get_key_passphrase(self, key_path):
                if secret_provider is None:
                    return None
                getter = getattr(secret_provider, "lookup_key_passphrase", None)
                if callable(getter):
                    try:
                        return getter(key_path)
                    except Exception:
                        return None
                return None

            def prepare_key_for_connection(self, key_path, *, force=True, lifetime=0):
                try:
                    from ..askpass_utils import ensure_key_in_agent

                    return bool(
                        ensure_key_in_agent(key_path, force=force, lifetime=lifetime)
                    )
                except Exception:
                    return False

        return _ManagerShim()

    def _get_app_config(self):
        if self._app_config_view is not None:
            return self._app_config_view
        if self._headless_settings is not None:
            return self._headless_settings
        return None

    def _prepare_ssh_launch(
        self,
        connection: HeadlessConnectionView,
        *,
        interaction_policy: str,
        command_type: str,
        extra_args: Optional[List[str]] = None,
        remote_command: Optional[str] = None,
        target_override: Optional[str] = None,
        force_tty: bool = False,
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        from ..ssh_connection_builder import ConnectionContext, build_ssh_connection

        app_config = self._get_app_config()
        connection.resolved_identity_files = (
            connection.collect_identity_file_candidates()
        )
        # One shared credential surface feeds both the builder's auth resolution
        # and the post-build preload so they see the same passphrases/preparer.
        manager = self._manager_shim(connection)
        ctx = ConnectionContext(
            connection=connection,
            connection_manager=manager,
            config=app_config,
            command_type=command_type,
            extra_args=extra_args,
            remote_command=remote_command,
            native_mode=True,
            interaction_policy=interaction_policy,
            target_override=target_override,
            force_tty=force_tty,
        )
        prepared = build_ssh_connection(ctx)
        connection._preload_keys_into_agent(app_config, credential_lookup=manager)
        argv = tuple(prepared.command or ())
        environment = dict(prepared.env or {})
        if not argv:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH session could not be prepared",
                connection_id=connection.id,
            )
        askpass_active = bool(
            getattr(prepared, "use_askpass", False)
            or environment.get("SSH_ASKPASS")
        )
        for key in tuple(environment):
            if key.startswith("SSHPILOT_ASKPASS") or key.startswith("SSHPILOT_SESSION_"):
                environment.pop(key, None)
        environment.pop("SSH_ASKPASS", None)
        environment.pop("SSH_ASKPASS_REQUIRE", None)
        if askpass_active:
            environment["SSHPILOT_DAEMON_ASKPASS_ACTIVE"] = "1"
        environment = self._apply_identity_env(environment)
        local_command = _string(connection.local_command)
        if local_command and len(argv) >= 2:
            argv = (
                *argv[:-1],
                "-o",
                "PermitLocalCommand=yes",
                "-o",
                f"LocalCommand={local_command}",
                argv[-1],
            )
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The OpenSSH executable is unavailable",
                connection_id=connection.id,
            )
        return (executable, *argv[1:]), environment

    def prepare_terminal_launch(
        self,
        connection_id: ConnectionId,
        *,
        interaction_policy: str = "none",
        remote_command: Optional[str] = None,
        force_tty: bool = False,
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        record = self._resolve(connection_id)
        connection = HeadlessConnectionView(record)
        protocol = connection.protocol
        if protocol != "ssh":
            return self._prepare_protocol_launch(
                connection, protocol, interaction_policy=interaction_policy
            )
        argv, environment = self._prepare_ssh_launch(
            connection,
            interaction_policy=interaction_policy,
            command_type="ssh",
            remote_command=remote_command,
            force_tty=force_tty,
        )
        # The daemon PTY is the semantic boundary for interactive SSH
        # terminals.  Authentication helpers intentionally preserve the
        # caller environment, but a missing or ``dumb`` TERM is not usable for
        # the interactive OpenSSH child.  Keep valid terminal types intact.
        term = environment.get("TERM")
        if not term or term.lower() == "dumb":
            environment["TERM"] = "xterm-256color"
        return argv, environment

    def prepare_scp_launch(
        self,
        connection_id: ConnectionId,
        *,
        extra_args: Optional[List[str]] = None,
        interaction_policy: str = "broker",
        target_override: Optional[str] = None,
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        record = self._resolve(connection_id)
        connection = HeadlessConnectionView(record)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                "SCP transfers require an SSH connection",
                connection_id=connection_id,
            )
        return self._prepare_ssh_launch(
            connection,
            interaction_policy=interaction_policy,
            command_type="scp",
            extra_args=extra_args,
            target_override=target_override,
        )

    def scp_target(self, connection_id: ConnectionId) -> str:
        record = self._resolve(connection_id)
        connection = HeadlessConnectionView(record)
        host = connection.resolve_host_identifier() if hasattr(
            connection, "resolve_host_identifier"
        ) else connection.host
        host = str(host or connection.hostname or "").strip()
        if not host:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The connection has no SSH host identifier",
                connection_id=connection_id,
            )
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            host = f"[{host}]"
        return f"{connection.username}@{host}" if connection.username else host

    def prepare_sftp_launch(
        self,
        connection_id: ConnectionId,
        *,
        interaction_policy: str = "broker",
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        record = self._resolve(connection_id)
        connection = HeadlessConnectionView(record)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP session could not be prepared",
                connection_id=connection_id,
            )
        return self._prepare_ssh_launch(
            connection,
            interaction_policy=interaction_policy,
            command_type="sftp",
            extra_args=["-s"],
        )

    def prepare_forward_launch(
        self,
        connection_id: ConnectionId,
        *,
        forward_type: str = "local",
        bind_host: str = "localhost",
        bind_port: int = 0,
        destination_host: Optional[str] = None,
        destination_port: Optional[int] = None,
        interaction_policy: str = "broker",
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        record = self._resolve(connection_id)
        connection = HeadlessConnectionView(record)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The forward could not be prepared",
                connection_id=connection_id,
            )
        if forward_type == "local":
            forward_flag, rule = "-L", f"{bind_host}:{bind_port}:{destination_host}:{destination_port}"
        elif forward_type == "remote":
            forward_flag, rule = "-R", f"{bind_host}:{bind_port}:{destination_host}:{destination_port}"
        elif forward_type == "dynamic":
            forward_flag, rule = "-D", f"{bind_host}:{bind_port}" if bind_host else str(bind_port)
        else:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "The requested forward type is not supported",
                connection_id=connection_id,
            )
        return self._prepare_ssh_launch(
            connection,
            interaction_policy=interaction_policy,
            command_type="ssh",
            extra_args=[
                "-N",
                "-T",
                forward_flag,
                rule,
                "-o",
                "ExitOnForwardFailure=yes",
            ],
        )

    def prepare_remote_command_launch(
        self,
        connection_id: ConnectionId,
        remote_command: str,
        *,
        interaction_policy: str = "broker",
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        """Canonical ``ssh <alias> <remote_command>`` for daemon remote reads/writes.

        Authorized-key management runs ordinary remote shell commands through
        the same native launch path as every other OpenSSH child: the saved
        Host alias stays the target so the user's SSH configuration (ProxyJump,
        identities, ports) applies unchanged.
        """
        if not isinstance(remote_command, str) or not remote_command.strip():
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "The remote command is invalid",
                connection_id=connection_id,
            )
        record = self._resolve(connection_id)
        connection = HeadlessConnectionView(record)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                "Remote commands require an SSH connection",
                connection_id=connection_id,
            )
        return self._prepare_ssh_launch(
            connection,
            interaction_policy=interaction_policy,
            command_type="ssh",
            extra_args=["-T"],
            remote_command=remote_command,
        )

    def prepare_copy_id_launch(
        self,
        connection_id: ConnectionId,
        public_key_path: str,
        *,
        force: bool = False,
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        """Native ``ssh-copy-id`` argv/env for daemon key deployment.

        Mirrors the retired frontend runner exactly: the shared base-command
        builder composes app overrides, port and ``ClearAllForwardings`` (and
        deliberately skips flags ``ssh-copy-id`` cannot take), then the public
        key, known-hosts path and the connection's auth preferences are added.
        OpenSSH — not SSH Pilot — performs the remote installation.
        """
        if (
            not isinstance(public_key_path, str)
            or not public_key_path
            or "\x00" in public_key_path
        ):
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "The public key path is invalid",
                connection_id=connection_id,
            )
        record = self._resolve(connection_id)
        connection = HeadlessConnectionView(record)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                "Public-key deployment requires an SSH connection",
                connection_id=connection_id,
            )

        host_value = connection.hostname or connection.host
        if not host_value:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The connection has no host or SSH alias",
                connection_id=connection_id,
            )
        config_file = connection._resolve_config_override_path()
        app_cfg = self._get_app_config()
        from ..core.ssh_config_effective import get_effective_ssh_config
        from ..ssh_connection_builder import (
            _build_base_ssh_command,
            resolve_native_auth,
        )

        try:
            effective_config = get_effective_ssh_config(
                host_value, config_file=config_file
            )
        except Exception:
            effective_config = {}
        argv = _build_base_ssh_command(
            connection,
            effective_config,
            app_cfg,
            "ssh-copy-id",
            config_file=config_file,
        )
        if force:
            argv.append("-f")
        argv.extend(["-i", public_key_path])

        # Which known_hosts file this is depends on the operation mode, not on
        # where the declaring file happens to sit: -F is given the file that
        # declared the Host, so for anything pulled in through an Include that
        # directory is a fragment's, not the root's.
        known_hosts_path = None
        try:
            from sshpilot.platform.paths import known_hosts_path_for

            known_hosts_path = str(
                known_hosts_path_for(bool(connection.data.get("isolated_mode")))
            )
        except Exception:
            known_hosts_path = None
        if known_hosts_path:
            argv += ["-o", f"UserKnownHostsFile={known_hosts_path}"]

        # Authentication preferences mirror the frontend runner: password
        # method narrows PreferredAuthentications; a stored password alongside
        # key auth keeps the combined method list.
        manager = self._manager_shim(connection)
        auth_method = connection.auth_method
        prefer_password = auth_method == 1
        stored_password = None
        try:
            stored_password = manager.get_connection_password(connection)
        except Exception:
            stored_password = None
        combined_auth = bool(stored_password) and not prefer_password
        if prefer_password:
            argv += ["-o", "PreferredAuthentications=keyboard-interactive,password"]
            if bool(connection.data.get("pubkey_auth_no")):
                argv += ["-o", "PubkeyAuthentication=no"]
        elif combined_auth:
            argv += [
                "-o",
                "PreferredAuthentications=gssapi-with-mic,hostbased,"
                "publickey,keyboard-interactive,password",
            ]

        target = (
            f"{connection.username}@{host_value}"
            if connection.username
            else host_value
        )
        argv.append(target)

        # Sanitized daemon base environment (same allowlist the session path
        # uses), then the daemon-owned identity agent environment.
        auth = resolve_native_auth(
            connection, manager, app_cfg, interaction_policy="broker"
        )
        environment = dict(auth.env or {})
        _ensure_writable_ssh_home(environment)
        if os.path.exists("/app/bin"):
            current_path = environment.get("PATH", "")
            if "/app/bin" not in current_path:
                environment["PATH"] = f"/app/bin:{current_path}"
        environment = self._apply_identity_env(environment)

        executable = _resolve_ssh_copy_id(environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "ssh-copy-id is not installed",
                connection_id=connection_id,
            )
        argv[0] = executable
        return tuple(argv), environment

    def _prepare_protocol_launch(
        self,
        connection: HeadlessConnectionView,
        protocol: str,
        *,
        interaction_policy: str,
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        from ..plugins.api import PluginContext, ProtocolError
        from ..plugins.builtin._session_failure import BuiltinProtocolError
        from ..plugins.loader import ensure_builtin_protocols, ensure_user_protocols
        from ..plugins.registry import protocol_registry

        # Built-ins are loaded by the GTK process at startup; the daemon is a
        # separate process and must register them before non-SSH launches.
        app_config = self._get_app_config()
        ensure_builtin_protocols(app_config=app_config)
        registry = protocol_registry()
        backend = registry.get_or_none(protocol)
        if backend is None:
            # Not a built-in, so an enabled user plugin may own it. Loading
            # them is deferred to here so the common SSH path never imports
            # third-party code.
            ensure_user_protocols(app_config=app_config, protocol=protocol)
            backend = registry.get_or_none(protocol)
        if backend is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                f"No terminal launch provider is registered for {protocol!r}",
                connection_id=connection.id,
            )
        plugin_id = registry.plugin_id_for(protocol) or protocol
        ctx = PluginContext.for_spawn(
            plugin_id=plugin_id,
            app_config=self._get_app_config(),
            connection_manager=self._manager_shim(connection),
            protocol_registry=registry,
        )
        try:
            spawn = backend.build_spawn(connection, ctx)
        except BuiltinProtocolError as exc:
            raise _PluginSessionLaunchError(
                exc.failure,
                connection_id=connection.id,
            ) from exc
        except ProtocolError as exc:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                str(exc),
                connection_id=connection.id,
            ) from exc
        argv = tuple(spawn.argv or ())
        environment = dict(spawn.env or {})
        if not argv:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                f"The {protocol} launch provider returned an empty command",
                connection_id=connection.id,
            )
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                f"The executable required by the {protocol} provider is unavailable",
                connection_id=connection.id,
            )
        return (executable, *argv[1:]), environment


def _resolve_ssh_copy_id(path: Optional[str]) -> Optional[str]:
    """Resolve the native ``ssh-copy-id`` executable (Flatpak-aware)."""
    flatpak_path = "/app/bin/ssh-copy-id"
    if os.path.exists(flatpak_path) and os.access(flatpak_path, os.X_OK):
        return flatpak_path
    return shutil.which("ssh-copy-id", path=path)


def _ensure_writable_ssh_home(env: Dict[str, str]) -> None:
    """Give ssh-copy-id a writable HOME inside the Flatpak sandbox.

    Mirrors ``ssh_utils.ensure_writable_ssh_home`` without importing the
    GI-coupled platform adapter (forbidden in the daemon).
    """
    if not os.path.exists("/.flatpak-info"):
        return
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    alt_home = os.path.join(runtime_dir, "sshcopyid-home")
    try:
        os.makedirs(os.path.join(alt_home, ".ssh"), exist_ok=True)
    except OSError:
        return
    env["HOME"] = alt_home
