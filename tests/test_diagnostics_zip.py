"""Tests for the Help > Export Diagnostics ZIP builder and its secret redaction.

A diagnostics bundle must never leak credentials: secret-ish config keys and
embedded private-key blobs are replaced with a placeholder, while innocuous
keys/values are preserved.
"""

import json
import zipfile

from sshpilot import log_viewer
from sshpilot import platform_utils


def test_redact_config_redacts_secrets_only():
    sample = {
        "ui": {"theme": "dark"},
        "password": "hunter2",
        "nested": {"api_key": "abc", "host": "example.com"},
        "blob": "-----BEGIN OPENSSH PRIVATE KEY-----xxx-----END",
        "keyboard": "qwerty",          # must NOT be redacted (not a secret)
        "key_select_mode": 1,          # must NOT be redacted
    }
    red = log_viewer._redact_config(sample)

    assert red["password"] == "***REDACTED***"
    assert red["nested"]["api_key"] == "***REDACTED***"
    assert red["blob"] == "***REDACTED***"
    # innocuous keys/values preserved
    assert red["ui"]["theme"] == "dark"
    assert red["nested"]["host"] == "example.com"
    assert red["keyboard"] == "qwerty"
    assert red["key_select_mode"] == 1


def test_build_diagnostics_zip_contents_and_redaction(tmp_path, monkeypatch):
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir()
    config.mkdir()

    (state / "sshpilot.log").write_text("master log line\n")
    (state / "sshpilot.log.1").write_text("rotated\n")
    (state / "crash.log.previous").write_text("Fatal Python error: ...\n")
    (config / "config.json").write_text(json.dumps({
        "ui": {"theme": "dark"},
        "password": "should-not-appear",
    }))

    monkeypatch.setattr(log_viewer, "get_state_dir", lambda: str(state))
    monkeypatch.setattr(platform_utils, "get_config_dir", lambda: str(config))

    dest = tmp_path / "diag.zip"
    log_viewer.build_diagnostics_zip(str(dest))

    assert dest.is_file()
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        assert "logs/sshpilot.log" in names
        assert "logs/sshpilot.log.1" in names
        assert "logs/crash.log.previous" in names
        assert "system-info.txt" in names
        assert "version.txt" in names
        assert "config.json" in names

        cfg = json.loads(zf.read("config.json"))
        assert cfg["password"] == "***REDACTED***"
        assert cfg["ui"]["theme"] == "dark"
        # the raw secret must not appear anywhere in the archived config
        assert "should-not-appear" not in zf.read("config.json").decode()

        assert "sshPilot" in zf.read("version.txt").decode()
