import argparse
import os
import sys
import types

import pytest

# Run the suite in English regardless of the developer's locale. A compiled
# catalogue ships inside the package (src/sshpilot/locale, for runs that never
# see Meson), so gettext translates happily at test time, and every assertion
# here that compares user-visible text assumes English.
#
# Set at conftest import, not from a fixture: modules are imported during
# collection, and several bake their labels at import time
# (shortcut_editor.ACTION_LABELS, connection_sort.CONNECTION_SORT_PRESETS,
# authorized_keys_window._RESTRICT_OPT_INS). A session fixture runs after that
# and would leave those constants frozen in the developer's language.
os.environ["LANGUAGE"] = "en"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("--race-repeats must be >= 1")
    return parsed


def pytest_addoption(parser):
    parser.addoption(
        "--race-repeats",
        type=_positive_int,
        default=1,
        help=(
            "Number of repetitions for concurrency/race tests. The ordinary "
            "suite runs each race scenario once; opt into heavier repetition "
            "for nightly/stress runs (e.g. --race-repeats=20)."
        ),
    )


def pytest_generate_tests(metafunc):
    """Parametrize the ``_repeat`` fixture from ``--race-repeats``.

    Race-sensitive daemon tests previously hard-coded repeated parametrize
    decorators (``@pytest.mark.parametrize("_repeat", range(5))``). Those are
    removed from the normal suite; the fixture is generated here instead so
    repetition is configurable and the development loop stays fast. Tests that
    keep their own ``_repeat`` parametrization (e.g. GUI smoke tests) are left
    alone via the marker guard below.
    """
    if "_repeat" not in metafunc.fixturenames:
        return
    for marker in metafunc.definition.iter_markers("parametrize"):
        argnames = getattr(marker, "args", ()) or ()
        if argnames and any(
            part.strip() == "_repeat" for part in str(argnames[0]).split(",")
        ):
            return
    repeats = metafunc.config.getoption("--race-repeats")
    metafunc.parametrize("_repeat", range(repeats))


# Pre-existing environment gaps used to be tracked as non-strict xfails in #987.
# Those entries XPASS on developer machines (ssh-keygen / system python present).
# Prefer explicit skipif inside the tests themselves so CI skips cleanly and a
# local pass is a normal PASS — not an XPASS.
_KNOWN_FAILING_NODEIDS: set[str] = set()


def pytest_ignore_collect(collection_path, config):
    """In GUI mode, collect only GUI test modules.

    ``SSHPILOT_GUI_TESTS=1`` makes the env-gate below load the *real* PyGObject.
    The rest of the suite is written against the stubbed ``gi`` and imports
    ``sshpilot.main`` / ``sshpilot.window`` at module level; importing those under
    real GTK during bare collection (no display init yet) can segfault
    (``main.py`` loads a GResource bundle at import). So when GUI mode is on, skip
    every ``test_*`` module that is not a GUI test. CI never sets the env var, so
    this is a no-op there.

    Collected when GUI mode is on:
    * ``test_gui_*.py`` anywhere under ``tests/``
    * any ``test_*.py`` under ``tests/gui/`` (Phase 14 production GTK gates)
    """
    if os.environ.get('SSHPILOT_GUI_TESTS') != '1':
        return False
    path_str = str(collection_path)
    # Normalize separators for ``tests/gui`` package membership.
    norm = path_str.replace('\\', '/')
    if '/tests/gui/' in norm or norm.rstrip('/').endswith('/tests/gui'):
        return False
    name = os.path.basename(path_str)
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


def _cleanup_integration_containers(label):
    """Clean test containers only for runs that selected integration tests."""
    try:
        from tests.fixtures.temporary_openssh import cleanup_orphaned_temporary_openssh

        removed = cleanup_orphaned_temporary_openssh()
        if removed:
            print(f"[fixtures] {label} removed OpenSSH containers: {removed}")
    except Exception as exc:
        print(f"[fixtures] {label} OpenSSH cleanup skipped: {exc!r}")


