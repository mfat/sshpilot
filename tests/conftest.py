import os
import sys
import types

# Ensure project root is on sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


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

    for name in ['Gtk', 'Adw', 'Gio', 'Gdk', 'Pango', 'PangoFT2']:
        submodule = _DummyGIModule(f'gi.repository.{name}')
        setattr(repository, name, submodule)
        sys.modules[f'gi.repository.{name}'] = submodule
