"""
Connection Manager for sshPilot
Handles SSH connections, configuration, and secure password storage
"""

import os
import stat
import shutil
import tempfile
import asyncio
import enum
import logging
import getpass
import subprocess
import shlex
import re
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union, Set

from .ssh_config_utils import resolve_ssh_config_files, get_effective_ssh_config, expand_ssh_tokens
from .platform_utils import get_config_dir, get_ssh_dir
from .key_utils import _is_private_key
from .ssh_connection_builder import build_ssh_connection, ConnectionContext

from gi.repository import GObject, GLib
from .askpass_utils import (
    clear_passphrase,
    lookup_passphrase,
    store_passphrase,
)

# Set up asyncio event loop for GTK integration
if os.name == 'posix':
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import GLib
    
    # Set up the asyncio event loop
    if not hasattr(GLib, 'MainLoop'):
        import asyncio
        import asyncio.events
        import asyncio.base_events
        import asyncio.unix_events

        # ``BaseDefaultEventLoopPolicy`` and the event loop policy machinery were
        # removed in Python 3.14. Only install the custom policy on interpreters
        # that still provide it; on newer versions ``_ensure_event_loop`` takes
        # care of provisioning a loop instead.
        _base_event_loop_policy = getattr(asyncio.events, 'BaseDefaultEventLoopPolicy', None)
        if _base_event_loop_policy is not None:
            class GLibEventLoopPolicy(_base_event_loop_policy):
                _loop_factory = asyncio.SelectorEventLoop

                def new_event_loop(self):
                    return asyncio.unix_events.DefaultEventLoopPolicy.new_event_loop(self)

            asyncio.set_event_loop_policy(GLibEventLoopPolicy())

logger = logging.getLogger(__name__)
_SERVICE_NAME = "sshPilot"


def _ensure_event_loop() -> asyncio.AbstractEventLoop:
    """Return the running asyncio event loop or create one if missing.

    Python 3.13+ no longer creates a default event loop implicitly, so we need
    to provision one ourselves when the GTK application starts up. This helper
    keeps the existing loop when present and falls back to creating and
    registering a new loop on the current thread.
    """

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass

    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
        except Exception:
            logger.debug("Failed to register newly created asyncio loop", exc_info=True)
        return loop


# Per ssh_config(5): "Configuration options may be separated by whitespace or
# optional whitespace and exactly one '='". A keyword and its argument may be
# separated by any run of whitespace (spaces or tabs) or by a single '=' with
# optional surrounding whitespace.
_CONFIG_OPTION_RE = re.compile(r'^(\S+?)(?:\s*=\s*|\s+)(.*)$')


