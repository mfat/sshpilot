"""Tests for the daemon-owned ``SecretBackendService``.

Covers the service's deterministic reentrancy (deadlock regressions), the
``secrets.*`` configuration contract with revisions, registry/state metadata,
every Bitwarden / rbw / KeePassXC lifecycle route, remembered-password
behavior, the authentication-challenge retry, and sentinel-secret absence from
every public surface.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models import SecretPromptKind
from sshpilot.api.models.secrets import (
    REVISION_CONFLICT,
    SecretMessageCode,
    SecretOperationState,
    SecretTransferMessageCode,
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


@pytest.fixture(autouse=True)
def _restore_secret_environment():
    """Undo the process-wide environment every service here writes.

    ``SecretBackendService._apply_environment`` mirrors the selected backend and
    the profile/database paths into ``os.environ`` (the daemon owns those, and
    spawned backends read them).  Without restoring them, a test that selects
    e.g. Bitwarden leaks that selection into the rest of the worker process, and
    a later test using the *real* secret manager (``askpass_utils`` passphrase
    storage) then stores against an unrelated backend and fails.
    """
    names = (
        "SSHPILOT_SECRET_BACKEND",
        "SSHPILOT_SECRET_SESSION_TIMEOUT",
        "BITWARDENCLI_APPDATA_DIR",
        "SSHPILOT_KDBX_DATABASE",
        "SSHPILOT_KDBX_KEYFILE",
    )
    saved = {name: os.environ.get(name) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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
        self._config: Dict[str, str] = {}
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
                if len(flat) >= 4:
                    self._config[str(flat[2])] = str(flat[3])
                    if flat[2] == "email":
                        self._configured = True
                else:
                    self._configured = True
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            if flat[1] == "unset":
                if len(flat) >= 3:
                    self._config.pop(str(flat[2]), None)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            if flat[1] == "show":
                payload = {
                    "email": self._config.get("email"),
                    "base_url": self._config.get("base_url"),
                }
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(payload).encode("utf-8"),
                    stderr=b"",
                )
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

    def request_client_secret_with_status(self, *, owner_client_id, **kwargs) -> Any:
        from sshpilot.api.models.interactions import InteractionState

        secret = self.request_client_secret(owner_client_id=owner_client_id, **kwargs)
        state = InteractionState.CANCELLED if secret is None else InteractionState.ANSWERED
        return secret, state


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
# Issue #1200: encrypted backup export — encrypted roundtrip, shorter
# operation-specific timeout, and cancellation vs. expiration messaging.
# ---------------------------------------------------------------------------

def test_export_backup_encrypted_roundtrip_decrypts_with_correct_password(tmp_path):
    """The full ``SecretBackendService.export_backup`` surface (interaction broker
    included, not just the lower ``daemon_export_backup`` helper): the produced
    ``.spbk`` is genuinely encrypted, decrypts with the correct passphrase, and a
    wrong passphrase fails cleanly instead of returning garbage."""
    from sshpilot.backup_archive import (
        SpbkPassphraseError,
        is_spbk,
        read_spbk,
        spbk_is_encrypted,
    )

    service, _manager, _backends, broker, _path = _make_service(
        tmp_path, expected_secrets=["s3cr3t"])
    dest = tmp_path / "encrypted.spbk"
    result = service.export_backup(
        destination=str(dest),
        options={"app_settings": True, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False, "encrypted": True},
        owner_client_id="client-1",
    )
    assert result.status.value == "success", result.message
    assert is_spbk(str(dest))
    assert spbk_is_encrypted(str(dest))
    # The passphrase went through the protected interaction, never as a param.
    assert broker.created and broker.created[0][1]["interaction_type"].value == "password"

    manifest = read_spbk(str(dest), "s3cr3t")
    assert "app_config" in manifest

    with pytest.raises(SpbkPassphraseError):
        read_spbk(str(dest), "wrong-password")


def test_export_backup_uses_shorter_backup_encryption_timeout(tmp_path):
    """The encryption-passphrase interaction must use the shorter, operation-
    specific backup-encryption timeout, not the general 120s secret timeout."""
    from sshpilot.daemon.interaction_broker import (
        DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT,
    )

    service, _manager, _backends, broker, _path = _make_service(
        tmp_path, expected_secrets=["s3cr3t"])
    dest = tmp_path / "encrypted.spbk"
    result = service.export_backup(
        destination=str(dest),
        options={"app_settings": True, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False, "encrypted": True},
        owner_client_id="client-1",
    )
    assert result.status.value == "success", result.message
    assert len(broker.created) == 1
    _interaction_id, kwargs = broker.created[0]
    assert kwargs.get("timeout") == DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT


def test_export_backup_reports_timeout_distinctly_from_cancellation(tmp_path):
    """A timed-out encryption prompt must not be reported with the same message
    as an explicit user cancellation (issue #1200: the old code always said
    'Encryption cancelled', even when the user never touched anything)."""
    from sshpilot.api.models.interactions import InteractionState

    class _ExpiredBroker(FakeBroker):
        def request_client_secret_with_status(self, *, owner_client_id, **kwargs):
            self.create(**kwargs)
            return None, InteractionState.EXPIRED

    service, _manager, _backends, _broker, _path = _make_service(
        tmp_path, broker=_ExpiredBroker())
    result = service.export_backup(
        destination=str(tmp_path / "never-written.spbk"),
        options={"app_settings": True, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False, "encrypted": True},
        owner_client_id="client-1",
    )
    assert result.status == SecretOperationState.INTERACTION_REQUIRED
    assert result.message.code is SecretTransferMessageCode.ENCRYPTION_REQUEST_TIMED_OUT


def test_export_backup_reports_explicit_cancellation(tmp_path):
    """An actual user cancellation must still read as a cancellation, distinct
    from the timeout message above."""
    from sshpilot.api.models.interactions import InteractionState

    class _CancelledBroker(FakeBroker):
        def request_client_secret_with_status(self, *, owner_client_id, **kwargs):
            self.create(**kwargs)
            return None, InteractionState.CANCELLED

    service, _manager, _backends, _broker, _path = _make_service(
        tmp_path, broker=_CancelledBroker())
    result = service.export_backup(
        destination=str(tmp_path / "never-written.spbk"),
        options={"app_settings": True, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False, "encrypted": True},
        owner_client_id="client-1",
    )
    assert result.status == SecretOperationState.INTERACTION_REQUIRED
    assert result.message.code is SecretTransferMessageCode.ENCRYPTION_CANCELLED


def test_export_backup_unencrypted_never_prompts(tmp_path):
    """Unencrypted export must not touch the interaction broker at all — it
    should be unaffected by any of the encrypted-export changes above."""
    service, _manager, _backends, broker, _path = _make_service(tmp_path)
    dest = tmp_path / "plain.spbk"
    result = service.export_backup(
        destination=str(dest),
        options={"app_settings": True, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False, "encrypted": False},
        owner_client_id="client-1",
    )
    assert result.status.value == "success", result.message
    assert broker.created == []

    from sshpilot.backup_archive import is_spbk, spbk_is_encrypted

    assert is_spbk(str(dest))
    assert not spbk_is_encrypted(str(dest))


def test_export_passphrase_prompt_does_not_block_metadata_state(tmp_path, monkeypatch):
    """Metadata requests must remain responsive while export waits for input.

    The export used to hold ``SecretBackendService._lock`` across the
    passphrase interaction.  A concurrent ``secrets.state.get`` then waited
    until the five-second client RPC timeout, which disconnected the peer and
    cancelled the export.
    """
    service, _manager, _backends, _broker, _path = _make_service(tmp_path)
    prompt_started = threading.Event()
    release_prompt = threading.Event()

    def _blocked_prompt(*_args, **_kwargs):
        prompt_started.set()
        assert release_prompt.wait(2.0)
        from sshpilot.api.models.interactions import InteractionState
        return None, InteractionState.CANCELLED

    monkeypatch.setattr(service, "_prompt_for_secret_with_status", _blocked_prompt)
    export_result = []
    export_thread = threading.Thread(
        target=lambda: export_result.append(service.export_backup(
            destination=str(tmp_path / "cancelled.spbk"),
            options={"encrypted": True},
            owner_client_id="client-1",
        ))
    )
    export_thread.start()
    assert prompt_started.wait(1.0)

    state_result = []
    state_thread = threading.Thread(target=lambda: state_result.append(service.get_state()))
    state_thread.start()
    state_thread.join(1.0)
    assert not state_thread.is_alive(), "secrets.state.get was blocked by the prompt"

    release_prompt.set()
    export_thread.join(1.0)
    assert not export_thread.is_alive()
    assert export_result[0].status == SecretOperationState.INTERACTION_REQUIRED


def _encrypted_backup(tmp_path, name="encrypted.spbk"):
    """Produce a genuinely encrypted ``.spbk`` through the service surface."""
    service, _manager, _backends, _broker, _path = _make_service(
        tmp_path, expected_secrets=["s3cr3t"])
    dest = tmp_path / name
    result = service.export_backup(
        destination=str(dest),
        options={"app_settings": True, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False, "encrypted": True},
        owner_client_id="client-1",
    )
    assert result.status.value == "success", result.message
    return dest


def _assert_state_stays_responsive(service, run, monkeypatch):
    """Run *run* with the decryption prompt blocked and assert that a
    concurrent ``secrets.state.get`` still returns.

    Same failure as the export regression above: holding
    ``SecretBackendService._lock`` across the passphrase interaction makes the
    metadata query wait for the five-second client RPC timeout, which
    disconnects the peer and cancels the interaction being waited on.
    """
    prompt_started = threading.Event()
    release_prompt = threading.Event()
    prompt_released = []

    def _blocked_prompt(*_args, **_kwargs):
        prompt_started.set()
        prompt_released.append(release_prompt.wait(5.0))
        return None

    monkeypatch.setattr(service, "_prompt_for_secret", _blocked_prompt)
    op_result: List[Any] = []
    op_thread = threading.Thread(target=lambda: op_result.append(run()), daemon=True)
    op_thread.start()
    try:
        assert prompt_started.wait(2.0), "the passphrase prompt was never reached"
        state_thread = threading.Thread(target=service.get_state, daemon=True)
        state_thread.start()
        state_thread.join(1.0)
        assert not state_thread.is_alive(), "secrets.state.get was blocked by the prompt"
    finally:
        release_prompt.set()
    op_thread.join(2.0)
    assert not op_thread.is_alive()
    assert prompt_released == [True]
    return op_result[0]


def test_import_passphrase_prompt_does_not_block_metadata_state(tmp_path, monkeypatch):
    """Encrypted import must not hold the service lock across its prompt."""
    source = _encrypted_backup(tmp_path)
    target = tmp_path / "import"
    target.mkdir()
    service, _manager, _backends, _broker, _path = _make_service(target)
    result = _assert_state_stays_responsive(
        service,
        lambda: service.import_backup(
            source=str(source), options={}, owner_client_id="client-1"),
        monkeypatch,
    )
    assert result.status == SecretOperationState.INTERACTION_REQUIRED
    assert result.message.code is SecretTransferMessageCode.DECRYPTION_CANCELLED


def test_preview_passphrase_prompt_does_not_block_metadata_state(tmp_path, monkeypatch):
    """Encrypted preview must not hold the service lock across its prompt."""
    source = _encrypted_backup(tmp_path)
    target = tmp_path / "preview"
    target.mkdir()
    service, _manager, _backends, _broker, _path = _make_service(target)
    public = _assert_state_stays_responsive(
        service,
        lambda: service.preview_backup(source=str(source), owner_client_id="client-1"),
        monkeypatch,
    )
    assert public.encrypted is True
    assert public.error.code is SecretTransferMessageCode.DECRYPTION_CANCELLED


# Every interactive lifecycle route, the way the frontend calls it.  These used
# to hold ``SecretBackendService._lock`` across their prompt: the reported
# symptom was the Bitwarden master-password dialog being replaced by "The
# daemon request timed out" moments after the email step — a concurrent
# ``secrets.state.get`` waited out its five-second client timeout, which failed
# the transport and cancelled the interaction the dialog was showing.
_PROMPTING_LIFECYCLE_ROUTES = {
    "unlock": lambda service, tmp_path: service.unlock(owner_client_id="client-1"),
    "bitwarden_login": lambda service, tmp_path: service.bitwarden_login(
        "alice@example.com", owner_client_id="client-1"
    ),
    "bitwarden_api_key_login": lambda service, tmp_path: service.bitwarden_api_key_login(
        "user.abc123", owner_client_id="client-1"
    ),
    "bitwarden_unlock": lambda service, tmp_path: service.bitwarden_unlock(
        owner_client_id="client-1"
    ),
    "keepassxc_create_database": lambda service, tmp_path: service.keepassxc_create_database(
        str(tmp_path / "new.kdbx"), owner_client_id="client-1"
    ),
    "keepassxc_unlock": lambda service, tmp_path: service.keepassxc_unlock(
        owner_client_id="client-1"
    ),
    "remember_master_password": lambda service, tmp_path: service.remember_master_password(
        owner_client_id="client-1"
    ),
}


@pytest.mark.parametrize("route", sorted(_PROMPTING_LIFECYCLE_ROUTES))
def test_lifecycle_prompts_do_not_block_metadata_state(tmp_path, monkeypatch, route):
    """No lifecycle route may hold the service lock across its interaction."""
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        selected="bitwarden",
    )
    # Signed in but locked: the state every prompting route is reached from.
    backends["bitwarden"]._needs_login = False
    run = _PROMPTING_LIFECYCLE_ROUTES[route]

    prompt_started = threading.Event()
    release_prompt = threading.Event()
    prompt_released: List[bool] = []

    def _blocked_prompt(*_args, **_kwargs):
        prompt_started.set()
        prompt_released.append(release_prompt.wait(5.0))
        return None

    def _blocked_master_prompt(*_args, **_kwargs):
        return _blocked_prompt(), False

    monkeypatch.setattr(service, "_prompt_for_secret", _blocked_prompt)
    monkeypatch.setattr(service, "_prompt_for_master_password", _blocked_master_prompt)

    op_thread = threading.Thread(target=lambda: run(service, tmp_path), daemon=True)
    op_thread.start()
    try:
        assert prompt_started.wait(2.0), f"{route} never reached its prompt"
        state_thread = threading.Thread(target=service.get_state, daemon=True)
        state_thread.start()
        state_thread.join(1.0)
        assert not state_thread.is_alive(), (
            f"secrets.state.get was blocked by the {route} prompt"
        )
    finally:
        release_prompt.set()
    op_thread.join(2.0)
    assert not op_thread.is_alive()
    assert prompt_released == [True]


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
    assert (
        status.message_code
        is SecretMessageCode.BITWARDEN_SERVER_CONFIGURATION_FAILED
    )
    assert service.get_configuration().bitwarden_server == ""
    assert (
        service.bitwarden_sync().message_code
        is SecretMessageCode.BITWARDEN_SYNC_FAILED
    )

    rbw_path = tmp_path / "rbw"
    rbw_path.mkdir()
    service, _manager, backends, _broker, _path = _make_service(
        rbw_path, secrets={"backend": "rbw", "session_timeout": 0}
    )
    backends["rbw"]._run = failed
    assert (
        service.rbw_configure(
            "alice@example.com", "https://vault.example.com"
        ).message_code
        is SecretMessageCode.RBW_CONFIGURATION_FAILED
    )
    assert service.rbw_unlock().message_code is SecretMessageCode.RBW_UNLOCK_FAILED
    assert service.rbw_sync().message_code is SecretMessageCode.RBW_SYNC_FAILED
    assert service.rbw_lock().message_code is SecretMessageCode.RBW_LOCK_FAILED


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

    assert status.message_code is SecretMessageCode.RBW_CONFIGURATION_FAILED
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


class CachingLoginBackend(FakeBackend):
    """Models BitwardenBackend: ``needs_login()`` is a slow CLI probe whose
    result is cached, plus a ``cached_needs_login()`` that never spawns."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.login_probes = 0
        self._login_cache: Optional[bool] = None

    def needs_login(self, *, force_refresh: bool = False) -> bool:
        self.login_probes += 1
        self._login_cache = self._needs_login
        return self._needs_login

    def cached_needs_login(self) -> Optional[bool]:
        return self._login_cache


