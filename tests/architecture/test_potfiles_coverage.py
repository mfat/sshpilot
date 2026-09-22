"""Keep gettext-using Python sources shipped with SSH Pilot in POTFILES."""

from __future__ import annotations

import ast
from fnmatch import fnmatchcase
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"
GETTEXT_FUNCTIONS = {"gettext", "ngettext", "pgettext", "npgettext"}


def _uses_gettext(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_aliases = set()
    function_aliases = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "gettext"
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "gettext":
                function_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in GETTEXT_FUNCTIONS
                )
            elif node.module in {"i18n", "sshpilot.i18n"}:
                function_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "N_"
                )

    # Some modules use ``import gettext; _ = gettext.gettext``.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id in module_aliases
            and value.attr in GETTEXT_FUNCTIONS
        ):
            function_aliases.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )

    return any(
        isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id in function_aliases
            )
            or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
                and node.func.attr in GETTEXT_FUNCTIONS
            )
        )
        for node in ast.walk(tree)
    )


def test_shipped_gettext_sources_are_in_potfiles():
    package_config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = package_config["tool"]["setuptools"]["packages"]["find"]["exclude"]
    potfiles = {
        line.strip()
        for line in (ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    missing = []
    for path in SOURCE.rglob("*.py"):
        module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
        if any(fnmatchcase(module, pattern) for pattern in excluded):
            continue
        if _uses_gettext(path) and path.relative_to(ROOT).as_posix() not in potfiles:
            missing.append(path.relative_to(ROOT).as_posix())

    assert sorted(missing) == []
