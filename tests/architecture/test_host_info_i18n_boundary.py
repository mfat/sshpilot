"""Architecture checks for frontend-owned Host Info localization."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"
SERVICE = SOURCE / "daemon" / "host_info_service.py"
PRESENTER = SOURCE / "gtk" / "host_info_failure_messages.py"
DIALOG = SOURCE / "machine_info_dialog.py"
CODEC = SOURCE / "api" / "transport" / "codec.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_host_info_daemon_never_calls_gettext():
    tree = _tree(SERVICE)
    imported_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "gettext" not in imported_modules
    assert "_" not in calls
    assert "N_" not in calls


def test_frontend_presenter_is_the_gettext_extraction_owner():
    potfiles = (ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines()

    assert "src/sshpilot/gtk/host_info_failure_messages.py" in potfiles
    assert "src/sshpilot/daemon/host_info_service.py" not in potfiles
    assert "N_(" in PRESENTER.read_text(encoding="utf-8")


def test_host_info_wire_failure_has_no_rendered_message_field():
    codec = CODEC.read_text(encoding="utf-8")
    start = codec.index("def _host_info_failure_to_wire")
    end = codec.index("def _host_info_failure_from_wire")
    encoder = codec[start:end]

    assert '"parameters": dict(failure.parameters)' in encoder
    assert '"diagnostic": failure.diagnostic' in encoder
    assert '"message"' not in encoder


def test_generic_service_failure_wire_contract_is_unchanged():
    codec = CODEC.read_text(encoding="utf-8")
    start = codec.index("def _service_failure_to_wire")
    end = codec.index("def _service_failure_from_wire")
    encoder = codec[start:end]

    assert '"code": failure.code' in encoder
    assert '"message": failure.message' in encoder


def test_dialog_never_uses_backend_text_as_its_primary_message():
    source = DIALOG.read_text(encoding="utf-8")

    assert "summary.failure.message" not in source
    assert '% str(error)' not in source
    assert "format_host_info_failure(summary.failure)" in source
    assert "format_host_info_error(error)" in source


def test_migrated_ui_sentences_live_only_in_the_frontend():
    daemon = SERVICE.read_text(encoding="utf-8")
    presenter = PRESENTER.read_text(encoding="utf-8")

    for sentence in (
        "The host information probe failed",
        "The remote host returned unreadable system information",
    ):
        assert sentence not in daemon
        assert sentence in presenter