def _service_with_caching_bitwarden(tmp_path, *, secrets=None, selected="auto"):
    backends = {
        "libsecret": FakeBackend("libsecret", session_backed=False),
        "keyring": FakeBackend("keyring", session_backed=False),
        "bitwarden": CachingLoginBackend("bitwarden", needs_login=True),
        "rbw": FakeBackend("rbw", needs_login=True),
        "keepassxc": FakeBackend("keepassxc"),
        "agent": FakeBackend("agent", session_backed=False),
    }
    return _make_service(
        tmp_path, secrets=secrets, backends=backends, selected=selected
    )


def test_registry_does_not_cold_probe_an_unselected_bitwarden_login(tmp_path):
    """``get_registry`` runs under the service lock, so any probe it makes
    blocks concurrent secrets queries — including the ``get_state`` the connect
    path waits on before opening a session. Bitwarden's login probe is a ~3s
    ``bw login --check``; describing a backend the user has not selected must
    not pay it (that delayed the first connection after startup, where the
    app's startup diagnostics reads the registry)."""
    service, _manager, backends, _broker, _path = _service_with_caching_bitwarden(
        tmp_path
    )
    bitwarden_backend = backends["bitwarden"]

    registry = service.get_registry()

    assert bitwarden_backend.login_probes == 0
    bitwarden = next(b for b in registry.backends if b.name == "bitwarden")
    assert bitwarden.selected is False
    assert bitwarden.login_required is False  # unknown reads as "not blocking"


