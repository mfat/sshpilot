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


@pytest.fixture(autouse=True, scope="session")
def _pin_english_ui():
    """Run the suite in English regardless of the developer's locale.

    A compiled catalogue now ships inside the package (src/sshpilot/locale, for
    runs that never see Meson), so gettext will happily translate at test time.
    Every assertion here that compares user-visible text assumes English, so a
    German-locale developer would otherwise see dozens of spurious failures.
    """
    previous = os.environ.get("LANGUAGE")
    os.environ["LANGUAGE"] = "en"
    yield
    if previous is None:
        os.environ.pop("LANGUAGE", None)
    else:
        os.environ["LANGUAGE"] = previous


def pytest_ignore_collect(collection_path, config):
    """In GUI mode, collect only ``test_gui_*`` modules.

    ``SSHPILOT_GUI_TESTS=1`` makes the env-gate below load the *real* PyGObject.
    The rest of the suite is written against the stubbed ``gi`` and imports
    ``sshpilot.main`` / ``sshpilot.window`` at module level; importing those under
    real GTK during bare collection (no display init yet) can segfault
    (``main.py`` loads a GResource bundle at import). So when GUI mode is on, skip
    every ``test_*`` module that is not a GUI test. CI never sets the env var, so
    this is a no-op there.
    """
    if os.environ.get('SSHPILOT_GUI_TESTS') != '1':
        return False
    name = os.path.basename(str(collection_path))
    if name.startswith('test_') and not name.startswith('test_gui'):
        return True
    return False


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


# GUI tests (``-m gui``, see tests/_gui_harness.py) need the REAL PyGObject, not
# the stubs below. When opted in via SSHPILOT_GUI_TESTS=1, import the real gi
# first so the ``'gi' not in sys.modules`` guard then leaves it untouched. If the
# real import fails (no PyGObject), fall through to the stubs so the normal suite
# still collects. Default behaviour (no env var) is unchanged.
_USE_REAL_GTK = os.environ.get('SSHPILOT_GUI_TESTS') == '1'
if _USE_REAL_GTK:
    try:
        import gi  # real PyGObject; populates sys.modules['gi'] + repository

        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Gtk, Adw, Gio, GLib  # noqa: F401
    except Exception:
        _USE_REAL_GTK = False


if not _USE_REAL_GTK and 'gi' not in sys.modules:
    gi = types.ModuleType('gi')
    gi.require_version = lambda *args, **kwargs: None
    repository = _DummyGIModule('gi.repository')

    gi.repository = repository
    sys.modules['gi'] = gi
    sys.modules['gi.repository'] = repository

    # Provide concrete stubs for modules referenced in tests
    gobject_module = _DummyGIModule('gi.repository.GObject')
    # GObject.Object must be a real, subclassable base with a *normal* metaclass:
    # app classes like Config/ConnectionManager subclass it and get instantiated
    # in tests. The auto-stub metaclass (_DummyGITypeMeta) returns a bare
    # object() from __call__, so a subclass built on it yields object() (with no
    # methods) when instantiated. Several test modules used to work around this
    # by rebinding GObject.Object at import time, which leaked across the shared
    # stub and made the suite order-dependent (see #985). Fixing it once here
    # makes every GObject subclass instantiate normally regardless of import
    # order. Other gi types (Gtk/Adw/…) keep the dummy metaclass.
    setattr(gobject_module, 'Object', type('Object', (object,), {}))
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

    # Blueprint-migrated widgets need a no-op ``Gtk.Template`` under the stubbed
    # gi so the classes still import (shared with the tests that build their own
    # gi stub — see tests/gtk_template_stub.py).
    from gtk_template_stub import install_template_stub
    install_template_stub(sys.modules['gi.repository.Gtk'])


# ``terminal_manager.py`` does a hard top-level ``import cairo``; the slim/no-GTK
# CI image ships ``gi`` (or the stub above) but not the ``cairo`` package, so any
# test importing ``sshpilot.window`` / ``sshpilot.main`` would die at collection.
# Provide a dummy so the import resolves (cairo's API is only touched inside
# runtime drawing callbacks, never at module import time). Guarded so a real
# cairo (local dev) is never shadowed.
if 'cairo' not in sys.modules:
    sys.modules['cairo'] = _DummyGIModule('cairo')


# --- Real-GTK GUI test fixtures (opt-in, marker ``gui``) -------------------
# Defined here (not in tests/_gui_harness.py) so the session-scoped app boots
# exactly once and is shared across every GUI test module. They are lazy: the
# real-GTK imports happen inside the fixture body, so they never affect the
# normal stubbed suite. See tests/_gui_harness.py for the helpers.

@pytest.fixture(scope='session')
def _gui_app_session(tmp_path_factory):
    """Boot the real app once per session in a hermetic temp HOME/XDG."""
    from tests._gui_harness import GuiApp, requires_gui

    requires_gui()
    cfg = tmp_path_factory.mktemp('sshpilot-gui-home')
    keys = ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')
    saved = {k: os.environ.get(k) for k in keys}
    os.environ['HOME'] = str(cfg)
    os.environ['XDG_CONFIG_HOME'] = str(cfg / 'config')
    os.environ['XDG_DATA_HOME'] = str(cfg / 'data')
    os.environ['XDG_STATE_HOME'] = str(cfg / 'state')
    os.environ['XDG_CACHE_HOME'] = str(cfg / 'cache')

    app = GuiApp()
    app.boot()
    try:
        yield app
    finally:
        app.shutdown()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def gui(_gui_app_session):
    """Per-test handle: a clean window (all user tabs closed) backed by the session app."""
    _gui_app_session.reset()
    return _gui_app_session
