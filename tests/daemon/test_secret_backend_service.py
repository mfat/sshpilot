"""Tests for the daemon-owned ``SecretBackendService``.

Covers the service's deterministic reentrancy (deadlock regressions), the
``secrets.*`` configuration contract with revisions, registry/state metadata,
every Bitwarden / rbw / KeePassXC lifecycle route, remembered-password
behavior, the authentication-challenge retry, and sentinel-secret absence from
every public surface.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from sshpilot.api.errors import SshPilotError
from sshpilot.api.models.secrets import (
    REVISION_CONFLICT,
    SecretOperationState,
    UnlockResultKind,
)
from sshpilot.daemon.secret_backend_service import (
    SecretBackendService,
    _login_needs_challenge,
)

# Sentinel secrets: if any of these ever appear in a DTO, a serialized
# response, an error detail, a log line or an operation result, the secrecy
# contract is broken.
SENTINEL_MASTER = "SENTINEL_MASTER_9f2a"
SENTINEL_2FA = "SENTINEL_2FA_77b1"
SENTINEL_CLIENT_SECRET = "SENTINEL_CLIENT_SECRET_c4e0"
SENTINEL_CHALLENGE = "SENTINEL_CHALLENGE_3d81"


class FakeBackend:
    """Session-backed fake backend recording every lifecycle call."""

    name = "fake"

    def __init__(self, name: str, *, available: bool = True,
                 session_backed: bool = True, unlocked: bool = False,
                 needs_login: bool = False) -> None:
        self.name = name
        self.session_backed = session_backed
        self._available = available
        self._unlocked = unlocked
        self._needs_login = needs_login
        self._configured = False
        self.calls: List[tuple] = []
        self.login_results: Dict[tuple, Any] = {}
        self.data: Dict[str, str] = {}

    # -- backend surface used by the service ----------------------------
    def describe(self) -> str:
        return f"fake-{self.name}"

    def is_available(self) -> bool:
        return self._available

    def is_discoverable(self) -> bool:
        return self._available

    def is_unlocked(self) -> bool:
        return self._unlocked

    def needs_login(self, *, force_refresh: bool = False) -> bool:
        return self._needs_login

    def unlock(self, secret: str) -> bool:
        self.calls.append(("unlock", secret))
        if secret == "wrong-password":
            return False
        self._unlocked = True
        self._needs_login = False
        return True

    def lock(self) -> None:
        self.calls.append(("lock",))
        self._unlocked = False

    def logout(self) -> None:
        self.calls.append(("logout",))
        self._unlocked = False
        self._needs_login = True

    def _run(self, *args: Any) -> "SimpleNamespace":
        self.calls.append(("_run", args))
        flat = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
        flat = tuple(flat)
        if flat and flat[0] == "unlock":
            self._unlocked = True
            self._needs_login = False
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if flat and flat[0] == "lock":
            self._unlocked = False
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if len(flat) >= 2 and flat[0] == "config":
            if flat[1] == "set":
                self._configured = True
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            if flat[1] == "get":
                key = flat[2] if len(flat) > 2 else ""
                if self._configured and key == "email":
                    return SimpleNamespace(returncode=0, stdout=b"alice@example.com\n", stderr=b"")
                if self._configured and key == "base_url":
                    return SimpleNamespace(returncode=0, stdout=b"https://vault.example.com\n", stderr=b"")
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def create_database(self, path: str, password: str, keyfile: Optional[str] = None) -> bool:
        self.calls.append(("create_database", path, password, keyfile))
        return True

    # -- Bitwarden login surface ----------------------------------------
    def login_with_password(self, email, password, *, twofa_method=None,
                            twofa_code=None, auth_client_secret=None):
        key = ("login_with_password", email, twofa_method, twofa_code, auth_client_secret)
        if key in self.login_results:
            return self.login_results[key]
        return True, "", False

    def login_with_api_key(self, client_id, client_secret):
        key = ("api_key", client_id, client_secret)
        if key in self.login_results:
            return self.login_results[key]
        return True, ""

    def login_with_sso(self, identifier=None):
        self.calls.append(("sso", identifier))
        return True, ""

    # -- keyring helpers (platform keyring simulation) ------------------
    def store_in_keyring(self, spec, secret) -> bool:
        self.calls.append(("store_in_keyring", spec.keyring_account, secret))
        self.data[spec.keyring_account] = secret
        return True

    def lookup_in_keyring(self, spec) -> Optional[str]:
        return self.data.get(spec.keyring_account)

    def delete_in_keyring(self, spec) -> bool:
        return self.data.pop(spec.keyring_account, None) is not None


class FakeManager:
    """Minimal ``SecretManager``-compatible surface the service touches."""

    def __init__(self, backends: Dict[str, FakeBackend], selected: str = "auto") -> None:
        self._backends = backends
        self._selected = selected
        self.active_backend_name = next(
            (n for n, b in backends.items() if b.is_available()), None
        ) or "none"

    def registered_backends(self) -> List[str]:
        return list(self._backends)

    def available_backends(self, *, cheap: bool = False) -> List[str]:
        return [n for n, b in self._backends.items() if b.is_available()]

    def get_backend(self, name: str) -> Optional[FakeBackend]:
        return self._backends.get((name or "").strip().lower())

    def set_selected(self, name: Optional[str]) -> None:
        self._selected = name or "auto"

    def selected_backend(self) -> Optional[FakeBackend]:
        if self._selected in (None, "", "auto"):
            return None
        return self._backends.get(self._selected)

    def persists_secrets(self) -> bool:
        backend = self.selected_backend()
        return backend is None or backend.name != "agent"

    def unlock_selected(self, secret: str, progress=None) -> bool:
        backend = self.selected_backend()
        if backend is None or not backend.session_backed:
            return True
        return backend.unlock(secret)

    def store_in_keyring(self, spec, secret: str) -> bool:
        for backend in self._backends.values():
            if getattr(backend, "name", "") in ("libsecret", "keyring"):
                if backend.store_in_keyring(spec, secret):
                    return True
        return False

    def lookup_in_keyring(self, spec) -> Optional[str]:
        for backend in self._backends.values():
            if getattr(backend, "name", "") in ("libsecret", "keyring"):
                value = backend.lookup_in_keyring(spec)
                if value:
                    return value
        return None

    def delete_in_keyring(self, spec) -> bool:
        removed = False
        for backend in self._backends.values():
            if getattr(backend, "name", "") in ("libsecret", "keyring"):
                if backend.delete_in_keyring(spec):
                    removed = True
        return removed


class FakeBroker:
    """Interaction broker returning scripted secrets (as bytearrays)."""

    def __init__(self, secrets: Optional[List[str]] = None) -> None:
        self._secrets: List[str] = list(secrets or [])
        self._counter = 0
        self.created: List[Any] = []

    def create(self, **kwargs) -> Any:
        self._counter += 1
        summary = SimpleNamespace(
            id=f"inter-{self._counter}",
            session_id=kwargs.get("session_id"),
        )
        self.created.append((summary.id, kwargs))
        return summary

    def wait_for_result(self, interaction_id: str) -> Any:
        if not self._secrets:
            return None
        value = self._secrets.pop(0)
        return SimpleNamespace(secret=bytearray(value.encode("utf-8")))

    def request_client_secret(self, *, owner_client_id, **kwargs) -> Any:
        summary = self.create(**kwargs)
        result = self.wait_for_result(summary.id)
        return None if result is None else result.secret

    def request_client_secret_with_remember(self, *, owner_client_id, **kwargs) -> Any:
        from sshpilot.api.models import RememberPolicy

        summary = self.create(**kwargs)
        result = self.wait_for_result(summary.id)
        if result is None:
            return None, RememberPolicy.DO_NOT_STORE
        return result.secret, getattr(
            result, "remember_policy", RememberPolicy.DO_NOT_STORE
        )


def _write_settings(path: Path, secrets: Optional[Dict[str, Any]] = None) -> Path:
    config = {
        "config_version": 3,
        "secrets": secrets or {
            "backend": "auto",
            "session_timeout": 0,
            "remember_in_keyring": False,
            "bitwarden": {"profile": "", "server": ""},
            "keepassxc": {"database": "", "keyfile": ""},
        },
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _make_service(tmp_path, *, secrets=None, backends=None, broker=None,
                  selected="auto", expected_secrets=None):
    path = _write_settings(tmp_path / "config.json", secrets)
    if backends is None:
        backends = {
            "libsecret": FakeBackend("libsecret", session_backed=False),
            "keyring": FakeBackend("keyring", session_backed=False),
            "bitwarden": FakeBackend("bitwarden", needs_login=True),
            "rbw": FakeBackend("rbw", needs_login=True),
            "keepassxc": FakeBackend("keepassxc"),
            "agent": FakeBackend("agent", session_backed=False),
        }
    manager = FakeManager(backends, selected=selected)
    if broker is None:
        broker = FakeBroker(expected_secrets or [])
    service = SecretBackendService(
        path, secret_manager=manager, interaction_broker=broker
    )
    return service, manager, backends, broker, path


def _all_strings(value: Any, out: Optional[List[str]] = None) -> List[str]:
    out = out if out is not None else []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _all_strings(v, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _all_strings(item, out)
    return out


# ---------------------------------------------------------------------------
# Deadlock regressions (timeout-bounded)
# ---------------------------------------------------------------------------

def _assert_returns_within(fn, timeout: float = 3.0):
    result: List[Any] = []
    error: List[BaseException] = []

    def _run():
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            error.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "operation deadlocked (re-entrant lock)"
    if error:
        raise error[0]
    return result[0]


def test_bitwarden_configure_server_does_not_deadlock(tmp_path):
    """``bitwarden_configure_server`` re-enters the lock via ``bitwarden_status``."""
    service, manager, backends, broker, _ = _make_service(
        tmp_path, secrets={"backend": "bitwarden", "session_timeout": 0}
    )
    status = _assert_returns_within(
        lambda: service.bitwarden_configure_server("https://vault.example.com")
    )
    assert status.logged_in is False  # needs_login backend
    assert any(
        args == (["config", "server", "https://vault.example.com"],)
        for _kind, args in backends["bitwarden"].calls
        if _kind == "_run"
    )


def test_native_command_failures_are_not_treated_as_success(tmp_path):
    service, _manager, backends, _broker, path = _make_service(
        tmp_path, secrets={"backend": "bitwarden", "session_timeout": 0}
    )

    def failed(*_args):
        return SimpleNamespace(returncode=1, stdout=b"secret output", stderr=b"failed")

    backends["bitwarden"]._run = failed
    status = service.bitwarden_configure_server("https://vault.example.com")
    assert status.message == "Bitwarden server configuration failed"
    assert service.get_configuration().bitwarden_server == ""
    assert service.bitwarden_sync().message == "Bitwarden sync failed"

    rbw_path = tmp_path / "rbw"
    rbw_path.mkdir()
    service, _manager, backends, _broker, _path = _make_service(
        rbw_path, secrets={"backend": "rbw", "session_timeout": 0}
    )
    backends["rbw"]._run = failed
    assert service.rbw_configure("alice@example.com", "https://vault.example.com").message == (
        "rbw configuration failed"
    )
    assert service.rbw_unlock().message == "rbw unlock failed"
    assert service.rbw_sync().message == "rbw sync failed"
    assert service.rbw_lock().message == "rbw lock failed"


def test_rbw_configure_uses_exact_set_and_unset_argv(tmp_path):
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path, secrets={"backend": "rbw", "session_timeout": 0}
    )
    backend = backends["rbw"]

    service.rbw_configure("alice@example.com", "https://vault.example.com")
    run_calls = [args for kind, args in backend.calls if kind == "_run"]
    assert run_calls[:2] == [
        ("config", "set", "email", "alice@example.com"),
        ("config", "set", "base_url", "https://vault.example.com"),
    ]

    backend.calls.clear()
    service.rbw_configure("alice@example.com", "")
    run_calls = [args for kind, args in backend.calls if kind == "_run"]
    assert run_calls[:2] == [
        ("config", "set", "email", "alice@example.com"),
        ("config", "unset", "base_url"),
    ]


def test_rbw_configure_unset_failure_returns_typed_failure(tmp_path):
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path, secrets={"backend": "rbw", "session_timeout": 0}
    )
    backend = backends["rbw"]
    original_run = backend._run

    def run(*args):
        if args == ("config", "unset", "base_url"):
            backend.calls.append(("_run", args))
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"failed")
        return original_run(*args)

    backend._run = run
    status = service.rbw_configure("alice@example.com", "")

    assert status.message == "rbw configuration failed"
    assert ("_run", ("config", "unset", "base_url")) in backend.calls


def test_rbw_configure_does_not_deadlock(tmp_path):
    """``rbw_configure`` re-enters the lock via ``rbw_status``."""
    service, manager, backends, broker, _ = _make_service(
        tmp_path, secrets={"backend": "rbw", "session_timeout": 0}
    )
    status = _assert_returns_within(
        lambda: service.rbw_configure("alice@example.com", "https://vault.example.com")
    )
    assert status.installed is True


def test_update_selection_reenters_lock(tmp_path):
    """``update_selection`` composes ``update_configuration`` + ``get_state``."""
    service, *_ = _make_service(tmp_path)
    state = _assert_returns_within(lambda: service.update_selection("bitwarden"))
    assert state.selected_backend == "bitwarden"


def test_all_lifecycle_routes_return_within_timeout(tmp_path):
    """Every public lifecycle route returns (no nested-lock deadlock)."""
    service, manager, backends, broker, _ = _make_service(tmp_path)
    routes = [
        lambda: service.get_configuration(),
        lambda: service.get_registry(),
        lambda: service.get_state(),
        lambda: service.lock(),
        lambda: service.bitwarden_status(),
        lambda: service.bitwarden_lock(),
        lambda: service.bitwarden_logout(),
        lambda: service.bitwarden_sync(),
        lambda: service.rbw_status(),
        lambda: service.rbw_lock(),
        lambda: service.rbw_sync(),
        lambda: service.keepassxc_lock(),
        lambda: service.forget_master_password(),
    ]
    for route in routes:
        _assert_returns_within(route)


# ---------------------------------------------------------------------------
# Configuration + revisions
# ---------------------------------------------------------------------------

def test_configuration_snapshot_and_revision(tmp_path):
    service, *_ = _make_service(tmp_path)
    config = service.get_configuration()
    assert config.backend == "auto"
    assert config.revision  # deterministic 12-hex token
    # No-op update keeps the same revision.
    again = service.update_configuration(_update_req(patch={"session_timeout": 0}))
    assert again.revision == config.revision


def _update_req(patch, expected_revision=None):
    from sshpilot.api.models.secrets import UpdateSecretConfigurationRequest

    return UpdateSecretConfigurationRequest(
        patch=patch, expected_revision=expected_revision
    )


def test_configuration_update_bumps_revision(tmp_path):
    service, *_ = _make_service(tmp_path)
    before = service.get_configuration()
    after = service.update_configuration(_update_req(patch={"session_timeout": 30}))
    assert after.session_timeout == 30
    assert after.revision != before.revision
    assert service.get_configuration().revision == after.revision


def test_configuration_update_rejects_unknown_field(tmp_path):
    service, *_ = _make_service(tmp_path)
    with pytest.raises(ValueError):
        service.update_configuration(_update_req(patch={"bogus": 1}))


def test_configuration_revision_conflict_raises(tmp_path):
    service, *_ = _make_service(tmp_path)
    before = service.get_configuration()
    service.update_configuration(_update_req(patch={"session_timeout": 10}))
    with pytest.raises(SshPilotError) as exc_info:
        service.update_configuration(
            _update_req(
                patch={"session_timeout": 20}, expected_revision=before.revision
            )
        )
    assert exc_info.value.details.get("code") == REVISION_CONFLICT


def test_selection_mutation_is_revision_safe(tmp_path):
    service, manager, *_ = _make_service(tmp_path)
    before = service.get_configuration()
    # A concurrent mutation bumps the revision…
    service.update_configuration(_update_req(patch={"session_timeout": 15}))
    # …so a selection update against the stale revision is rejected.
    with pytest.raises(SshPilotError) as exc_info:
        service.update_selection(
            "bitwarden", expected_revision=before.revision
        )
    assert exc_info.value.details.get("code") == REVISION_CONFLICT
    # With the current revision the selection applies.
    current = service.get_configuration()
    state = service.update_selection(
        "bitwarden", expected_revision=current.revision
    )
    assert state.selected_backend == "bitwarden"
    assert service.get_configuration().backend == "bitwarden"


def test_revision_conflict_does_not_mutate_settings(tmp_path):
    service, *_ = _make_service(tmp_path)
    before = service.get_configuration()
    service.update_configuration(_update_req(patch={"session_timeout": 10}))
    with pytest.raises(SshPilotError):
        service.update_configuration(
            _update_req(
                patch={"remember_in_keyring": True},
                expected_revision=before.revision,
            )
        )
    persisted = service.get_configuration()
    assert persisted.remember_in_keyring is False
    assert persisted.session_timeout == 10


# ---------------------------------------------------------------------------
# Registry / state
# ---------------------------------------------------------------------------

def test_registry_lists_descriptors_and_availability(tmp_path):
    service, *_ = _make_service(tmp_path)
    registry = service.get_registry()
    names = {b.name for b in registry.backends}
    assert {"libsecret", "bitwarden", "rbw", "keepassxc", "agent"} <= names
    bitwarden = next(b for b in registry.backends if b.name == "bitwarden")
    assert bitwarden.capabilities == (
        "login", "unlock", "lock", "sync", "logout", "configure_server"
    )
    assert bitwarden.login_required is True
    keepassxc = next(b for b in registry.backends if b.name == "keepassxc")
    assert keepassxc.capabilities == ("unlock", "lock", "create_database")


def test_registry_selected_flag_follows_config(tmp_path):
    service, *_ = _make_service(
        tmp_path, secrets={"backend": "keepassxc", "session_timeout": 0}
    )
    registry = service.get_registry()
    keepassxc = next(b for b in registry.backends if b.name == "keepassxc")
    assert keepassxc.selected is True
    assert registry.selected_backend == "keepassxc"


def test_state_reports_lock_and_login(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path, secrets={"backend": "bitwarden", "session_timeout": 0}
    )
    state = service.get_state()
    assert state.selected_backend == "bitwarden"
    assert state.login_required is True  # fake needs_login=True
    assert state.locked is True


def test_unlock_returns_login_required_for_unauthenticated_vault(tmp_path):
    service, *_ = _make_service(tmp_path, secrets={"backend": "bitwarden"})
    result = service.unlock(owner_client_id="client-1")
    assert result.kind == UnlockResultKind.LOGIN_REQUIRED


def test_unlock_prompts_and_unlocks(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    backends["bitwarden"]._needs_login = False
    result = service.unlock(owner_client_id="client-1")
    assert result.kind == UnlockResultKind.UNLOCKED
    assert backends["bitwarden"]._unlocked is True


class _OwnerOnlyBroker:
    """Implements only request_client_secret_with_remember — no create()/
    wait_for_result(). If the service ever regresses to calling those
    directly (the old bug: an interaction visible to no client, since
    nothing registered a direct-scope owner), this raises AttributeError
    instead of silently degrading back to the invisible-interaction bug."""

    def __init__(self, secret: str, owner_client_id) -> None:
        self._secret = secret
        self._owner_client_id = owner_client_id
        self.calls: List[Any] = []

    def request_client_secret_with_remember(self, *, owner_client_id, **kwargs):
        from sshpilot.api.models import RememberPolicy

        assert owner_client_id == self._owner_client_id
        self.calls.append(kwargs)
        return bytearray(self._secret.encode("utf-8")), RememberPolicy.DO_NOT_STORE


def test_unlock_routes_through_the_client_scoped_broker_api(tmp_path):
    """Regression: unlock()'s prompt used to call broker.create() +
    wait_for_result() directly, which never registers a direct-scope owner —
    the daemon server then has no client to forward the interaction event
    to, so it silently expires and no dialog is ever shown, however many
    clients are connected. Using a broker that only implements
    request_client_secret_with_remember proves unlock() goes through the
    owner-registering path exclusively."""
    broker = _OwnerOnlyBroker(SENTINEL_MASTER, "client-42")
    service, manager, backends, _broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        broker=broker,
    )
    backends["bitwarden"]._needs_login = False
    result = service.unlock(owner_client_id="client-42")
    assert result.kind == UnlockResultKind.UNLOCKED
    assert backends["bitwarden"]._unlocked is True
    assert len(broker.calls) == 1


def test_unlock_uses_remembered_password_when_policy_on(tmp_path):
    keyring = FakeBackend("keyring", session_backed=False)
    backends = {
        "libsecret": FakeBackend("libsecret", session_backed=False),
        "keyring": keyring,
        "bitwarden": FakeBackend("bitwarden", needs_login=False),
        "rbw": FakeBackend("rbw", needs_login=True),
        "keepassxc": FakeBackend("keepassxc"),
        "agent": FakeBackend("agent", session_backed=False),
    }
    keyring.data["bitwarden-master:default"] = SENTINEL_MASTER
    service, manager, *_ = _make_service(
        tmp_path,
        backends=backends,
        secrets={"backend": "bitwarden", "remember_in_keyring": True},
        expected_secrets=[],
    )
    result = service.unlock(owner_client_id="client-1")
    assert result.kind == UnlockResultKind.UNLOCKED
    # No protected interaction was opened: the remembered password was used.
    assert manager._backends["bitwarden"]._unlocked is True


# ---------------------------------------------------------------------------
# Bitwarden lifecycle
# ---------------------------------------------------------------------------

def test_bitwarden_password_login_flow(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    backends["bitwarden"]._needs_login = True
    status = service.bitwarden_login("alice@example.com", owner_client_id="client-1")
    assert status.logged_in is True
    assert status.unlocked is True
    calls = backends["bitwarden"].calls
    assert any(kind == "unlock" for kind, *_ in calls)


def test_bitwarden_login_2fa_prompts_for_code(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER, SENTINEL_2FA],
    )
    bw = backends["bitwarden"]
    bw.login_results[("login_with_password", "alice@example.com", "0", None, None)] = (
        False, "Login failed.", True,
    )
    bw.login_results[("login_with_password", "alice@example.com", "0", SENTINEL_2FA, None)] = (
        True, "", False,
    )
    status = service.bitwarden_login("alice@example.com", twofa_method="0", owner_client_id="client-1")
    assert status.logged_in is True
    assert status.twofa_required is False


def test_bitwarden_auth_challenge_retries_with_client_secret(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER, SENTINEL_CHALLENGE],
    )
    bw = backends["bitwarden"]
    bw.login_results[("login_with_password", "alice@example.com", None, None, None)] = (
        False, "Authentication challenge required. Use your API key client secret.", False,
    )
    bw.login_results[
        ("login_with_password", "alice@example.com", None, None, SENTINEL_CHALLENGE)
    ] = (True, "", False)
    status = service.bitwarden_login("alice@example.com", owner_client_id="client-1")
    assert status.logged_in is True
    # The retry carried the protected client secret to the backend.
    assert any(
        key == (
            "login_with_password", "alice@example.com", None, None, SENTINEL_CHALLENGE
        )
        for key in bw.login_results
    )
    # The client secret never surfaces in the status DTO.
    assert SENTINEL_CHALLENGE not in _all_strings(status.to_dict())


def test_login_needs_challenge_detection():
    assert _login_needs_challenge("bot detected, authentication challenge")
    assert _login_needs_challenge("An authentication challenge is required")
    assert _login_needs_challenge("auth challenge: enter your client secret")
    assert not _login_needs_challenge("Login failed.")
    assert not _login_needs_challenge("Two-step login required")


def test_bitwarden_api_key_login_prompts_for_secret(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_CLIENT_SECRET],
    )
    status = service.bitwarden_api_key_login("user.abc123", owner_client_id="client-1")
    assert status.logged_in is True
    assert SENTINEL_CLIENT_SECRET not in _all_strings(status.to_dict())


def test_bitwarden_sso_login(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path, secrets={"backend": "bitwarden", "session_timeout": 0}
    )
    status = service.bitwarden_sso_login()
    assert status.logged_in is True
    assert any(kind == "sso" for kind, *_ in backends["bitwarden"].calls)


def test_bitwarden_unlock_sync_lock_logout(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    backends["bitwarden"]._needs_login = False
    assert service.bitwarden_unlock(owner_client_id="client-1").unlocked is True
    service.bitwarden_sync()
    status = service.bitwarden_lock()
    assert status.unlocked is False
    status = service.bitwarden_logout()
    assert status.needs_login is True


# ---------------------------------------------------------------------------
# rbw lifecycle
# ---------------------------------------------------------------------------

def test_rbw_status_configure_unlock_sync_lock(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path, secrets={"backend": "rbw", "session_timeout": 0}
    )
    status = service.rbw_status()
    assert status.installed is True
    assert status.configured is False

    status = service.rbw_configure("alice@example.com", "https://vault.example.com")
    assert status.email == "alice@example.com"
    calls = backends["rbw"].calls
    assert ("config", "set", "email", "alice@example.com") in [a for _k, a in calls if _k == "_run"]

    status = service.rbw_unlock()
    assert status.unlocked is True
    service.rbw_sync()
    status = service.rbw_lock()
    assert status.unlocked is False


# ---------------------------------------------------------------------------
# KeePassXC lifecycle
# ---------------------------------------------------------------------------

def test_keepassxc_create_database(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "keepassxc", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    result = service.keepassxc_create_database("/home/u/vault.kdbx", owner_client_id="client-1")
    assert result.state == SecretOperationState.SUCCESS
    calls = backends["keepassxc"].calls
    assert any(
        kind == "create_database" and args[0] == "/home/u/vault.kdbx"
        for kind, *args in calls
    )
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())


def test_keepassxc_unlock_and_lock(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "keepassxc", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    assert service.keepassxc_unlock(owner_client_id="client-1").state == SecretOperationState.SUCCESS
    assert backends["keepassxc"]._unlocked is True
    result = service.keepassxc_lock()
    assert result.state == SecretOperationState.SUCCESS
    assert backends["keepassxc"]._unlocked is False


# ---------------------------------------------------------------------------
# Remembered master password
# ---------------------------------------------------------------------------

def test_remember_master_password_stores_in_keyring_and_toggles_policy(tmp_path):
    keyring = FakeBackend("keyring", session_backed=False)
    backends = {
        "libsecret": FakeBackend("libsecret", session_backed=False),
        "keyring": keyring,
        "bitwarden": FakeBackend("bitwarden", needs_login=False),
        "rbw": FakeBackend("rbw", needs_login=True),
        "keepassxc": FakeBackend("keepassxc"),
        "agent": FakeBackend("agent", session_backed=False),
    }
    service, manager, *_ = _make_service(
        tmp_path,
        backends=backends,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    result = service.remember_master_password(owner_client_id="client-1")
    assert result.state == SecretOperationState.SUCCESS
    assert service.get_configuration().remember_in_keyring is True
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())


def test_forget_master_password_clears_keyring_and_policy(tmp_path):
    keyring = FakeBackend("keyring", session_backed=False)
    keyring.data["bitwarden-master:default"] = SENTINEL_MASTER
    backends = {
        "libsecret": FakeBackend("libsecret", session_backed=False),
        "keyring": keyring,
        "bitwarden": FakeBackend("bitwarden", needs_login=False),
        "rbw": FakeBackend("rbw", needs_login=True),
        "keepassxc": FakeBackend("keepassxc"),
        "agent": FakeBackend("agent", session_backed=False),
    }
    service, manager, *_ = _make_service(
        tmp_path,
        backends=backends,
        secrets={
            "backend": "bitwarden",
            "session_timeout": 0,
            "remember_in_keyring": True,
        },
    )
    result = service.forget_master_password()
    assert result.state == SecretOperationState.SUCCESS
    assert service.get_configuration().remember_in_keyring is False
    assert keyring.data.get("bitwarden-master:default") is None


# ---------------------------------------------------------------------------
# Remember/forget failure semantics (rollback, no partial success)
# ---------------------------------------------------------------------------

def _keyring_service(tmp_path, *, remember: bool = False, broker=None, expected_secrets=None):
    keyring = FakeBackend("keyring", session_backed=False)
    backends = {
        "keyring": keyring,
        "bitwarden": FakeBackend("bitwarden", needs_login=False),
    }
    service, manager, *_ = _make_service(
        tmp_path,
        backends=backends,
        secrets={
            "backend": "bitwarden",
            "session_timeout": 0,
            "remember_in_keyring": remember,
        },
        broker=broker,
        expected_secrets=expected_secrets,
    )
    return service, manager, keyring


def test_remember_master_password_rolls_back_when_policy_toggle_fails(
    tmp_path, monkeypatch, caplog
):
    """Keyring store succeeded but enabling the policy failed: the operation
    must report failure and roll the keyring value back so the two do not
    intentionally diverge."""
    service, manager, keyring = _keyring_service(
        tmp_path, expected_secrets=[SENTINEL_MASTER]
    )

    def _failing_save(path, config):
        raise OSError("disk full")

    monkeypatch.setattr(
        "sshpilot.daemon.secret_backend_service.save_settings", _failing_save)
    result = service.remember_master_password(owner_client_id="client-1")
    assert result.state == SecretOperationState.FAILED
    # Best-effort rollback removed the stored value again.
    assert keyring.data.get("bitwarden-master:default") is None
    assert service.get_configuration().remember_in_keyring is False
    # The remembered password never appears in the result or the logs.
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())
    assert SENTINEL_MASTER not in caplog.text


def test_remember_master_password_fails_when_keyring_store_fails(tmp_path):
    """Keyring storage failure must not report success."""
    class _FailingKeyring(FakeBackend):
        def store_in_keyring(self, spec, secret):
            self.calls.append(("store_in_keyring", spec.keyring_account, secret))
            raise OSError("keyring unavailable")

    keyring = _FailingKeyring("keyring", session_backed=False)
    backends = {
        "keyring": keyring,
        "bitwarden": FakeBackend("bitwarden", needs_login=False),
    }
    service, manager, *_ = _make_service(
        tmp_path,
        backends=backends,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    result = service.remember_master_password(owner_client_id="client-1")
    assert result.state == SecretOperationState.FAILED
    assert service.get_configuration().remember_in_keyring is False
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())


def test_forget_master_password_fails_when_keyring_delete_fails(tmp_path):
    """Keyring deletion failure must not report success, and the policy is
    left untouched so keyring state and policy do not diverge."""
    class _FailingKeyring(FakeBackend):
        def delete_in_keyring(self, spec):
            self.calls.append(("delete_in_keyring", spec.keyring_account))
            raise OSError("keyring unavailable")

    keyring = _FailingKeyring("keyring", session_backed=False)
    keyring.data["bitwarden-master:default"] = SENTINEL_MASTER
    backends = {
        "keyring": keyring,
        "bitwarden": FakeBackend("bitwarden", needs_login=False),
    }
    service, manager, *_ = _make_service(
        tmp_path,
        backends=backends,
        secrets={
            "backend": "bitwarden",
            "session_timeout": 0,
            "remember_in_keyring": True,
        },
    )
    result = service.forget_master_password()
    assert result.state == SecretOperationState.FAILED
    assert service.get_configuration().remember_in_keyring is True
    assert keyring.data.get("bitwarden-master:default") == SENTINEL_MASTER
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())


def test_forget_master_password_fails_when_policy_persistence_fails(
    tmp_path, monkeypatch
):
    """Keyring deletion succeeded but clearing the policy failed: the operation
    must report failure (a retry clears the policy)."""
    service, manager, keyring = _keyring_service(tmp_path, remember=True)
    keyring.data["bitwarden-master:default"] = SENTINEL_MASTER

    def _failing_save(path, config):
        raise OSError("disk full")

    monkeypatch.setattr(
        "sshpilot.daemon.secret_backend_service.save_settings", _failing_save)
    result = service.forget_master_password()
    assert result.state == SecretOperationState.FAILED
    assert "could not be forgotten" in result.message
    assert keyring.data.get("bitwarden-master:default") is None
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())


# ---------------------------------------------------------------------------
# Sentinel secrecy across the public surface
# ---------------------------------------------------------------------------

def test_sentinel_secrets_never_cross_public_surface(tmp_path):
    sentinels = [SENTINEL_MASTER, SENTINEL_2FA, SENTINEL_CLIENT_SECRET, SENTINEL_CHALLENGE]
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER, SENTINEL_2FA, SENTINEL_CLIENT_SECRET],
    )
    bw = backends["bitwarden"]
    bw.login_results[("login_with_password", "alice@example.com", "0", None, None)] = (
        False, "Login failed.", True,
    )
    bw.login_results[("login_with_password", "alice@example.com", "0", SENTINEL_2FA, None)] = (
        True, "", False,
    )
    bw.login_results[("api_key", "user.abc", SENTINEL_CLIENT_SECRET)] = (True, "")

    surfaces: List[str] = []
    surfaces.extend(_all_strings(service.get_configuration().to_dict()))
    registry = service.get_registry()
    for b in registry.backends:
        surfaces.extend(_all_strings(b.to_dict()))
    surfaces.extend(_all_strings(service.get_state().to_dict()))
    surfaces.extend(_all_strings(service.bitwarden_status().to_dict()))
    surfaces.extend(_all_strings(service.bitwarden_login("alice@example.com", twofa_method="0", owner_client_id="client-1").to_dict()))
    surfaces.extend(_all_strings(service.bitwarden_api_key_login("user.abc", owner_client_id="client-1").to_dict()))
    surfaces.extend(_all_strings(service.bitwarden_sso_login().to_dict()))
    surfaces.extend(_all_strings(service.bitwarden_unlock(owner_client_id="client-1").to_dict()))
    surfaces.extend(_all_strings(service.rbw_status().to_dict()))
    surfaces.extend(_all_strings(service.lock().to_dict()))
    surfaces.extend(_all_strings(service.unlock(owner_client_id="client-1").to_dict()))
    surfaces.extend(_all_strings(service.keepassxc_lock().to_dict()))
    surfaces.extend(_all_strings(service.forget_master_password().to_dict()))

    joined = "\n".join(surfaces)
    for sentinel in sentinels:
        assert sentinel not in joined, f"sentinel {sentinel} leaked into the public surface"


def test_unlock_result_never_carries_secret(tmp_path):
    service, manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    backends["bitwarden"]._needs_login = False
    result = service.unlock(owner_client_id="client-1")
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())
    assert result.kind == UnlockResultKind.UNLOCKED


def test_import_backup_retries_wrong_passphrase(tmp_path, monkeypatch):
    """A wrong .spbk passphrase re-prompts through a fresh protected interaction."""
    from sshpilot.api.models.secrets import SecretTransferResult

    class _ScriptedBroker:
        def __init__(self, secrets):
            self._secrets = list(secrets)
            self.created = []

        def create(self, **kwargs):
            self.created.append(kwargs)
            return SimpleNamespace(
                id="inter-{}".format(len(self.created)),
                session_id=kwargs.get("session_id"))

        def wait_for_result(self, _interaction_id):
            return SimpleNamespace(
                secret=bytearray(self._secrets.pop(0).encode("utf-8")))

        def request_client_secret(self, *, owner_client_id, **kwargs):
            summary = self.create(**kwargs)
            result = self.wait_for_result(summary.id)
            return None if result is None else result.secret

    calls = []

    def _fake_import(_manager, *, source, options, passphrase, settings_path, manifest):
        calls.append(passphrase)
        if passphrase == "wrong":
            return SecretTransferResult(
                operation="import", path=source, counts={}, warnings=(),
                status=SecretOperationState.FAILED,
                message="Wrong passphrase or corrupt backup",
            )
        return SecretTransferResult(
            operation="import", path=source,
            counts={"restored": 1}, warnings=(),
            status=SecretOperationState.SUCCESS, message="",
        )

    monkeypatch.setattr(
        "sshpilot.daemon.secret_transfer.daemon_import_backup", _fake_import)
    broker = _ScriptedBroker(["wrong", "s3cr3t"])
    service, _manager, _backends, _broker, _ = _make_service(
        tmp_path, broker=broker)
    result = service.import_backup(
        source="/tmp/never-written.spbk", options={"encrypted": True},
        owner_client_id="client-1")
    assert result.status == SecretOperationState.SUCCESS
    # Prompted once per attempt, each a distinct protected interaction; the
    # wrong passphrase never leaked into the returned result.
    assert calls == ["wrong", "s3cr3t"]
    assert len(broker.created) == 2
    assert result.counts == {"restored": 1}
    assert "wrong" not in _all_strings(result.to_dict())


def test_import_backup_gives_up_after_bounded_wrong_passphrases(tmp_path, monkeypatch):
    """Wrong-passphrase re-prompts are bounded; the daemon stops and reports."""
    from sshpilot.api.models.secrets import SecretTransferResult

    class _AlwaysWrongBroker:
        def __init__(self):
            self.prompts = 0

        def create(self, **kwargs):
            self.prompts += 1
            return SimpleNamespace(
                id="inter-{}".format(self.prompts),
                session_id=kwargs.get("session_id"))

        def wait_for_result(self, _interaction_id):
            return SimpleNamespace(secret=bytearray(b"still-wrong"))

        def request_client_secret(self, *, owner_client_id, **kwargs):
            summary = self.create(**kwargs)
            result = self.wait_for_result(summary.id)
            return None if result is None else result.secret

    def _fake_import(_manager, *, source, options, passphrase, settings_path, manifest):
        return SecretTransferResult(
            operation="import", path=source, counts={}, warnings=(),
            status=SecretOperationState.FAILED,
            message="Wrong passphrase or corrupt backup",
        )

    monkeypatch.setattr(
        "sshpilot.daemon.secret_transfer.daemon_import_backup", _fake_import)
    broker = _AlwaysWrongBroker()
    service, _manager, _backends, _broker, _ = _make_service(
        tmp_path, broker=broker)
    result = service.import_backup(
        source="/tmp/never-written.spbk", options={"encrypted": True},
        owner_client_id="client-1")
    assert result.status == SecretOperationState.FAILED
    assert broker.prompts == service._MAX_IMPORT_PASSPHRASE_ATTEMPTS
    assert "still-wrong" not in _all_strings(result.to_dict())


def test_manifest_expires_without_lookup(tmp_path):
    """A cached manifest is removed by its expiry timer even when no later API
    request touches the key (no passive TTL check needed)."""
    service, _manager, _backends, _broker, _ = _make_service(tmp_path)
    service._MANIFEST_CACHE_TTL = 0.05
    key = service._manifest_key("file", "/nonexistent/backup.spbk")
    manifest = {"credentials": [{"name": "alice"}]}
    service._cache_manifest(key, manifest)
    assert service._cached_manifest(key) == manifest
    # Wait beyond the TTL *without* calling _cached_manifest: the timer removes
    # the entry on its own.
    time.sleep(0.2)
    assert service._cached_manifest(key) is None
    assert not service._manifest_cache
    assert not service._manifest_timers


def test_replacing_entry_invalidates_previous_expiry(tmp_path):
    """A stale expiry timer must not clear a replaced manifest."""
    service, _manager, _backends, _broker, _ = _make_service(tmp_path)
    service._MANIFEST_CACHE_TTL = 0.05
    key = service._manifest_key("file", "/nonexistent/backup.spbk")
    service._cache_manifest(key, {"generation": 1})
    # Replacing the entry arms a new, longer timer; the first timer must be
    # invalidated so it cannot clear the replacement.
    service._MANIFEST_CACHE_TTL = 0.5
    service._cache_manifest(key, {"generation": 2})
    time.sleep(0.15)  # past the first timer, before the second
    assert service._cached_manifest(key) == {"generation": 2}
    # The replacement still expires on its own timer.
    time.sleep(0.5)
    assert service._cached_manifest(key) is None


def test_lock_clears_cached_manifests(tmp_path):
    """The generic lock route drops every decrypted preview manifest."""
    service, _manager, _backends, _broker, _ = _make_service(tmp_path)
    service._cache_manifest(
        service._manifest_key("file", "/nonexistent/backup.spbk"),
        {"credentials": []},
    )
    service.lock()
    assert not service._manifest_cache
    assert not service._manifest_timers


def test_backend_lock_routes_clear_cached_manifests(tmp_path):
    """Bitwarden, rbw and KeePassXC lock routes also clear cached manifests."""
    service, _manager, _backends, _broker, _ = _make_service(tmp_path)
    for method in ("bitwarden_lock", "rbw_lock", "keepassxc_lock"):
        service._cache_manifest(
            service._manifest_key("bw", "entry"), {"credentials": []}
        )
        getattr(service, method)()
        assert not service._manifest_cache, method
        assert not service._manifest_timers, method


def test_shutdown_clears_cached_manifests(tmp_path):
    """Daemon shutdown cancels timers and drops cached manifests."""
    service, _manager, _backends, _broker, _ = _make_service(tmp_path)
    service._cache_manifest(
        service._manifest_key("file", "/nonexistent/backup.spbk"),
        {"credentials": []},
    )
    service.shutdown()
    assert not service._manifest_cache
    assert not service._manifest_timers


def test_import_consume_clears_cached_manifest(tmp_path):
    """A one-time import pops the entry, so the cache ends empty."""
    service, _manager, _backends, _broker, _ = _make_service(tmp_path)
    key = service._manifest_key("file", "/nonexistent/backup.spbk")
    service._cache_manifest(key, {"credentials": []})
    assert service._pop_cached_manifest(key) == {"credentials": []}
    assert not service._manifest_cache
    assert not service._manifest_timers
