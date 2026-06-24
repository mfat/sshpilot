"""The file-manager backend factory picks the configured implementation."""

from tests.test_file_manager_auth import _load_file_manager_module


def test_resolve_backend_name_validation(monkeypatch):
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager as fm

    assert fm._resolve_backend_name("openssh") == "openssh"
    assert fm._resolve_backend_name("Paramiko") == "paramiko"
    assert fm._resolve_backend_name("bogus") == "paramiko"
    # No explicit value → reads config; the stub Config lacks the helper, so it
    # falls back to the paramiko default.
    assert fm._resolve_backend_name(None) == "paramiko"


def test_factory_returns_selected_backend(monkeypatch):
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager as fm
    from sshpilot.file_manager.openssh_backend import OpenSSHSFTPManager
    from sshpilot.file_manager.sftp_manager import AsyncSFTPManager

    paramiko_backend = fm.create_file_manager_backend(
        "host", "user", 22, backend="paramiko"
    )
    openssh_backend = fm.create_file_manager_backend(
        "host", "user", 22, backend="openssh"
    )
    try:
        assert isinstance(paramiko_backend, AsyncSFTPManager)
        assert isinstance(openssh_backend, OpenSSHSFTPManager)
        # Default (no explicit backend) → paramiko.
        default_backend = fm.create_file_manager_backend("host", "user", 22)
        assert isinstance(default_backend, AsyncSFTPManager)
        default_backend.close()
    finally:
        paramiko_backend.close()
        openssh_backend.close()


def test_config_backend_getter_validates():
    """Config.get_file_manager_config normalizes the backend value.

    Uses the real Config (no stub harness) so we exercise the actual getter.
    """
    from sshpilot.config import Config

    cfg = object.__new__(Config)  # avoid full init / gsettings

    def fake_get_setting(key, default=None):
        return "OpenSSH" if key == "file_manager.backend" else default

    cfg.get_setting = fake_get_setting
    cfg.get_default_config = lambda: {"file_manager": {"backend": "paramiko"}}
    assert cfg.get_file_manager_config()["backend"] == "openssh"

    cfg.get_setting = lambda key, default=None: ("nonsense" if key == "file_manager.backend" else default)
    assert cfg.get_file_manager_config()["backend"] == "paramiko"
