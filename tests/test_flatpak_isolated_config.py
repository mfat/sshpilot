import importlib

import sshpilot.config as config


def test_isolated_config_default_on_flatpak(monkeypatch, tmp_path):
    importlib.reload(config)
    monkeypatch.setattr(config, "is_flatpak", lambda: True)
    monkeypatch.setattr(config, "get_config_dir", lambda: str(tmp_path))
    cfg = config.Config()
    assert cfg.get_setting('ssh.use_isolated_config', False) is True