def test_registry_reports_cached_login_state_for_an_unselected_bitwarden(tmp_path):
    """Skipping the cold probe must not throw away account state the daemon has
    already learned."""
    service, _manager, backends, _broker, _path = _service_with_caching_bitwarden(
        tmp_path
    )
    bitwarden_backend = backends["bitwarden"]
    assert bitwarden_backend.needs_login() is True  # warm the cache

    registry = service.get_registry()

    assert bitwarden_backend.login_probes == 1  # no second probe
    bitwarden = next(b for b in registry.backends if b.name == "bitwarden")
    assert bitwarden.login_required is True


def test_registry_probes_the_selected_bitwarden_login(tmp_path):
    """The selected backend's account state drives the unlock prompt, so it is
    still probed for real."""
    service, _manager, backends, _broker, _path = _service_with_caching_bitwarden(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        selected="bitwarden",
    )
    bitwarden_backend = backends["bitwarden"]

    registry = service.get_registry()

    assert bitwarden_backend.login_probes == 1
    bitwarden = next(b for b in registry.backends if b.name == "bitwarden")
    assert bitwarden.selected is True
    assert bitwarden.login_required is True


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


def _twostep_service(tmp_path, *, secrets_queue):
    """A Bitwarden that refuses the first sign-in with "a code is required"
    and accepts it once the code is supplied."""
    service, _manager, backends, broker, _ = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=secrets_queue,
    )
    bw = backends["bitwarden"]
    bw._needs_login = True
    # No method yet (the wizard has not asked which one), then the method
    # without a code — both stall on the two-step code.
    for method in (None, "0"):
        bw.login_results[
            ("login_with_password", "alice@example.com", method, None, None)
        ] = (False, "Code is required.", True)
    bw.login_results[
        ("login_with_password", "alice@example.com", "0", SENTINEL_2FA, None)
    ] = (True, "", False)
    return service, bw, broker


