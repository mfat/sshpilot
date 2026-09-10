"""Ásbrú Connection Manager export parser tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from sshpilot.core.errors import CoreError
from sshpilot.core.import_export.asbru import (
    build_proxy_jump,
    load_asbru_export,
    parse_asbru_export,
    parse_asbru_export_text,
    parse_forwards_from_options,
    sanitize_host_alias,
)

SAMPLE_EXPORT = """
group-1:
  _is_group: 1
  name: Production
  parent: __PAC__EXPORTED__
  description: prod folder
child-group:
  _is_group: 1
  name: Web
  parent: group-1
conn-1:
  _is_group: 0
  name: App Server
  title: App Server
  parent: child-group
  method: SSH
  ip: 10.0.0.5
  user: deploy
  port: 2222
  options: "-L 8080:localhost:80 -R 127.0.0.1:9000:localhost:9000"
  jump ip: bastion.example
  jump user: jumpuser
  jump port: 2200
conn-vnc:
  _is_group: 0
  name: Desktop
  method: VNC
  ip: 10.0.0.9
  user: vnc
conn-key-user:
  _is_group: 0
  name: key-only
  method: SSH
  ip: 10.0.0.8
  passphrase user: keyuser
"""


def test_sanitize_host_alias_strips_whitespace_and_uniquifies():
    assert sanitize_host_alias("App Server") == "App-Server"
    assert sanitize_host_alias("App Server", existing={"App-Server"}) == "App-Server-2"


def test_parse_forwards_and_proxyjump():
    rules = parse_forwards_from_options(
        "-L 8080:localhost:80 -R 127.0.0.1:9000:localhost:9000"
    )
    assert rules[0]["type"] == "local"
    assert rules[0]["listen_port"] == 8080
    assert rules[0]["remote_host"] == "localhost"
    assert rules[1]["type"] == "remote"
    assert rules[1]["listen_addr"] == "127.0.0.1"
    jump = build_proxy_jump(
        {"jump ip": "bastion", "jump user": "j", "jump port": 2200}
    )
    assert jump == ("j@bastion:2200",)


def test_parse_export_maps_fields_and_skips_non_ssh(tmp_path: Path):
    pytest.importorskip("yaml")
    path = tmp_path / "asbru_export.yml"
    path.write_text(SAMPLE_EXPORT, encoding="utf-8")
    result = load_asbru_export(path)
    assert result.ok
    assert [g.name for g in result.groups] == ["Production", "Web"]
    assert result.groups[1].parent_source_id == "group-1"

    by_nick = {c.nickname: c for c in result.connections}
    assert "App-Server" in by_nick
    app = by_nick["App-Server"]
    assert app.hostname == "10.0.0.5"
    assert app.username == "deploy"
    assert app.port == 2222
    assert app.proxy_jump == ("jumpuser@bastion.example:2200",)
    assert len(app.forwarding_rules) == 2
    assert app.group_source_id == "child-group"
    assert app.display_name == "App Server"

    key_only = by_nick["key-only"]
    assert key_only.username == "keyuser"

    assert all(c.nickname != "Desktop" for c in result.connections)
    assert any("non-SSH" in w for w in result.warnings)


def test_prefer_user_over_passphrase_user():
    result = parse_asbru_export(
        {
            "c1": {
                "_is_group": 0,
                "name": "host1",
                "method": "SSH",
                "ip": "1.2.3.4",
                "user": "real",
                "passphrase user": "wrong",
            }
        }
    )
    assert result.connections[0].username == "real"


def test_maps_public_key_to_identity_files():
    result = parse_asbru_export(
        {
            "c1": {
                "_is_group": 0,
                "name": "keyed",
                "method": "SSH",
                "ip": "1.2.3.4",
                "user": "deploy",
                "public_key": "~/.ssh/id_ed25519",
            }
        }
    )
    assert result.connections[0].identity_files == ("~/.ssh/id_ed25519",)


def test_prunes_groups_with_only_non_ssh_children():
    result = parse_asbru_export(
        {
            "g-ssh": {"_is_group": 1, "name": "SSH Folder"},
            "g-rdp": {"_is_group": 1, "name": "RDP Folder"},
            "c1": {
                "_is_group": 0,
                "name": "box",
                "method": "SSH",
                "ip": "1.2.3.4",
                "user": "u",
                "parent": "g-ssh",
            },
            "c2": {
                "_is_group": 0,
                "name": "win",
                "method": "RDP",
                "ip": "1.2.3.5",
                "user": "Administrator",
                "parent": "g-rdp",
            },
        }
    )
    assert [g.name for g in result.groups] == ["SSH Folder"]
    assert any("RDP Folder" in w for w in result.warnings)
    assert any("non-SSH" in w for w in result.warnings)
    assert len(result.connections) == 1


def test_rejects_missing_file(tmp_path: Path):
    with pytest.raises(CoreError):
        load_asbru_export(tmp_path / "missing.yml")


def test_skips_integer_children_entries_from_raw_asbru():
    result = parse_asbru_export(
        {
            "__PAC__EXPORTED__": {"children": {"x": 1}},
            "x": 1,
            "good": {
                "_is_group": 0,
                "name": "good",
                "method": "SSH",
                "ip": "1.1.1.1",
                "user": "u",
            },
        }
    )
    assert result.ok
    assert len(result.connections) == 1


def test_parse_text_requires_pyyaml_or_works():
    try:
        import yaml  # noqa: F401
    except ImportError:
        with pytest.raises(CoreError) as exc:
            parse_asbru_export_text("a: 1")
        assert "PyYAML" in str(exc.value.message)
        return
    result = parse_asbru_export_text(
        "g:\n  _is_group: 1\n  name: G\n  parent: __PAC__EXPORTED__\n"
        "c:\n  _is_group: 0\n  name: box\n  method: SSH\n  ip: 1.2.3.4\n"
        "  user: u\n  parent: g\n"
    )
    assert result.ok
    assert result.groups[0].name == "G"
    assert result.connections[0].nickname == "box"
