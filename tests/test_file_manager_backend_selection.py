"""The file-manager backend factory builds the OpenSSH backend (the only one)."""

from tests._fm_harness import _load_file_manager_module


def test_factory_returns_openssh_backend(monkeypatch):
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager as fm
    from sshpilot.file_manager.openssh_backend import OpenSSHSFTPManager

    backend = fm.create_file_manager_backend("host", "user", 22)
    try:
        assert isinstance(backend, OpenSSHSFTPManager)
    finally:
        backend.close()


def test_config_has_no_backend_key():
    """The file-manager backend setting is gone (single backend)."""
    from sshpilot.config import Config

    cfg = object.__new__(Config)  # avoid full init / gsettings
    cfg.get_setting = lambda key, default=None: default
    cfg.get_default_config = lambda: {"file_manager": {}}

    fm_config = cfg.get_file_manager_config()
    assert "backend" not in fm_config