def test_two_step_retry_reuses_the_master_password(tmp_path):
    """The two-step sign-in asks for the master password once, then the code.

    The wizard needs two calls — the first learns a code is required, the
    second carries the method the user picked. The second used to open a fresh
    master-password prompt, so the user saw the password dialog again where
    they expected the two-step code.
    """
    service, bw, broker = _twostep_service(
        tmp_path, secrets_queue=[SENTINEL_MASTER, SENTINEL_2FA]
    )

    first = service.bitwarden_login("alice@example.com", owner_client_id="client-1")
    assert first.logged_in is False
    assert first.twofa_required is True
    assert len(broker.created) == 1  # the master password only

    second = service.bitwarden_login(
        "alice@example.com", twofa_method="0", owner_client_id="client-1"
    )
    assert second.logged_in is True
    # One further prompt, and it is the code — not the password again. Each
    # prompt names what it asks for, so the dialog heading is right.
    assert len(broker.created) == 2
    password_prompt = broker.created[0][1]["prompt"]
    code_prompt = broker.created[1][1]["prompt"]
    assert password_prompt.secret_prompt_kind is SecretPromptKind.BITWARDEN_SIGN_IN
    assert dict(password_prompt.secret_prompt_parameters) == {
        "email": "alice@example.com"
    }
    assert code_prompt.secret_prompt_kind is SecretPromptKind.BITWARDEN_TWO_STEP_LOGIN
    assert dict(code_prompt.secret_prompt_parameters) == {
        "email": "alice@example.com"
    }
    assert password_prompt.username == password_prompt.hostname == ""
    assert code_prompt.username == code_prompt.hostname == ""
    assert bw.login_results[
        ("login_with_password", "alice@example.com", "0", SENTINEL_2FA, None)
    ] == (True, "", False)


