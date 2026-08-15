"""Daemon-owned operation-mode transition tests."""

import json
import os
import threading
import time
from types import SimpleNamespace

from sshpilot.api.models.daemon import (
    OperationMode,
    SetOperationModeRequest,
)
from sshpilot.daemon.operation_mode_service import OperationModeService
from sshpilot.core.settings import CONFIG_VERSION
from sshpilot.api.models.identity import UpdateIdentitySelectionRequest
from sshpilot.api.models.secrets import UpdateSecretConfigurationRequest
from sshpilot.api.models.settings import UpdateGlobalSshOverridesRequest
from sshpilot.core.identity_service import IdentityStateService
from sshpilot.core.ssh_overrides_service import SshOverridesService
from sshpilot.daemon.plugin_settings import PluginSettingsService
from sshpilot.daemon.secret_backend_service import SecretBackendService


class _Repository:
    def __init__(self, *, isolated=False, fail=False):
        self.ssh_config_isolated = isolated
        self.fail = fail
        self.generation = 4

    def snapshot(self):
        return SimpleNamespace(generation=self.generation)

    def transition_ssh_config(self, store, isolated):
        store.load()
        if self.fail:
            raise OSError("injected transition failure")
        self.ssh_config_isolated = isolated
        self.generation += 1
        return self.snapshot()


class _FailOnRollbackRepository(_Repository):
    def __init__(self):
        super().__init__()
        self.transitions = 0

    def transition_ssh_config(self, store, isolated):
        self.transitions += 1
        if self.transitions == 2:
            self.ssh_config_isolated = True
            raise OSError("injected repository rollback failure")
        return super().transition_ssh_config(store, isolated)


def _service(tmp_path, repo):
    ssh = tmp_path / "ssh"
    app = tmp_path / "app"
    ssh.mkdir()
    app.mkdir()
    default = ssh / "config"
    default.write_text("Host prod\n  HostName example.com\n", encoding="utf-8")
    config = app / "config.json"
    config.write_text(json.dumps({"ssh": {"use_isolated_config": False}}), encoding="utf-8")
    return OperationModeService(
        repo,
        config_path=config,
        default_root=default,
        isolated_root=app / "ssh_config",
    ), config, default, app / "ssh_config"


def test_isolated_transition_seeds_secure_target_and_persists_mode(tmp_path):
    service, config, default, isolated = _service(tmp_path, _Repository())

    result = service.apply(
        SetOperationModeRequest(
            mode=OperationMode.ISOLATED,
            seed_isolated_config=True,
        )
    )

    assert result.accepted is True
    assert result.active_mode is OperationMode.ISOLATED
    assert result.seeded is True
    assert isolated.read_text(encoding="utf-8") == default.read_text(encoding="utf-8")
    assert os.stat(isolated).st_mode & 0o777 == 0o600
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is True


def test_missing_config_is_created_as_canonical_tree_and_survives_later_write(tmp_path):
    repo = _Repository()
    ssh = tmp_path / "ssh"
    app = tmp_path / "app"
    ssh.mkdir()
    app.mkdir()
    default = ssh / "config"
    default.write_text("Host prod\n", encoding="utf-8")
    config = app / "config.json"
    service = OperationModeService(
        repo,
        config_path=config,
        default_root=default,
        isolated_root=app / "ssh_config",
    )

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))
    assert result.accepted is True
    tree = json.loads(config.read_text(encoding="utf-8"))
    assert tree["config_version"] == CONFIG_VERSION
    assert tree["ssh"]["use_isolated_config"] is True
    assert {"terminal", "secrets", "identity", "file_manager"} <= set(tree)

    PluginSettingsService(config).set("demo", "enabled", True)
    after = json.loads(config.read_text(encoding="utf-8"))
    assert after["ssh"]["use_isolated_config"] is True
    assert after["plugins"]["demo"]["enabled"] is True

    restarted = OperationModeService(
        _Repository(isolated=True),
        config_path=config,
        default_root=default,
        isolated_root=app / "ssh_config",
    )
    assert restarted.status().active_mode is OperationMode.ISOLATED


def test_same_mode_request_reconciles_missing_config_to_canonical_tree(tmp_path):
    repo = _Repository()
    ssh = tmp_path / "ssh"
    app = tmp_path / "app"
    ssh.mkdir()
    app.mkdir()
    default = ssh / "config"
    default.write_text("Host prod\n", encoding="utf-8")
    config = app / "config.json"
    service = OperationModeService(
        repo,
        config_path=config,
        default_root=default,
        isolated_root=app / "ssh_config",
    )

    result = service.apply(SetOperationModeRequest(mode=OperationMode.DEFAULT))

    assert result.accepted is True
    tree = json.loads(config.read_text(encoding="utf-8"))
    assert tree["config_version"] == CONFIG_VERSION
    assert tree["ssh"]["use_isolated_config"] is False
    assert {"plugins", "identity", "secrets"} <= set(tree)


