"""AST-based dependency-direction enforcement for the GTK-free boundary."""
from __future__ import annotations

import ast
from pathlib import Path

from sshpilot.core.package_graph import (
    BOUNDARY_PACKAGES,
    FORBIDDEN_GI_MODULES,
    FORBIDDEN_GI_NAMES,
    FORBIDDEN_UI_PREFIXES,
)

ROOT = Path(__file__).resolve().parents[2] / "src" / "sshpilot"


def _iter_py_files(package: str):
    base = ROOT / package
    yield from sorted(base.rglob("*.py"))


def _collect_imports(path: Path) -> list[tuple[str, str]]:
    """Return (kind, module_name) for absolute, relative, aliased, and constant importlib calls."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hits.append(("import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue
            if node.level and node.module:
                # Relative import within package — resolve against file package.
                pkg_parts = path.relative_to(ROOT).parts[:-1]
                if node.level > 1:
                    pkg_parts = pkg_parts[: -(node.level - 1)]
                base = ".".join(("sshpilot",) + pkg_parts)
                hits.append(("from", f"{base}.{node.module}" if pkg_parts else f"sshpilot.{node.module}"))
            elif node.module:
                hits.append(("from", node.module))
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name) and func.id in {"__import__", "import_module"}:
                name = func.id
            elif isinstance(func, ast.Attribute) and func.attr in {"import_module", "__import__"}:
                name = func.attr
            if name and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    hits.append(("dynamic", arg0.value))
    return hits


def _forbidden_gi(path: Path) -> list[str]:
    hits: list[str] = []
    for kind, name in _collect_imports(path):
        root = name.split(".", 1)[0]
        if name in FORBIDDEN_GI_MODULES or root == "gi":
            hits.append(f"{kind} {name}")
        if name.startswith("gi.repository"):
            hits.append(f"{kind} {name}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gi"):
            for alias in node.names:
                if alias.name in FORBIDDEN_GI_NAMES or alias.name == "*":
                    hits.append(f"from {node.module} import {alias.name}")
    return hits


def _forbidden_ui(path: Path) -> list[str]:
    hits: list[str] = []
    for _kind, name in _collect_imports(path):
        if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_UI_PREFIXES):
            hits.append(name)
    return hits


def test_boundary_packages_have_no_gi_imports():
    failures: list[str] = []
    for package in BOUNDARY_PACKAGES:
        for path in _iter_py_files(package):
            for hit in _forbidden_gi(path):
                failures.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not failures, "Forbidden gi/GTK imports:\n" + "\n".join(failures)


def test_dependency_direction_core_api_daemon_do_not_import_gtk():
    failures: list[str] = []
    for package in BOUNDARY_PACKAGES:
        for path in _iter_py_files(package):
            for hit in _forbidden_ui(path):
                failures.append(f"{path.relative_to(ROOT)} imports {hit}")
    assert not failures, "Forbidden dependency direction:\n" + "\n".join(failures)


def test_package_graph_manifest_lists_boundary_packages():
    assert set(BOUNDARY_PACKAGES) == {"core", "api", "daemon"}
    assert "sshpilot.gtk" in FORBIDDEN_UI_PREFIXES
