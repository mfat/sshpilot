import os
import sys
import types

import pytest


# Pre-existing test failures tracked in #987. The original list (introduced by
# the CI PR #985) bundled three buckets: API/architecture drift, gi/paramiko stub
# gaps, and order-dependent module-state leakage. The drift and order-dependence
# entries have since been fixed at the source (tests rewritten to today's
# native-SSH behaviour; order-sensitive files made to re-import a consistent
# module set / use the app's own class objects). What remains is purely
# environment-specific: tests that need a binary/package absent from CI's slim
# image. They pass locally (where the binary exists) but fail in CI, so they stay
# marked xfail; ``strict=False`` means a local XPASS won't fail the build.
#
# Remove an entry once CI grows the dependency it needs.
_KNOWN_FAILING_NODEIDS = {
    # Environment-specific (need binaries / pip packages not in CI's slim image).
    # These pass locally where the binaries exist but fail in CI's slim image, so
    # they stay tracked (strict=False means a local XPASS won't fail the build).
    "tests/test_certificate_support.py::test_certificate_support",  # needs ssh-keygen
    "tests/test_key_discovery.py::test_discover_keys_recurses",     # needs /usr/bin/python3 + paramiko
}


def pytest_collection_modifyitems(config, items):
    xfail_marker = pytest.mark.xfail(
        reason="Environment-specific pre-existing failure tracked in #987; see tests/conftest.py.",
        strict=False,
    )
    for item in items:
        if item.nodeid in _KNOWN_FAILING_NODEIDS:
            item.add_marker(xfail_marker)


# Ensure project root is on sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, 'src')
if os.path.isdir(SRC) and SRC not in sys.path:
    sys.path.insert(0, SRC)


class _DummyGITypeMeta(type):
    def __getattr__(cls, name):
        value = _DummyGITypeMeta(name, (object,), {})
        setattr(cls, name, value)
        return value

    def __call__(cls, *args, **kwargs):
        return object()


def _make_dummy_gi_type(name: str):
    return _DummyGITypeMeta(name, (object,), {})


class _DummyGIModule(types.ModuleType):
    def __getattr__(self, name):
        value = _make_dummy_gi_type(name)
        setattr(self, name, value)
        return value


def _build_secret_stub():
    return types.SimpleNamespace(
        Schema=types.SimpleNamespace(new=lambda *a, **k: object()),
        SchemaFlags=types.SimpleNamespace(NONE=0),
        SchemaAttributeType=types.SimpleNamespace(STRING=0),
        password_store_sync=lambda *a, **k: True,
        password_lookup_sync=lambda *a, **k: None,
        password_clear_sync=lambda *a, **k: None,
        COLLECTION_DEFAULT=None,
    )


if 'gi' not in sys.modules:
    gi = types.ModuleType('gi')
    gi.require_version = lambda *args, **kwargs: None
    repository = _DummyGIModule('gi.repository')

    gi.repository = repository
    sys.modules['gi'] = gi
    sys.modules['gi.repository'] = repository

    # Provide concrete stubs for modules referenced in tests
    gobject_module = _DummyGIModule('gi.repository.GObject')
    setattr(gobject_module, 'Object', _make_dummy_gi_type('Object'))
    setattr(
        gobject_module,
        'SignalFlags',
        types.SimpleNamespace(RUN_FIRST=None, RUN_LAST=None),
    )
    setattr(repository, 'GObject', gobject_module)
    sys.modules['gi.repository.GObject'] = gobject_module

    glib_module = _DummyGIModule('gi.repository.GLib')
    setattr(glib_module, 'idle_add', lambda *a, **k: None)
    setattr(repository, 'GLib', glib_module)
    sys.modules['gi.repository.GLib'] = glib_module

    secret_module = _build_secret_stub()
    setattr(repository, 'Secret', secret_module)
    sys.modules['gi.repository.Secret'] = secret_module

    for name in ['Gtk', 'Adw', 'Gio', 'Gdk', 'GdkPixbuf', 'Pango', 'PangoFT2', 'Vte', 'GtkSource']:
        submodule = _DummyGIModule(f'gi.repository.{name}')
        setattr(repository, name, submodule)
        sys.modules[f'gi.repository.{name}'] = submodule


# ``terminal_manager.py`` does a hard top-level ``import cairo``; the slim/no-GTK
# CI image ships ``gi`` (or the stub above) but not the ``cairo`` package, so any
# test importing ``sshpilot.window`` / ``sshpilot.main`` would die at collection.
# Provide a dummy so the import resolves (cairo's API is only touched inside
# runtime drawing callbacks, never at module import time). Guarded so a real
# cairo (local dev) is never shadowed.
if 'cairo' not in sys.modules:
    sys.modules['cairo'] = _DummyGIModule('cairo')