def test_two_step_password_is_not_reused_by_another_client(tmp_path):
    """The held password belongs to the client and email that produced it."""
    service, _bw, broker = _twostep_service(
        tmp_path, secrets_queue=[SENTINEL_MASTER, SENTINEL_MASTER, SENTINEL_2FA]
    )
    service.bitwarden_login("alice@example.com", owner_client_id="client-1")
    assert len(broker.created) == 1

    service.bitwarden_login(
        "alice@example.com", twofa_method="0", owner_client_id="client-2"
    )
    # A different client gets its own master-password prompt (then the code).
    assert len(broker.created) == 3


def test_two_step_password_is_dropped_once_the_flow_ends(tmp_path):
    """Nothing is held after a sign-in that no longer owes a code."""
    service, _bw, broker = _twostep_service(
        tmp_path, secrets_queue=[SENTINEL_MASTER, SENTINEL_2FA, SENTINEL_MASTER]
    )
    service.bitwarden_login("alice@example.com", owner_client_id="client-1")
    service.bitwarden_login(
        "alice@example.com", twofa_method="0", owner_client_id="client-1"
    )
    assert service._pending_login is None

    # A later sign-in prompts for the password again.
    service.bitwarden_login(
        "alice@example.com", twofa_method="0", owner_client_id="client-1"
    )
    assert (
        broker.created[2][1]["prompt"].secret_prompt_kind
        is SecretPromptKind.BITWARDEN_SIGN_IN
    )


