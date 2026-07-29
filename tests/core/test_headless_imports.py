"""Headless import gate: core / api / daemon must not touch ``gi``."""
from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


FORBIDDEN_ROOTS = (
    "gi",
    "gi.repository",
)


class _BlockedGi(ModuleType):
    """Module stand-in that fails on any attribute access."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.__path__ = []  # type: ignore[attr-defined]

    def __getattr__(self, item: str):
        raise ImportError(f"gi is blocked in headless tests ({self.__name__}.{item})")


HEADLESS_MODULES = (
    "sshpilot.core",
    "sshpilot.core.errors",
    "sshpilot.core.settings",
    "sshpilot.core.validation",
    "sshpilot.core.keys",
    "sshpilot.core.known_hosts",
    "sshpilot.core.secrets",
    "sshpilot.core.plugins",
    "sshpilot.core.forwards",
    "sshpilot.core.import_export",
    "sshpilot.core.ssh",
    "sshpilot.core.cli",
    "sshpilot.api",
    "sshpilot.daemon",
)


@pytest.fixture
def blocked_gi():
    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name == "gi"
        or name.startswith("gi.")
        or name.startswith("sshpilot.core")
        or name.startswith("sshpilot.api")
        or name.startswith("sshpilot.daemon")
    }
    for name in list(saved):
        sys.modules.pop(name, None)

    for name in FORBIDDEN_ROOTS + (
        "gi.repository.Gtk",
        "gi.repository.Gdk",
        "gi.repository.Adw",
        "gi.repository.GLib",
        "gi.repository.Gio",
        "gi.repository.GObject",
        "gi.repository.Vte",
        "gi.repository.Secret",
    ):
        sys.modules[name] = _BlockedGi(name)

    try:
        yield
    finally:
        for name in list(sys.modules):
            if (
                name == "gi"
                or name.startswith("gi.")
                or name.startswith("sshpilot.core")
                or name.startswith("sshpilot.api")
                or name.startswith("sshpilot.daemon")
            ):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def test_headless_core_api_daemon_import_without_gi(blocked_gi):
    for mod_name in HEADLESS_MODULES:
        importlib.import_module(mod_name)
