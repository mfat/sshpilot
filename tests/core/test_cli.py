"""CLI command coverage for sshpilot-core."""
from __future__ import annotations

import json

from sshpilot.core.cli import main


def test_validate_connection_ok_and_fail():
    assert main(["validate-connection", "--nickname", "Demo", "--host", "example.com", "--user", "a"]) == 0
    assert main(["validate-connection", "--nickname", "bad name", "--host", "example.com", "--user", "a"]) == 1


def test_inspect_connections_and_build_ssh(tmp_path, capsys):
    store = tmp_path / "c.json"
    from sshpilot.core.connections import ConnectionService

    svc = ConnectionService(store_path=store, autosave=True)
    svc.create({"nickname": "CLI", "hostname": "c.test", "username": "u"})
    assert main(["inspect-connections", "--path", str(store), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert main(["build-ssh-command", "host", "--user", "alice", "--port", "2222", "--json"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["argv"][0] == "ssh"


def test_validate_and_plan_import(tmp_path, capsys):
    payload = {
        "version": 1,
        "connections": [{"nickname": "Imp", "hostname": "i.test", "username": "u"}],
    }
    path = tmp_path / "imp.json"
    path.write_text(json.dumps(payload))
    assert main(["validate-import", str(path), "--json"]) == 0
    assert main(["plan-import", str(path), "--strategy", "merge", "--json"]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1}))
    assert main(["validate-import", str(bad)]) == 1