def test_wrong_two_step_code_keeps_the_password_for_the_retry(tmp_path):
    """A wrong code re-asks for the code only — the password is still held."""
    service, bw, broker = _twostep_service(
        tmp_path, secrets_queue=[SENTINEL_MASTER, "000000", SENTINEL_2FA]
    )
    bw.login_results[
        ("login_with_password", "alice@example.com", "0", "000000", None)
    ] = (False, "Two-step token is invalid. Try again.", False)

    service.bitwarden_login("alice@example.com", owner_client_id="client-1")
    rejected = service.bitwarden_login(
        "alice@example.com", twofa_method="0", owner_client_id="client-1"
    )
    assert rejected.logged_in is False
    assert service._pending_login is not None

    accepted = service.bitwarden_login(
        "alice@example.com", twofa_method="0", owner_client_id="client-1"
    )
    assert accepted.logged_in is True
    # password, code, code — the password was never asked for twice.
    assert len(broker.created) == 3
    assert (
        broker.created[0][1]["prompt"].secret_prompt_kind
        is SecretPromptKind.BITWARDEN_SIGN_IN
    )
    assert all(
        entry[1]["prompt"].secret_prompt_kind
        is SecretPromptKind.BITWARDEN_TWO_STEP_LOGIN
        for entry in broker.created[1:]
    )


def test_locking_drops_a_held_two_step_password(tmp_path):
    """Locking the vault discards the password held for a two-step retry."""
    service, _bw, broker = _twostep_service(
        tmp_path, secrets_queue=[SENTINEL_MASTER, SENTINEL_MASTER, SENTINEL_2FA]
    )
    service.bitwarden_login("alice@example.com", owner_client_id="client-1")
    assert service._pending_login is not None

    service.bitwarden_lock()
    assert service._pending_login is None

    service.bitwarden_login(
        "alice@example.com", twofa_method="0", owner_client_id="client-1"
    )
    assert len(broker.created) == 3  # password prompted again, then the code


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


def test_bitwarden_failure_separates_code_from_external_diagnostic(tmp_path):
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    external_diagnostic = "bw: invalid grant for alice@example.com"
    backends["bitwarden"].login_results[
        ("login_with_password", "alice@example.com", None, None, None)
    ] = (False, external_diagnostic, False)

    status = service.bitwarden_login(
        "alice@example.com", owner_client_id="client-1"
    )

    assert status.message_code is SecretMessageCode.BITWARDEN_SIGN_IN_FAILED
    assert status.message_parameters == {}
    assert status.diagnostic == external_diagnostic
    assert "Sign-in failed." not in _all_strings(status.to_dict())


def test_bitwarden_internal_login_exception_is_not_a_user_diagnostic(tmp_path):
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )

    def fail_login(*_args, **_kwargs):
        raise RuntimeError("backend exception detail")

    backends["bitwarden"].login_with_password = fail_login

    status = service.bitwarden_login(
        "alice@example.com", owner_client_id="client-1"
    )

    assert status.message_code is SecretMessageCode.BITWARDEN_SIGN_IN_FAILED
    assert status.diagnostic == ""
    assert "backend exception detail" not in _all_strings(status.to_dict())
    assert "Bitwarden password login failed" not in _all_strings(status.to_dict())


def test_bitwarden_login_unlock_failure_uses_frontend_message_code(tmp_path):
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path,
        secrets={"backend": "bitwarden", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    backends["bitwarden"].unlock = lambda _password: False

    status = service.bitwarden_login(
        "alice@example.com", owner_client_id="client-1"
    )

    assert status.logged_in is False
    assert status.message_code is SecretMessageCode.BITWARDEN_UNLOCK_FAILED
    assert status.message_parameters == {}
    assert status.diagnostic == ""
    assert "Bitwarden vault unlock failed" not in _all_strings(status.to_dict())


@pytest.mark.parametrize(
    ("route", "backend"),
    [
        (
            lambda service: service.bitwarden_login(
                "alice@example.com", owner_client_id="client-1"
            ),
            "bitwarden",
        ),
        (lambda service: service.rbw_sync(), "rbw"),
    ],
)
def test_backend_unavailable_errors_use_stable_code_and_parameter(
    tmp_path, route, backend
):
    service, *_ = _make_service(
        tmp_path,
        backends={"agent": FakeBackend("agent", session_backed=False)},
    )

    with pytest.raises(SshPilotError) as raised:
        route(service)

    assert raised.value.code is ErrorCode.SECRET_BACKEND_UNAVAILABLE
    assert raised.value.message == ErrorCode.SECRET_BACKEND_UNAVAILABLE.value
    assert raised.value.details == {"backend": backend}
    assert f"{backend} is unavailable" not in raised.value.message


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

def test_rbw_status_reads_config_show_json(tmp_path):
    """rbw has no ``config get``; status must parse ``rbw config show`` JSON."""
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path, secrets={"backend": "rbw", "session_timeout": 0}
    )
    backend = backends["rbw"]
    status = service.rbw_status()
    assert status.configured is False
    assert ("_run", ("config", "show")) in backend.calls
    assert not any(
        kind == "_run" and args[:2] == ("config", "get")
        for kind, args in backend.calls
    )

    backend.calls.clear()
    status = service.rbw_configure("alice@example.com", "https://vault.example.com")
    assert status.configured is True
    assert status.email == "alice@example.com"
    assert status.base_url == "https://vault.example.com"
    assert ("_run", ("config", "show")) in backend.calls
    assert not any(
        kind == "_run" and args[:2] == ("config", "get")
        for kind, args in backend.calls
    )


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


