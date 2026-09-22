"""Ásbrú import apply path through ConnectionApplicationService."""
from __future__ import annotations

from pathlib import Path

import pytest

from sshpilot.api.models.connections import AsbruImportMessageCode as Code, AsbruImportRequest
from sshpilot.core.connection_application_service import ConnectionApplicationService
from tests.helpers.fake_connection_repository import make_test_repository


pytest.importorskip("yaml")


SAMPLE = """
g1:
  _is_group: 1
  name: Lab
  parent: __PAC__EXPORTED__
c1:
  _is_group: 0
  name: lab-host
  parent: g1
  method: SSH
  ip: 192.0.2.10
  user: alice
  options: "-L 18080:localhost:80"
  jump ip: jump.example
  jump user: juser
  public_key: ~/.ssh/id_ed25519
c2:
  _is_group: 0
  name: lab-host
  parent: g1
  method: SSH
  ip: 192.0.2.11
  user: bob
"""


def test_preview_and_import_asbru(tmp_path: Path):
    client = ConnectionApplicationService(make_test_repository())
    export = tmp_path / "export.yml"
    export.write_text(SAMPLE, encoding="utf-8")

    preview = client.preview_asbru_import(str(export))
    assert preview.ok
    assert "lab-host" in preview.connections_to_add
    assert "lab-host-2" in preview.connections_to_add
    assert "Lab" in preview.groups_to_add

    result = client.import_asbru(AsbruImportRequest(source=str(export)))
    assert result.ok, (result.errors, result.partial_failures, result.message)
    assert result.message.code is Code.IMPORTED
    assert set(result.connections_added) == {"lab-host", "lab-host-2"}
    assert "Lab" in result.groups_added

    nicknames = {c.nickname for c in client.list_connections()}
    assert {"lab-host", "lab-host-2"} <= nicknames

    host = next(c for c in client.list_connections() if c.nickname == "lab-host")
    details = client.get_connection(host.id)
    assert details.hostname == "192.0.2.10"
    assert details.username == "alice"
    assert details.proxy_jump == ("juser@jump.example",)
    assert details.forwarding_rule_count == 1
    editor = client.get_connection_editor(host.id)
    assert editor.identity_files == ("~/.ssh/id_ed25519",)

    again = client.import_asbru(AsbruImportRequest(source=str(export)))
    assert again.connections_added == ()
    assert set(again.connections_skipped) == {"lab-host", "lab-host-2"}
    assert again.message.code is Code.ALL_EXIST
    client.close()


def test_missing_and_invalid_export_return_structured_errors(tmp_path: Path):
    client = ConnectionApplicationService(make_test_repository())
    missing = client.preview_asbru_import(str(tmp_path / "missing.yml"))
    assert missing.errors[0].code is Code.EXPORT_NOT_FOUND
    assert missing.errors[0].parameters == {"path": str(tmp_path / "missing.yml")}

    invalid_path = tmp_path / "invalid.yml"
    invalid_path.write_text("a: [broken", encoding="utf-8")
    invalid = client.import_asbru(AsbruImportRequest(source=str(invalid_path)))
    assert not invalid.ok
    assert invalid.message.code is Code.PARSE_FAILED
    assert invalid.errors[0].code is Code.YAML_INVALID
    assert invalid.errors[0].diagnostic
    client.close()


def test_partial_group_failure_keeps_opaque_detail_separate(tmp_path: Path, monkeypatch):
    client = ConnectionApplicationService(make_test_repository())
    export = tmp_path / "export.yml"
    export.write_text(SAMPLE, encoding="utf-8")

    from sshpilot.api.errors import ErrorCode, SshPilotError

    def fail_group(*_args, **_kwargs):
        raise SshPilotError(ErrorCode.PERSISTENCE_FAILED, "opaque repository detail")

    monkeypatch.setattr(client, "create_group_rpc", fail_group)
    result = client.import_asbru(AsbruImportRequest(source=str(export)))
    assert not result.ok
    assert result.message.code is Code.PARTIAL_FAILURES
    failure = result.partial_failures[0]
    assert failure.code is Code.GROUP_CREATE_FAILED
    assert failure.parameters == {"name": "Lab"}
    assert failure.diagnostic == "opaque repository detail"
    client.close()
