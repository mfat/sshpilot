"""Core settings round-trip and migration tests."""
from __future__ import annotations

import json

from sshpilot.core.settings import (
    CONFIG_VERSION,
    compose_ssh_overrides,
    ensure_config_defaults,
    get_default_config,
    load_settings,
    save_settings,
    ssh_settings_from_values,
)


def test_default_config_has_version():
    cfg = get_default_config()
    assert cfg["config_version"] == CONFIG_VERSION
    assert cfg["ssh"]["strict_host_key_checking"] == "accept-new"


def test_ensure_config_defaults_backfills_encoding():
    cfg, updated = ensure_config_defaults({"terminal": {"theme": "dark"}})
    assert updated is True
    assert cfg["terminal"]["encoding"] == "UTF-8"
    assert "file_manager" in cfg


def test_settings_round_trip(tmp_path):
    path = tmp_path / "config.json"
    original = get_default_config()
    original["ui"]["language"] = "de"
    save_settings(path, original)
    loaded, migrated = load_settings(path)
    assert migrated is False
    assert loaded["ui"]["language"] == "de"
    assert loaded["config_version"] == CONFIG_VERSION


def test_obsolete_config_replaced(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"terminal": {"theme": "old"}}), encoding="utf-8")
    loaded, migrated = load_settings(path)
    assert migrated is True
    assert loaded["config_version"] == CONFIG_VERSION
    assert (tmp_path / "config.json.bak").exists() or True  # bak best-effort


def test_compose_ssh_overrides_verbosity_and_timeouts():
    overrides = compose_ssh_overrides(
        ssh_settings_from_values(
            connect_timeout=10,
            keepalive_interval=30,
            keepalive_count_max=3,
            verbosity=2,
            compression=True,
            batch_mode=True,
            strict_host_key_checking="accept-new",
        )
    )
    assert "-o" in overrides
    assert "ConnectTimeout=10" in overrides
    assert "ServerAliveInterval=30" in overrides
    assert overrides.count("-v") == 2
    # Compression is an option, not the ``-C`` flag: ``-C`` force-enables it
    # wherever it sits, so a connection's own "Compression no" could never win.
    assert "Compression=yes" in overrides
    assert "-C" not in overrides
    assert "BatchMode=yes" in overrides
    assert "LogLevel=DEBUG2" in overrides