def test_status_reports_runtime_persisted_split_brain_as_recovery_required(tmp_path):
    repo = _Repository(isolated=True)
    service, config, _default, _isolated = _service(tmp_path, repo)
    config.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {"use_isolated_config": False}}),
        encoding="utf-8",
    )

    result = service.status()

    assert result.accepted is False
    assert result.recovery_required is True
    assert result.rollback_completed is False
    assert result.active_mode is OperationMode.ISOLATED
    assert result.persisted_mode is OperationMode.DEFAULT


def test_mode_transaction_serializes_all_settings_writers(tmp_path):
    """A mode publication cannot race a daemon settings read/modify/write.

    Each writer is paused behind the mode transaction and then allowed to
    continue.  It must load the newly published canonical tree, preserving the
    mode while adding its unrelated setting.  The four writers are deliberately
    exercised separately because each is a production daemon owner of a
    different config.json namespace.
    """
    writers = (
        (
            "plugin",
            lambda path: PluginSettingsService(path).set("demo", "enabled", True),
            lambda tree: tree["plugins"]["demo"]["enabled"] is True,
        ),
        (
            "ssh",
            lambda path: SshOverridesService(path).update(
                UpdateGlobalSshOverridesRequest(patch={"verbosity": 2})
            ),
            lambda tree: tree["ssh"]["verbosity"] == 2,
        ),
        (
            "identity",
            lambda path: IdentityStateService(path).update_selection(
                UpdateIdentitySelectionRequest(provider="onepassword")
            ),
            lambda tree: tree["identity"]["provider"] == "onepassword",
        ),
        (
            "secrets",
            lambda path: SecretBackendService(
                path,
                secret_manager=SimpleNamespace(set_selected=lambda _name: None),
            ).update_configuration(
                UpdateSecretConfigurationRequest(patch={"session_timeout": 9})
            ),
            lambda tree: tree["secrets"]["session_timeout"] == 9,
        ),
    )

    for name, write_unrelated, assert_unrelated in writers:
        repo = _Repository()
        case_dir = tmp_path / name
        case_dir.mkdir()
        service, config, _default, _isolated = _service(case_dir, repo)
        original_write = service._write_config
        mode_write_started = threading.Event()
        release_mode_write = threading.Event()

        def paused_write(value):
            mode_write_started.set()
            assert release_mode_write.wait(5), f"{name} mode write was not released"
            original_write(value)

        service._write_config = paused_write
        mode_result = {}

        def apply_mode():
            mode_result["result"] = service.apply(
                SetOperationModeRequest(mode=OperationMode.ISOLATED)
            )

        mode_thread = threading.Thread(target=apply_mode, daemon=True)
        mode_thread.start()
        assert mode_write_started.wait(5), f"{name} mode transaction did not begin"

        writer_started = threading.Event()
        writer_result = {}

        def write_setting():
            writer_started.set()
            try:
                write_unrelated(config)
            except BaseException as error:  # noqa: BLE001 - surfaced below
                writer_result["error"] = error

        writer_thread = threading.Thread(target=write_setting, daemon=True)
        writer_thread.start()
        assert writer_started.wait(5)
        time.sleep(0.05)
        assert writer_thread.is_alive(), f"{name} writer bypassed mode transaction lock"

        release_mode_write.set()
        mode_thread.join(5)
        writer_thread.join(5)
        assert not mode_thread.is_alive() and not writer_thread.is_alive()
        assert mode_result["result"].accepted is True
        assert "error" not in writer_result, writer_result.get("error")

        tree = json.loads(config.read_text(encoding="utf-8"))
        assert tree["ssh"]["use_isolated_config"] is True
        assert assert_unrelated(tree)


def test_isolated_to_default_transition_is_live_and_persisted(tmp_path):
    repo = _Repository(isolated=True)
    service, config, default, isolated = _service(tmp_path, repo)
    config.write_text(json.dumps({"ssh": {"use_isolated_config": True}}), encoding="utf-8")
    isolated.write_text("Host isolated\n", encoding="utf-8")

    result = service.apply(SetOperationModeRequest(mode=OperationMode.DEFAULT))

    assert result.accepted is True
    assert result.active_mode is OperationMode.DEFAULT
    assert result.persisted_mode is OperationMode.DEFAULT
    assert default.exists()
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False


