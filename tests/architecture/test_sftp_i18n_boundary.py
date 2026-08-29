"""Architecture checks for frontend-owned direct SFTP error localization."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"
RUNTIME = SOURCE / "daemon" / "sftp_runtime.py"
PRESENTER = SOURCE / "gtk" / "sftp_error_messages.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_sftp_daemon_never_calls_gettext():
    tree = _tree(RUNTIME)
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
    assert "src/sshpilot/daemon/sftp_runtime.py" not in potfiles


def test_sftp_presenter_does_not_depend_on_service_failure_models():
    imports = {
        node.module for node in ast.walk(_tree(PRESENTER)) if isinstance(node, ast.ImportFrom)
    }

    assert "..api.models.operations" not in imports
    assert "..api.models.transfers" not in imports


def test_file_manager_item_count_text_remains_out_of_scope():
    pane_source = (SOURCE / "file_manager" / "pane.py").read_text(encoding="utf-8")

    assert pane_source.count('f"{entry.item_count} items"') == 3