def test_locked_rbw_needs_unlock_and_unlock_uses_master_password(tmp_path):
    """rbw is session-backed: a locked agent uses the GTK master-password dialog.

    ``unlock()`` must collect the password through the interaction broker (with
    Remember) and call ``backend.unlock(secret)`` — not native pinentry.
    """
    service, _manager, backends, broker, _path = _make_service(
        tmp_path,
        secrets={"backend": "rbw", "session_timeout": 0},
        expected_secrets=[SENTINEL_MASTER],
    )
    rbw = backends["rbw"]
    rbw._unlocked = False
    rbw._needs_login = False

    state = service.get_state()
    assert state.needs_unlock is True
    assert state.locked is True

    rbw.calls.clear()
    result = service.unlock(owner_client_id="client-1")
    assert result.kind == UnlockResultKind.UNLOCKED
    assert rbw._unlocked is True
    assert ("unlock", SENTINEL_MASTER) in rbw.calls
    assert broker.created
    prompt = broker.created[0][1].get("prompt")
    assert prompt is not None
    assert prompt.can_remember is True
    assert prompt.hostname == "rbw"
    assert SENTINEL_MASTER not in _all_strings(result.to_dict())


def test_unlock_cancellation_keeps_interaction_result_and_structured_reason(tmp_path):
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path,
        secrets={"backend": "rbw", "session_timeout": 0},
        selected="rbw",
    )
    backends["rbw"]._needs_login = False
    backends["rbw"]._unlocked = False

    result = service.unlock(owner_client_id="client-1")

    assert result.kind is UnlockResultKind.INTERACTION_REQUIRED
    assert result.message_code is SecretMessageCode.UNLOCK_CANCELLED
    assert result.diagnostic == ""