def test_transition_conflict_does_not_change_persisted_or_runtime_mode(tmp_path):
    repo = _Repository()
    service, config, _default, _isolated = _service(tmp_path, repo)
    service.set_runtime_hooks(
        resource_probe=lambda: ("sessions",),
        on_committed=lambda: (_ for _ in ()).throw(AssertionError("must not commit")),
    )

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert result.conflict is True
    assert repo.ssh_config_isolated is False
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False


def test_transition_rolls_back_persistence_when_repository_rejects_target(tmp_path):
    repo = _Repository(fail=True)
    service, config, _default, isolated = _service(tmp_path, repo)

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert result.conflict is False
    assert result.active_mode is OperationMode.DEFAULT
    assert not isolated.exists()
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False


def test_target_preparation_failure_does_not_publish_or_persist(tmp_path, monkeypatch):
    repo = _Repository()
    service, config, _default, isolated = _service(tmp_path, repo)

    def fail_prepare(_request, _target):
        raise OSError("injected target creation failure")

    monkeypatch.setattr(service, "_prepare_target", fail_prepare)
    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert result.recovery_required is False
    assert result.active_mode is OperationMode.DEFAULT
    assert result.persisted_mode is OperationMode.DEFAULT
    assert not isolated.exists()
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False


def test_config_write_failure_rolls_back_without_publication(tmp_path, monkeypatch):
    repo = _Repository()
    service, config, _default, isolated = _service(tmp_path, repo)
    original_write = service._write_config
    writes = 0

    def fail_first_write(value):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("injected config write failure")
        return original_write(value)

    monkeypatch.setattr(service, "_write_config", fail_first_write)
    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert result.recovery_required is False
    assert result.active_mode is OperationMode.DEFAULT
    assert result.persisted_mode is OperationMode.DEFAULT
    assert not isolated.exists()
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False


def test_transition_rolls_back_runtime_when_reconfiguration_hook_fails(tmp_path):
    repo = _Repository()
    service, config, _default, isolated = _service(tmp_path, repo)
    service.set_runtime_hooks(
        resource_probe=lambda: (),
        on_committed=lambda: (_ for _ in ()).throw(OSError("reload failed")),
    )

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert repo.ssh_config_isolated is False
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False
    assert not isolated.exists()


def test_commit_hook_failure_reports_completed_rollback_and_restart_consistency(tmp_path):
    repo = _Repository()
    service, config, _default, isolated = _service(tmp_path, repo)
    calls = []
    service.set_runtime_hooks(
        resource_probe=lambda: (),
        on_committed=lambda: (_ for _ in ()).throw(OSError("reload failed")),
        on_rollback=lambda: calls.append("rollback-refresh"),
    )

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert result.recovery_required is False
    assert result.rollback_completed is True
    assert result.active_mode is OperationMode.DEFAULT
    assert result.persisted_mode is OperationMode.DEFAULT
    assert calls == ["rollback-refresh"]
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False
    assert not isolated.exists()


def test_repository_rollback_failure_is_recovery_required_and_truthful(tmp_path):
    repo = _FailOnRollbackRepository()
    service, config, _default, isolated = _service(tmp_path, repo)
    service.set_runtime_hooks(
        resource_probe=lambda: (),
        on_committed=lambda: (_ for _ in ()).throw(OSError("reload failed")),
    )

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert result.recovery_required is True
    assert result.rollback_completed is False
    assert result.active_mode is OperationMode.ISOLATED
    assert result.persisted_mode is OperationMode.DEFAULT
    assert "Restart or manual recovery is required" in result.message
    assert json.loads(config.read_text(encoding="utf-8"))["ssh"]["use_isolated_config"] is False
    assert not isolated.exists()


def test_persisted_config_rollback_failure_is_recovery_required(tmp_path, monkeypatch):
    repo = _Repository()
    service, config, _default, isolated = _service(tmp_path, repo)
    original_write = service._write_config
    writes = 0

    def fail_after_publication(value):
        nonlocal writes
        writes += 1
        if writes >= 2:
            raise OSError("injected persisted rollback failure")
        return original_write(value)

    monkeypatch.setattr(service, "_write_config", fail_after_publication)
    service.set_runtime_hooks(
        resource_probe=lambda: (),
        on_committed=lambda: (_ for _ in ()).throw(OSError("reload failed")),
    )

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.recovery_required is True
    assert result.rollback_completed is False
    assert result.active_mode is OperationMode.DEFAULT
    assert result.persisted_mode is OperationMode.ISOLATED
    assert "persisted mode is isolated" in result.message
    assert isolated.exists() is False
