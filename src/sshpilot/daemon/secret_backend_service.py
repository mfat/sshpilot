"""Daemon-owned secret-backend management service.

The production daemon owns the single authoritative :class:`SecretManager`
(session selection, unlock/lock, Bitwarden and rbw lifecycle, and the
``secrets.*`` configuration).  The frontend never touches backends directly:
it reads metadata and drives lifecycle through this service.

No secret values ever cross this service's public surface.  Protected input
(master passwords, two-factor codes, API client secrets, auth-challenge
secrets) is collected through the daemon's existing
:class:`~sshpilot.daemon.interaction_broker.InteractionBroker` — secret frames
travel as bytearrays with a one-use nonce and are cleared after use.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import RLock, Timer
from typing import Any, Dict, List, Optional, Tuple

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.interactions import (
    InteractionState,
    InteractionType,
    PasswordPrompt,
    RememberPolicy,
)
from sshpilot.api.models.secrets import (
    BACKEND_UNAVAILABLE,
    REVISION_CONFLICT,
    SETTINGS_MALFORMED,
    SETTINGS_PERSISTENCE_FAILED,
    BitwardenStatus,
    RbwStatus,
    SecretBackendDescriptor,
    SecretBackendRegistry,
    SecretBackendState,
    SecretConfiguration,
    SecretOperationResult,
    SecretOperationState,
    SecretTransferResult,
    SecretUnlockResult,
    UnlockResultKind,
    UpdateSecretConfigurationRequest,
)
from sshpilot.core.secrets import (
    SecretDecisionKind,
    decide_unlock,
    normalize_backend_name,
)
from sshpilot.core.secrets.management import (
    FIELD_TO_CONFIG_KEY,
    compute_secret_settings_revision,
    normalize_secret_settings,
)
from sshpilot.core.settings import (
    SettingsFileError,
    load_settings_strict,
    save_settings,
    set_nested,
    settings_transaction_lock,
)

logger = logging.getLogger(__name__)

# Reserved interaction session namespace.  Interactions created by this service
# use synthetic session ids under this prefix; the GTK secrets presenter filters
# on it and the session-scoped interaction dialogs ignore it.
SECRET_SESSION_PREFIX = "secret-session"
_SECRET_SESSION_COUNTER = 0
_SECRET_SESSION_LOCK = RLock()

DEFAULT_SECRET_INTERACTION_TIMEOUT = 120.0


def _next_secret_session_id() -> SessionId:
    global _SECRET_SESSION_COUNTER
    with _SECRET_SESSION_LOCK:
        _SECRET_SESSION_COUNTER += 1
        return SessionId(f"{SECRET_SESSION_PREFIX}-{_SECRET_SESSION_COUNTER}")


def is_secret_service_session(session_id: Any) -> bool:
    """True when an interaction belongs to the secret-backend service."""
    return isinstance(session_id, str) and session_id.startswith(SECRET_SESSION_PREFIX)


class SecretBackendService:
    """Thread-safe manager for daemon-owned secret-backend state.

    ``secret_manager`` defaults to the process-wide singleton
    (:func:`get_secret_manager`) so every daemon-side consumer — connection
    secrets, askpass, shutdown, and this service — shares one backend instance.
    """

    def __init__(
        self,
        settings_path: Path | str,
        *,
        secret_manager: Any = None,
        interaction_broker: Any = None,
        connections_source: Any = None,
        connection_store_snapshot: Any = None,
        connection_store_restore: Any = None,
    ) -> None:
        self._path = Path(settings_path)
        if secret_manager is None:
            from sshpilot.secret_storage import get_secret_manager

            secret_manager = get_secret_manager()
        self._manager = secret_manager
        self._broker = interaction_broker
        self._connections_source = connections_source
        # Two more narrow bound-method callables, parallel to
        # ``connections_source`` above — the portable connection-store
        # snapshot/restore surface used by backup export/import. Never the
        # repository object itself.
        self._connection_store_snapshot = connection_store_snapshot
        self._connection_store_restore = connection_store_restore
        # Reentrant: several public operations compose nested public calls under
        # the lock (``update_selection`` -> ``update_configuration`` ->
        # ``get_state``; ``bitwarden_configure_server`` -> ``bitwarden_status``;
        # ``rbw_configure`` -> ``rbw_status``). A plain ``Lock`` deadlocks on
        # those paths.
        self._lock = RLock()
        # Short-lived decrypted-manifest cache: an import preview reads the
        # ``.spbk`` (prompting for a passphrase via a protected interaction), and
        # the subsequent import reuses the cached manifest so the user is never
        # prompted for the passphrase twice. Every entry has a real expiry timer
        # so a manifest is removed even when no later API request touches its
        # key.  Cache contents never leave this process and never appear in
        # reprs, logs, diagnostics or API results.
        self._manifest_cache: Dict[str, Dict[str, Any]] = {}
        self._manifest_timers: Dict[str, Timer] = {}
        self._MANIFEST_CACHE_TTL = 120.0
        # Bounded re-prompts for a wrong import passphrase; each retry goes through
        # a fresh protected interaction so the secret never travels as an RPC param.
        self._MAX_IMPORT_PASSPHRASE_ATTEMPTS = 3

    def attach_interaction_broker(self, broker: Any) -> None:
        """Inject the daemon's interaction broker once it exists (the broker is
        created per-session-runtime, after this service is composed)."""
        self._broker = broker

    # ------------------------------------------------------------------
    # Configuration (daemon-owned ``secrets.*``)
    # ------------------------------------------------------------------

    def get_configuration(self) -> SecretConfiguration:
        with self._lock:
            config = self._load_strict()
            return self._snapshot(config)

    def update_configuration(
        self,
        request: UpdateSecretConfigurationRequest,
    ) -> SecretConfiguration:
        if type(request) is not UpdateSecretConfigurationRequest:
            raise TypeError("an UpdateSecretConfigurationRequest is required")
        with self._lock:
            # The settings transaction lock serializes this complete
            # load -> validate -> apply -> save cycle against the SSH overrides
            # service, which mutates the same config.json.  It is never held
            # while prompting or running native backends (see
            # ``bitwarden_configure_server`` for the slow-command pattern).
            with settings_transaction_lock(self._path):
                config = self._load_strict()
                current = self._snapshot(config)

                if (
                    request.expected_revision is not None
                    and request.expected_revision != current.revision
                ):
                    raise SshPilotError(
                        ErrorCode.VALIDATION_FAILED,
                        "The secret configuration has been modified since last read",
                        details={"code": REVISION_CONFLICT},
                    )

                if not request.patch:
                    return current

                for key, value in request.patch.items():
                    config_key = self._field_to_config_key(key)
                    set_nested(config, config_key, value)

                semantic = self._normalize(config)
                self._write_semantic(config, semantic)
                try:
                    save_settings(self._path, config)
                except Exception as exc:
                    raise SshPilotError(
                        ErrorCode.PERSISTENCE_FAILED,
                        "The secret settings could not be saved",
                        details={"code": SETTINGS_PERSISTENCE_FAILED},
                    ) from exc
                self._apply_environment(config)
                return self._snapshot(config)

    def update_selection(
        self,
        backend: str,
        *,
        expected_revision: Optional[str] = None,
    ) -> SecretBackendState:
        """Change the selected backend under the same optimistic-concurrency
        contract as any other configuration mutation.

        ``expected_revision`` is the revision the caller last observed (the
        controller passes its loaded configuration snapshot). A concurrent
        mutation that bumps the revision rejects this update with
        ``REVISION_CONFLICT`` instead of silently overwriting it.
        """
        self.update_configuration(
            UpdateSecretConfigurationRequest(
                patch={"backend": normalize_backend_name(backend)},
                expected_revision=expected_revision,
            )
        )
        return self.get_state()

    # ------------------------------------------------------------------
    # Registry / state (metadata only)
    # ------------------------------------------------------------------

    def get_registry(self) -> SecretBackendRegistry:
        with self._lock:
            config = self._load_strict()
            semantic = self._normalize(config)
            self._apply_environment(config)
            selected = semantic["backend"]
            effective = self._manager.active_backend_name or "none"
            names = list(self._manager.registered_backends())
            available = set(self._manager.available_backends(cheap=True))
            return SecretBackendRegistry(
                backends=tuple(
                    self._descriptor(
                        name,
                        selected=name == selected or (
                            selected == "auto"
                            and name == self._manager.active_backend_name
                        ),
                        available=name in available,
                    )
                    for name in names
                ),
                effective_backend=effective,
                selected_backend=selected,
            )

    def get_state(self) -> SecretBackendState:
        with self._lock:
            config = self._load_strict()
            semantic = self._normalize(config)
            self._apply_environment(config)
            return self._state_from(semantic)

    def _state_from(self, semantic: Dict[str, Any]) -> SecretBackendState:
        selected = semantic["backend"]
        effective = self._manager.active_backend_name or "none"
        decision = self._selected_decision()
        locked = decision.kind == SecretDecisionKind.UNLOCK_REQUIRED
        needs_login = self._selected_needs_login()
        return SecretBackendState(
            selected_backend=selected,
            effective_backend=effective,
            locked=locked,
            needs_unlock=locked,
            login_required=needs_login,
            session_timeout=semantic["session_timeout"],
            remember_in_keyring=semantic["remember_in_keyring"],
            persists_secrets=self._manager.persists_secrets(),
        )

    # ------------------------------------------------------------------
    # Unlock / lock
    # ------------------------------------------------------------------

    def unlock(self, *, owner_client_id) -> SecretUnlockResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            decision = self._selected_decision()
            backend = self._manager.selected_backend()
            name = backend.name if backend is not None else "none"

            if decision.kind == SecretDecisionKind.BACKEND_UNAVAILABLE:
                return SecretUnlockResult(
                    kind=UnlockResultKind.BACKEND_UNAVAILABLE,
                    backend=name,
                    message=decision.message,
                )
            if decision.kind == SecretDecisionKind.READY:
                return SecretUnlockResult(
                    kind=UnlockResultKind.UNLOCKED,
                    backend=name,
                )
            if self._selected_needs_login():
                return SecretUnlockResult(
                    kind=UnlockResultKind.LOGIN_REQUIRED,
                    backend=name,
                    message="The selected vault requires sign-in before unlock",
                )
            if decision.kind == SecretDecisionKind.UNLOCK_REQUIRED:
                master = self._remembered_master_password()
                remember = False
                if master is None:
                    secret, remember = self._prompt_for_master_password(
                        name, owner_client_id=owner_client_id
                    )
                    if secret is None:
                        return SecretUnlockResult(
                            kind=UnlockResultKind.INTERACTION_REQUIRED,
                            backend=name,
                            message="Unlock cancelled",
                        )
                    try:
                        master = secret.decode("utf-8", "replace")
                    finally:
                        _clear_secret(secret)
                try:
                    ok = self._manager.unlock_selected(master)
                    # Store the password already collected for unlock — never a
                    # second prompt just to remember it.
                    if ok and remember:
                        self._store_master_password(name, master)
                finally:
                    master = ""  # drop the protected value after use
                if not ok:
                    return SecretUnlockResult(
                        kind=UnlockResultKind.BACKEND_UNAVAILABLE,
                        backend=name,
                        message="The vault could not be unlocked",
                    )
                return SecretUnlockResult(
                    kind=UnlockResultKind.UNLOCKED,
                    backend=name,
                )
            return SecretUnlockResult(
                kind=UnlockResultKind.UNLOCKED,
                backend=name,
            )

    def lock(self) -> SecretBackendState:
        with self._lock:
            config = self._load_strict()
            semantic = self._normalize(config)
            self._apply_environment(config)
            backend = self._manager.selected_backend()
            if backend is not None:
                try:
                    backend.lock()
                except Exception:
                    logger.debug("secret backend lock failed", exc_info=True)
            # Locking invalidates any decrypted preview manifests.
            self._clear_cached_manifests()
            return self._state_from(semantic)

    # ------------------------------------------------------------------
    # Bitwarden lifecycle
    # ------------------------------------------------------------------

    def bitwarden_status(self, *, force_refresh: bool = False) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            return _bitwarden_status(bw, force_refresh=force_refresh)

    def bitwarden_configure_server(self, url: str) -> BitwardenStatus:
        with self._lock:
            url = str(url or "").strip()
            # Load under the settings transaction lock, then release it before
            # the slow native ``bw config server`` command so a concurrent SSH
            # overrides write is never blocked on backend I/O.
            with settings_transaction_lock(self._path):
                config = self._load_strict()
                self._apply_environment(config)
            command_ok = self._configure_bitwarden_server(url)
            if not command_ok:
                status = self.bitwarden_status(force_refresh=True)
                return BitwardenStatus(
                    logged_in=status.logged_in,
                    unlocked=status.unlocked,
                    needs_login=status.needs_login,
                    email=status.email,
                    server_url=status.server_url,
                    profile=status.profile,
                    twofa_required=status.twofa_required,
                    message="Bitwarden server configuration failed",
                )
            # Reacquire the transaction lock and reload the latest file
            # immediately before applying the configuration change, so the write
            # is based on the most recent state (no lost concurrent edits).
            with settings_transaction_lock(self._path):
                config = self._load_strict()
                semantic = self._normalize(config)
                semantic["bitwarden_server"] = url
                self._write_semantic(config, semantic)
                try:
                    save_settings(self._path, config)
                except Exception as exc:
                    raise SshPilotError(
                        ErrorCode.PERSISTENCE_FAILED,
                        "The secret settings could not be saved",
                        details={"code": SETTINGS_PERSISTENCE_FAILED},
                    ) from exc
                self._apply_environment(config)
            return self.bitwarden_status(force_refresh=True)

    def bitwarden_login(
        self,
        email: str,
        *,
        twofa_method: Optional[str] = None,
        owner_client_id,
    ) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            email = (email or "").strip()
            if not email:
                raise ValueError("email is required")

            password = self._prompt_for_secret(
                f"Enter the Bitwarden master password for {email}",
                owner_client_id=owner_client_id,
            )
            if password is None:
                return BitwardenStatus(
                    logged_in=False, unlocked=False, needs_login=True,
                    email=email, server_url="", profile="",
                    twofa_required=False, message="Login cancelled",
                )
            password_text = password.decode("utf-8", "replace")
            _clear_secret(password)
            try:
                # Auth-challenge client secret is only collected when the backend
                # sign-in later reports it is required — it never travels in RPC
                # parameters and is cleared right after the retry.
                ok, detail, needs_2fa = self._bitwarden_login_with_password(
                    bw, email, password_text, twofa_method=twofa_method, twofa_code=None,
                    auth_client_secret=None,
                )

                if not ok and not needs_2fa and _login_needs_challenge(detail):
                    client_secret = self._prompt_for_secret(
                        "Enter the Bitwarden API client secret to complete the "
                        "authentication challenge",
                        owner_client_id=owner_client_id,
                    )
                    if client_secret is None:
                        return BitwardenStatus(
                            logged_in=False, unlocked=False, needs_login=True,
                            email=email, server_url=_server_url(config),
                            profile=_profile(config),
                            message="Authentication challenge cancelled",
                        )
                    challenge_text = client_secret.decode("utf-8", "replace")
                    _clear_secret(client_secret)
                    try:
                        ok, detail, needs_2fa = self._bitwarden_login_with_password(
                            bw, email, password_text, twofa_method=twofa_method,
                            twofa_code=None, auth_client_secret=challenge_text,
                        )
                    finally:
                        challenge_text = ""

                if needs_2fa and twofa_method:
                    code = self._prompt_for_secret(
                        f"Enter the two-step login code for {email}",
                        owner_client_id=owner_client_id,
                    )
                    if code is None:
                        return BitwardenStatus(
                            logged_in=False, unlocked=False, needs_login=True,
                            email=email, server_url=_server_url(config),
                            profile=_profile(config),
                            twofa_required=True, message="Two-step login cancelled",
                        )
                    code_text = code.decode("utf-8", "replace")
                    _clear_secret(code)
                    try:
                        ok, detail, needs_2fa = self._bitwarden_login_with_password(
                            bw, email, password_text, twofa_method=twofa_method,
                            twofa_code=code_text, auth_client_secret=None,
                        )
                    finally:
                        code_text = ""

                # Mirror the CLI "log in and unlock in one step": a successful login
                # can still leave the vault locked, so unlock with the password we have.
                if ok and password_text and not self._safe_is_unlocked(bw):
                    if not self._safe(lambda: bw.unlock(password_text)):
                        ok = False
                        detail = "Bitwarden vault unlock failed"

                return BitwardenStatus(
                    logged_in=ok,
                    unlocked=self._safe_is_unlocked(bw),
                    needs_login=not ok,
                    email=email,
                    server_url=_server_url(config),
                    profile=_profile(config),
                    twofa_required=needs_2fa,
                    message=detail if not ok else "",
                )
            finally:
                password_text = ""

    def bitwarden_api_key_login(self, client_id: str, *, owner_client_id) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            client_id = (client_id or "").strip()
            if not client_id:
                raise ValueError("client_id is required")
            secret = self._prompt_for_secret(
                f"Enter the API key client secret for {client_id}",
                owner_client_id=owner_client_id,
            )
            if secret is None:
                return BitwardenStatus(
                    logged_in=False, unlocked=False, needs_login=True,
                    email="", server_url=_server_url(config), profile=_profile(config),
                    message="Login cancelled",
                )
            secret_text = secret.decode("utf-8", "replace")
            _clear_secret(secret)
            try:
                try:
                    ok, detail = bw.login_with_api_key(client_id, secret_text)
                except Exception:
                    logger.debug("Bitwarden API-key login failed", exc_info=True)
                    ok, detail = False, "Bitwarden API-key login failed"
                return BitwardenStatus(
                    logged_in=ok,
                    unlocked=self._safe_is_unlocked(bw),
                    needs_login=not ok,
                    email=client_id,
                    server_url=_server_url(config),
                    profile=_profile(config),
                    message=detail if not ok else "",
                )
            finally:
                secret_text = ""

    def bitwarden_sso_login(self, identifier: Optional[str] = None) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            try:
                ok, detail = bw.login_with_sso(identifier or None)
            except Exception:
                logger.debug("Bitwarden SSO login failed", exc_info=True)
                ok, detail = False, "Bitwarden SSO login failed"
            return BitwardenStatus(
                logged_in=ok,
                unlocked=self._safe_is_unlocked(bw),
                needs_login=not ok,
                email="",
                server_url=_server_url(config),
                profile=_profile(config),
                message=detail if not ok else "",
            )

    def bitwarden_unlock(self, *, owner_client_id) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            if self._safe_is_unlocked(bw):
                return _bitwarden_status(bw)
            secret = self._prompt_for_secret(
                "Enter the Bitwarden master password to unlock the vault",
                owner_client_id=owner_client_id,
            )
            if secret is None:
                return _bitwarden_status(bw, message="Unlock cancelled")
            secret_text = secret.decode("utf-8", "replace")
            _clear_secret(secret)
            try:
                try:
                    ok = bool(bw.unlock(secret_text))
                except Exception:
                    logger.debug("Bitwarden unlock failed", exc_info=True)
                    ok = False
                return _bitwarden_status(
                    bw,
                    message="" if ok else "Bitwarden unlock failed",
                )
            finally:
                secret_text = ""

    def bitwarden_sync(self) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            ok = self._run_safely(lambda: bw._run(["sync"]))
            return _bitwarden_status(
                bw,
                message="" if ok else "Bitwarden sync failed",
            )

    def bitwarden_lock(self) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is not None:
                try:
                    bw.lock()
                except Exception:
                    logger.debug("Bitwarden lock failed", exc_info=True)
            self._clear_cached_manifests()
            return _bitwarden_status(bw)

    def bitwarden_logout(self) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            try:
                bw.logout()
            except Exception:
                logger.debug("Bitwarden logout failed", exc_info=True)
            return _bitwarden_status(bw, force_refresh=True)

    # ------------------------------------------------------------------
    # rbw lifecycle (native agent / pinentry ownership preserved)
    # ------------------------------------------------------------------

    def rbw_status(self) -> RbwStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            rbw = self._manager.get_backend("rbw")
            return _rbw_status(rbw)

    def rbw_configure(self, email: str, base_url: str) -> RbwStatus:
        with self._lock:
            ok = self._apply_rbw_config(email=email, base_url=base_url)
            status = self.rbw_status()
            if not ok:
                return RbwStatus(
                    installed=status.installed,
                    configured=status.configured,
                    unlocked=status.unlocked,
                    email=status.email,
                    base_url=status.base_url,
                    message="rbw configuration failed",
                )
            return status

    def rbw_unlock(self) -> RbwStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            rbw = self._manager.get_backend("rbw")
            if rbw is None:
                raise self._unavailable("rbw is unavailable")
            # Native pinentry/agent owns secret entry — never drive it here.
            ok = self._run_safely(lambda: rbw._run("unlock"))
            return _rbw_status(rbw, message="" if ok else "rbw unlock failed")

    def rbw_sync(self) -> RbwStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            rbw = self._manager.get_backend("rbw")
            if rbw is None:
                raise self._unavailable("rbw is unavailable")
            ok = self._run_safely(lambda: rbw._run("sync"))
            return _rbw_status(rbw, message="" if ok else "rbw sync failed")

    def rbw_lock(self) -> RbwStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            rbw = self._manager.get_backend("rbw")
            if rbw is not None:
                command_ok = self._run_safely(lambda: rbw._run("lock"))
                cache_ok = self._safe(lambda: rbw.lock()) is not False
            else:
                command_ok = False
                cache_ok = True
            self._clear_cached_manifests()
            ok = command_ok and cache_ok
            return _rbw_status(rbw, message="" if ok else "rbw lock failed")

    # ------------------------------------------------------------------
    # KeePassXC lifecycle
    # ------------------------------------------------------------------

    def keepassxc_create_database(
        self,
        path: str,
        *,
        keyfile: Optional[str] = None,
        owner_client_id,
    ) -> SecretOperationResult:
        """Create a new ``.kdbx`` database at ``path``.

        The master password is collected through a protected interaction — it
        never appears in RPC parameters and is cleared immediately after use.
        """
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            backend = self._manager.get_backend("keepassxc")
            if backend is None or not self._safe(lambda: backend.is_available()):
                return SecretOperationResult(
                    state=SecretOperationState.FAILED,
                    backend="keepassxc",
                    message="KeePassXC is unavailable (pykeepass is not installed)",
                )
            path = (path or "").strip()
            if not path:
                return SecretOperationResult(
                    state=SecretOperationState.FAILED,
                    backend="keepassxc",
                    message="A database path is required",
                )
            password = self._prompt_for_secret(
                "Enter a master password for the new KeePass database",
                owner_client_id=owner_client_id,
            )
            if password is None:
                return SecretOperationResult(
                    state=SecretOperationState.INTERACTION_REQUIRED,
                    backend="keepassxc",
                    message="Database creation cancelled",
                )
            try:
                password_text = password.decode("utf-8", "replace")
            finally:
                _clear_secret(password)
            try:
                ok = backend.create_database(
                    path, password_text, keyfile=(keyfile or None)
                )
                # Mirror the GUI "create and unlock in one step": the password is
                # in hand, so unlock so it isn't asked again.
                if ok and not self._safe_is_unlocked(backend):
                    try:
                        ok = bool(backend.unlock(password_text))
                    except Exception:
                        logger.debug("KDBX auto-unlock after create failed", exc_info=True)
                        ok = False
            except Exception:
                logger.debug("KDBX database creation failed", exc_info=True)
                ok = False
            finally:
                password_text = ""
            return SecretOperationResult(
                state=(
                    SecretOperationState.SUCCESS
                    if ok else SecretOperationState.FAILED
                ),
                backend="keepassxc",
                message="" if ok else "The KeePass database could not be created or unlocked",
            )

    def keepassxc_unlock(self, *, owner_client_id) -> SecretOperationResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            backend = self._manager.get_backend("keepassxc")
            if backend is None:
                return SecretOperationResult(
                    state=SecretOperationState.FAILED,
                    backend="keepassxc",
                    message="KeePassXC is unavailable",
                )
            if not self._safe_is_unlocked(backend):
                master = self._remembered_master_password()
                if master is None:
                    secret = self._prompt_for_secret(
                        "Enter the master password to unlock the KeePass database",
                        owner_client_id=owner_client_id,
                    )
                    if secret is None:
                        return SecretOperationResult(
                            state=SecretOperationState.INTERACTION_REQUIRED,
                            backend="keepassxc",
                            message="Unlock cancelled",
                        )
                    try:
                        master = secret.decode("utf-8", "replace")
                    finally:
                        _clear_secret(secret)
                try:
                    ok = self._safe(lambda: backend.unlock(master))
                finally:
                    master = ""  # drop the protected value after use
                if not ok:
                    return SecretOperationResult(
                        state=SecretOperationState.FAILED,
                        backend="keepassxc",
                        message="The KeePass database could not be unlocked",
                    )
            return SecretOperationResult(
                state=SecretOperationState.SUCCESS,
                backend="keepassxc",
            )

    def keepassxc_lock(self) -> SecretOperationResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            backend = self._manager.get_backend("keepassxc")
            if backend is not None:
                try:
                    backend.lock()
                except Exception:
                    logger.debug("KDBX lock failed", exc_info=True)
            self._clear_cached_manifests()
            return SecretOperationResult(
                state=SecretOperationState.SUCCESS,
                backend="keepassxc",
            )

    # ------------------------------------------------------------------
    # Remembered master password (platform keyring, daemon-owned)
    # ------------------------------------------------------------------

    def remember_master_password(self, *, owner_client_id) -> SecretOperationResult:
        """Save the selected vault's master password in the OS keyring and enable
        the ``remember_in_keyring`` policy.

        The password is collected through a protected interaction, stored with
        the existing ``master_password_spec`` identity, and never returned.
        Caller holds no lock yet — this is the standalone "remember later" RPC;
        :meth:`unlock` calls :meth:`_store_master_password` directly instead,
        reusing the password just collected for unlock so it never re-prompts.
        """
        with self._lock:
            self._apply_environment(self._load_strict())
            backend = self._manager.selected_backend()
            name = getattr(backend, "name", "none") or "none"
            if backend is None or not getattr(backend, "session_backed", False):
                return SecretOperationResult(
                    state=SecretOperationState.FAILED,
                    backend=name,
                    message="Only session-backed vaults can remember their master password",
                )
            secret = self._prompt_for_secret(
                f"Enter the master password to remember for {name}",
                owner_client_id=owner_client_id,
            )
            if secret is None:
                return SecretOperationResult(
                    state=SecretOperationState.INTERACTION_REQUIRED,
                    backend=name,
                    message="Remember cancelled",
                )
            try:
                password = secret.decode("utf-8", "replace")
            finally:
                _clear_secret(secret)
            try:
                return self._store_master_password(name, password)
            finally:
                password = ""

    def _store_master_password(self, name: str, password: str) -> SecretOperationResult:
        """Save *password* (already known — no interaction here) in the OS
        keyring under the selected vault's ``master_password_spec`` identity,
        and enable the ``remember_in_keyring`` policy. Caller holds ``self._lock``."""
        semantic = self._normalize(self._load_strict())
        spec = None
        stored = False
        try:
            from sshpilot.secret_storage import selected_master_spec

            spec = selected_master_spec(self._manager)
            stored = self._manager.store_in_keyring(spec, password)
        except Exception:
            logger.debug("Remembering master password failed")
            stored = False
        if not stored:
            return SecretOperationResult(
                state=SecretOperationState.FAILED,
                backend=name,
                message="The master password could not be saved",
            )
        if not semantic["remember_in_keyring"]:
            try:
                self.update_configuration(
                    UpdateSecretConfigurationRequest(
                        patch={"remember_in_keyring": True}
                    )
                )
            except SshPilotError:
                logger.debug("Enabling remember_in_keyring policy failed")
                # Best-effort rollback so keyring state and policy do not
                # intentionally diverge: the password was stored but the
                # policy could not be enabled, so remove it again.
                if spec is not None:
                    try:
                        self._manager.delete_in_keyring(spec)
                    except Exception:
                        logger.debug(
                            "Rolling back remembered master password failed"
                        )
                return SecretOperationResult(
                    state=SecretOperationState.FAILED,
                    backend=name,
                    message="The master password could not be remembered",
                )
        return SecretOperationResult(
            state=SecretOperationState.SUCCESS,
            backend=name,
            message="",
        )

    def forget_master_password(self) -> SecretOperationResult:
        """Remove the selected vault's master password from the OS keyring and
        clear the ``remember_in_keyring`` policy."""
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            backend = self._manager.selected_backend()
            name = getattr(backend, "name", "none") or "none"
            removed = False
            delete_failed = False
            try:
                from sshpilot.secret_storage import selected_master_spec

                removed = self._manager.delete_in_keyring(
                    selected_master_spec(self._manager)
                )
            except Exception:
                logger.debug("Forgetting master password failed")
                delete_failed = True
            if delete_failed:
                # The keyring still holds the value; leave the policy untouched
                # so keyring state and policy do not diverge.
                return SecretOperationResult(
                    state=SecretOperationState.FAILED,
                    backend=name,
                    message="The remembered master password could not be removed",
                )
            semantic = self._normalize(config)
            policy_was_on = bool(semantic["remember_in_keyring"])
            policy_cleared = True
            if policy_was_on:
                try:
                    self.update_configuration(
                        UpdateSecretConfigurationRequest(
                            patch={"remember_in_keyring": False}
                        )
                    )
                except SshPilotError:
                    logger.debug("Clearing remember_in_keyring policy failed")
                    policy_cleared = False
            if policy_was_on and not policy_cleared:
                # The keyring value is gone but the policy could not be
                # persisted off; report failure so the user can retry.
                return SecretOperationResult(
                    state=SecretOperationState.FAILED,
                    backend=name,
                    message="The remembered master password could not be forgotten",
                )
            return SecretOperationResult(
                state=SecretOperationState.SUCCESS,
                backend=name,
                message="" if removed else "No remembered master password was found",
            )

    def _remembered_master_password(self) -> Optional[str]:
        """The keyring-saved master password when the ``remember_in_keyring``
        policy is on, else ``None``. The caller owns clearing the returned value.
        """
        config = self._load_strict()
        semantic = self._normalize(config)
        if not semantic["remember_in_keyring"]:
            return None
        try:
            from sshpilot.secret_storage import selected_master_spec

            return self._manager.lookup_in_keyring(
                selected_master_spec(self._manager)
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Secret transfer (export / import) — runs inside the daemon
    # ------------------------------------------------------------------

    def export_backup(
        self,
        *,
        destination: str,
        connection_ids: Optional[List[str]] = None,
        options: Optional[Dict[str, Any]] = None,
        mirror_logins: bool = False,
        owner_client_id,
    ) -> SecretTransferResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            opts = dict(options or {})
            passphrase = None
            if opts.get("encrypted"):
                from sshpilot.daemon.interaction_broker import (
                    DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT,
                )

                prompt, state = self._prompt_for_secret_with_status(
                    "Enter a passphrase to encrypt the backup",
                    owner_client_id=owner_client_id,
                    timeout=DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT,
                )
                if prompt is None:
                    message = (
                        "Encryption password request timed out"
                        if state is InteractionState.EXPIRED
                        else "Encryption cancelled"
                    )
                    return SecretTransferResult(
                        operation="export",
                        path=destination,
                        counts={},
                        warnings=(),
                        status=SecretOperationState.INTERACTION_REQUIRED,
                        message=message,
                    )
                passphrase = prompt.decode("utf-8", "replace")
                _clear_secret(prompt)
            from sshpilot.daemon.secret_transfer import (
                daemon_export_backup,
            )

            return daemon_export_backup(
                self._manager,
                destination=destination,
                connection_ids=connection_ids,
                options=opts,
                mirror_logins=mirror_logins,
                connections_source=self._connections_source,
                passphrase=passphrase,
                settings_path=self._path,
                connection_store_snapshot=self._connection_store_snapshot,
            )

    def preview_backup(
        self,
        *,
        source: str,
        options: Optional[Dict[str, Any]] = None,
        owner_client_id,
    ) -> Dict[str, Any]:
        """Inspect a backup file: kind, encryption flag, and included categories.

        Metadata only — the frontend uses it to build the import-mode dialog.
        For an encrypted ``.spbk`` the passphrase is collected through a
        protected interaction; the decrypted manifest is cached so the
        following import never re-prompts.
        """
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            from sshpilot.daemon.secret_transfer import daemon_preview_backup

            public, manifest = daemon_preview_backup(
                self._manager, source=source, settings_path=self._path
            )
            if manifest is None and public.get("encrypted") and not public.get("error"):
                prompt = self._prompt_for_secret(
                    "Enter the passphrase to decrypt the backup",
                    owner_client_id=owner_client_id,
                )
                if prompt is None:
                    return {**public, "error": "Decryption cancelled"}
                passphrase = prompt.decode("utf-8", "replace")
                _clear_secret(prompt)
                public, manifest = daemon_preview_backup(
                    self._manager,
                    source=source,
                    passphrase=passphrase,
                    settings_path=self._path,
                )
            if manifest is not None:
                self._cache_manifest(self._manifest_key("file", source), manifest)
            return public

    def preview_bitwarden_backup(
        self,
        *,
        entry_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Preview one Bitwarden backup note: included categories (metadata only)."""
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            from sshpilot.daemon.secret_transfer import daemon_preview_bitwarden_backup

            public, manifest = daemon_preview_bitwarden_backup(
                self._manager, entry_id=entry_id, settings_path=self._path
            )
            if manifest is not None:
                self._cache_manifest(self._manifest_key("bw", entry_id), manifest)
            return public

    def preview_ssh_backup(
        self,
        *,
        connection_id: str,
        remote_dir: str,
        entry_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Preview one SSH-stored backup: included categories (metadata only)."""
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            from sshpilot.daemon.secret_transfer import daemon_preview_ssh_backup

            public, manifest = daemon_preview_ssh_backup(
                self._manager,
                connection_id=connection_id,
                remote_dir=remote_dir,
                entry_id=entry_id,
                connections_source=self._connections_source,
                settings_path=self._path,
            )
            if manifest is not None:
                self._cache_manifest(
                    self._manifest_key("ssh", f"{connection_id}:{entry_id}"), manifest
                )
            return public

    def import_backup(
        self,
        *,
        source: str,
        options: Optional[Dict[str, Any]] = None,
        owner_client_id,
    ) -> SecretTransferResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            opts = dict(options or {})
            manifest = self._pop_cached_manifest(self._manifest_key("file", source))
            passphrase = None
            needs_prompt = bool(opts.get("encrypted"))
            if manifest is None and not needs_prompt:
                # The daemon decides: prompt only for genuinely encrypted .spbk.
                try:
                    from sshpilot.backup_archive import is_spbk, spbk_is_encrypted

                    src = os.path.expanduser(source)
                    needs_prompt = bool(is_spbk(src) and spbk_is_encrypted(src))
                except Exception:
                    needs_prompt = False
            from sshpilot.daemon.secret_transfer import (
                daemon_import_backup,
            )

            # A wrong passphrase is retried through fresh protected interactions,
            # bounded so a corrupt file cannot loop forever.
            result = None
            for attempt in range(self._MAX_IMPORT_PASSPHRASE_ATTEMPTS):
                if manifest is None and (needs_prompt or attempt > 0):
                    prompt = self._prompt_for_secret(
                        "Enter the passphrase to decrypt the backup",
                        owner_client_id=owner_client_id,
                    )
                    if prompt is None:
                        return SecretTransferResult(
                            operation="import",
                            path=source,
                            counts={},
                            warnings=(),
                            status=SecretOperationState.INTERACTION_REQUIRED,
                            message="Decryption cancelled",
                        )
                    passphrase = prompt.decode("utf-8", "replace")
                    _clear_secret(prompt)
                result = daemon_import_backup(
                    self._manager,
                    source=source,
                    options=opts,
                    passphrase=passphrase,
                    settings_path=self._path,
                    manifest=manifest,
                    connection_store_restore=self._connection_store_restore,
                )
                last_attempt = attempt + 1 >= self._MAX_IMPORT_PASSPHRASE_ATTEMPTS
                if (
                    result.status is SecretOperationState.FAILED
                    and passphrase is not None
                    and "passphrase" in (result.message or "").lower()
                    and not last_attempt
                ):
                    passphrase = None
                    needs_prompt = True
                    continue
                return result
            return result

    # -- manifest preview cache (import preview -> apply, one passphrase prompt) --

    def _manifest_key(self, kind: str, value: str) -> str:
        if kind == "file":
            return "file:{}".format(os.path.abspath(os.path.expanduser(value)))
        return "{}:{}".format(kind, value)

    def _cache_manifest(self, key: str, manifest: Dict[str, Any]) -> None:
        """Store a decrypted preview manifest and arm its real expiry timer.

        Replacing an entry cancels the previous expiry callback so a stale
        timer can never clear the newer manifest.
        """
        self._cancel_manifest_timer(key)
        self._manifest_cache[key] = manifest

        holder: Dict[str, Timer] = {}

        def _expire() -> None:
            with self._lock:
                # Only the *current* timer for the key may clear it; a replaced
                # or already-popped entry must not be removed by a stale timer.
                if self._manifest_timers.get(key) is not holder.get("timer"):
                    return
                self._manifest_timers.pop(key, None)
                self._manifest_cache.pop(key, None)

        timer = Timer(self._MANIFEST_CACHE_TTL, _expire)
        timer.daemon = True
        holder["timer"] = timer
        self._manifest_timers[key] = timer
        timer.start()

    def _cancel_manifest_timer(self, key: str) -> None:
        timer = self._manifest_timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _cached_manifest(self, key: str) -> Optional[Dict[str, Any]]:
        # The expiry timer owns removal; this is a plain read.
        return self._manifest_cache.get(key)

    def _pop_cached_manifest(self, key: str) -> Optional[Dict[str, Any]]:
        """One-time import consume: return the manifest and clear the entry."""
        manifest = self._cached_manifest(key)
        self._cancel_manifest_timer(key)
        self._manifest_cache.pop(key, None)
        return manifest

    def _clear_cached_manifests(self) -> None:
        """Cancel every expiry timer and drop all cached manifests.

        Called on every lock route and on daemon shutdown so decrypted
        manifests never outlive the unlocked session.
        """
        for key in list(self._manifest_timers):
            self._cancel_manifest_timer(key)
        self._manifest_cache.clear()

    def shutdown(self) -> None:
        """Clear cached decrypted manifests (daemon exit hook)."""
        with self._lock:
            self._clear_cached_manifests()

    def list_bitwarden_backups(self) -> List[Dict[str, str]]:
        """List Bitwarden backup-note metadata (id/name/date only)."""
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            from sshpilot.daemon.secret_transfer import daemon_list_bitwarden_backups

            return daemon_list_bitwarden_backups(self._manager)

    def list_ssh_backups(
        self,
        *,
        connection_id: str,
        remote_dir: str,
    ) -> List[Dict[str, str]]:
        """List sshPilot backups stored on one of the user's SSH servers."""
        with self._lock:
            self._load_strict()
            from sshpilot.daemon.secret_transfer import daemon_list_ssh_backups

            return daemon_list_ssh_backups(
                self._manager,
                connection_id=connection_id,
                remote_dir=remote_dir,
                connections_source=self._connections_source,
                settings_path=self._path,
            )

    def import_ssh_backup(
        self,
        *,
        connection_id: str,
        remote_dir: str,
        entry_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> SecretTransferResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            from sshpilot.daemon.secret_transfer import daemon_import_ssh_backup

            manifest = self._pop_cached_manifest(
                self._manifest_key("ssh", f"{connection_id}:{entry_id}")
            )
            return daemon_import_ssh_backup(
                self._manager,
                connection_id=connection_id,
                remote_dir=remote_dir,
                entry_id=entry_id,
                options=options,
                connections_source=self._connections_source,
                settings_path=self._path,
                manifest=manifest,
                connection_store_restore=self._connection_store_restore,
            )

    def import_bitwarden_backup(
        self,
        *,
        entry_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> SecretTransferResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            from sshpilot.daemon.secret_transfer import daemon_import_bitwarden_backup

            manifest = self._pop_cached_manifest(self._manifest_key("bw", entry_id))
            return daemon_import_bitwarden_backup(
                self._manager,
                entry_id=entry_id,
                options=options,
                settings_path=self._path,
                manifest=manifest,
                connection_store_restore=self._connection_store_restore,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _field_to_config_key(self, field: str) -> str:
        try:
            return FIELD_TO_CONFIG_KEY[field]
        except KeyError:
            raise ValueError(f"unknown secret configuration field {field!r}") from None

    def _load_strict(self) -> Dict[str, Any]:
        try:
            config, _migrated = load_settings_strict(self._path)
        except SettingsFileError as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The secret settings file could not be read",
                details={"code": SETTINGS_MALFORMED},
            ) from exc
        if not isinstance(config, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The secret settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        self._normalize(config)
        return config

    def _normalize(self, config: Dict[str, Any]) -> Dict[str, Any]:
        secrets = config.get("secrets", {})
        if not isinstance(secrets, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The secret settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        try:
            return normalize_secret_settings(secrets)
        except (TypeError, ValueError) as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The secret settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            ) from exc

    def _write_semantic(
        self,
        config: Dict[str, Any],
        semantic: Dict[str, Any],
    ) -> None:
        secrets = config.setdefault("secrets", {})
        if not isinstance(secrets, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The secret settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        for field, value in semantic.items():
            raw_key = FIELD_TO_CONFIG_KEY[field].split(".", 1)[1]
            _set_nested_raw(secrets, raw_key, value)

    def _snapshot(self, config: Dict[str, Any]) -> SecretConfiguration:
        semantic = self._normalize(config)
        return SecretConfiguration(
            revision=compute_secret_settings_revision(semantic),
            backend=semantic["backend"],
            session_timeout=semantic["session_timeout"],
            remember_in_keyring=semantic["remember_in_keyring"],
            bitwarden_profile=semantic["bitwarden_profile"],
            bitwarden_server=semantic["bitwarden_server"],
            keepassxc_database=semantic["keepassxc_database"],
            keepassxc_keyfile=semantic["keepassxc_keyfile"],
        )

    def _apply_environment(self, config: Dict[str, Any]) -> None:
        """Mirror the ``secrets.*`` configuration into this process's environment.

        The backends read their paths/profiles from env (e.g. the KDBX backend
        reads ``SSHPILOT_KDBX_DATABASE``); the daemon owns those values now and
        applies them to its own process so spawned subprocesses inherit them.
        """
        semantic = self._normalize(config)
        selected = semantic["backend"]
        self._manager.set_selected(selected)
        if selected == "auto":
            os.environ.pop("SSHPILOT_SECRET_BACKEND", None)
        else:
            os.environ["SSHPILOT_SECRET_BACKEND"] = selected
        _apply_profile_env("BITWARDENCLI_APPDATA_DIR", semantic["bitwarden_profile"])
        _apply_profile_env("SSHPILOT_KDBX_DATABASE", semantic["keepassxc_database"])
        _apply_profile_env("SSHPILOT_KDBX_KEYFILE", semantic["keepassxc_keyfile"])
        timeout_seconds = int(max(0, semantic["session_timeout"]) * 60)
        if timeout_seconds > 0:
            os.environ["SSHPILOT_SECRET_SESSION_TIMEOUT"] = str(timeout_seconds)
        else:
            os.environ.pop("SSHPILOT_SECRET_SESSION_TIMEOUT", None)

    def _configure_bitwarden_server(self, url: str) -> bool:
        bw = self._manager.get_backend("bitwarden")
        if bw is None:
            raise self._unavailable("Bitwarden is unavailable")
        return self._run_safely(lambda: bw._run(["config", "server", url]))

    def _selected_decision(self):
        backend = self._manager.selected_backend()
        if backend is None:
            from sshpilot.core.secrets import SecretPolicyDecision
            return SecretPolicyDecision(
                kind=SecretDecisionKind.READY,
                backend="auto",
            )
        return decide_unlock(
            backend=backend.name,
            session_backed=bool(getattr(backend, "session_backed", False)),
            is_unlocked=bool(self._safe_is_unlocked(backend)),
            available=bool(self._safe(lambda: backend.is_available())),
        )

    def _selected_needs_login(self) -> bool:
        backend = self._manager.selected_backend()
        probe = getattr(backend, "needs_login", None)
        if backend is None or not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def _descriptor(
        self,
        name: str,
        *,
        selected: bool,
        available: bool,
    ) -> SecretBackendDescriptor:
        backend = self._manager.get_backend(name)
        session_backed = bool(
            backend is not None and getattr(backend, "session_backed", False)
        )
        is_unlocked = bool(
            self._safe_is_unlocked(backend) if backend is not None else False
        )
        decision = decide_unlock(
            backend=name,
            session_backed=session_backed,
            is_unlocked=is_unlocked,
            available=available,
        )
        locked = decision.kind == SecretDecisionKind.UNLOCK_REQUIRED
        login_required = False
        if backend is not None and name == "bitwarden" and callable(
            getattr(backend, "needs_login", None)
        ):
            try:
                login_required = bool(backend.needs_login())
            except Exception:
                login_required = False
        label = _backend_label(name)
        capabilities = _backend_capabilities(name)
        diagnostic = ""
        if backend is not None and hasattr(backend, "describe"):
            try:
                diagnostic = str(backend.describe())
            except Exception:
                diagnostic = ""
        return SecretBackendDescriptor(
            name=name,
            label=label,
            available=available,
            selected=selected,
            session_backed=session_backed,
            locked=locked,
            needs_unlock=locked,
            login_required=login_required,
            persists_secrets=name != "agent",
            capabilities=capabilities,
            diagnostic=diagnostic,
        )

    def _prompt_for_secret(self, message: str, *, owner_client_id) -> Optional[bytearray]:
        """Create a protected PASSWORD interaction and wait for the secret.

        Returns the secret as a bytearray (the caller must clear it), or ``None``
        when the interaction was cancelled, expired, or the broker is unavailable.
        See :meth:`_prompt_for_secret_with_status` for why this is routed
        through ``request_client_secret`` rather than a bare create() +
        wait_for_result().
        """
        secret, _state = self._prompt_for_secret_with_status(
            message, owner_client_id=owner_client_id
        )
        return secret

    def _prompt_for_secret_with_status(
        self,
        message: str,
        *,
        owner_client_id,
        timeout: Optional[float] = None,
    ) -> Tuple[Optional[bytearray], InteractionState]:
        """Like :meth:`_prompt_for_secret`, but also returns the interaction's
        final :class:`InteractionState` so callers can tell an explicit user
        cancellation apart from a timed-out prompt, and can request a
        shorter-than-default ``timeout`` for a self-contained prompt (e.g.
        the backup encryption passphrase) without touching the timeout used
        by every other secret prompt.

        Routed through ``request_client_secret_with_status`` (not a bare
        ``create()`` + ``wait_for_result()``): the server only forwards
        INTERACTION_CREATED/STATE_CHANGED events for a synthetic
        ``secret-session-N`` scope to a client that is registered as that
        scope's owner (see ``DaemonServer._client_can_interact``) — a
        session id with no recognized prefix and no registered owner is
        invisible to every client, so a bare create()+wait_for_result() call
        creates an interaction nobody is ever told about: it just silently
        expires at ``DEFAULT_SECRET_INTERACTION_TIMEOUT`` with no dialog ever
        shown, no matter how many clients are connected.
        """
        if self._broker is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Protected secret interactions are unavailable",
            )
        session_id = _next_secret_session_id()
        connection_id = ConnectionId(f"secret-{session_id}")
        return self._broker.request_client_secret_with_status(
            owner_client_id=owner_client_id,
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=InteractionType.PASSWORD,
            prompt=PasswordPrompt(
                username="Secret backend",
                hostname=message or "secret backend",
                port=22,
                attempt=1,
                can_remember=False,
                stored_secret_available=False,
            ),
            timeout=timeout,
        )

    def _prompt_for_master_password(
        self, backend_name: str, *, owner_client_id
    ) -> Tuple[Optional[bytearray], bool]:
        """Create a protected master-password unlock interaction and wait for it.

        Distinct from :meth:`_prompt_for_secret`: ``hostname`` carries only the
        bare backend name (e.g. ``"keepassxc"``) instead of a free-text
        sentence, so the GTK secrets presenter can show a correctly-labeled
        "Unlock {backend}" dialog instead of reusing the SSH host-login
        password dialog's ``{username}@{hostname}`` template. ``can_remember``
        is set so that dialog also offers a "Remember master password"
        checkbox. Returns ``(secret, remember_requested)`` — ``secret`` is
        ``None`` when cancelled/expired; the caller must clear it. See
        :meth:`_prompt_for_secret` for why this goes through
        ``request_client_secret_with_remember`` rather than a bare create().
        """
        if self._broker is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Protected secret interactions are unavailable",
            )
        session_id = _next_secret_session_id()
        connection_id = ConnectionId(f"secret-{session_id}")
        secret, remember_policy = self._broker.request_client_secret_with_remember(
            owner_client_id=owner_client_id,
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=InteractionType.PASSWORD,
            prompt=PasswordPrompt(
                # Unused by the GTK secrets presenter's dedicated master-password
                # dialog — it only reads ``hostname`` (the bare backend name).
                # Must still be a non-empty safe-display string per the model.
                username="Secret backend",
                hostname=backend_name or "vault",
                port=22,
                attempt=1,
                can_remember=True,
                stored_secret_available=False,
            ),
        )
        remember = remember_policy in (
            RememberPolicy.STORE_AFTER_SUCCESS,
            RememberPolicy.REPLACE_STORED_AFTER_SUCCESS,
        )
        return secret, remember

    def _bitwarden_login_with_password(
        self,
        bw: Any,
        email: str,
        password: str,
        *,
        twofa_method: Optional[str],
        twofa_code: Optional[str],
        auth_client_secret: Optional[str],
    ) -> Tuple[bool, str, bool]:
        try:
            return bw.login_with_password(
                email,
                password,
                twofa_method=twofa_method,
                twofa_code=twofa_code,
                auth_client_secret=auth_client_secret,
            )
        except Exception:
            logger.debug("Bitwarden password login failed", exc_info=True)
            return False, "Bitwarden password login failed", False

    def _safe_is_unlocked(self, backend: Any) -> bool:
        return bool(self._safe(lambda: backend.is_unlocked()))

    @staticmethod
    def _safe(fn, default: Any = False):
        try:
            return fn()
        except Exception:
            return default

    @staticmethod
    def _run_safely(fn) -> bool:
        try:
            result = fn()
            return getattr(result, "returncode", 1) == 0
        except Exception:
            return False

    def _unavailable(self, message: str) -> SshPilotError:
        return SshPilotError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            message,
            details={"code": BACKEND_UNAVAILABLE},
        )

    def _apply_rbw_config(self, email: str, base_url: str) -> bool:
        rbw = self._manager.get_backend("rbw")
        if rbw is None:
            raise self._unavailable("rbw is unavailable")
        email = (email or "").strip()
        base_url = (base_url or "").strip()
        ok = True
        if email:
            ok = self._run_safely(
                lambda: rbw._run("config", "set", "email", email)
            ) and ok
        if base_url:
            ok = self._run_safely(
                lambda: rbw._run("config", "set", "base_url", base_url)
            ) and ok
        else:
            ok = self._run_safely(
                lambda: rbw._run("config", "unset", "base_url")
            ) and ok
        return ok


def _clear_secret(secret: bytearray) -> None:
    try:
        secret[:] = b"\0" * len(secret)
        secret.clear()
    except Exception:
        pass


def _login_needs_challenge(detail: str) -> bool:
    """True when a failed ``bw login`` reports an authentication challenge (a
    bot-detection / auth-challenge prompt), which the CLI satisfies with the
    account's API-key client secret."""
    lower = (detail or "").lower()
    return any(
        token in lower
        for token in ("bot", "authentication challenge", "auth challenge")
    )


def _apply_profile_env(key: str, value: str) -> None:
    path = (value or "").strip()
    if path:
        os.environ[key] = os.path.expanduser(path)
    else:
        os.environ.pop(key, None)


def _server_url(config: Dict[str, Any]) -> str:
    secrets = config.get("secrets", {})
    if isinstance(secrets, dict):
        value = secrets.get("bitwarden", {}).get("server")
        if isinstance(value, str):
            return value
    return ""


def _profile(config: Dict[str, Any]) -> str:
    secrets = config.get("secrets", {})
    if isinstance(secrets, dict):
        value = secrets.get("bitwarden", {}).get("profile")
        if isinstance(value, str):
            return value
    return ""


def _set_nested_raw(container: Dict[str, Any], dotted_key: str, value: Any) -> None:
    """Set a dotted key relative to a *container* dict (no leading namespace)."""
    parts = dotted_key.split(".")
    cur = container
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _backend_label(name: str) -> str:
    labels = {
        "libsecret": "Libsecret",
        "keyring": "OS keyring",
        "pass": "pass",
        "bitwarden": "Bitwarden",
        "rbw": "rbw",
        "keepassxc": "KeePassXC",
        "agent": "SSH agent",
    }
    return labels.get(name, name)


def _backend_capabilities(name: str) -> Tuple[str, ...]:
    if name == "bitwarden":
        return ("login", "unlock", "lock", "sync", "logout", "configure_server")
    if name == "rbw":
        return ("configure", "unlock", "sync", "lock")
    if name == "keepassxc":
        return ("unlock", "lock", "create_database")
    return ("store", "lookup", "delete")


def _bitwarden_status(
    bw: Any,
    *,
    force_refresh: bool = False,
    message: str = "",
) -> BitwardenStatus:
    if bw is None or not bool(bw.is_available()):
        return BitwardenStatus(
            logged_in=False, unlocked=False, needs_login=True,
            email="", server_url="", profile="", message=message,
        )
    unlocked = bool(bw.is_unlocked())
    if unlocked:
        needs_login = False
    else:
        try:
            needs_login = bool(
                bw.needs_login(force_refresh=force_refresh)
            )
        except Exception:
            needs_login = True
    return BitwardenStatus(
        logged_in=not needs_login,
        unlocked=unlocked,
        needs_login=needs_login,
        email="",
        server_url="",
        profile="",
        message=message,
    )


def _rbw_status(rbw: Any, *, message: str = "") -> RbwStatus:
    installed = bool(rbw is not None and rbw.is_available())
    if not installed:
        return RbwStatus(
            installed=False, configured=False, unlocked=False,
            email="", base_url="", message=message or "rbw is not installed",
        )
    unlocked = bool(rbw.is_unlocked())
    try:
        configured = bool(rbw._run("config", "get", "email").returncode == 0)
    except Exception:
        configured = False
    email = ""
    base_url = ""
    try:
        res = rbw._run("config", "get", "email")
        if res.returncode == 0:
            email = res.stdout.decode("utf-8", "replace").strip()
    except Exception:
        pass
    try:
        res = rbw._run("config", "get", "base_url")
        if res.returncode == 0:
            base_url = res.stdout.decode("utf-8", "replace").strip()
    except Exception:
        pass
    return RbwStatus(
        installed=True,
        configured=configured,
        unlocked=unlocked,
        email=email,
        base_url=base_url,
        message=message,
    )
