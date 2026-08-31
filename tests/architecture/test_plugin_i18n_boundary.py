"""Architecture checks for frontend-owned built-in plugin localization."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"
BUILTINS = SOURCE / "plugins" / "builtin"
SHELL = BUILTINS / "_shell.py"
PLUGIN_PATHS = (
    BUILTINS / "docker_protocol" / "__init__.py",
    BUILTINS / "kubernetes_protocol" / "__init__.py",
    BUILTINS / "mosh_protocol" / "__init__.py",
    BUILTINS / "serial_protocol" / "__init__.py",
)
SHELL_FIELD_PATHS = PLUGIN_PATHS[:3]
RELEASE_NOTE_SOURCES = (
    ROOT / "data" / "io.github.mfat.sshpilot.metainfo.xml.in",
    ROOT / "debian" / "changelog",
    ROOT / "packaging" / "fedora" / "rpm.spec",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _method(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def test_shell_parser_helper_never_calls_gettext():
    tree = _tree(SHELL)
    imported_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert "gettext" not in imported_modules
    assert "_" not in _calls(tree)
    assert "N_" not in _calls(tree)


def test_shell_validation_and_daemon_helpers_stay_on_separate_paths():
    for path in SHELL_FIELD_PATHS:
        tree = _tree(path)
        validation_calls = _calls(_method(tree, "validate"))
        spawn_calls = _calls(_method(tree, "build_spawn"))

        assert "command_split_diagnostic" in validation_calls
        assert "split_command" not in validation_calls
        assert "split_command" in spawn_calls
        assert "command_split_diagnostic" not in spawn_calls
        assert "_" not in spawn_calls

    serial_spawn_calls = _calls(_method(_tree(PLUGIN_PATHS[-1]), "build_spawn"))
    assert "_" not in serial_spawn_calls


def test_localized_plugin_methods_are_frontend_only():
    expected_caller = SOURCE / "connection_dialog.py"

    for call in ("backend.validate(", "backend.connection_fields("):
        callers = {
            path
            for path in SOURCE.rglob("*.py")
            if call in path.read_text(encoding="utf-8")
        }
        assert callers == {expected_caller}


def test_frontend_gettext_owners_are_in_potfiles():
    potfiles = (ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines()

    for path in PLUGIN_PATHS:
        assert str(path.relative_to(ROOT)) in potfiles
    assert "src/sshpilot/connection_dialog.py" in potfiles
    assert "src/sshpilot/plugins/builtin/_shell.py" not in potfiles


def test_non_ssh_release_note_typo_is_fixed_in_every_maintained_source():
    for path in RELEASE_NOTE_SOURCES:
        source = path.read_text(encoding="utf-8")
        assert "Uprovements to non-SSH protocol plugins" not in source
        assert "Improvements to non-SSH protocol plugins" in source