def pytest_collection_finish(session):
    """Clean leftovers before a run that will actually exercise integration."""
    selected = any(
        item.get_closest_marker("integration") is not None
        for item in session.items
    )
    integration_run = selected and not session.config.option.collectonly
    session.config._sshpilot_integration_selected = integration_run
    if integration_run:
        _cleanup_integration_containers("pre-run")


def pytest_sessionfinish(session, exitstatus):
    """Sweep fixtures only if this invocation selected integration tests."""
    if getattr(session.config, "_sshpilot_integration_selected", False):
        _cleanup_integration_containers("session-end")


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
        # Module/introspection tooling probes attributes such as ``__file__``.
        # Pretending those are GI types breaks inspect (and therefore coverage)
        # because it expects the metadata attributes to have standard values.
        if name.startswith('__'):
            raise AttributeError(name)
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
_real_gtk_available = False
if _USE_REAL_GTK:
    try:
        import gi  # real PyGObject; populates sys.modules['gi'] + repository

        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Gtk, Adw, Gio, GLib  # noqa: F401

        _real_gtk_available = True
    except Exception:
        _USE_REAL_GTK = False


# When real GTK is available (either opt-in GUI tests or a full test run that
# already has `gi` in sys.modules), preload the GResource bundle so that any
# module with ``@Gtk.Template(resource_path=...)`` can find its .ui files.
# Without this, transitive imports of template-decorated widgets fail with
# "resource does not exist" in subprocess environments (e.g. the smoke harness).
def _preload_gresource():
    if 'gi' not in sys.modules:
        return
    try:
        from gi.repository import Gio as _Gio
    except Exception:
        return
    for _res_path in [
        os.path.join(ROOT, 'src', 'sshpilot', 'resources', 'sshpilot.gresource'),
        os.path.join(ROOT, 'src', 'sshpilot', 'resources', 'io.github.mfat.sshpilot.gresource'),
        '/usr/share/io.github.mfat.sshpilot/io.github.mfat.sshpilot.gresource',
    ]:
        if os.path.exists(_res_path):
            try:
                _res = _Gio.Resource.load(_res_path)
                _Gio.resources_register(_res)
            except Exception:
                pass
            break


_preload_gresource()


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
    """Boot the real app once per session in a hermetic temp HOME/XDG/runtime.

    Isolates the GUI suite from the developer's user daemon socket by giving
    every session its own ``XDG_RUNTIME_DIR``. The production application
    starts or connects to the daemon on that isolated runtime path. Individual
    tests that need a specifically owned daemon start an owned ``DaemonServer``
    on a unique temp socket and inject it via ``_apply_client_selection``.
    """
    from tests._gui_harness import GuiApp, requires_gui

    requires_gui()
    cfg = tmp_path_factory.mktemp('sshpilot-gui-home')
    runtime = cfg / 'runtime'
    runtime.mkdir(mode=0o700)
    keys = (
        'HOME',
        'XDG_CONFIG_HOME',
        'XDG_DATA_HOME',
        'XDG_STATE_HOME',
        'XDG_CACHE_HOME',
        'XDG_RUNTIME_DIR',
    )
    saved = {k: os.environ.get(k) for k in keys}
    os.environ['HOME'] = str(cfg)
    os.environ['XDG_CONFIG_HOME'] = str(cfg / 'config')
    os.environ['XDG_DATA_HOME'] = str(cfg / 'data')
    os.environ['XDG_STATE_HOME'] = str(cfg / 'state')
    os.environ['XDG_CACHE_HOME'] = str(cfg / 'cache')
    os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    app = GuiApp()
    app.boot()
    # Sanity: the session app must use the daemon on the isolated runtime path.
    from sshpilot.api.daemon_client import DaemonClient

    assert isinstance(getattr(app.window, 'client', None), DaemonClient), (
        "GUI harness did not connect the mandatory daemon on its isolated runtime"
    )
    try:
        yield app
    finally:
        try:
            bridge = getattr(app.app, '_api_client_bridge', None)
            if bridge is not None:
                bridge.shutdown()
            client = getattr(app.window, 'client', None)
            if client is not None and hasattr(client, 'close'):
                client.close()
        except Exception:
            pass
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
