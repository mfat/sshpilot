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
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.interactions import (
    InteractionType,
    PasswordPrompt,
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
)

logger = logging.getLogger(__name__)

# Reserved interaction session namespace.  Interactions created by this service
# use synthetic session ids under this prefix; the GTK secrets presenter filters
# on it and the session-scoped interaction dialogs ignore it.
SECRET_SESSION_PREFIX = "secret-session"
_SECRET_SESSION_COUNTER = 0
_SECRET_SESSION_LOCK = Lock()

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
    ) -> None:
        self._path = Path(settings_path)
        if secret_manager is None:
            from sshpilot.secret_storage import get_secret_manager

            secret_manager = get_secret_manager()
        self._manager = secret_manager
        self._broker = interaction_broker
        self._connections_source = connections_source
        self._lock = Lock()

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

    def update_selection(self, backend: str) -> SecretBackendState:
        self.update_configuration(
            UpdateSecretConfigurationRequest(
                patch={"backend": normalize_backend_name(backend)}
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

    def unlock(self) -> SecretUnlockResult:
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
                secret = self._prompt_for_secret(
                    f"Enter the master password to unlock {name}"
                )
                if secret is None:
                    return SecretUnlockResult(
                        kind=UnlockResultKind.INTERACTION_REQUIRED,
                        backend=name,
                        message="Unlock cancelled",
                    )
                try:
                    ok = self._manager.unlock_selected(
                        secret.decode("utf-8", "replace")
                    )
                finally:
                    _clear_secret(secret)
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
            config = self._load_strict()
            self._apply_environment(config)
            self._configure_bitwarden_server(str(url or "").strip())
            semantic = self._normalize(config)
            semantic["bitwarden_server"] = str(url or "").strip()
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
                f"Enter the Bitwarden master password for {email}"
            )
            if password is None:
                return BitwardenStatus(
                    logged_in=False, unlocked=False, needs_login=True,
                    email=email, server_url="", profile="",
                    twofa_required=False, message="Login cancelled",
                )
            password_text = password.decode("utf-8", "replace")
            _clear_secret(password)

            # Auth-challenge client secret is only collected when the backend
            # sign-in later reports it is required.
            ok, detail, needs_2fa = self._bitwarden_login_with_password(
                bw, email, password_text, twofa_method=twofa_method, twofa_code=None,
            )

            if needs_2fa and twofa_method:
                code = self._prompt_for_secret(
                    f"Enter the two-step login code for {email}"
                )
                if code is None:
                    return BitwardenStatus(
                        logged_in=False, unlocked=False, needs_login=True,
                        email=email, server_url=_server_url(config),
                        profile=_profile(config),
                        twofa_required=True, message="Two-step login cancelled",
                    )
                ok, detail, needs_2fa = self._bitwarden_login_with_password(
                    bw, email, password_text, twofa_method=twofa_method,
                    twofa_code=code.decode("utf-8", "replace"),
                )
                _clear_secret(code)

            # Mirror the CLI "log in and unlock in one step": a successful login
            # can still leave the vault locked, so unlock with the password we have.
            if ok and password_text and not self._safe_is_unlocked(bw):
                self._safe(lambda: bw.unlock(password_text))

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

    def bitwarden_api_key_login(self, client_id: str) -> BitwardenStatus:
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
                f"Enter the API key client secret for {client_id}"
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
                ok, detail = bw.login_with_api_key(client_id, secret_text)
            except Exception as exc:
                logger.debug("Bitwarden API-key login failed", exc_info=True)
                ok, detail = False, str(exc)
            return BitwardenStatus(
                logged_in=ok,
                unlocked=self._safe_is_unlocked(bw),
                needs_login=not ok,
                email=client_id,
                server_url=_server_url(config),
                profile=_profile(config),
                message=detail if not ok else "",
            )

    def bitwarden_sso_login(self) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            try:
                ok, detail = bw.login_with_sso()
            except Exception as exc:
                logger.debug("Bitwarden SSO login failed", exc_info=True)
                ok, detail = False, str(exc)
            return BitwardenStatus(
                logged_in=ok,
                unlocked=self._safe_is_unlocked(bw),
                needs_login=not ok,
                email="",
                server_url=_server_url(config),
                profile=_profile(config),
                message=detail if not ok else "",
            )

    def bitwarden_unlock(self) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            if self._safe_is_unlocked(bw):
                return _bitwarden_status(bw)
            secret = self._prompt_for_secret(
                "Enter the Bitwarden master password to unlock the vault"
            )
            if secret is None:
                return _bitwarden_status(bw, message="Unlock cancelled")
            secret_text = secret.decode("utf-8", "replace")
            _clear_secret(secret)
            try:
                bw.unlock(secret_text)
            except Exception:
                logger.debug("Bitwarden unlock failed", exc_info=True)
            return _bitwarden_status(bw)

    def bitwarden_sync(self) -> BitwardenStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            bw = self._manager.get_backend("bitwarden")
            if bw is None:
                raise self._unavailable("Bitwarden is unavailable")
            try:
                self._safe(lambda: bw._run(["sync"]))
            except Exception:
                logger.debug("Bitwarden sync failed", exc_info=True)
            return _bitwarden_status(bw)

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
            self._apply_rbw_config(email=email, base_url=base_url)
            return self.rbw_status()

    def rbw_unlock(self) -> RbwStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            rbw = self._manager.get_backend("rbw")
            if rbw is None:
                raise self._unavailable("rbw is unavailable")
            try:
                # Native pinentry/agent owns secret entry — never drive it here.
                self._safe(lambda: rbw._run("unlock"))
            except Exception:
                logger.debug("rbw unlock trigger failed", exc_info=True)
            return _rbw_status(rbw)

    def rbw_sync(self) -> RbwStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            rbw = self._manager.get_backend("rbw")
            if rbw is None:
                raise self._unavailable("rbw is unavailable")
            try:
                self._safe(lambda: rbw._run("sync"))
            except Exception:
                logger.debug("rbw sync failed", exc_info=True)
            return _rbw_status(rbw)

    def rbw_lock(self) -> RbwStatus:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            rbw = self._manager.get_backend("rbw")
            if rbw is not None:
                try:
                    rbw._run("lock")
                except Exception:
                    logger.debug("rbw lock failed", exc_info=True)
                try:
                    rbw.lock()
                except Exception:
                    logger.debug("rbw cache clear failed", exc_info=True)
            return _rbw_status(rbw)

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
    ) -> SecretTransferResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            opts = dict(options or {})
            passphrase = None
            if opts.get("encrypted"):
                prompt = self._prompt_for_secret(
                    "Enter a passphrase to encrypt the backup"
                )
                if prompt is None:
                    return SecretTransferResult(
                        operation="export",
                        path=destination,
                        counts={},
                        warnings=(),
                        status=SecretOperationState.INTERACTION_REQUIRED,
                        message="Encryption cancelled",
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
            )

    def import_backup(
        self,
        *,
        source: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> SecretTransferResult:
        with self._lock:
            config = self._load_strict()
            self._apply_environment(config)
            opts = dict(options or {})
            passphrase = None
            if opts.get("encrypted"):
                prompt = self._prompt_for_secret(
                    "Enter the passphrase to decrypt the backup"
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
            from sshpilot.daemon.secret_transfer import (
                daemon_import_backup,
            )

            return daemon_import_backup(
                self._manager,
                source=source,
                options=opts,
                passphrase=passphrase,
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

    def _configure_bitwarden_server(self, url: str) -> None:
        bw = self._manager.get_backend("bitwarden")
        if bw is None:
            raise self._unavailable("Bitwarden is unavailable")
        try:
            self._safe(lambda: bw._run(["config", "server", url]))
        except Exception as exc:
            logger.debug("bw config server failed: %s", exc)

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

    def _prompt_for_secret(self, message: str) -> Optional[bytearray]:
        """Create a protected PASSWORD interaction and wait for the secret.

        Returns the secret as a bytearray (the caller must clear it), or ``None``
        when the interaction was cancelled, expired, or the broker is unavailable.
        """
        if self._broker is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Protected secret interactions are unavailable",
            )
        session_id = _next_secret_session_id()
        connection_id = ConnectionId(f"secret-{session_id}")
        summary = self._broker.create(
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
            attempt=1,
            timeout=DEFAULT_SECRET_INTERACTION_TIMEOUT,
        )
        result = self._broker.wait_for_result(summary.id)
        if result is None or result.secret is None:
            return None
        # Ownership of the bytearray transfers to the caller, who MUST clear it.
        secret = result.secret
        result.secret = None
        return secret

    def _bitwarden_login_with_password(
        self,
        bw: Any,
        email: str,
        password: str,
        *,
        twofa_method: Optional[str],
        twofa_code: Optional[str],
    ) -> Tuple[bool, str, bool]:
        try:
            return bw.login_with_password(
                email,
                password,
                twofa_method=twofa_method,
                twofa_code=twofa_code,
                auth_client_secret=None,
            )
        except Exception as exc:
            logger.debug("Bitwarden password login failed", exc_info=True)
            return False, str(exc), False

    def _safe_is_unlocked(self, backend: Any) -> bool:
        return bool(self._safe(lambda: backend.is_unlocked()))

    @staticmethod
    def _safe(fn, default: Any = False):
        try:
            return fn()
        except Exception:
            return default

    def _unavailable(self, message: str) -> SshPilotError:
        return SshPilotError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            message,
            details={"code": BACKEND_UNAVAILABLE},
        )

    def _apply_rbw_config(self, email: str, base_url: str) -> None:
        rbw = self._manager.get_backend("rbw")
        if rbw is None:
            raise self._unavailable("rbw is unavailable")
        email = (email or "").strip()
        base_url = (base_url or "").strip()
        if email:
            self._safe(lambda: rbw._run("config", "set", "email", email))
        if base_url:
            self._safe(lambda: rbw._run("config", "set", "base_url", base_url))


def _clear_secret(secret: bytearray) -> None:
    try:
        secret[:] = b"\0" * len(secret)
        secret.clear()
    except Exception:
        pass


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


def _rbw_status(rbw: Any) -> RbwStatus:
    installed = bool(rbw is not None and rbw.is_available())
    if not installed:
        return RbwStatus(
            installed=False, configured=False, unlocked=False,
            email="", base_url="", message="rbw is not installed",
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
    )