def _split_config_option(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a config line into (key, value) honouring whitespace and '=' separators.

    Returns ``(None, None)`` for lines that carry no value (a bare keyword), which
    the caller skips just as the old ``' ' in line`` guard did.
    """
    match = _CONFIG_OPTION_RE.match(line)
    if not match:
        return None, None
    key, value = match.group(1), match.group(2).strip()
    if not value:
        return None, None
    return key, value


def _split_keyword(line: str) -> Tuple[str, str]:
    """Return ``(lowercased keyword, remainder)`` for a config line.

    Honours every ssh_config(5) separator, so ``Host x``, ``Host=x``,
    ``Host = x`` and tab-separated forms all yield ``('host', 'x')``. A bare
    keyword with no argument yields ``('host', '')``. Used to dispatch the
    Host/Match/Include block keywords regardless of separator.
    """
    match = _CONFIG_OPTION_RE.match(line)
    if match:
        return match.group(1).lower(), match.group(2).strip()
    return line.strip().lower(), ''


def _safe_int(value: Any, default: int) -> int:
    """Best-effort int conversion that falls back to *default* instead of raising."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class ConnectionState(enum.Enum):
    """Authoritative lifecycle state of a saved connection.

    This is the single source of truth consumed by the sidebar indicator and
    (in future) any health/dashboard feature. The legacy boolean
    ``Connection.is_connected`` is derived from this (CONNECTED ⇔ True) and kept
    for backward compatibility with existing call sites.
    """

    UNKNOWN = 'unknown'        # never connected this session
    CONNECTING = 'connecting'  # ssh process started, login not yet confirmed
    CONNECTED = 'connected'    # session is live
    DISCONNECTED = 'disconnected'  # cleanly down / no active terminal
    FAILED = 'failed'          # last attempt failed (auth, unreachable, lost)


class Connection:
    """Represents an SSH connection"""

    def __init__(self, data: Dict[str, Any]):
        self.data = data
        # Authoritative status. ``is_connected`` (below) is a derived compat
        # property; set state via set_status()/the is_connected setter.
        self._status = ConnectionState.UNKNOWN
        self._status_reason = ''
        self.connection = None
        self.forwarders: List[asyncio.Task] = []
        self.listeners: List[asyncio.Server] = []

        unparsed = data.get('unparsed_args', []) if isinstance(data, dict) else []
        if isinstance(unparsed, (list, tuple)):
            self.unparsed_args = list(unparsed)
        else:
            self.unparsed_args = []

        hostname_value = data.get('hostname')
        host_value = data.get('host', '')

        nickname_value = data.get('nickname')
        if nickname_value:
            self.nickname = nickname_value
        else:
            fallback_nickname = hostname_value if hostname_value else host_value
            self.nickname = fallback_nickname or 'Unknown'

        if 'aliases' in data:
            self.aliases = data.get('aliases', [])
        self.hostname = hostname_value or ''
        host_alias = data.get('host')
        if host_alias is None:
            host_alias = self.nickname
        self.host = host_alias or ''


        self.username = data.get('username', '')
        self.port = data.get('port', 22)
        # Protocol backend handling this connection ('ssh' for every existing
        # saved/ssh_config-derived connection; see sshpilot.plugins).
        self.protocol = data.get('protocol', 'ssh')
        # previously: self.keyfile = data.get('keyfile', '')
        self.keyfile = data.get('keyfile') or data.get('private_key', '') or ''
        # Full list of IdentityFile/CertificateFile entries (ssh_config(5) allows
        # multiples; ``keyfile``/``certificate`` above are just the primary entry).
        self.identity_files = list(data.get('identity_files') or ([self.keyfile] if self.keyfile else []))
        self.identity_file_none = bool(data.get('identity_file_none', False))
        self.certificate = data.get('certificate') or ''
        self.certificate_files = list(data.get('certificate_files') or ([self.certificate] if self.certificate else []))
        # Agent / hardware key sources (verbatim ssh_config values)
        self.identity_agent = data.get('identity_agent', '') or ''
        self.add_keys_to_agent = data.get('add_keys_to_agent', '') or ''
        self.pkcs11_provider = data.get('pkcs11_provider', '') or ''
        self.security_key_provider = data.get('security_key_provider', '') or ''
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        # Source file of this configuration block
        self.source = data.get('source', '')
        self.config_root = data.get('config_root', '')
        self.isolated_config = bool(data.get('isolated_mode', False))

        # Cache of identity files resolved for this connection (expanded paths)
        self.resolved_identity_files: List[str] = []

        # Provide friendly accessor for UI components that wish to display
        # the originating config file for this connection.
        
        # Proxy settings
        self.proxy_command = data.get('proxy_command', '')
        pj = data.get('proxy_jump', [])
        if isinstance(pj, str):
            pj = [h.strip() for h in re.split(r'[\s,]+', pj) if h.strip()]
        self.proxy_jump = pj
        self.forward_agent = bool(data.get('forward_agent', False))
        # Commands
        self.pre_command = data.get('pre_command', '')
        self.local_command = data.get('local_command', '')
        self.remote_command = data.get('remote_command', '')
        # Extra SSH config parameters
        self.extra_ssh_config = data.get('extra_ssh_config', '')
        self.pubkey_auth_no = bool(data.get('pubkey_auth_no', False))
        # Authentication method: 0 = key-based, 1 = password
        try:
            self.auth_method = int(data.get('auth_method', 0))
        except Exception:
            self.auth_method = 0
        # X11 forwarding preference
        self.x11_forwarding = bool(data.get('x11_forwarding', False))

        # Track IdentityAgent directives so terminals can adjust askpass behaviour
        self.identity_agent_directive: str = ''
        self.identity_agent_disabled: bool = False
        
        # Key selection mode: 0 try all, 1 specific key (IdentitiesOnly), 2 specific key (no IdentitiesOnly)
        try:
            self.key_select_mode = int(data.get('key_select_mode', 0) or 0)
        except Exception:
            self.key_select_mode = 0

        # Port forwarding rules
        self.forwarding_rules = data.get('forwarding_rules', [])
        
        # Asyncio event loop
        self.loop = _ensure_event_loop()
        self._connection_manager = None

    def __str__(self):
        return f"{self.nickname} ({self.username}@{self.hostname})"

    def get_effective_host(self) -> str:
        """Return the hostname used for operations, falling back to aliases."""

        if self.hostname:
            return self.hostname
        if getattr(self, 'host', ''):
            return self.host
        return self.nickname

    def resolve_host_identifier(self) -> str:
        """Return the preferred host alias used for launching native SSH commands."""

        candidates: List[str] = []
        data = self.data if isinstance(self.data, dict) else {}

        if isinstance(data, dict):
            tokens = data.get('__host_tokens')
            if isinstance(tokens, (list, tuple)):
                for token in tokens:
                    if not token:
                        continue
                    token = str(token).strip()
                    if not token or token.startswith('!'):
                        continue
                    if any(ch in token for ch in ('*', '?')):
                        continue
                    candidates.append(token)
            for key in ('host', 'nickname', 'hostname'):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())

        for attr in ('host', 'nickname', 'hostname'):
            value = getattr(self, attr, '')
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

        try:
            effective = self.get_effective_host()
            if effective:
                candidates.append(str(effective))
        except Exception:
            pass

        for candidate in candidates:
            if candidate:
                return candidate
        return ''

    def _resolve_config_override_path(self) -> Optional[str]:
        """Return an absolute path to the SSH config override, if any."""

        config_override: Optional[str] = None

        source_path = str(getattr(self, 'source', '') or '')
        if source_path:
            expanded_source = os.path.abspath(
                os.path.expanduser(os.path.expandvars(source_path))
            )
            if os.path.exists(expanded_source):
                config_override = expanded_source

        if not config_override and getattr(self, 'isolated_mode', False):
            isolated_candidate = os.path.join(get_config_dir(), 'ssh_config')
            expanded_isolated = os.path.abspath(
                os.path.expanduser(os.path.expandvars(isolated_candidate))
            )
            if os.path.exists(expanded_isolated):
                config_override = expanded_isolated

        if self.isolated_config and self.config_root:
            config_override = self.config_root

        if config_override:
            return os.path.abspath(
                os.path.expanduser(os.path.expandvars(config_override))
            )
        return None

    def collect_identity_file_candidates(
        self,
        effective_cfg: Optional[Dict[str, Union[str, List[str]]]] = None,
    ) -> List[str]:
        """Return resolved identity file paths that exist on disk for this host."""

        # Reset IdentityAgent state before evaluating configuration
        self._update_identity_agent_state(None)

        candidates: List[str] = []
        seen: Set[str] = set()

        try:
            key_mode = int(getattr(self, 'key_select_mode', 0) or 0)
        except Exception:
            key_mode = 0

        keyfile_raw = getattr(self, 'keyfile', '') or ''
        keyfile_value = str(keyfile_raw).strip()
        if keyfile_value.lower().startswith('select key file'):
            keyfile_value = ''

        def _add_candidate(path: str):
            if not path:
                return
            expanded = os.path.expanduser(path)
            if os.path.isfile(expanded) and expanded not in seen:
                candidates.append(expanded)
                seen.add(expanded)

        if key_mode in (1, 2):
            if effective_cfg is not None:
                self._update_identity_agent_state(
                    effective_cfg.get('identityagent')  # type: ignore[arg-type]
                )
            _add_candidate(keyfile_value)
            return candidates

        if effective_cfg is None:
            host_label = ''
            try:
                host_label = self.resolve_host_identifier()
            except Exception:
                host_label = ''
            if not host_label:
                try:
                    host_label = self.get_effective_host()
                except Exception:
                    host_label = ''

            if host_label:
                config_override = None
                try:
                    config_override = self._resolve_config_override_path()
                except Exception:
                    config_override = None

                try:
                    if config_override:
                        effective_cfg = get_effective_ssh_config(host_label, config_file=config_override)
                    else:
                        effective_cfg = get_effective_ssh_config(host_label)
                except Exception:
                    effective_cfg = {}
            else:
                effective_cfg = {}

        cfg = effective_cfg or {}
        self._update_identity_agent_state(cfg.get('identityagent'))
        cfg_ids = cfg.get('identityfile') if isinstance(cfg, dict) else None
        if isinstance(cfg_ids, list):
            for value in cfg_ids:
                _add_candidate(value)
        elif isinstance(cfg_ids, str):
            _add_candidate(cfg_ids)

        _add_candidate(keyfile_value)

        return candidates

    def _update_identity_agent_state(self, directive: Optional[Union[str, List[str]]]) -> None:
        """Update cached IdentityAgent directive information for the connection."""

        directive_value = ''
        disabled = False

        if isinstance(directive, list):
            values = [
                str(entry).strip()
                for entry in directive
                if isinstance(entry, str) and str(entry).strip()
            ]
        elif isinstance(directive, str):
            stripped = directive.strip()
            values = [stripped] if stripped else []
        else:
            values = []

        if values:
            directive_value = values[-1]
            disabled = directive_value.lower() == 'none'

        self.identity_agent_directive = directive_value
        self.identity_agent_disabled = disabled
        if disabled:
            logger.debug(
                "IdentityAgent directive disables ssh-agent; forcing askpass for this connection"
            )

    @property
    def source_file(self) -> str:
        """Return path to the config file where this host is defined."""
        return self.source
        
    async def connect(self):
        """Prepare an SSH connection.

        sshPilot connects in native mode everywhere (~/.ssh/config is the source
        of truth), so this legacy entry point now delegates to native_connect().
        Kept so existing callers keep working; there is no separate non-native
        connection path anymore.
        """
        return await self.native_connect()

    async def native_connect(self, remote_command: Optional[str] = None,
                             force_tty: bool = False):
        """Prepare a minimal SSH command using ssh_connection_builder in native mode.

        ``remote_command``, when given, is appended to the ssh invocation on the
        CLI (a one-off command to run on the host) instead of opening an
        interactive login shell — used by ``ctx.open_command_terminal``. It is
        not persisted to ``~/.ssh/config``.

        ``force_tty`` adds ``-t`` so ssh allocates a remote PTY even though a
        command is given (ssh only auto-allocates one for interactive sessions).
        Required for interactive remote programs like ``docker exec -it``."""
        try:
            self._update_identity_agent_state(None)
            # Reset resolved identity cache when preparing native command
            self.resolved_identity_files = []

            # Get config for ssh_connection_builder
            try:
                from .config import Config  # avoid circular import at top level
                cfg = Config()
            except Exception:
                cfg = None

            connection_manager = getattr(self, '_connection_manager', None)
            known_hosts_path = None
            if connection_manager:
                kh_path = getattr(connection_manager, 'known_hosts_path', '') or ''
                if kh_path and os.path.exists(kh_path):
                    known_hosts_path = kh_path

            # Build connection context with native_mode=True
            ctx = ConnectionContext(
                connection=self,
                connection_manager=connection_manager,
                config=cfg,
                command_type='ssh',
                extra_args=(['-t'] if force_tty else []),
                port_forwarding_rules=None,
                remote_command=remote_command,
                local_command=None,
                extra_ssh_config=None,
                known_hosts_path=known_hosts_path,
                native_mode=True,  # Use native mode
            )

            # Resolve identity files FIRST so resolve_native_auth (inside
            # build_ssh_connection) can decide auth from what's saved for them
            # (saved passphrase -> askpass; else saved password -> PTY fill; else
            # native prompts) without recomputing `ssh -G`.
            try:
                self.resolved_identity_files = self.collect_identity_file_candidates()
            except Exception:
                self.resolved_identity_files = []

            # Build SSH connection command using ssh_connection_builder
            ssh_conn_cmd = build_ssh_connection(ctx)
            ssh_cmd = ssh_conn_cmd.command

            self.ssh_cmd = ssh_cmd
            # Store the full builder result (command + env + auth flags) so the
            # terminal can spawn it directly without re-deriving auth/env.
            self.ssh_env = dict(ssh_conn_cmd.env)
            self.ssh_connection_cmd = ssh_conn_cmd
            # NOTE: key preload into ssh-agent is NOT done here. native_connect
            # runs under loop.run_until_complete on the GLib main thread, which
            # blocks the main loop — so our in-process askpass dialog could not
            # render for a not-stored passphrase. The terminal calls
            # _preload_keys_into_agent() from its worker thread instead (where the
            # GLib loop is free), so the prompt works. See terminal.py.
            self.is_connected = True
            return True
        except Exception as exc:
            logger.error(f"Failed to prepare native SSH command for {self}: {exc}")
            self.is_connected = False
            return False

    def _preload_keys_into_agent(self, app_config=None) -> None:
        """Best-effort: load this host's on-disk key(s) into ssh-agent — but ONLY
        keys whose passphrase the user has stored in the keyring. A stored
        passphrase is the user's opt-in for silent agent auth; we then ``ssh-add``
        the key (askpass autofills the passphrase) so a gnome-keyring-locked key
        gets unlocked and can sign (the agent is never disabled).

        Keys with NO stored passphrase are left untouched — we do NOT ``ssh-add``
        them. That signals the user prefers SSH / the OS / ssh-agent to prompt
        naturally, and avoids adding/unlocking a key they didn't ask us to.

        MUST be called from a thread where the GLib main loop is free (e.g. the
        terminal's connect worker thread). Never raises.
        """
        try:
            from .askpass_utils import ensure_key_in_agent, lookup_passphrase

            cfg = app_config
            if cfg is None:
                try:
                    from .config import Config
                    cfg = Config()
                except Exception:
                    cfg = None

            preload = True
            lifetime = 0
            if cfg is not None and hasattr(cfg, 'get_setting'):
                try:
                    preload = bool(cfg.get_setting('ssh.agent_preload_keys', True))
                    lifetime = int(cfg.get_setting('ssh.agent_preload_lifetime', 0) or 0)
                except Exception:
                    preload, lifetime = True, 0
            if not preload:
                return

            # Key-based auth only.
            if int(getattr(self, 'auth_method', 0) or 0) != 0:
                return

            # Respect a user-pinned agent (IdentityAgent none / custom socket):
            # never disturb the agent they chose.
            if getattr(self, 'identity_agent_disabled', False) or \
                    (getattr(self, 'identity_agent_directive', '') or '').strip():
                return

            # Use the cached identities when present, else fall back to the same
            # discovery the auth resolver uses (collect_identity_file_candidates),
            # so a fresh, non-terminal caller still preloads the keys the resolver
            # based its combined-auth decision on.
            candidates = getattr(self, 'resolved_identity_files', None)
            if not candidates and hasattr(self, 'collect_identity_file_candidates'):
                try:
                    candidates = self.collect_identity_file_candidates()
                except Exception:
                    candidates = None

            for path in (candidates or []):
                try:
                    # Keyring-only: skip keys with no stored passphrase entirely
                    # (no ssh-add) → user gets the natural OS/agent prompt.
                    if not lookup_passphrase(path):
                        continue
                    ensure_key_in_agent(path, force=True, lifetime=lifetime)
                    logger.debug("Preloaded key into ssh-agent: %s", path)
                except Exception as exc:
                    logger.debug("Key preload failed for %s: %s", path, exc)
        except Exception:
            pass

    async def disconnect(self):
        """Close the SSH connection and clean up"""
        if not self.is_connected:
            return
            
        try:
            # Cancel all forwarding tasks
            for task in self.forwarders:
                if not task.done():
                    task.cancel()
            
            # Close all listeners
            for listener in self.listeners:
                listener.close()
            
            # Clean up any running processes
            if hasattr(self, 'process') and self.process:
                try:
                    # Try to terminate gracefully first
                    self.process.terminate()
                    try:
                        # Wait a bit for the process to terminate
                        await asyncio.wait_for(self.process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        # Force kill if it doesn't terminate
                        self.process.kill()
                        await self.process.wait()
                except ProcessLookupError:
                    # Process already terminated
                    pass
                except Exception as e:
                    logger.error(f"Error terminating SSH process: {e}")
                finally:
                    self.process = None
            
            logger.info(f"Disconnected from {self}")
            return True
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
            return False
        finally:
            # Always ensure is_connected is set to False
            self.is_connected = False
            self.listeners.clear()
        

    def update_data(self, new_data: Dict[str, Any]):
        """Update connection data while preserving object identity"""
        # When isolated mode is toggled off, the refreshed data no longer includes
        # the isolated_mode/config_root keys. Reusing the previous values would keep
        # the connection flagged as isolated and force an incorrect config override.
        if 'isolated_mode' not in new_data:
            if 'isolated_mode' in self.data:
                self.data.pop('isolated_mode', None)
            self.isolated_config = False

            if 'config_root' not in new_data:
                self.data.pop('config_root', None)
                self.config_root = ''

        self.data.update(new_data)
        self._update_properties_from_data(self.data)
    
    def _update_properties_from_data(self, data: Dict[str, Any]):
        """Update instance properties from data dictionary"""
        hostname_value = data.get('hostname')
        host_value = data.get('host', '')

        nickname_value = data.get('nickname')
        if nickname_value:
            self.nickname = nickname_value
        else:
            fallback_nickname = hostname_value if hostname_value else host_value
            self.nickname = fallback_nickname or getattr(self, 'nickname', 'Unknown')

        if 'aliases' in data:
            self.aliases = data.get('aliases', getattr(self, 'aliases', []))

        if hostname_value in (None, ''):
            resolved_host = host_value or getattr(self, 'host', '')
        else:
            resolved_host = hostname_value
        self.host = resolved_host

        if hostname_value is None:
            self.hostname = resolved_host
        elif hostname_value == '':
            self.hostname = ''
        else:
            self.hostname = hostname_value


        self.username = data.get('username', '')
        self.port = data.get('port', 22)
        self.protocol = data.get('protocol', getattr(self, 'protocol', 'ssh'))
        self.keyfile = data.get('keyfile') or data.get('private_key', '') or ''
        self.identity_files = list(data.get('identity_files') or ([self.keyfile] if self.keyfile else []))
        self.identity_file_none = bool(data.get('identity_file_none', False))


        self.certificate = data.get('certificate') or ''
        self.certificate_files = list(data.get('certificate_files') or ([self.certificate] if self.certificate else []))
        # Agent / hardware key sources (verbatim ssh_config values)
        self.identity_agent = data.get('identity_agent', '') or ''
        self.add_keys_to_agent = data.get('add_keys_to_agent', '') or ''
        self.pkcs11_provider = data.get('pkcs11_provider', '') or ''
        self.security_key_provider = data.get('security_key_provider', '') or ''
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        self.source = data.get('source', getattr(self, 'source', ''))
        self.config_root = data.get('config_root', '')
        self.isolated_config = bool(data.get('isolated_mode', False))
        self.local_command = data.get('local_command', '')
        self.remote_command = data.get('remote_command', '')
        self.proxy_command = data.get('proxy_command', '')
        pj = data.get('proxy_jump', [])
        if isinstance(pj, str):
            pj = [h.strip() for h in re.split(r'[\s,]+', pj) if h.strip()]
        self.proxy_jump = pj
        self.forward_agent = bool(data.get('forward_agent', False))
        # Extra SSH config parameters
        self.extra_ssh_config = data.get('extra_ssh_config', '')
        self.pubkey_auth_no = bool(data.get('pubkey_auth_no', False))

        # Authentication method: 0 = key-based, 1 = password
        # Preserve existing auth_method if not present in new data
        if 'auth_method' in data:
            try:
                self.auth_method = int(data.get('auth_method', 0))
            except Exception:
                self.auth_method = 0
            
        # X11 forwarding preference
        self.x11_forwarding = bool(data.get('x11_forwarding', False))
        
        # Key selection mode: 0 try all, 1 specific key
        try:
            self.key_select_mode = int(data.get('key_select_mode', 0) or 0)
        except Exception:
            self.key_select_mode = 0

        # Port forwarding rules
        self.forwarding_rules = data.get('forwarding_rules', [])

    # --- Status (authoritative state + legacy compat) --------------------
    def get_status(self) -> 'ConnectionState':
        """Return the authoritative :class:`ConnectionState`."""
        return self._status

    def get_status_reason(self) -> str:
        """Return the human-readable reason for the current status (may be '')."""
        return self._status_reason

    def set_status(self, state: 'ConnectionState', reason: str = '') -> None:
        """Set the authoritative status. Does not emit; the ConnectionManager
        owns signal emission so the UI updates through a single path."""
        self._status = state
        self._status_reason = reason or ''

    @property
    def is_connected(self) -> bool:
        """Legacy boolean view of status (True only when CONNECTED).

        Kept so the many existing ``connection.is_connected`` readers/writers
        keep working. Writing maps the bool onto CONNECTED/DISCONNECTED; richer
        states (CONNECTING/FAILED) are set explicitly via :meth:`set_status`.
        """
        return self._status == ConnectionState.CONNECTED

    @is_connected.setter
    def is_connected(self, value: bool) -> None:
        if value:
            self._status = ConnectionState.CONNECTED
            self._status_reason = ''
        else:
            # Don't clobber a richer "down" reason (FAILED) with a plain bool.
            if self._status not in (ConnectionState.FAILED,):
                self._status = ConnectionState.DISCONNECTED


class ConnectionManager(GObject.Object):
    """Manages SSH connections and configuration"""

    __gsignals__ = {
        'connection-added': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'connection-removed': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'connection-updated': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Legacy boolean status signal (connection, is_connected). Retained for
        # backward compatibility; emitted alongside the richer signal below.
        'connection-status-changed': (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
        # Authoritative status signal (connection, ConnectionState, reason).
        'connection-state-changed': (GObject.SignalFlags.RUN_FIRST, None, (object, object, str)),
    }

    def __init__(self, config, isolated_mode: bool = False):
        super().__init__()
        self.config = config
        self.connections: List[Connection] = []
        # Store wildcard/negated host blocks (rules) separately
        self.rules: List[Dict[str, Any]] = []
        self.ssh_config = {}
        self.loop = _ensure_event_loop()
        self.active_connections: Dict[str, asyncio.Task] = {}
        self._active_connection_keys: Dict[int, str] = {}
        self.ssh_config_path = ''
        self.known_hosts_path = ''

        # Diagnostic label of the active secret backend (set during init).
        self.secure_storage_backend = 'uninitialized'

        # Initialize SSH config paths
        self.set_isolated_mode(isolated_mode)

        # Defer slower operations to idle to avoid blocking startup
        GLib.idle_add(self._post_init_slow_path)

    def _register_connection(self, connection: Connection) -> None:
        """Link a connection to this manager and add it to the list."""
        connection._connection_manager = self
        self.connections.append(connection)

    # --- Non-SSH (plugin protocol) connection persistence -----------------
    #
    # ~/.ssh/config is the source of truth for SSH connections only; plugin
    # protocols (telnet, serial, ...) persist their Connection.data dicts as
    # JSON in the app config under 'connections.non_ssh', mirroring the
    # existing connections_meta pattern. Passwords never enter the JSON —
    # they go through store_password()/get_password() like SSH ones.

    _NON_SSH_SETTING = 'connections.non_ssh'

    @staticmethod
    def _serializable_connection_data(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            k: v for k, v in (data or {}).items()
            if not k.startswith('__') and k not in ('password', 'password_changed')
        }

    @staticmethod
    def _non_ssh_password_host(connection_or_data) -> str:
        """Keyring host identifier for a non-SSH connection.

        Scoped by ``protocol:nickname`` so two connections to the same host (or
        with empty usernames) can't collide on one keyring slot; the nickname is
        unique within the app."""
        if isinstance(connection_or_data, dict):
            data = connection_or_data
        else:
            data = getattr(connection_or_data, 'data', None) or {}
        protocol = data.get('protocol') or 'plugin'
        ident = (data.get('nickname') or data.get('host')
                 or data.get('hostname') or '')
        return f"{protocol}:{ident}" if ident else ''

    def _load_non_ssh_connections(self, existing_by_nickname: Dict[str, 'Connection']) -> None:
        """Append persisted plugin-protocol connections to self.connections.

        Reuses prior Connection objects by nickname (object identity matters:
        active_terminals and the sidebar are keyed by the objects)."""
        try:
            stored = self.config.get_setting(self._NON_SSH_SETTING, []) or []
        except Exception:
            stored = []
        for data in stored:
            if not isinstance(data, dict) or (data.get('protocol', 'ssh') == 'ssh'):
                continue
            try:
                nickname = data.get('nickname') or ''
                existing = existing_by_nickname.get(nickname)
                if existing is not None and getattr(existing, 'protocol', 'ssh') != 'ssh':
                    existing.update_data(dict(data))
                    self._register_connection(existing)
                else:
                    self._register_connection(Connection(dict(data)))
            except Exception:
                logger.exception("Failed to load non-SSH connection %r",
                                 data.get('nickname'))

    def _persist_non_ssh_connections(self) -> None:
        """Write all plugin-protocol connections back to the app config."""
        try:
            payload = [
                self._serializable_connection_data(conn.data)
                for conn in self.connections
                if getattr(conn, 'protocol', 'ssh') != 'ssh'
            ]
            self.config.set_setting(self._NON_SSH_SETTING, payload)
        except Exception:
            logger.exception("Failed to persist non-SSH connections")

    def _update_non_ssh_connection(self, connection: Connection,
                                   new_data: Dict[str, Any]) -> bool:
        """update_connection() counterpart for plugin protocols: keyring for
        the password, JSON store instead of the ssh_config write path."""
        try:
            new_data = dict(new_data)
            new_data.pop('__split_from_group', None)
            new_data.pop('__split_source', None)
            new_data.pop('__split_original_nickname', None)

            prev_host = self._non_ssh_password_host(connection)
            prev_user = getattr(connection, 'username', '') or ''

            password = new_data.pop('password', None)
            new_data.pop('password_changed', None)

            connection.update_data(new_data)

            if password is not None:
                curr_host = self._non_ssh_password_host(connection)
                curr_user = getattr(connection, 'username', '') or ''
                if password and curr_host:
                    self.store_password(curr_host, curr_user, password)
                else:
                    for host, user in {(prev_host, prev_user), (curr_host, curr_user)}:
                        if host:
                            try:
                                self.delete_password(host, user)
                            except Exception:
                                pass

            if connection not in self.connections:
                self._register_connection(connection)
            self._persist_non_ssh_connections()
            self.emit('connection-updated', connection)
            logger.info(f"Non-SSH connection updated: {connection.nickname}")
            return True
        except Exception as e:
            logger.error(f"Failed to update non-SSH connection: {e}")
            return False

    def _get_active_connection_key(self, connection: Connection) -> str:
        identifier = connection.resolve_host_identifier()
        if identifier:
            return identifier
        return f"connection-{id(connection)}"

    def set_isolated_mode(self, isolated: bool):
        """Switch between standard and isolated SSH configuration"""
        self.isolated_mode = bool(isolated)
        if self.isolated_mode:
            base = self._normalize_path(get_config_dir())
            os.makedirs(base, mode=0o700, exist_ok=True)
            self._ensure_secure_permissions(base, 0o700)
            self.ssh_config_path = self._normalize_path(os.path.join(base, 'ssh_config'))
            self.known_hosts_path = self._normalize_path(os.path.join(base, 'known_hosts'))
            for path in (self.ssh_config_path, self.known_hosts_path):
                if not os.path.exists(path):
                    open(path, 'a', encoding='utf-8').close()
                self._ensure_secure_permissions(path, 0o600)
        else:
            ssh_dir = self._normalize_path(get_ssh_dir())
            os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
            self._ensure_secure_permissions(ssh_dir, 0o700)
            self.ssh_config_path = self._normalize_path(os.path.join(ssh_dir, 'config'))
            self.known_hosts_path = self._normalize_path(os.path.join(ssh_dir, 'known_hosts'))
            if os.path.exists(self.ssh_config_path):
                self._ensure_secure_permissions(self.ssh_config_path, 0o600)
            if os.path.exists(self.known_hosts_path):
                self._ensure_secure_permissions(self.known_hosts_path, 0o600)

        # Reload SSH config to reflect new paths
        self.load_ssh_config()

    def _ensure_secure_permissions(self, path: str, mode: int):
        """Best effort at applying restrictive permissions to files/directories."""
        try:
            current_mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.debug("Unable to stat %s for permission fix: %s", path, exc)
            return

        if current_mode == mode:
            return

        try:
            os.chmod(path, mode)
        except Exception as exc:
            logger.debug("Unable to set permissions on %s: %s", path, exc)

    def _safe_write_config(self, path: str, text: str) -> None:
        """Atomically write *text* to *path*, keeping a ``.bak`` of the prior file.

        SSH config is precious user data, so writes must never leave a truncated
        file if the process dies mid-write. We back up the current contents to
        ``<path>.bak``, write the new contents to a temp file in the same
        directory, fsync it, then ``os.replace`` (atomic on the same filesystem)
        so readers only ever see the old or the complete new file.
        """
        directory = os.path.dirname(path) or '.'

        # One-shot backup of the previous good contents.
        if os.path.exists(path):
            try:
                shutil.copy2(path, f"{path}.bak")
                self._ensure_secure_permissions(f"{path}.bak", 0o600)
            except Exception as exc:
                logger.warning("Could not back up %s before writing: %s", path, exc)

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.sshpilot-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self._ensure_secure_permissions(path, 0o600)

    # -- managed global identity defaults (Host *) -----------------------
    _MANAGED_BEGIN = "# >>> sshpilot: identity defaults (managed) >>>"
    _MANAGED_END = "# <<< sshpilot: identity defaults (managed) <<<"

    @classmethod
    def _strip_managed_block(cls, text: str):
        """Remove the sentinel-delimited managed block from *text*.

        Returns ``(new_text, removed)``. When no managed block is present the text is
        returned byte-for-byte unchanged (``removed=False``) so we never churn the file.
        """
        if cls._MANAGED_BEGIN not in text:
            return text, False
        lines = text.splitlines(keepends=True)
        out, i, n, removed = [], 0, len(text.splitlines(keepends=True)), False
        while i < n:
            if lines[i].strip() == cls._MANAGED_BEGIN:
                removed = True
                i += 1
                while i < n and lines[i].strip() != cls._MANAGED_END:
                    i += 1
                i += 1  # skip the END marker line
                if i < n and lines[i].strip() == "":   # consume one trailing blank line
                    i += 1
                continue
            out.append(lines[i])
            i += 1
        return "".join(out), removed

    def apply_global_identity_agent(self, socket: Optional[str]) -> bool:
        """Write/update/remove a managed global ``Host *`` block setting ``IdentityAgent``
        to *socket* in ``~/.ssh/config``. A falsy *socket* removes the managed block.

        Idempotent and non-destructive: only the sentinel-delimited block is touched, so
        all other user content (including a user's own ``Host *``) is preserved. The block
        is placed at end of file, so per-host blocks (earlier) win ssh's first-match
        semantics and a per-connection ``IdentityAgent`` still overrides this default.
        """
        path = self.ssh_config_path
        if not path:
            return False
        socket = (socket or "").strip()
        try:
            existing = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    existing = f.read()
            stripped, _removed = self._strip_managed_block(existing)
            if socket:
                block = (f"{self._MANAGED_BEGIN}\n"
                         f"Host *\n"
                         f"    IdentityAgent {socket}\n"
                         f"{self._MANAGED_END}\n")
                base = stripped.rstrip("\n")
                new_text = (base + "\n\n" + block) if base.strip() else block
            else:
                new_text = stripped
            if new_text == existing:
                return True  # already in the desired state — no write
            if not new_text and not os.path.exists(path):
                return True  # nothing to remove and no file to create
            self._safe_write_config(path, new_text)
            logger.info("Updated managed IdentityAgent block (%s)",
                        socket or "removed")
            return True
        except Exception as exc:
            logger.error("Failed to apply global IdentityAgent: %s", exc)
            return False

    def _normalize_path(self, path: str) -> str:
        """Expand user/env vars and return absolute, non-empty paths."""
        if not path or not str(path).strip():
            raise ValueError("Cannot normalize empty SSH path")
        expanded = os.path.expanduser(os.path.expandvars(path))
        return os.path.abspath(expanded)

    def _ensure_config_parent_dir(self, path: str) -> str:
        """Normalize *path* and ensure its parent directory exists with secure permissions."""
        normalized = self._normalize_path(path)
        parent_dir = os.path.dirname(normalized)
        if parent_dir:
            try:
                os.makedirs(parent_dir, mode=0o700, exist_ok=True)
            except Exception as exc:
                logger.error("Unable to create SSH config directory %s: %s", parent_dir, exc)
                raise
            self._ensure_secure_permissions(parent_dir, 0o700)
        return normalized

    def _post_init_slow_path(self):
        """Run slower initialization steps after UI is responsive."""
        # Initialize secure storage via the pluggable secret manager.
        self.secure_storage_backend = 'none'
        try:
            from .secret_storage import get_secret_manager
            manager = get_secret_manager()
            try:
                _cfg = self.config
                selected = _cfg.get_setting('secrets.backend', 'auto')
            except Exception:
                _cfg = None
                selected = 'auto'
            # The legacy separate 'vaultwarden' backend was merged into 'bitwarden'
            # (one `bw` CLI). Migrate an old selection so it keeps working.
            if str(selected).strip().lower() == 'vaultwarden':
                selected = 'bitwarden'
                try:
                    if _cfg is not None:
                        _cfg.set_setting('secrets.backend', 'bitwarden')
                except Exception:
                    pass
            manager.set_selected(selected)
            # Propagate the selection (and session-backend settings) to child
            # processes (e.g. the askpass helper) so they resolve the same backend
            # and can read session-backed secrets non-interactively.
            os.environ['SSHPILOT_SECRET_BACKEND'] = str(selected or 'auto')
            try:
                if _cfg is None:
                    _cfg = self.config
                timeout_min = int(_cfg.get_setting('secrets.session_timeout', 0) or 0)
                os.environ['SSHPILOT_SECRET_SESSION_TIMEOUT'] = str(max(0, timeout_min) * 60)
                # Bitwarden CLI account/profile (BITWARDENCLI_APPDATA_DIR). Set in the
                # process env so every `bw` spawn — and the inherited askpass subprocess —
                # uses the same account.
                profile = str(_cfg.get_setting('secrets.bitwarden.profile', '') or '').strip()
                if profile:
                    os.environ['BITWARDENCLI_APPDATA_DIR'] = os.path.expanduser(profile)
                else:
                    os.environ.pop('BITWARDENCLI_APPDATA_DIR', None)
                os.environ.pop('SSHPILOT_VAULTWARDEN_SERVER', None)  # retired
                # KeePass (.kdbx) backend: database + optional key file paths, so the backend
                # and the inherited askpass subprocess open the same file.
                kdbx_db = str(_cfg.get_setting('secrets.keepassxc.database', '') or '').strip()
                if kdbx_db:
                    os.environ['SSHPILOT_KDBX_DATABASE'] = os.path.expanduser(kdbx_db)
                else:
                    os.environ.pop('SSHPILOT_KDBX_DATABASE', None)
                kdbx_kf = str(_cfg.get_setting('secrets.keepassxc.keyfile', '') or '').strip()
                if kdbx_kf:
                    os.environ['SSHPILOT_KDBX_KEYFILE'] = os.path.expanduser(kdbx_kf)
                else:
                    os.environ.pop('SSHPILOT_KDBX_KEYFILE', None)
            except Exception:
                pass
            self.secure_storage_backend = manager.active_backend_label()
            logger.info(
                "Secure storage backend: %s (selected=%s)",
                self.secure_storage_backend, selected,
            )
            # Surface (don't silently swallow) an explicit selection that can't work.
            try:
                sel_be = manager.selected_backend()
                if sel_be is not None and not sel_be.is_available():
                    logger.warning(
                        "Selected secret backend '%s' is unavailable — secrets will not "
                        "be stored or autofilled until it is available or you change the "
                        "backend in Preferences.", selected)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Secret manager initialization failed: %s", e)
        # Identity provider selection (parallel to the secret backend): propagate the
        # configured default provider to child processes and the in-process manager so
        # connection env injection routes through it.
        try:
            _idcfg = self.config
            identity_provider = str(
                _idcfg.get_setting('identity.provider', 'auto') or 'auto'
            ).strip().lower()
            agent_socket = str(
                _idcfg.get_setting('identity.agent_socket', '') or ''
            ).strip()
            os.environ['SSHPILOT_IDENTITY_PROVIDER'] = identity_provider
            if agent_socket:
                os.environ['SSHPILOT_IDENTITY_AGENT_SOCKET'] = agent_socket
            else:
                os.environ.pop('SSHPILOT_IDENTITY_AGENT_SOCKET', None)
            from .identity import get_identity_manager
            mgr = get_identity_manager()
            mgr.set_selected(identity_provider)
            # Reconcile the managed Host * IdentityAgent block with the saved selection.
            directives = dict(mgr.selected_config_directives())
            self.apply_global_identity_agent(directives.get('IdentityAgent'))
        except Exception as exc:
            logger.debug("Identity provider initialization failed: %s", exc)
        if self.secure_storage_backend == 'none':
            logger.info("Secure storage backend: unavailable; password storage disabled")
        return False  # run once

    # No _ensure_collection needed with libsecret's high-level API

    def _get_active_connection_key(self, connection: Connection, *, prefer_stored: bool = True) -> str:
        """Return the key used to track a connection's keepalive task."""

        conn_id = id(connection)
        if prefer_stored:
            stored = self._active_connection_keys.get(conn_id)
            if stored:
                return stored

        try:
            effective_host = connection.get_effective_host()
        except AttributeError:
            effective_host = getattr(connection, 'hostname', '') or getattr(connection, 'host', '') or ''

        username = getattr(connection, 'username', '') or ''
        key = effective_host or ''
        if username:
            key = f"{key}::{username}" if key else username
        if not key:
            key = connection.nickname or str(conn_id)

        if prefer_stored:
            self._active_connection_keys[conn_id] = key
        return key

        
    def load_ssh_config(self):
        """Load connections from SSH config file"""
        try:
            existing_by_nickname = {conn.nickname: conn for conn in self.connections}
            self.connections = []
            self.rules = []
            try:
                self.ssh_config_path = self._ensure_config_parent_dir(self.ssh_config_path)
            except Exception as exc:
                logger.error("Unable to prepare SSH config path '%s': %s", self.ssh_config_path, exc)
                return
            if not os.path.exists(self.ssh_config_path):
                logger.info("SSH config file not found, creating empty one")
                with open(self.ssh_config_path, 'w', encoding='utf-8') as f:
                    f.write("# SSH configuration file\n")
                    f.write('\n')
                self._ensure_secure_permissions(self.ssh_config_path, 0o600)
                self._load_non_ssh_connections(existing_by_nickname)
                return
            else:
                self._ensure_secure_permissions(self.ssh_config_path, 0o600)
            config_files = resolve_ssh_config_files(self.ssh_config_path)

            # Directives that accumulate per ssh_config(5) ("Multiple ...
            # directives will add to the list") rather than first-value-wins.
            ACCUMULATE_KEYS = {
                'localforward', 'remoteforward', 'dynamicforward',
                'identityfile', 'certificatefile',
            }

            # nickname -> {'raw': merged authored config, 'tokens': [...],
            #              'conn': Connection, 'source': cfg_file}
            # Tracks concrete hosts already materialised in THIS load so repeated
            # ``Host <name>`` stanzas (same file or across includes) merge into a
            # single connection with ssh_config(5) semantics — first-value-wins for
            # scalars, accumulation for IdentityFile/CertificateFile/forwards —
            # mirroring how ``ssh`` itself resolves duplicate Host blocks.
            loaded_this_load: Dict[str, Dict[str, Any]] = {}

            def _merge_raw(into: Dict[str, Any], new: Dict[str, Any]) -> None:
                """Merge authored directives (first-value-wins; lists accumulate)."""
                for k, v in new.items():
                    if k in ('host', '__host_tokens'):
                        continue
                    if k in into:
                        if k in ACCUMULATE_KEYS:
                            base = into[k] if isinstance(into[k], list) else [into[k]]
                            extra = v if isinstance(v, list) else [v]
                            into[k] = base + extra
                        # else: first-value-wins — keep the existing value.
                    else:
                        into[k] = v

            def _materialise(token: str, raw_cfg: Dict[str, Any], tokens: List[str], cfg_file: str):
                """Parse one concrete host, merging into a prior same-name block."""
                prior = loaded_this_load.get(token)
                if prior is not None:
                    _merge_raw(prior['raw'], raw_cfg)
                    host_cfg = dict(prior['raw'])
                    host_cfg['host'] = token
                    host_cfg['__host_tokens'] = [token]
                    connection_data = self.parse_host_config(host_cfg, source=prior['source'])
                    if connection_data:
                        connection_data['source'] = prior['source']
                        prior['conn'].update_data(connection_data)
                        prior['conn']._connection_manager = self
                    return

                raw_copy = dict(raw_cfg)
                host_cfg = dict(raw_copy)
                host_cfg['host'] = token
                host_cfg['__host_tokens'] = list(tokens)
                connection_data = self.parse_host_config(host_cfg, source=cfg_file)
                if not connection_data:
                    return
                connection_data['source'] = cfg_file
                nickname = connection_data.get('nickname', '')
                existing = existing_by_nickname.get(nickname)
                if existing:
                    existing.update_data(connection_data)
                    existing._connection_manager = self
                    self.connections.append(existing)
                    conn = existing
                else:
                    conn = Connection(connection_data)
                    if getattr(self, 'isolated_mode', False):
                        conn.isolated_config = True
                        conn.config_root = self.ssh_config_path
                        conn.data['isolated_mode'] = True
                        conn.data['config_root'] = self.ssh_config_path
                    self._register_connection(conn)
                loaded_this_load[token] = {
                    'raw': raw_copy, 'tokens': list(tokens),
                    'conn': conn, 'source': cfg_file,
                }

            def flush_block(tokens: List[str], config: Dict[str, Any], cfg_file: str):
                """Flush a completed Host block: wildcard/negation -> rule, else
                materialise (and merge) each concrete host token."""
                cleaned = [t.strip() for t in tokens if t and t.strip()]
                if not cleaned:
                    return
                if any('*' in t or '?' in t or t.startswith('!') for t in cleaned):
                    host_cfg = dict(config)
                    host_cfg['host'] = cleaned[0]
                    host_cfg['__host_tokens'] = list(cleaned)
                    self.parse_host_config(host_cfg, source=cfg_file)
                    return
                for token in cleaned:
                    _materialise(token, config, cleaned, cfg_file)

            for cfg_file in config_files:
                current_hosts: List[str] = []
                current_config: Dict[str, Any] = {}
                try:
                    with open(cfg_file) as f:
                        lines = f.readlines()
                except Exception as e:
                    logger.warning(f"Skipping unreadable config {cfg_file}: {e}")
                    continue
                i = 0
                while i < len(lines):
                    raw_line = lines[i]
                    line = raw_line.strip()
                    if not line:
                        i += 1
                        continue
                    if line.startswith('#'):
                        if current_hosts and line.startswith('# sshpilot:PreCommand '):
                            current_config['__pre_command'] = line[len('# sshpilot:PreCommand '):].strip()
                        i += 1
                        continue
                    # Identify the leading keyword honouring all separators
                    # (``Host x``, ``Host=x``, ``Host = x``, tabs) so equals-form
                    # block headers are recognised, not merged into the prior host.
                    keyword, remainder = _split_keyword(line)
                    if keyword == 'include':
                        i += 1
                        continue
                    if keyword == 'match':
                        if current_hosts and current_config:
                            flush_block(current_hosts, current_config, cfg_file)
                        current_hosts = []
                        current_config = {}
                        block_lines = [raw_line.rstrip('\n')]
                        i += 1
                        while i < len(lines) and _split_keyword(lines[i].strip())[0] not in ('host', 'match', 'include'):
                            block_lines.append(lines[i].rstrip('\n'))
                            i += 1
                        while block_lines and block_lines[-1].strip() == '':
                            block_lines.pop()
                        self.rules.append({'raw': '\n'.join(block_lines), 'source': cfg_file})
                        continue
                    if keyword == 'host':
                        tokens = shlex.split(remainder) if remainder else []
                        if not tokens:
                            i += 1
                            continue
                        if current_hosts and current_config:
                            flush_block(current_hosts, current_config, cfg_file)
                        current_hosts = tokens
                        current_config = {}
                        i += 1
                        continue
                    key, value = _split_config_option(line)
                    if key is not None:
                        key = key.lower()
                        # Directives that accumulate per ssh_config(5): forwardings
                        # and IdentityFile/CertificateFile ("Multiple ... directives
                        # will add to the list").
                        accumulates = key in (
                            'localforward', 'remoteforward', 'dynamicforward',
                            'identityfile', 'certificatefile',
                        )
                        if key in current_config:
                            if accumulates:
                                if not isinstance(current_config[key], list):
                                    current_config[key] = [current_config[key]]
                                current_config[key].append(value)
                            # Otherwise ssh_config(5) is first-value-wins: a
                            # repeated non-accumulating option is ignored.
                        else:
                            current_config[key] = value
                    i += 1
                if current_hosts and current_config:
                    flush_block(current_hosts, current_config, cfg_file)
            self._load_non_ssh_connections(existing_by_nickname)
            logger.info(f"Loaded {len(self.connections)} connections from SSH config")
        except Exception as e:
            logger.error(f"Failed to load SSH config: {e}", exc_info=True)

    def parse_host_config(self, config: Dict[str, Any], source: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Parse host configuration from SSH config"""
        try:
            def _unwrap(val: Any) -> Any:
                if isinstance(val, str) and len(val) >= 2:
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        return val[1:-1]
                return val

            host_token = _unwrap(config.get('host', ''))
            if not host_token:
                return None

            raw_tokens = config.get('__host_tokens')
            tokens = [_unwrap(t) for t in raw_tokens] if raw_tokens else [host_token]

            config = dict(config)
            config.pop('__host_tokens', None)
            config.pop('aliases', None)

            # Detect wildcard or negated host tokens (e.g., '*', '?', '!pattern')
            if any('*' in t or '?' in t or t.startswith('!') for t in tokens):
                if not hasattr(self, 'rules'):
                    self.rules = []
                rule_block = dict(config)
                rule_block['host'] = host_token
                if source:
                    rule_block['source'] = source
                self.rules.append(rule_block)
                return None

            host = host_token

            # Determine whether the config explicitly defined a HostName value.
            has_explicit_hostname = 'hostname' in config and str(config['hostname']).strip() != ''
            hostname_value = config['hostname'] if has_explicit_hostname else None
            parsed_host = _unwrap(hostname_value) if has_explicit_hostname else ''

            # IdentityFile/CertificateFile may appear multiple times; per
            # ssh_config(5) they accumulate ("Multiple ... directives will add
            # to the list"). Expand ~ and ${ENV} (but not the %-tokens, which are
            # host/runtime specific and resolved via `ssh -G` when the real
            # connection is built). An IdentityFile argument of ``none`` is a
            # suppressor ("no identity files should be loaded"), not a path.
            def _expand_path_value(val: Any) -> str:
                # ~ + ${ENV} + the host-independent %-tokens (%d, %u, ...). Runtime
                # tokens such as %h/%r are left intact for ssh / `ssh -G` to resolve.
                return os.path.expanduser(os.path.expandvars(expand_ssh_tokens(_unwrap(val))))

            def _as_list(raw: Any) -> List[Any]:
                if raw is None:
                    return []
                return list(raw) if isinstance(raw, list) else [raw]

            identity_files: List[str] = []
            identity_suppressed = False
            for entry in _as_list(config.get('identityfile')):
                unwrapped = _unwrap(entry)
                if isinstance(unwrapped, str) and unwrapped.strip().lower() == 'none':
                    identity_suppressed = True
                    continue
                if unwrapped:
                    identity_files.append(_expand_path_value(entry))

            certificate_files: List[str] = [
                _expand_path_value(entry)
                for entry in _as_list(config.get('certificatefile'))
                if _unwrap(entry)
            ]

            # Extract relevant configuration
            parsed = {
                'nickname': host,
                # Keep HostName empty when it was omitted in the original
                # configuration but record the label separately via ``host`` so
                # consumers can fall back to the alias when needed.
                'hostname': parsed_host,
                'host': host,

                'port': _safe_int(_unwrap(config.get('port', 22)), 22),
                'username': _unwrap(config.get('user', getpass.getuser())),
                # previously: 'private_key': config.get('identityfile'),

                # ``keyfile``/``certificate`` remain the primary (first) entry for
                # backward compatibility; the full lists live alongside them.
                'keyfile': identity_files[0] if identity_files else '',
                'identity_files': identity_files,
                'identity_file_none': identity_suppressed,
                'certificate': certificate_files[0] if certificate_files else '',
                'certificate_files': certificate_files,
                'forwarding_rules': []
            }
            if has_explicit_hostname:
                parsed['aliases'] = []
            if source:
                parsed['source'] = source

            if getattr(self, 'isolated_mode', False):
                parsed['isolated_mode'] = True
                if getattr(self, 'ssh_config_path', ''):
                    parsed['config_root'] = self.ssh_config_path


            # Map ForwardX11 yes/no → x11_forwarding boolean
            try:
                fwd_x11 = str(config.get('forwardx11', 'no')).strip().lower()
                parsed['x11_forwarding'] = fwd_x11 in ('yes', 'true', '1', 'on')
            except Exception:
                parsed['x11_forwarding'] = False
            
            # Handle port forwarding rules
            for forward_type in ['localforward', 'remoteforward', 'dynamicforward']:
                if forward_type not in config:
                    continue
                    
                forward_specs = config[forward_type]
                if not isinstance(forward_specs, list):
                    forward_specs = [forward_specs]
                    
                def _parse_listen_spec(spec):
                    """Return (bind_addr, port) for a "[bind_address:]port" token, or None.

                    An omitted/empty bind address is returned as '' (not coerced to
                    localhost); callers decide the default per forwarding type.
                    """
                    if ':' in spec:
                        bind_addr, port_str = spec.rsplit(':', 1)
                        bind_addr = bind_addr.strip().strip('[]')
                    else:
                        bind_addr, port_str = '', spec
                    port = _safe_int(port_str, None)
                    return None if port is None else (bind_addr, port)

                for forward_spec in forward_specs:
                    if forward_type == 'dynamicforward':
                        # Format is usually "[bind_address:]port"
                        listen = _parse_listen_spec(forward_spec.strip())
                        if listen is None:
                            continue
                        bind_addr, listen_port = listen
                        parsed['forwarding_rules'].append({
                            'type': 'dynamic',
                            'listen_addr': bind_addr or 'localhost',
                            'listen_port': listen_port,
                            'enabled': True
                        })
                    else:
                        # LocalForward / RemoteForward:
                        #   "[bind_address:]port [host:hostport]"
                        # RemoteForward may omit the destination, in which case it
                        # acts as a SOCKS proxy (ssh_config(5)).
                        parts = forward_spec.split()
                        if not parts:
                            continue
                        listen = _parse_listen_spec(parts[0])
                        if listen is None:
                            continue
                        bind_addr, listen_port = listen
                        dest_spec = parts[1] if len(parts) >= 2 else None

                        if forward_type == 'localforward':
                            # LocalForward requires a destination.
                            if dest_spec is None:
                                continue
                            if ':' in dest_spec:
                                remote_host, remote_port_str = dest_spec.rsplit(':', 1)
                                remote_port = _safe_int(remote_port_str, None)
                            else:
                                remote_host, remote_port = dest_spec, 22
                            if remote_port is None:
                                continue
                            parsed['forwarding_rules'].append({
                                'type': 'local',
                                'listen_addr': bind_addr or 'localhost',
                                'listen_port': listen_port,
                                'remote_host': remote_host,
                                'remote_port': remote_port,
                                'enabled': True
                            })
                        else:
                            # RemoteForward: remote host/port listens, destination
                            # (if any) is the local host/port.
                            rule = {
                                'type': 'remote',
                                'listen_addr': bind_addr,   # remote host
                                'listen_port': listen_port, # remote port
                                'enabled': True,
                            }
                            if dest_spec is None:
                                # Single-argument form → SOCKS proxy.
                                rule['socks'] = True
                            else:
                                if ':' in dest_spec:
                                    local_host, local_port_str = dest_spec.rsplit(':', 1)
                                    local_port = _safe_int(local_port_str, None)
                                else:
                                    local_host, local_port = dest_spec, 22
                                if local_port is None:
                                    continue
                                rule['local_host'] = local_host   # destination host (local)
                                rule['local_port'] = local_port   # destination port (local)
                            parsed['forwarding_rules'].append(rule)
            
            # Handle proxy settings if any
            if 'proxycommand' in config:
                parsed['proxy_command'] = config['proxycommand']
            if 'proxyjump' in config:
                pj = config['proxyjump']
                if isinstance(pj, list):
                    parsed['proxy_jump'] = [p.strip() for p in pj]
                else:
                    parsed['proxy_jump'] = [p.strip() for p in re.split(r'[\s,]+', pj)]
            # Agent / hardware key sources (kept verbatim — IdentityAgent may be
            # a path, "none", or a $ENV reference; providers are library paths).
            for direct_key, parsed_key in (
                ('identityagent', 'identity_agent'),
                ('addkeystoagent', 'add_keys_to_agent'),
                ('pkcs11provider', 'pkcs11_provider'),
                ('securitykeyprovider', 'security_key_provider'),
            ):
                if direct_key in config:
                    val = _unwrap(config.get(direct_key))
                    if val is not None and str(val).strip():
                        parsed[parsed_key] = str(val).strip()

            if 'forwardagent' in config:
                fa_raw = str(_unwrap(config.get('forwardagent', ''))).strip()
                fa = fa_raw.lower()
                # ssh_config(5): the argument may be yes, no, an explicit path to
                # an agent socket, or the name of an environment variable
                # (beginning with '$'). Anything that is not an explicit "off"
                # value enables agent forwarding.
                if fa in ('no', 'false', '0', 'off', ''):
                    parsed['forward_agent'] = False
                else:
                    parsed['forward_agent'] = True
                    # Preserve a socket path / $ENV reference for callers that need it.
                    if fa not in ('yes', 'true', '1', 'on'):
                        parsed['forward_agent_target'] = fa_raw
            
            # Commands: LocalCommand requires PermitLocalCommand
            try:
                def _unescape_cfg_value(val: str) -> str:
                    if not isinstance(val, str):
                        return val
                    v = val.strip()
                    # If the value is wrapped in double quotes, strip only the outer quotes
                    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                        v = v[1:-1]
                    # Convert escaped quotes back for UI
                    v = v.replace('\\"', '"').replace('\\\\', '\\')
                    return v

                if '__pre_command' in config:
                    parsed['pre_command'] = config['__pre_command']
                if 'localcommand' in config:
                    parsed['local_command'] = _unescape_cfg_value(config.get('localcommand', ''))
                if 'remotecommand' in config:
                    parsed['remote_command'] = _unescape_cfg_value(config.get('remotecommand', ''))
                # Map RequestTTY to a boolean flag to aid terminal decisions if needed
                if 'requesttty' in config:
                    parsed['request_tty'] = str(config.get('requesttty', '')).strip().lower() in ('yes', 'force', 'true', '1', 'on')
            except Exception:
                pass

            # Key selection mode defaults: prefer "specific key" when IdentityFile is explicit
            keyfile_value = parsed.get('keyfile', '')
            keyfile_path = keyfile_value.strip() if isinstance(keyfile_value, str) else ''
            has_specific_key = bool(keyfile_path and not keyfile_path.lower().startswith('select key file'))
            try:
                ident_only_raw = config.get('identitiesonly')
                ident_only_normalized = ident_only_raw
                if ident_only_raw and not isinstance(ident_only_raw, str):
                    ident_only_normalized = str(ident_only_raw)

                ident_only = ''
                if isinstance(ident_only_normalized, str):
                    ident_only = ident_only_normalized.strip().lower()

                if ident_only in ('yes', 'true', '1', 'on'):
                    parsed['key_select_mode'] = 1
                elif ident_only in ('no', 'false', '0', 'off'):
                    parsed['key_select_mode'] = 2 if has_specific_key else 0
                elif ident_only_raw is None or (isinstance(ident_only_raw, str) and not ident_only_raw.strip()):
                    parsed['key_select_mode'] = 2 if has_specific_key else 0
                else:
                    parsed['key_select_mode'] = 0
            except Exception:
                parsed['key_select_mode'] = 2 if has_specific_key else 0

            # Determine authentication method
            try:
                prefer_auth_raw = str(config.get('preferredauthentications', '')).strip()
                # Split into an ordered list while normalizing case
                prefer_auth_list = [p.strip().lower() for p in prefer_auth_raw.split(',') if p.strip()]
                parsed['preferred_authentications'] = prefer_auth_list

                pubkey_auth = str(config.get('pubkeyauthentication', '')).strip().lower()
                parsed['pubkey_auth_no'] = (pubkey_auth == 'no')

                if pubkey_auth == 'no':
                    parsed['auth_method'] = 1
                else:
                    # Determine based on first occurrence of publickey or password
                    idx_pubkey = prefer_auth_list.index('publickey') if 'publickey' in prefer_auth_list else None
                    idx_password = prefer_auth_list.index('password') if 'password' in prefer_auth_list else None

                    if idx_pubkey is not None and (idx_password is None or idx_pubkey < idx_password):
                        parsed['auth_method'] = 0
                    elif idx_password is not None and (idx_pubkey is None or idx_password < idx_pubkey):
                        parsed['auth_method'] = 1
                    else:
                        parsed['auth_method'] = 0
            except Exception:
                parsed['auth_method'] = 0
            
            # Parse extra SSH config options (custom options not handled by standard fields)
            extra_config_lines = []
            # Only include options that are explicitly handled by the main UI fields
            standard_options = {
                'host', 'hostname', 'aliases', 'port', 'user', 'identityfile', 'certificatefile',
                'forwardx11', 'localforward', 'remoteforward', 'dynamicforward',
                'proxycommand', 'proxyjump', 'forwardagent', 'localcommand', 'remotecommand', 'requesttty',
                'identitiesonly', 'permitlocalcommand',
                'preferredauthentications', 'pubkeyauthentication',
                'identityagent', 'addkeystoagent', 'pkcs11provider', 'securitykeyprovider',
            }
            
            for key, value in config.items():
                if key.lower() not in standard_options:
                    # This is a custom SSH option (including Ciphers, Compression, etc.)
                    if isinstance(value, list):
                        # Handle multiple values for the same option
                        for val in value:
                            extra_config_lines.append(f"{key} {val}")
                    else:
                        extra_config_lines.append(f"{key} {value}")
            
            if extra_config_lines:
                parsed['extra_ssh_config'] = '\n'.join(extra_config_lines)
                
            return parsed
            
        except Exception as e:
            logger.error(f"Error parsing host config: {e}", exc_info=True)
            return None

    def load_ssh_keys(self):
        """Auto-detect SSH keys in configured SSH directories."""
        search_dirs = []
        if getattr(self, 'isolated_mode', False):
            search_dirs.append(get_config_dir())
        search_dirs.append(get_ssh_dir())

        keys: List[str] = []
        seen: Set[str] = set()
        validation_cache: Dict[str, bool] = {}
        fallback_to_pub = False
        for ssh_dir in search_dirs:
            if not os.path.exists(ssh_dir):
                continue
            try:
                for filename in os.listdir(ssh_dir):
                    file_path = Path(ssh_dir) / filename

                    if filename.endswith('.pub'):
                        if fallback_to_pub:
                            private_key_path = file_path.with_suffix('')
                            key_path = str(private_key_path)
                            if private_key_path.exists() and key_path not in seen:
                                keys.append(key_path)
                                seen.add(key_path)
                        continue

                    if fallback_to_pub:
                        pub_candidate = file_path.with_suffix(file_path.suffix + '.pub')
                        if pub_candidate.exists():
                            key_path = str(file_path)
                            if key_path not in seen:
                                keys.append(key_path)
                                seen.add(key_path)
                        continue

                    try:
                        if _is_private_key(file_path, cache=validation_cache):
                            key_path = str(file_path)
                            if key_path not in seen:
                                keys.append(key_path)
                                seen.add(key_path)
                    except FileNotFoundError:
                        fallback_to_pub = True
                        logger.debug(
                            "ssh-keygen not available; falling back to public-key discovery in %s",
                            ssh_dir,
                        )
                        pub_candidate = file_path.with_suffix(file_path.suffix + '.pub')
                        if pub_candidate.exists():
                            key_path = str(file_path)
                            if key_path not in seen:
                                keys.append(key_path)
                                seen.add(key_path)
                    except Exception as exc:
                        logger.debug(
                            "Failed to validate potential key %s: %s",
                            file_path,
                            exc,
                            exc_info=True,
                        )
            except Exception as e:
                logger.debug(
                    "Failed to load SSH keys from %s: %s",
                    ssh_dir,
                    e,
                    exc_info=True,
                )

        logger.info(f"Found {len(keys)} SSH keys: {keys}")
        return keys

    def store_password(self, host: str, username: str, password: str):
        """Store a password via the selected secret backend (see secret_storage)."""
        from .secret_storage import get_secret_manager, password_spec
        stored = get_secret_manager().store(password_spec(host, username), password)
        if not stored:
            logger.warning("No secure storage backend available; password not stored")
        return stored

    def store_connection_password(self, connection, password: str,
                                  username: Optional[str] = None,
                                  previous_connection=None) -> bool:
        """Store a connection's SSH password under the canonical host key.

        Clears legacy copies stored under older host aliases (nickname, etc.) and,
        when an edited connection changed host or username, its previous identity.
        """
        from .credential_model import canonical_password_host, password_host_candidates

        user = (username or getattr(connection, 'username', '') or '').strip()
        canonical = canonical_password_host(connection)
        if not canonical or not user or not password:
            return False
        stored = self.store_password(canonical, user, password)
        if stored:
            cleanup = {
                (host, user)
                for host in password_host_candidates(connection)
                if host and host != canonical
            }
            if previous_connection:
                previous_user = (
                    previous_connection.get('username')
                    if isinstance(previous_connection, dict)
                    else getattr(previous_connection, 'username', '')
                ) or user
                cleanup.update(
                    (host, previous_user)
                    for host in password_host_candidates(previous_connection)
                    if host and (host != canonical or previous_user != user)
                )
            for host, cleanup_user in cleanup:
                self.delete_password(host, cleanup_user)
        return stored

    def get_password(self, host: str, username: str) -> Optional[str]:
        """Retrieve a password via the selected secret backend."""
        from .secret_storage import get_secret_manager, password_spec
        return get_secret_manager().lookup(password_spec(host, username))

    def get_connection_password(self, connection,
                                username: Optional[str] = None) -> Optional[str]:
        """Look up a connection's SSH password, migrating legacy host aliases on hit."""
        from .credential_model import canonical_password_host, password_host_candidates

        user = (username or getattr(connection, 'username', '') or '').strip()
        if not user:
            return None
        canonical = canonical_password_host(connection)
        for host in password_host_candidates(connection) or ([canonical] if canonical else []):
            if not host:
                continue
            value = self.get_password(host, user)
            if value:
                if canonical and host != canonical:
                    if self.store_password(canonical, user, value):
                        self.delete_password(host, user)
                return value
        return None

    def delete_password(self, host: str, username: str) -> bool:
        """Delete a stored password from all available secret backends."""
        from .secret_storage import get_secret_manager, password_spec
        removed_any = get_secret_manager().delete(password_spec(host, username))
        if removed_any:
            logger.debug(f"Deleted stored password for {username}@{host}")
        return removed_any

    def delete_connection_passwords(self, connection,
                                    username: Optional[str] = None) -> bool:
        """Delete a connection's SSH password from every host alias and backend."""
        from .credential_model import password_host_candidates

        user = (username or getattr(connection, 'username', '') or '').strip()
        removed = False
        for host in password_host_candidates(connection):
            if host and user and self.delete_password(host, user):
                removed = True
        return removed

    # --- Plugin secrets ----------------------------------------------------
    #
    # Namespaced per plugin id so a plugin can never read another plugin's
    # (or a connection's) secrets. Reuses the store_password() path — which routes
    # through the configurable secret backend (see secret_storage.py) — with a
    # reserved host identifier: real SSH hosts are stored under their hostname,
    # plugin secrets under 'sshpilot-plugin/<id>'.

    @staticmethod
    def _plugin_secret_host(plugin_id: str) -> str:
        return f"sshpilot-plugin/{plugin_id}"

    def store_plugin_secret(self, plugin_id: str, key: str, value: str) -> bool:
        return bool(self.store_password(self._plugin_secret_host(plugin_id), key, value))

    def get_plugin_secret(self, plugin_id: str, key: str) -> Optional[str]:
        return self.get_password(self._plugin_secret_host(plugin_id), key)

    def delete_plugin_secret(self, plugin_id: str, key: str) -> bool:
        return self.delete_password(self._plugin_secret_host(plugin_id), key)

    def store_key_passphrase(self, key_path: str, passphrase: str) -> bool:
        """Store key passphrase securely in system keyring"""
        # Use the unified passphrase storage from askpass_utils
        return store_passphrase(key_path, passphrase)

    def get_key_passphrase(self, key_path: str) -> Optional[str]:
        """Retrieve key passphrase from system keyring"""
        # Use the unified passphrase lookup from askpass_utils
        passphrase = lookup_passphrase(key_path)
        return passphrase if passphrase else None

    def delete_key_passphrase(self, key_path: str) -> bool:
        """Delete stored key passphrase from system keyring"""
        # Use the unified passphrase clearing from askpass_utils
        return clear_passphrase(key_path)

    def _ensure_ssh_agent(self) -> bool:
        """Ensure ssh-agent is running and export environment variables"""
        try:
            # Check if ssh-agent is already running (via the identity provider so
            # the agent-presence check lives in one place).
            from .providers.system_agent import SystemAgentProvider
            if SystemAgentProvider().is_available():
                logger.debug("SSH agent already running")
                return True
            
            # Start a new ssh-agent
            logger.debug("Starting new ssh-agent")
            result = subprocess.run(
                ['ssh-agent', '-s'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to start ssh-agent: {result.stderr}")
                return False
            
            # Parse the output to extract environment variables
            for line in result.stdout.split('\n'):
                if line.startswith('export '):
                    # Extract variable name and value
                    var_part = line[7:]  # Remove 'export '
                    if '=' in var_part:
                        name, value = var_part.split('=', 1)
                        # Remove quotes if present
                        value = value.strip().strip('"\'')
                        os.environ[name] = value
            
            logger.debug("SSH agent started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error ensuring ssh-agent is running: {e}")
            return False



    def add_key_to_agent(self, key_path: str) -> bool:
        """Add SSH key to ssh-agent using secure SSH_ASKPASS script"""
        from .askpass_utils import ensure_key_in_agent
        return ensure_key_in_agent(key_path)

    def prepare_key_for_connection(self, key_path: str, *, force: bool = True) -> bool:
        """Prepare SSH key for connection by unlocking it in ssh-agent"""
        from .askpass_utils import prepare_key_for_connection
        return prepare_key_for_connection(key_path, force=force)

    def invalidate_cached_commands(self):
        """Clear cached SSH commands so future launches pick up new settings."""
        for connection in list(self.connections):
            try:
                if hasattr(connection, 'ssh_cmd'):
                    connection.ssh_cmd = []
                if getattr(connection, 'is_connected', False):
                    connection.is_connected = False
            except Exception as exc:
                logger.debug("Failed to invalidate cached command for %s: %s", connection, exc)

    def format_ssh_config_entry(self, data: Dict[str, Any]) -> str:
        """Format connection data as SSH config entry"""
        def _quote_token(token: str) -> str:
            if not token:
                return '""'
            if any(c.isspace() for c in token):
                return f'"{token}"'
            return token

        def _format_forward_host(host: str) -> str:
            host = (host or '').strip()
            if not host:
                return host
            if ':' in host and not (host.startswith('[') and host.endswith(']')):
                return f"[{host}]"
            return host

        host = data.get('hostname') or data.get('host', '')
        nickname = data.get('nickname') or host
        primary_token = _quote_token(nickname)
        lines = [f"Host {primary_token}"]

        # Add basic connection info
        if host and host != nickname:
            lines.append(f"    HostName {host}")
        lines.append(f"    User {data.get('username', '')}")
        
        # Add port if specified and not default
        port = data.get('port')
        if port and port != 22:  # Only add port if it's not the default 22
            lines.append(f"    Port {port}")

        # Proxy settings
        proxy_jump = data.get('proxy_jump') or []
        if isinstance(proxy_jump, str):
            proxy_jump = [h.strip() for h in re.split(r'[\s,]+', proxy_jump) if h.strip()]
        if proxy_jump:
            lines.append(f"    ProxyJump {','.join(proxy_jump)}")
        if data.get('forward_agent'):
            lines.append("    ForwardAgent yes")

        # Add IdentityFile/IdentitiesOnly per selection when auth is key-based
        keyfile = data.get('keyfile') or data.get('private_key')
        auth_method = int(data.get('auth_method', 0) or 0)
        key_select_mode = int(data.get('key_select_mode', 0) or 0)
        dedicated_key = key_select_mode in (1, 2)

        def _quote_if_spaced(value: str) -> str:
            if ' ' in value and not (value.startswith('"') and value.endswith('"')):
                return f'"{value}"'
            return value

        def _clean_list(values, placeholder_prefix):
            cleaned = []
            for value in values:
                if not isinstance(value, str):
                    continue
                stripped = value.strip()
                if not stripped or stripped.lower().startswith(placeholder_prefix):
                    continue
                if stripped not in cleaned:
                    cleaned.append(stripped)
            return cleaned

        if auth_method == 0:
            # ssh_config(5) allows multiple IdentityFile/CertificateFile entries;
            # write the full list when present, falling back to the primary key.
            identity_files = _clean_list(
                data.get('identity_files') or ([keyfile] if keyfile else []),
                'select key file',
            )
            # Only write IdentityFile when using a dedicated key mode
            if dedicated_key and identity_files:
                for kf in identity_files:
                    lines.append(f"    IdentityFile {_quote_if_spaced(kf)}")

                if key_select_mode == 1:
                    lines.append("    IdentitiesOnly yes")

                # Add certificate(s) if specified (exclude placeholder text)
                certificate_files = _clean_list(
                    data.get('certificate_files') or ([data.get('certificate')] if data.get('certificate') else []),
                    'select certificate',
                )
                for cert in certificate_files:
                    lines.append(f"    CertificateFile {_quote_if_spaced(cert)}")

            # Agent / hardware key sources — valid in both automatic and
            # specific-key modes (the key may come from an agent socket, a
            # PKCS#11 smartcard, or a FIDO security key rather than a file).
            ident_agent = (data.get('identity_agent') or '').strip()
            if ident_agent:
                lines.append(f"    IdentityAgent {_quote_if_spaced(ident_agent)}")
            add_keys = (data.get('add_keys_to_agent') or '').strip()
            if add_keys:
                lines.append(f"    AddKeysToAgent {add_keys}")
            pkcs11 = (data.get('pkcs11_provider') or '').strip()
            if pkcs11:
                lines.append(f"    PKCS11Provider {_quote_if_spaced(pkcs11)}")
            sk_provider = (data.get('security_key_provider') or '').strip()
            if sk_provider:
                lines.append(f"    SecurityKeyProvider {_quote_if_spaced(sk_provider)}")
            # Include password-based fallback if a password is provided
            if data.get('password'):
                lines.append(
                    "    PreferredAuthentications gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
                )
        else:
            # Password-based authentication. Include keyboard-interactive so
            # PAM/2FA hosts (which often disable the raw "password" method)
            # still negotiate; order prefers kbd-int first.
            lines.append(
                "    PreferredAuthentications keyboard-interactive,password"
            )
            if data.get('pubkey_auth_no'):
                lines.append("    PubkeyAuthentication no")
        
        # Add X11 forwarding if enabled
        if data.get('x11_forwarding', False):
            lines.append("    ForwardX11 yes")

        # Add PreCommand (sshpilot-specific, stored as a comment)
        pre_cmd = (data.get('pre_command') or '').strip()
        if pre_cmd:
            lines.append(f"    # sshpilot:PreCommand {pre_cmd}")

        # Add LocalCommand if specified, ensure PermitLocalCommand (write exactly as provided)
        local_cmd = (data.get('local_command') or '').strip()
        if local_cmd:
            lines.append("    PermitLocalCommand yes")
            lines.append(f"    LocalCommand {local_cmd}")

        # Add RemoteCommand and RequestTTY if specified (ensure shell stays active)
        remote_cmd = (data.get('remote_command') or '').strip()
        if remote_cmd:
            # Ensure we keep an interactive shell after the command
            remote_cmd_aug = remote_cmd if 'exec $SHELL' in remote_cmd else f"{remote_cmd} ; exec $SHELL -l"
            # Write RemoteCommand first, then RequestTTY (order for readability)
            lines.append(f"    RemoteCommand {remote_cmd_aug}")
            lines.append("    RequestTTY yes")
        
        # Add port forwarding rules if any (ensure sane defaults)
        for rule in data.get('forwarding_rules', []):
            listen_addr = (rule.get('listen_addr') or '').strip()
            listen_port = rule.get('listen_port', '')
            if not listen_port:
                continue
            # An empty bind address is written without a host prefix (omitted), so
            # ssh/GatewayPorts decides the bind. local/dynamic always carry a
            # localhost default, so only an empty remote bind drops the prefix.
            listen_host = _format_forward_host(listen_addr)
            listen_spec = f"{listen_host}:{listen_port}" if listen_host else f"{listen_port}"
            
            if rule.get('type') == 'local':
                dest_host = rule.get('remote_host', '')
                dest_spec = f"{_format_forward_host(dest_host) or dest_host}:{rule.get('remote_port', '')}"
                lines.append(f"    LocalForward {listen_spec} {dest_spec}")
            elif rule.get('type') == 'remote':
                # Single-argument (SOCKS) form has no destination. A destination
                # needs both a host and a port; if either is missing fall back to
                # the SOCKS form rather than emitting a malformed "host:" spec.
                dest_host = rule.get('local_host') or rule.get('remote_host', '')
                dest_port = rule.get('local_port') or rule.get('remote_port')
                if rule.get('socks') or not dest_host or not dest_port:
                    lines.append(f"    RemoteForward {listen_spec}")
                else:
                    # For RemoteForward we forward remote listen -> local destination
                    dest_spec = f"{_format_forward_host(dest_host) or dest_host}:{dest_port}"
                    lines.append(f"    RemoteForward {listen_spec} {dest_spec}")
            elif rule.get('type') == 'dynamic':
                lines.append(f"    DynamicForward {listen_spec}")
        
        # Add extra SSH config parameters if provided
        extra_config = data.get('extra_ssh_config', '').strip()
        if extra_config:
            # Split by lines and add each line as a separate config option
            for line in extra_config.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    # Ensure proper indentation
                    if not line.startswith('    '):
                        line = f"    {line}"
                    lines.append(line)

        # Remove duplicate or unwanted auth lines
        cleaned_lines: List[str] = []
        seen_auth_lines = set()
        auth_keys = {
            "preferredauthentications password",
            "pubkeyauthentication no",
        }
        for line in lines:
            key = line.strip().lower()
            if auth_method == 0 and key in auth_keys:
                # Strip password-only directives when using key-based auth
                continue
            if auth_method != 0 and key in auth_keys:
                if key in seen_auth_lines:
                    # Avoid duplicates for password auth
                    continue
                seen_auth_lines.add(key)
            cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

    def get_host_block_details(self, host_identifier: str, source: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return details for the first Host block matching *host_identifier*."""
        try:
            host_identifier = (host_identifier or '').strip()
            if not host_identifier:
                return None

            target_path = source or self.ssh_config_path
            if not target_path or not os.path.exists(target_path):
                return None

            with open(target_path) as f:
                lines = f.readlines()

            i = 0
            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                # Detect the Host header honouring all separators (Host=x, tabs).
                kw, full_value = _split_keyword(lstripped)
                if kw == 'host':
                    try:
                        host_names = shlex.split(full_value)
                    except ValueError:
                        host_names = [h for h in full_value.split() if h]

                    if host_identifier in host_names:
                        start_index = i
                        i += 1
                        while i < len(lines) and _split_keyword(lines[i].strip())[0] not in ('host', 'match', 'include'):
                            i += 1
                        end_index = i
                        block_lines = [lines[j].rstrip('\n') for j in range(start_index, end_index)]
                        return {
                            'source': target_path,
                            'hosts': host_names,
                            'start': start_index,
                            'end': end_index,
                            'lines': block_lines,
                        }
                i += 1
        except Exception as e:
            logger.debug(f"Failed to inspect host block for '{host_identifier}': {e}")
        return None

    def _split_host_block(self, original_host: str, new_data: Dict[str, Any], target_path: str) -> bool:
        """Remove *original_host* from its group and append a new block."""
        try:
            if not target_path:
                target_path = self.ssh_config_path
            target_path = self._ensure_config_parent_dir(target_path)

            try:
                with open(target_path) as f:
                    lines = f.readlines()
            except FileNotFoundError:
                lines = []

            updated_lines: List[str] = []
            i = 0
            found = False
            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                # Detect the Host header honouring all separators (Host=x, tabs).
                kw, full_value = _split_keyword(lstripped)
                if kw == 'host':
                    try:
                        host_names = shlex.split(full_value)
                    except ValueError:
                        host_names = [h for h in full_value.split() if h]

                    if not found and original_host in host_names:
                        found = True
                        remaining_hosts = [h for h in host_names if h != original_host]
                        indent_len = len(raw_line) - len(lstripped)
                        indent = raw_line[:indent_len]
                        if remaining_hosts:
                            updated_lines.append(f"{indent}Host {' '.join(remaining_hosts)}\n")
                            i += 1
                            while i < len(lines) and _split_keyword(lines[i].strip())[0] not in ('host', 'match', 'include'):
                                updated_lines.append(lines[i])
                                i += 1
                        else:
                            i += 1
                            while i < len(lines) and _split_keyword(lines[i].strip())[0] not in ('host', 'match', 'include'):
                                i += 1
                        continue
                updated_lines.append(raw_line)
                i += 1

            formatted_block = self.format_ssh_config_entry(new_data).rstrip('\n')
            if updated_lines:
                if not updated_lines[-1].endswith('\n'):
                    updated_lines[-1] = updated_lines[-1] + '\n'
                if updated_lines[-1].strip():
                    updated_lines.append('\n')

            updated_lines.append(formatted_block + '\n')

            self._safe_write_config(target_path, ''.join(updated_lines))

            logger.info(
                "Split host block for '%s' (found=%s) and appended dedicated entry to %s",
                original_host,
                found,
                target_path,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to split host block for '{original_host}': {e}")
            return False

    def update_ssh_config_file(self, connection: Connection, new_data: Dict[str, Any], original_nickname: Optional[str] = None):
        """Update SSH config file with new connection data"""
        try:
            target_path = new_data.get('source') or getattr(connection, 'source', None) or self.ssh_config_path
            target_path = self._ensure_config_parent_dir(target_path)
            if not os.path.exists(target_path):
                updated_config = self.format_ssh_config_entry(new_data)
                self._safe_write_config(
                    target_path,
                    "# SSH configuration file\n\n" + updated_config.rstrip('\n') + '\n',
                )
                return

            try:
                with open(target_path) as f:
                    lines = f.readlines()
            except OSError as e:
                logger.error(f"Failed to read SSH config: {e}")
                raise
            
            # Find and update the connection's Host block using nickname matching
            updated_lines = []
            new_name = str(new_data.get('nickname') or '')
            host_found = False
            replaced_once = False

            # For renaming, we need to find the existing block by the original nickname
            # The connection object might already have the new nickname, so we need to be smarter
            candidate_names = {new_name}
            
            # Add the original nickname to candidate names for proper matching during renames
            if original_nickname:
                candidate_names.add(original_nickname)
            
            logger.debug(f"Looking for host block with candidate names: {candidate_names}")
            logger.debug(f"Original nickname: {original_nickname}, New name: {new_name}")

            i = 0
            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                # Detect the Host header honouring every ssh_config(5) separator
                # (``Host x``, ``Host=x``, ``Host = x``, tabs) — the same logic the
                # loader uses — so an equals/tab-form block is found and replaced
                # rather than leaving a stale block and appending a duplicate.
                kw, remainder = _split_keyword(lstripped)

                if kw == 'host':
                    host_names = shlex.split(remainder) if remainder else []

                    logger.debug(
                        f"Found Host line: '{lstripped.strip()}' -> host_names={host_names}"
                    )

                    if any(host_name in candidate_names for host_name in host_names):
                        logger.debug(
                            f"MATCH FOUND! Host '{host_names}' matches candidate names {candidate_names}"
                        )
                        host_found = True
                        if not replaced_once:
                            updated_config = self.format_ssh_config_entry(new_data)
                            updated_lines.append(updated_config + '\n')
                            replaced_once = True
                        # Skip this Host line and the block's lines until the next
                        # Host/Match/Include header (Include stops the skip so an
                        # Include directive is never swallowed).
                        i += 1
                        while i < len(lines) and _split_keyword(lines[i].strip())[0] not in ('host', 'match', 'include'):
                            i += 1
                        continue
                    else:
                        # This is a different Host block, keep it
                        updated_lines.append(raw_line)
                else:
                    # Not a Host line, keep it
                    updated_lines.append(raw_line)

                i += 1
            
            # If host not found, append the new config
            if not host_found:
                updated_config = self.format_ssh_config_entry(new_data)
                updated_lines.append('\n' + updated_config + '\n')
            
            try:
                self._safe_write_config(target_path, ''.join(updated_lines))
                logger.info(
                    "Wrote SSH config for host %s (found=%s, rules=%d) to %s",
                    new_name,
                    host_found,
                    len(new_data.get('forwarding_rules', []) or []),
                    target_path,
                )
            except OSError as e:
                logger.error(f"Failed to write SSH config: {e}")
                raise
        except Exception as e:
            logger.error(f"Error updating SSH config: {e}", exc_info=True)
            raise

    def remove_ssh_config_entry(self, host_nickname: str, source: Optional[str] = None) -> bool:
        """Remove a host label from SSH config, or entire block if it's the only label.

        Returns True if a modification was made, False if not found or on error.
        """
        try:
            target_path = source or self.ssh_config_path
            if not os.path.exists(target_path):
                return False
            try:
                with open(target_path) as f:
                    lines = f.readlines()
            except OSError as e:
                logger.error(f"Failed to read SSH config for delete: {e}")
                return False

            updated_lines = []
            i = 0
            modified = False

            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                # Match the Host header honouring all separators (Host=x, tabs).
                kw, full_value = _split_keyword(lstripped)

                if kw == 'host':
                    try:
                        current_names = shlex.split(full_value) if full_value else []
                    except ValueError:
                        # Fallback to simple split if shlex fails
                        current_names = [h for h in full_value.split() if h]
                    
                    # Check if our target host is in this Host directive
                    if host_nickname in current_names:
                        modified = True
                        # Remove the target hostname from the list
                        remaining_names = [name for name in current_names if name != host_nickname]
                        
                        if remaining_names:
                            # Update the Host line with remaining names
                            indent = raw_line[:len(raw_line) - len(lstripped)]
                            # Use shlex.join if available (Python 3.8+), otherwise manual quoting
                            try:
                                remaining_hosts_str = shlex.join(remaining_names)
                            except AttributeError:
                                # Fallback for older Python versions
                                remaining_hosts_str = ' '.join(
                                    f'"{name}"' if ' ' in name or '"' in name else name 
                                    for name in remaining_names
                                )
                            updated_line = f"{indent}Host {remaining_hosts_str}\n"
                            updated_lines.append(updated_line)
                            logger.info(f"Updated Host line: removed '{host_nickname}', remaining: {remaining_names}")
                            
                            # Keep the rest of the block
                            i += 1
                            while i < len(lines) and _split_keyword(lines[i].strip())[0] not in ('host', 'match', 'include'):
                                updated_lines.append(lines[i])
                                i += 1
                        else:
                            # No remaining names, delete the entire block
                            logger.info(f"Deleting entire Host block for '{host_nickname}' (was the only host)")
                            i += 1
                            # Skip the entire block
                            while i < len(lines) and _split_keyword(lines[i].strip())[0] not in ('host', 'match', 'include'):
                                i += 1
                        continue
                
                # Keep line as-is
                updated_lines.append(raw_line)
                i += 1

            if modified:
                try:
                    self._safe_write_config(target_path, ''.join(updated_lines))
                    logger.info(f"SSH config updated: {'removed' if host_nickname else 'modified'} entry for '{host_nickname}'")
                except OSError as e:
                    logger.error(f"Failed to write SSH config after delete: {e}")
                    return False
            return modified
        except Exception as e:
            logger.error(f"Error removing SSH config entry: {e}", exc_info=True)
            return False

    def _preserve_multivalue_on_update(self, connection: 'Connection', new_data: Dict[str, Any]) -> None:
        """Carry forward extra IdentityFile/CertificateFile entries on edit.

        The connection dialog edits a single primary key, but a host may carry
        several IdentityFile/CertificateFile directives. When the save payload
        omits the full list, fold the existing extras back in so they are not
        dropped from ~/.ssh/config: the edited primary
        (``new_data['keyfile']``/``['certificate']``) replaces the old first
        entry and the remaining entries are kept, in order, de-duplicated.
        """
        def _reconcile(list_key: str, primary_key: str):
            if list_key in new_data:
                return  # caller already supplied the full list
            existing = list(getattr(connection, list_key, []) or [])
            if len(existing) <= 1:
                return  # nothing extra to preserve
            new_primary = str(new_data.get(primary_key, '') or '').strip()
            merged = ([new_primary] if new_primary else []) + list(existing[1:])
            deduped = list(dict.fromkeys(m for m in merged if m))
            if deduped:
                new_data[list_key] = deduped

        _reconcile('identity_files', 'keyfile')
        _reconcile('certificate_files', 'certificate')

    def add_connection_from_data(self, data: Dict[str, Any]) -> Connection:
        """Create, persist, and announce a new connection from a data dict.

        The programmatic counterpart of the connection dialog's save path,
        exposed to plugins via PluginContext.add_connection (e.g. a VPS
        provider provisioning hosts). Raises ValueError on invalid data."""
        data = dict(data)
        data.setdefault('protocol', 'ssh')

        # Validate via the protocol backend (late import: plugins.api/registry
        # never import connection_manager, so there is no cycle).
        from .plugins.registry import protocol_registry
        backend = protocol_registry().get_or_none(data['protocol'])
        if backend is None:
            raise ValueError(f"Unknown protocol {data['protocol']!r}")

        errors = list(backend.validate(data) or [])
        nickname = (data.get('nickname') or data.get('host')
                    or data.get('hostname') or '').strip()
        if not nickname:
            errors.append("A nickname or host is required.")
        elif self.find_connection_by_nickname(nickname):
            errors.append(f"A connection named {nickname!r} already exists.")
        if errors:
            raise ValueError('; '.join(errors))
        data['nickname'] = nickname

        connection = Connection(dict(data))
        if self.isolated_mode:
            connection.isolated_config = True
            connection.config_root = self.ssh_config_path
            connection.data['isolated_mode'] = True
            if self.ssh_config_path:
                connection.data['config_root'] = self.ssh_config_path

        self._register_connection(connection)
        # update_connection persists to the protocol's store (ssh_config for
        # SSH — including password keyring handling — or the non-SSH JSON
        # store) and emits 'connection-updated'.
        if not self.update_connection(connection, dict(data)):
            try:
                self.connections.remove(connection)
            except ValueError:
                pass
            raise RuntimeError("Failed to persist connection")

        # Announce on the main loop: provider plugins may call from workers.
        GLib.idle_add(self.emit, 'connection-added', connection)
        return connection

    def update_connection(self, connection: Connection, new_data: Dict[str, Any]) -> bool:
        """Update an existing connection"""
        try:
            secret_storage_done = bool(new_data.pop('__secret_storage_done', False))
            if isinstance(getattr(connection, 'data', None), dict):
                connection.data.pop('__secret_storage_done', None)
            protocol = (new_data.get('protocol')
                        or getattr(connection, 'protocol', 'ssh') or 'ssh')
            if protocol != 'ssh':
                # Plugin protocols never touch ~/.ssh/config.
                return self._update_non_ssh_connection(connection, new_data)

            split_from_group = bool(new_data.pop('__split_from_group', False))
            split_source_override = new_data.pop('__split_source', None)
            split_original_host = new_data.pop('__split_original_nickname', None)

            target_path = split_source_override or new_data.get('source') or getattr(connection, 'source', self.ssh_config_path)
            logger.info(
                "Updating connection '%s' → writing to %s (rules=%d)",
                connection.nickname,
                target_path,
                len(new_data.get('forwarding_rules', []) or [])
            )
            prev_user = getattr(connection, 'username', '')
            original_nickname = getattr(connection, 'nickname', '')

            # Preserve multiple IdentityFile/CertificateFile entries the single-key
            # dialog doesn't surface, so "open → change a field → save" never drops
            # keys from ~/.ssh/config.
            self._preserve_multivalue_on_update(connection, new_data)

            # Update existing object IN-PLACE instead of creating new ones
            connection.update_data(new_data)

            # The dialog's new_data carries the new nickname/hostname but not the
            # parsed Host-line tokens. resolve_host_identifier() prefers the cached
            # data['__host_tokens'] / data['host'], so without refreshing them the
            # connection would keep using the *pre-edit* alias (e.g. a duplicate's
            # "Name (Copy)") as the native ssh target — ssh then rejects it with
            # "hostname contains invalid characters" until the next config reload.
            # The Host line is rewritten as `Host <nickname>`, so re-derive the
            # tokens from the (authoritative) new nickname + aliases. (issue #953)
            try:
                if '__host_tokens' not in new_data and isinstance(getattr(connection, 'data', None), dict):
                    alias = (getattr(connection, 'nickname', '') or '').strip()
                    if alias:
                        connection.data['host'] = alias
                        extra_aliases = [a for a in (getattr(connection, 'aliases', []) or []) if a]
                        connection.data['__host_tokens'] = [alias] + extra_aliases
            except Exception:
                logger.debug("Failed to refresh host tokens after update", exc_info=True)

            # Update the SSH config file with original nickname for proper matching
            if split_from_group:
                original_token = split_original_host or original_nickname
                if not self._split_host_block(original_token, new_data, target_path):
                    logger.error("Failed to split host block for %s", original_token)
                    return False
            else:
                self.update_ssh_config_file(connection, new_data, original_nickname)

            # Handle password storage/removal
            if 'password' in new_data and not secret_storage_done:
                pwd = new_data.get('password') or ''
                curr_user = new_data.get('username') or getattr(connection, 'username', prev_user)
                if pwd:
                    self.store_connection_password(connection, pwd, username=curr_user)
                else:
                    try:
                        self.delete_connection_passwords(connection, username=prev_user)
                    except Exception:
                        pass
                    if curr_user != prev_user:
                        try:
                            self.delete_connection_passwords(connection, username=curr_user)
                        except Exception:
                            pass
            
            # DO NOT call load_ssh_config() here - it breaks object references

            # The host's SSH config changed: retire its ControlMaster (if any)
            # so the next connect negotiates the new settings instead of
            # silently riding the old transport. Live sessions drain naturally.
            try:
                from .ssh_multiplex import invalidate_master
                invalidate_master(connection, self)
            except Exception:
                logger.debug("Master invalidation skipped", exc_info=True)

            # Emit signal with SAME connection object
            self.emit('connection-updated', connection)
            
            logger.info(f"Connection updated: {connection}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update connection: {e}")
            return False

    def remove_connection(
        self,
        connection: Connection,
        *,
        reload_config: bool = True,
    ) -> bool:
        """Remove a connection, optionally deferring the final config reload."""
        try:
            # Remove from list
            if connection in self.connections:
                self.connections.remove(connection)
            
            # Remove password from secure storage (all host aliases)
            try:
                self.delete_connection_passwords(connection)
            except Exception as e:
                logger.warning(f"Failed to remove password from storage: {e}")
            
            # Remove from the protocol's store (ssh_config for SSH, the JSON
            # list for plugin protocols)
            if getattr(connection, 'protocol', 'ssh') != 'ssh':
                self._persist_non_ssh_connections()
            else:
                try:
                    removed = self.remove_ssh_config_entry(connection.nickname, getattr(connection, 'source', None))
                    logger.debug(f"SSH config entry removed={removed} for {connection.nickname}")
                except Exception as e:
                    logger.warning(f"Failed to remove SSH config entry for {connection.nickname}: {e}")
            
            # Remove per-connection metadata (auth method, etc.) to avoid lingering entries
            try:
                from .config import Config
                cfg = Config()
                meta_all = cfg.get_setting('connections_meta', {}) or {}
                if isinstance(meta_all, dict) and connection.nickname in meta_all:
                    del meta_all[connection.nickname]
                    cfg.set_setting('connections_meta', meta_all)
                    logger.debug(f"Removed metadata for {connection.nickname}")
            except Exception as e:
                logger.debug(f"Could not remove metadata for {connection.nickname}: {e}")
            
            # Emit signal
            self.emit('connection-removed', connection)
            
            # Bulk UI deletion defers this expensive full parse until every
            # selected connection has been persisted.
            if reload_config:
                try:
                    self.load_ssh_config()
                except Exception:
                    pass

            logger.info(f"Connection removed: {connection}")
            return True

        except Exception as e:
            logger.error(f"Failed to remove connection: {e}")
            return False

    def _get_active_connection_key(self, connection: Connection) -> str:
        """Return the dictionary key used to track active connection tasks."""
        try:
            key = connection.get_connection_key()
        except AttributeError:
            host = getattr(connection, 'hostname', '') or getattr(connection, 'host', '')
            username = getattr(connection, 'username', '')
            if username and host:
                key = f"{username}@{host}"
            elif host:
                key = host
            elif username:
                key = username
            else:
                key = ''
        if not key:
            key = f"connection-{id(connection)}"
        return key

    async def connect(self, connection: Connection):
        """Connect to an SSH host asynchronously"""
        try:
            if hasattr(self, 'isolated_mode'):
                connection.isolated_mode = bool(getattr(self, 'isolated_mode', False))
            # Connect to the SSH server (native-only; connect() delegates to it).
            if hasattr(connection, 'native_connect'):
                connected = await connection.native_connect()
            else:
                connected = await connection.connect()
            if not connected:
                raise Exception("Failed to establish SSH connection")

            # Determine the tracking key used for keepalive management
            key = self._get_active_connection_key(connection, prefer_stored=False)
            existing_key = self._active_connection_keys.get(id(connection))
            if existing_key and existing_key != key:
                old_task = self.active_connections.pop(existing_key, None)
                if old_task:
                    old_task.cancel()
            self._active_connection_keys[id(connection)] = key

            # Store the connection task
            if key in self.active_connections:
                self.active_connections[key].cancel()

            
            # Create a task to keep the connection alive
            async def keepalive():
                try:
                    while connection.is_connected:
                        try:
                            # Send keepalive every 30 seconds
                            await asyncio.sleep(30)
                            if connection.connection and connection.is_connected:
                                await connection.connection.ping()
                        except (ConnectionError, asyncio.CancelledError):
                            break
                        except Exception as e:
                            logger.error(f"Keepalive error for {connection}: {e}")
                            break
                finally:
                    if connection.is_connected:
                        await connection.disconnect()
                    connection.is_connected = False
                    self.emit('connection-status-changed', connection, False)
                    logger.info(f"Disconnected from {connection}")
            
            # Start the keepalive task
            task = asyncio.create_task(keepalive())
            self.active_connections[key] = task

            
            # Update the connection state and emit status change
            connection.is_connected = True
            GLib.idle_add(self.emit, 'connection-status-changed', connection, True)
            logger.info(f"Connected to {connection}")
            
            return True
            
        except Exception as e:
            error_msg = f"Failed to connect to {connection}: {e}"
            logger.error(error_msg, exc_info=True)
            if hasattr(connection, 'connection') and connection.connection:
                await connection.disconnect()
            connection.is_connected = False
            raise Exception(error_msg) from e
    
    async def disconnect(self, connection: Connection):
        """Disconnect from SSH host and clean up resources asynchronously"""
        try:
            # Cancel the keepalive task if it exists
            key = self._get_active_connection_key(connection)
            if key in self.active_connections:
                self.active_connections[key].cancel()
                try:
                    await self.active_connections[key]
                except asyncio.CancelledError:
                    pass
                del self.active_connections[key]
            self._active_connection_keys.pop(id(connection), None)

            
            # Disconnect the connection
            if hasattr(connection, 'connection') and connection.connection and connection.is_connected:
                await connection.disconnect()
            
            # Update the connection state and emit status change signal
            connection.is_connected = False
            GLib.idle_add(self.emit, 'connection-status-changed', connection, False)
            logger.info(f"Disconnected from {connection}")
            
        except Exception as e:
            logger.error(f"Failed to disconnect from {connection}: {e}", exc_info=True)
            raise

    def update_connection_status(self, connection: Connection, is_connected: bool):
        """Update connection status in the manager (legacy boolean entry point).

        This method is called by terminals to update the connection manager's
        tracking of connection status, especially after reconnections. It maps
        the boolean onto the authoritative state and delegates to
        :meth:`update_connection_state`.
        """
        state = ConnectionState.CONNECTED if is_connected else ConnectionState.DISCONNECTED
        self.update_connection_state(connection, state)

    def update_connection_state(
        self,
        connection: Connection,
        state: 'ConnectionState',
        reason: str = '',
    ):
        """Set the authoritative connection state and notify the UI.

        Emits both the richer ``connection-state-changed`` signal and the legacy
        ``connection-status-changed`` boolean so old and new listeners both work.
        """
        try:
            is_connected = (state == ConnectionState.CONNECTED)
            # Lightweight connection stand-ins (e.g. LocalConnection for local
            # terminals) don't implement the status API; fall back to the plain
            # boolean so they don't crash the state update.
            if hasattr(connection, 'set_status'):
                connection.set_status(state, reason)
            else:
                try:
                    connection.is_connected = is_connected
                except Exception:
                    pass

            # Emit both signals on the main loop so UI handlers run on the GTK thread.
            GLib.idle_add(self.emit, 'connection-state-changed', connection, state, reason or '')
            GLib.idle_add(self.emit, 'connection-status-changed', connection, is_connected)

            logger.debug(
                f"Connection manager updated state for {connection.nickname}: "
                f"{state.value}{f' ({reason})' if reason else ''}"
            )

        except Exception as e:
            logger.error(f"Failed to update connection status: {e}")

    def get_connections(self) -> List[Connection]:
        """Get list of all connections"""
        return self.connections.copy()

    def find_connection_by_nickname(self, nickname: str) -> Optional[Connection]:
        """Find connection by nickname"""
        for connection in self.connections:
            if connection.nickname == nickname:
                return connection
        return None
