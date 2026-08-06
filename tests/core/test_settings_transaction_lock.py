"""Cross-service settings transaction serialization.

``SshOverridesService`` and ``SecretBackendService`` mutate the same
``config.json``.  Both hold the process-wide per-path settings transaction
lock for their complete load -> validate -> apply -> save cycle, so a writer
never clobbers keys a concurrent writer changed.  These tests pause one
transaction mid-save and prove the other waits, then that the final file
contains both changes (in both orderings).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from sshpilot.api.models.secrets import UpdateSecretConfigurationRequest
from sshpilot.api.models.settings import UpdateGlobalSshOverridesRequest
from sshpilot.core.settings import CONFIG_VERSION
from sshpilot.core.ssh_overrides_service import SshOverridesService
from sshpilot.daemon import secret_backend_service as secrets_mod
from sshpilot.daemon.secret_backend_service import SecretBackendService
from sshpilot.core import ssh_overrides_service as ssh_mod


class MinimalSecretManager:
    """The tiny ``SecretManager`` surface the secrets update path touches."""

    def set_selected(self, name: str) -> None:
        self.selected = name


def _write_initial(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "config_version": CONFIG_VERSION,
                "ssh": {"verbosity": 0},
                "secrets": {"backend": "auto", "session_timeout": 1},
            }
        ),
        encoding="utf-8",
    )


def _build(path: Path):
    return (
        SshOverridesService(path),
        SecretBackendService(path, secret_manager=MinimalSecretManager()),
    )


def _run_threaded(fn) -> threading.Thread:
    out: dict = {}

    def _wrap():
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            out["error"] = exc

    t = threading.Thread(target=_wrap, daemon=True)
    t.start()
    return t, out


def test_secret_then_ssh_transactions_serialize(tmp_path, monkeypatch):
    """Secret loads/prepares -> SSH commits while it waits -> secret commits;
    the final file contains both changes."""
    path = tmp_path / "config.json"
    _write_initial(path)
    ssh, secrets = _build(path)

    started = threading.Event()
    release = threading.Event()
    real_save = secrets_mod.save_settings

    def paused_save(p, c):
        started.set()
        assert release.wait(5)
        real_save(p, c)

    monkeypatch.setattr(secrets_mod, "save_settings", paused_save)

    secret_t, secret_out = _run_threaded(
        lambda: secrets.update_configuration(
            UpdateSecretConfigurationRequest(patch={"session_timeout": 9})
        )
    )
    assert started.wait(5), "the secret transaction did not begin"

    # While the secret transaction holds the settings lock mid-save, the SSH
    # update must wait rather than clobber the in-flight write.
    ssh_t, ssh_out = _run_threaded(
        lambda: ssh.update(
            UpdateGlobalSshOverridesRequest(patch={"verbosity": 2})
        )
    )
    time.sleep(0.2)
    assert ssh_t.is_alive(), "SSH update did not wait for the secret transaction"

    release.set()
    secret_t.join(5)
    ssh_t.join(5)
    assert not secret_t.is_alive() and not ssh_t.is_alive()
    assert "error" not in secret_out, secret_out.get("error")
    assert "error" not in ssh_out, ssh_out.get("error")

    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["secrets"]["session_timeout"] == 9
    assert final["ssh"]["verbosity"] == 2


def test_ssh_then_secret_transactions_serialize(tmp_path, monkeypatch):
    """Reverse ordering: SSH loads/prepares -> secret waits -> SSH commits;
    the final file contains both changes."""
    path = tmp_path / "config.json"
    _write_initial(path)
    ssh, secrets = _build(path)

    started = threading.Event()
    release = threading.Event()
    real_save = ssh_mod.save_settings

    def paused_save(p, c):
        started.set()
        assert release.wait(5)
        real_save(p, c)

    monkeypatch.setattr(ssh_mod, "save_settings", paused_save)

    ssh_t, ssh_out = _run_threaded(
        lambda: ssh.update(
            UpdateGlobalSshOverridesRequest(patch={"verbosity": 2})
        )
    )
    assert started.wait(5), "the SSH transaction did not begin"

    secret_t, secret_out = _run_threaded(
        lambda: secrets.update_configuration(
            UpdateSecretConfigurationRequest(patch={"session_timeout": 9})
        )
    )
    time.sleep(0.2)
    assert secret_t.is_alive(), "secret update did not wait for the SSH transaction"

    release.set()
    ssh_t.join(5)
    secret_t.join(5)
    assert not ssh_t.is_alive() and not secret_t.is_alive()
    assert "error" not in ssh_out, ssh_out.get("error")
    assert "error" not in secret_out, secret_out.get("error")

    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["ssh"]["verbosity"] == 2
    assert final["secrets"]["session_timeout"] == 9


def test_both_services_share_one_lock_per_path(tmp_path):
    """The two services acquire the same per-path lock object."""
    path = tmp_path / "config.json"
    _write_initial(path)
    ssh, secrets = _build(path)
    from sshpilot.core.settings import settings_transaction_lock

    shared = settings_transaction_lock(path)
    # A manual acquisition blocks both services' mutations.
    shared.acquire()
    try:
        t, out = _run_threaded(
            lambda: ssh.update(
                UpdateGlobalSshOverridesRequest(patch={"verbosity": 2})
            )
        )
        time.sleep(0.1)
        assert t.is_alive()
        t.join(0.05)
        assert "result" not in out
    finally:
        shared.release()
    t.join(5)
    assert not t.is_alive()
    assert "error" not in out, out.get("error")
