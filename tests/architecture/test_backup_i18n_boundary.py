"""Architecture checks for frontend-owned backup/import localization."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"

BACKEND_PRODUCERS = (
    SOURCE / "backup_backends.py",
    SOURCE / "backup_manager.py",
    SOURCE / "core" / "connections" / "repository.py",
    SOURCE / "daemon" / "secret_backend_service.py",
    SOURCE / "daemon" / "secret_transfer.py",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_backup_backend_producers_never_call_gettext():
    for path in BACKEND_PRODUCERS:
        tree = _tree(path)
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "gettext" not in imports, path
        assert "_" not in calls, path
        assert "N_" not in calls, path


def test_secret_transfer_results_do_not_embed_rendered_message_literals():
    for path in (
        SOURCE / "daemon" / "secret_backend_service.py",
        SOURCE / "daemon" / "secret_transfer.py",
    ):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name != "SecretTransferResult":
                continue
            message = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "message"),
                None,
            )
            assert not (
                isinstance(message, ast.JoinedStr)
                or (
                    isinstance(message, ast.Constant)
                    and isinstance(message.value, str)
                )
            ), path


def test_backup_section_categories_are_stable_codes_not_english_labels():
    tree = _tree(SOURCE / "backup_backends.py")
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_NOTE_SECTIONS"
            for target in node.targets
        )
    )
    labels = {
        item.elts[0].value
        for item in assignment.value.elts
        if isinstance(item, ast.Tuple)
    }
    assert labels == {
        "app_settings",
        "ssh_config",
        "known_hosts",
        "credentials",
        "private_keys",
    }


def test_transfer_presenter_is_the_gettext_extraction_owner():
    potfiles = (ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines()
    assert "src/sshpilot/gtk/secret_transfer_messages.py" in potfiles
    assert "src/sshpilot/daemon/secret_transfer.py" not in potfiles
    assert "src/sshpilot/backup_backends.py" not in potfiles
