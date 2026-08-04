"""Daemon bootstrap settings view tests (M3).

``DaemonBootstrapSettings`` is the GI-free, read-only view over ``config.json``
that production composition uses. It must never import ``sshpilot.config`` or
PyGObject and must fall back to application defaults for missing or malformed
input.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from sshpilot.daemon.bootstrap_settings import DEFAULT_SSH_CONFIG, DaemonBootstrapSettings


def _settings(tmp_path, *, payload=None, raw_text=None):
    if raw_text is None:
        raw_text = json.dumps(payload) if payload is not None else "{}"
    config_path = tmp_path / "config.json"
    config_path.write_text(raw_text, encoding="utf-8")
    return DaemonBootstrapSettings(config_path)


def test_missing_config_uses_defaults(tmp_path):
    settings = DaemonBootstrapSettings(tmp_path / "does-not-exist.json")
    ssh = settings.get_ssh_config()
    assert ssh["use_isolated_config"] is False
    assert ssh["batch_mode"] is False
    assert ssh["auto_add_host_keys"] is True


def test_malformed_config_uses_defaults(tmp_path):
    settings = _settings(tmp_path, raw_text="{not valid json")
    assert settings.use_isolated_config is False
    assert settings.get_ssh_config()["strict_host_key_checking"] == "accept-new"


def test_non_dict_config_uses_defaults(tmp_path):
    settings = _settings(tmp_path, raw_text='["not", "a", "dict"]')
    assert settings.get_ssh_config() == DEFAULT_SSH_CONFIG


def test_isolated_flag_from_ssh_namespace(tmp_path):
    settings = _settings(tmp_path, payload={"ssh": {"use_isolated_config": True}})
    assert settings.use_isolated_config is True


def test_top_level_setting_lookup(tmp_path):
    settings = _settings(tmp_path, payload={"use-askpass": False})
    assert settings.get_setting("use-askpass") is False
    assert settings.askpass_enabled is False


def test_ssh_namespaced_setting_lookup(tmp_path):
    settings = _settings(tmp_path, payload={"ssh": {"agent_preload_keys": False}})
    assert settings.get_setting("ssh.agent_preload_keys") is False
    assert settings.agent_preload_keys is False


def test_null_values_fall_back_to_defaults(tmp_path):
    settings = _settings(
        tmp_path, payload={"ssh": {"use_isolated_config": None, "verbosity": None}}
    )
    assert settings.use_isolated_config is False


def test_config_file_setting(tmp_path):
    settings = _settings(tmp_path, payload={"config_file": "~/.ssh/custom"})
    assert settings.config_file == "~/.ssh/custom"


def test_settings_module_never_imports_config_or_gi():
    source = Path("src/sshpilot/daemon/bootstrap_settings.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    assert "sshpilot.config" not in imports
    assert not any(name.startswith("gi") for name in imports)
    assert not any("GLib" in name or "GObject" in name for name in imports)
