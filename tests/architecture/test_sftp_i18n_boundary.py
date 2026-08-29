"""Architecture checks for frontend-owned direct SFTP error localization."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"
RUNTIME = SOURCE / "daemon" / "sftp_runtime.py"
TRANSFER_RUNTIME = SOURCE / "daemon" / "transfer_runtime.py"
IDENTITY_RUNTIME = SOURCE / "daemon" / "identity_service.py"
SECRET_RUNTIME = SOURCE / "daemon" / "secret_backend_service.py"
PRESENTER = SOURCE / "gtk" / "sftp_error_messages.py"
FAILURE_PRESENTER = SOURCE / "gtk" / "sftp_failure_messages.py"
SCP_FAILURE_PRESENTER = SOURCE / "gtk" / "scp_failure_messages.py"
IDENTITY_FAILURE_PRESENTER = SOURCE / "gtk" / "identity_failure_messages.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_sftp_daemon_never_calls_gettext():
    for runtime in (RUNTIME, TRANSFER_RUNTIME, IDENTITY_RUNTIME, SECRET_RUNTIME):
        tree = _tree(runtime)
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


def test_direct_sftp_status_map_contains_only_stable_codes():
    tree = _tree(RUNTIME)
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_SFTP_STATUS_TO_ERROR_CODE"
            for target in node.targets
        )
    )

    assert all(isinstance(value, ast.Attribute) for value in assignment.value.values)


def test_sftp_presenter_is_the_gettext_extraction_owner():
    potfiles = (ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines()

    assert "src/sshpilot/gtk/sftp_error_messages.py" in potfiles
    assert "src/sshpilot/gtk/sftp_failure_messages.py" in potfiles
    assert "src/sshpilot/gtk/scp_failure_messages.py" in potfiles
    assert "src/sshpilot/gtk/identity_failure_messages.py" in potfiles
    assert "src/sshpilot/daemon/sftp_runtime.py" not in potfiles
    assert "src/sshpilot/daemon/transfer_runtime.py" not in potfiles


def test_sftp_presenter_does_not_depend_on_service_failure_models():
    imports = {
        node.module for node in ast.walk(_tree(PRESENTER)) if isinstance(node, ast.ImportFrom)
    }

    assert "..api.models.operations" not in imports
    assert "..api.models.transfers" not in imports


def test_structured_sftp_failure_presenter_owns_only_frontend_msgids():
    presenter = FAILURE_PRESENTER.read_text(encoding="utf-8")
    daemon_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (RUNTIME, TRANSFER_RUNTIME)
    )

    assert "N_(" in presenter
    assert "N_(" not in daemon_sources
    assert "gettext" not in daemon_sources


def test_terminal_failure_presenters_own_only_frontend_msgids():
    presenters = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (SCP_FAILURE_PRESENTER, IDENTITY_FAILURE_PRESENTER)
    )
    daemon_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (TRANSFER_RUNTIME, IDENTITY_RUNTIME)
    )

    assert "N_(" in presenters
    assert "N_(" not in daemon_sources
    assert "gettext" not in daemon_sources


def test_terminal_frontends_do_not_depend_on_summary_text():
    scp_source = (SOURCE / "scp_window.py").read_text(encoding="utf-8")
    identity_source = (SOURCE / "sshcopyid_window.py").read_text(encoding="utf-8")

    assert 'getattr(summary.failure, "message"' not in scp_source
    assert "summary.failure.message" not in scp_source
    assert "summary.message" not in identity_source


def test_file_manager_item_count_text_uses_frontend_gettext():
    pane_source = (SOURCE / "file_manager" / "pane.py").read_text(encoding="utf-8")
    properties_source = (
        SOURCE / "file_manager" / "properties_dialog.py"
    ).read_text(encoding="utf-8")
    format_source = (
        SOURCE / "file_manager" / "format_utils.py"
    ).read_text(encoding="utf-8")

    assert 'f"{entry.item_count} items"' not in pane_source
    assert "item{'s'" not in properties_source
    assert "ngettext(" in format_source

    potfiles = (ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines()
    assert "src/sshpilot/file_manager/format_utils.py" in potfiles


def test_direct_sftp_consumers_use_the_shared_frontend_formatter():
    for relative in (
        "authorized_keys_window.py",
        "scp_window.py",
        "text_editor.py",
    ):
        source = (SOURCE / relative).read_text(encoding="utf-8")
        assert "format_direct_sftp_error" in source
