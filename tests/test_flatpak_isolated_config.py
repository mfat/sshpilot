import importlib
import json

import sshpilot.config as config


def test_default_mode_is_default_on_flatpak(monkeypatch, tmp_path):
    importlib.reload(config)
    monkeypatch.setattr(config, "get_config_dir", lambda: str(tmp_path))
    cfg = config.Config()
    assert cfg.get_setting('ssh.use_isolated_config', True) is False


def test_existing_flatpak_isolated_config_preserved(monkeypatch, tmp_path):
    importlib.reload(config)
    monkeypatch.setattr(config, "get_config_dir", lambda: str(tmp_path))

    config_data = {
        "config_version": config.CONFIG_VERSION,
        "ssh": {"use_isolated_config": True},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))

    cfg = config.Config()
    assert cfg.get_setting('ssh.use_isolated_config', False) is True
