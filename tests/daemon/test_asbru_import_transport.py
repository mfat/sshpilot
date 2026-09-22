"""Ásbrú notices survive the actual daemon request/response path."""

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.models.connections import AsbruImportMessageCode as Code, AsbruImportRequest


pytest.importorskip("yaml")


def test_asbru_preview_and_import_return_structured_messages(daemon_factory, tmp_path):
    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)

    export = tmp_path / "export.yml"
    export.write_text(
        "one:\n  _is_group: 0\n  name: new-host\n  method: SSH\n  ip: 192.0.2.12\n"
        "two:\n  _is_group: 0\n  name: desktop\n  method: VNC\n  ip: 192.0.2.13\n",
        encoding="utf-8",
    )
    preview = client.preview_asbru_import(str(export))
    assert preview.ok
    assert preview.connections_to_add == ("new-host",)
    assert preview.warnings[0].code is Code.SKIPPED_NON_SSH
    assert preview.warnings[0].parameters == {"name": "desktop", "method": "VNC"}

    result = client.import_asbru(AsbruImportRequest(source=str(export)))
    assert result.ok
    assert result.connections_added == ("new-host",)
    assert result.message.code is Code.IMPORTED
    assert result.warnings == preview.warnings
    client.close()
