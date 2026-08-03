"""The file-manager factory enforces daemon ownership."""

import pytest

from tests._fm_harness import _load_file_manager_module


def test_factory_rejects_missing_daemon_backend(monkeypatch):
    monkeypatch.setenv("SSHPILOT_CLIENT_MODE", "core_service")
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager as fm
    with pytest.raises(RuntimeError, match="daemon SFTP service is required"):
        fm.create_file_manager_backend("host", "user", 22)


def test_config_has_no_backend_key():
    """The file-manager backend setting is gone (single backend)."""
    from sshpilot.config import Config

    cfg = object.__new__(Config)  # avoid full init / gsettings
    cfg.get_setting = lambda key, default=None: default
    cfg.get_default_config = lambda: {"file_manager": {}}

    fm_config = cfg.get_file_manager_config()
    assert "backend" not in fm_config
