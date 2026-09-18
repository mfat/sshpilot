"""Keep PEP 562 lazy package exports in sync with ``__all__``.

``sshpilot.api``, ``sshpilot.api.models``, and ``sshpilot.daemon`` defer
heavy submodule imports via ``_LAZY_EXPORTS`` + ``__getattr__``. Every public
name must be either eagerly bound at import time or listed in the lazy map —
otherwise ``from pkg import Name`` fails at runtime after a casual ``__all__``
edit.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

_LAZY_MODULES = (
    ("sshpilot.api", SOURCE / "api" / "__init__.py"),
    ("sshpilot.api.models", SOURCE / "api" / "models" / "__init__.py"),
    ("sshpilot.daemon", SOURCE / "daemon" / "__init__.py"),
)


def _string_constants(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return {
            elt.value
            for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
    return set()


def _lazy_export_keys(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    return {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _parse_export_maps(path: Path) -> tuple[set[str], set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    all_names: set[str] = set()
    lazy_names: set[str] = set()
    eager_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.If) and (
            (
                isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"
            )
            or (
                isinstance(node.test, ast.Attribute)
                and node.test.attr == "TYPE_CHECKING"
            )
        ):
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "__all__":
                    all_names = _string_constants(value)
                elif target.id == "_LAZY_EXPORTS":
                    lazy_names = _lazy_export_keys(value)

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                eager_names.add(alias.asname or alias.name)

    return all_names, lazy_names, eager_names


@pytest.mark.parametrize(("module_name", "path"), _LAZY_MODULES)
def test_lazy_exports_cover_all(module_name: str, path: Path) -> None:
    all_names, lazy_names, eager_names = _parse_export_maps(path)
    eager_public = all_names & eager_names

    assert all_names, f"{module_name}: empty __all__"
    assert lazy_names, f"{module_name}: missing _LAZY_EXPORTS"
    assert lazy_names <= all_names, (
        f"{module_name}: _LAZY_EXPORTS has names not in __all__: "
        f"{sorted(lazy_names - all_names)}"
    )
    assert eager_public | lazy_names == all_names, (
        f"{module_name}: __all__ names neither lazily mapped nor eagerly imported: "
        f"{sorted(all_names - eager_public - lazy_names)}"
    )
    assert eager_public.isdisjoint(lazy_names), (
        f"{module_name}: names both eagerly imported and in _LAZY_EXPORTS: "
        f"{sorted(eager_public & lazy_names)}"
    )


@pytest.mark.parametrize(("module_name", "path"), _LAZY_MODULES)
def test_lazy_exports_resolve(module_name: str, path: Path) -> None:
    del path  # used only for parametrization alignment with the drift test
    mod = importlib.import_module(module_name)
    lazy = getattr(mod, "_LAZY_EXPORTS")
    for name, (submodule, attr) in sorted(lazy.items()):
        value = getattr(mod, name)
        source = importlib.import_module(f".{submodule}", module_name)
        assert value is getattr(source, attr), (
            f"{module_name}.{name} did not resolve to {submodule}.{attr}"
        )
