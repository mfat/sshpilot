"""Daemon-owned operation-mode transition tests."""

import json
import os
from types import SimpleNamespace

from sshpilot.api.models.daemon import (
    OperationMode,
    SetOperationModeRequest,
)
from sshpilot.daemon.operation_mode_service import OperationModeService


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
