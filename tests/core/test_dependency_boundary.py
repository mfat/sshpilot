"""AST-based dependency-direction enforcement for the GTK-free boundary."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

FORBIDDEN_MODULES = {
    "gi",
    "gi.repository",
}
FORBIDDEN_NAMES = {"Gtk", "Gdk", "Adw", "Vte", "GLib", "Gio", "GObject"}

# Packages that must remain GTK-free.
BOUNDARY_PACKAGES = ("core", "api", "daemon")

# Allowed reverse-direction imports (none today).
ALLOWED_CORE_TO_GTK: set[str] = set()


def _iter_py_files(package: str):
    base = ROOT / package
    yield from sorted(base.rglob("*.py"))


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if alias.name in FORBIDDEN_MODULES or root == "gi":
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "gi" or mod.startswith("gi."):
                hits.append(f"from {mod} import ...")
                continue
            if mod in {"gi.repository"} or mod.startswith("gi.repository"):
                for alias in node.names:
                    if alias.name in FORBIDDEN_NAMES or alias.name == "*":
                        hits.append(f"from {mod} import {alias.name}")
            # Also catch ``from gi.repository import X`` style already covered.
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES and (
                    mod.endswith("repository") or "gi" in mod.split(".")
                ):
                    hits.append(f"from {mod} import {alias.name}")
    return hits


def _imports_gtk_package(path: Path) -> list[str]:
    """Detect imports of sshpilot GTK-facing packages from core/api/daemon."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    forbidden_prefixes = (
        "sshpilot.gtk",
        "sshpilot.main",
        "sshpilot.window",
        "sshpilot.preferences",
        "sshpilot.terminal",
        "sshpilot.sidebar",
        "sshpilot.connection_dialog",
        "sshpilot.known_hosts_editor",
        "sshpilot.secret_unlock_dialog",
    )
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name in ALLOWED_CORE_TO_GTK:
                continue
            if any(name == p or name.startswith(p + ".") for p in forbidden_prefixes):
                hits.append(name)
    return hits


def test_boundary_packages_have_no_gi_imports():
    failures: list[str] = []
    for package in BOUNDARY_PACKAGES:
        for path in _iter_py_files(package):
            for hit in _forbidden_imports(path):
                failures.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not failures, "Forbidden gi/GTK imports:\n" + "\n".join(failures)


def test_dependency_direction_core_api_daemon_do_not_import_gtk():
    failures: list[str] = []
    for package in BOUNDARY_PACKAGES:
        for path in _iter_py_files(package):
            for hit in _imports_gtk_package(path):
                failures.append(f"{path.relative_to(ROOT)} imports {hit}")
    assert not failures, "Forbidden dependency direction:\n" + "\n".join(failures)