def test_unlock_unavailable_keeps_backend_as_structured_parameter(tmp_path):
    service, _manager, backends, _broker, _path = _make_service(
        tmp_path,
        secrets={"backend": "rbw", "session_timeout": 0},
        selected="rbw",
    )
    backends["rbw"]._available = False

    result = service.unlock(owner_client_id="client-1")

    assert result.kind is UnlockResultKind.BACKEND_UNAVAILABLE
    assert result.message_code is SecretMessageCode.SECRET_BACKEND_UNAVAILABLE
    assert dict(result.message_parameters) == {"backend": "rbw"}


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
    assert (
        result.message_code
        is SecretMessageCode.REMEMBERED_MASTER_PASSWORD_FORGET_FAILED
    )
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
    from sshpilot.api.models.secrets import SecretTransferMessage, SecretTransferResult

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

        def request_client_secret_with_status(self, *, owner_client_id, **kwargs):
            from sshpilot.api.models.interactions import InteractionState

            secret = self.request_client_secret(owner_client_id=owner_client_id, **kwargs)
            state = (
                InteractionState.CANCELLED if secret is None else InteractionState.ANSWERED
            )
            return secret, state

    calls = []

    def _fake_import(_manager, *, source, options, passphrase, settings_path, manifest, **_kwargs):
        calls.append(passphrase)
        if passphrase == "wrong":
            return SecretTransferResult(
                operation="import", path=source, counts={}, warnings=(),
                status=SecretOperationState.FAILED,
                message=SecretTransferMessage(
                    SecretTransferMessageCode.WRONG_PASSPHRASE_OR_CORRUPT_BACKUP
                ),
            )
        return SecretTransferResult(
            operation="import", path=source,
            counts={"restored": 1}, warnings=(),
            status=SecretOperationState.SUCCESS, message=None,
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
    from sshpilot.api.models.secrets import SecretTransferMessage, SecretTransferResult

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

        def request_client_secret_with_status(self, *, owner_client_id, **kwargs):
            from sshpilot.api.models.interactions import InteractionState

            secret = self.request_client_secret(owner_client_id=owner_client_id, **kwargs)
            state = (
                InteractionState.CANCELLED if secret is None else InteractionState.ANSWERED
            )
            return secret, state

    def _fake_import(_manager, *, source, options, passphrase, settings_path, manifest, **_kwargs):
        return SecretTransferResult(
            operation="import", path=source, counts={}, warnings=(),
            status=SecretOperationState.FAILED,
            message=SecretTransferMessage(
                SecretTransferMessageCode.WRONG_PASSPHRASE_OR_CORRUPT_BACKUP
            ),
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


# ---------------------------------------------------------------------------
# connection_store threading: SecretBackendService.export_backup/import_backup
# actually reach a real ConnectionRepository, not just the lower daemon_*
# functions tested directly in test_secret_transfer.py.
# ---------------------------------------------------------------------------


def _connection_store_repo(config_dir: Path, ssh_dir: Path):
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    return ConnectionRepository(
        ssh_store=SshConfigStore(ssh_dir / "config"),
        state_path=config_dir / "connections.json",
        legacy_config_path=config_dir / "config.json",
        isolated=False,
    )


def test_export_backup_threads_connection_store_snapshot(monkeypatch, tmp_path):
    import sshpilot.backup_manager as bm

    config_dir = tmp_path / "config"
    ssh_dir = tmp_path / "ssh"
    config_dir.mkdir()
    ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(ssh_dir))

    repo = _connection_store_repo(config_dir, ssh_dir)
    repo.create_connection(
        {"nickname": "svc-switch", "protocol": "telnet", "hostname": "10.0.0.30"}
    )

    path = _write_settings(config_dir / "config.json")
    manager = FakeManager({"libsecret": FakeBackend("libsecret", session_backed=False)})
    service = SecretBackendService(
        path, secret_manager=manager,
        connection_store_snapshot=repo.snapshot_for_backup,
    )
    dest = tmp_path / "out.spbk"
    result = service.export_backup(
        destination=str(dest),
        options={"app_settings": True, "ssh_config": True, "known_hosts": False,
                 "secrets": False, "private_keys": False},
        owner_client_id="client-1",
    )
    assert result.status.value == "success", result.message

    from sshpilot.backup_archive import read_spbk

    manifest = read_spbk(str(dest), None)
    connection_ids = {c["id"] for c in manifest["connection_store"]["connections"]}
    assert "svc-switch" in connection_ids


def test_import_backup_threads_connection_store_restore(monkeypatch, tmp_path):
    import sshpilot.backup_manager as bm

    source_config_dir = tmp_path / "source_config"
    source_ssh_dir = tmp_path / "source_ssh"
    source_config_dir.mkdir()
    source_ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(source_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(source_ssh_dir))

    source_repo = _connection_store_repo(source_config_dir, source_ssh_dir)
    source_repo.create_connection(
        {"nickname": "svc-switch", "protocol": "telnet", "hostname": "10.0.0.30"}
    )
    source_path = _write_settings(source_config_dir / "config.json")
    source_manager = FakeManager({"libsecret": FakeBackend("libsecret", session_backed=False)})
    source_service = SecretBackendService(
        source_path, secret_manager=source_manager,
        connection_store_snapshot=source_repo.snapshot_for_backup,
    )
    dest = tmp_path / "out.spbk"
    export_result = source_service.export_backup(
        destination=str(dest),
        options={"app_settings": True, "ssh_config": True, "known_hosts": False,
                 "secrets": False, "private_keys": False},
        owner_client_id="client-1",
    )
    assert export_result.status.value == "success", export_result.message

    target_config_dir = tmp_path / "target_config"
    target_ssh_dir = tmp_path / "target_ssh"
    target_config_dir.mkdir()
    target_ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(target_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(target_ssh_dir))

    target_repo = _connection_store_repo(target_config_dir, target_ssh_dir)
    target_path = _write_settings(target_config_dir / "config.json")
    target_manager = FakeManager({"libsecret": FakeBackend("libsecret", session_backed=False)})
    target_service = SecretBackendService(
        target_path, secret_manager=target_manager,
        connection_store_restore=target_repo.restore_connection_store,
    )
    import_result = target_service.import_backup(
        source=str(dest), options={"mode": "merge"}, owner_client_id="client-1",
    )
    assert import_result.status.value == "success", import_result.message
    assert "svc-switch" in {c.id for c in target_repo.snapshot().connections}
