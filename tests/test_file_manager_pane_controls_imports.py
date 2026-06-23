"""Regression tests for ``sshpilot.file_manager.pane_controls``.

Issue #981 (``NameError: name '_MIN_ICON_LEVEL' is not defined``) happened
because ``pane_controls.py`` was extracted from the original
``file_manager_window`` module and kept referencing three module-level
constants (``_MIN_ICON_LEVEL``, ``_MAX_ICON_LEVEL``, ``_DEFAULT_ICON_LEVEL``)
without bringing the imports along. The references live inside class
methods, so the bug stayed hidden until the zoom-slider was actually built
at runtime.

These tests catch the class of mistake statically:

* The three specific constants must be resolvable from ``pane_controls``.
* No function or method in ``pane_controls`` may ``LOAD_GLOBAL`` a name
  that isn't resolvable as a module attribute or a Python builtin.
"""

from __future__ import annotations

import builtins
import dis
import importlib
import inspect
import sys
import types


def _ensure_paramiko_stub() -> None:
    """Match the stub other file-manager tests install (see test_file_pane_typeahead)."""
    if "paramiko" in sys.modules:
        return

    class _DummySSHClient:
        def set_missing_host_key_policy(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            pass

        def open_sftp(self):
            return types.SimpleNamespace(close=lambda: None)

        def close(self):
            pass

    sys.modules["paramiko"] = types.SimpleNamespace(
        SSHClient=_DummySSHClient,
        AutoAddPolicy=type("AutoAddPolicy", (), {}),
    )


def _import_pane_controls():
    _ensure_paramiko_stub()
    return importlib.import_module("sshpilot.file_manager.pane_controls")


def _iter_code_objects(module):
    """Yield every code object reachable from ``module``'s top level."""
    seen: set[int] = set()
    stack = []
    for value in vars(module).values():
        if inspect.isfunction(value) or inspect.ismethod(value):
            stack.append(value.__code__)
        elif inspect.isclass(value) and value.__module__ == module.__name__:
            for attr in vars(value).values():
                if inspect.isfunction(attr):
                    stack.append(attr.__code__)
                elif isinstance(attr, (staticmethod, classmethod)):
                    stack.append(attr.__func__.__code__)
    while stack:
        code = stack.pop()
        if id(code) in seen:
            continue
        seen.add(id(code))
        yield code
        for const in code.co_consts:
            if inspect.iscode(const):
                stack.append(const)


def test_icon_level_constants_are_importable():
    """Issue #981: pane_controls must expose the three icon-level constants.

    They don't need to be defined in this module, but they must be reachable
    via module globals so the zoom-slider code can read them.
    """
    pane_controls = _import_pane_controls()

    for name in ("_MIN_ICON_LEVEL", "_MAX_ICON_LEVEL", "_DEFAULT_ICON_LEVEL"):
        assert hasattr(pane_controls, name), (
            f"pane_controls must expose {name!r} so the zoom-slider code "
            f"can resolve it at runtime (regression of #981)"
        )


def test_pane_controls_has_no_unresolved_global_names():
    """Every LOAD_GLOBAL in pane_controls must resolve to a module attr or builtin.

    This is the generic version of #981: any future extraction that forgets
    to bring a constant or helper along will fail here instead of crashing
    a user at runtime.
    """
    pane_controls = _import_pane_controls()

    module_names = set(vars(pane_controls))
    builtin_names = set(dir(builtins))

    unresolved: dict[str, list[str]] = {}
    for code in _iter_code_objects(pane_controls):
        for instr in dis.get_instructions(code):
            if instr.opname not in ("LOAD_GLOBAL", "LOAD_NAME"):
                continue
            name = instr.argval
            if name in module_names or name in builtin_names:
                continue
            unresolved.setdefault(code.co_qualname, []).append(name)

    assert not unresolved, (
        "Unresolved global names in sshpilot.file_manager.pane_controls "
        f"(regression of #981 class): {unresolved}"
    )
