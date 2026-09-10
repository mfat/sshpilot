"""Ásbrú import apply path through ConnectionApplicationService."""
from __future__ import annotations

from pathlib import Path

import pytest

from sshpilot.api.models.connections import AsbruImportRequest
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
    client.close()
