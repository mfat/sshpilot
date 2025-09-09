import os
import sys
import types

# Ensure project root is on sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Provide minimal gi stubs so connection_manager can be imported without GTK
if 'gi' not in sys.modules:
    gi = types.ModuleType('gi')
    gi.require_version = lambda *args, **kwargs: None
    repository = types.ModuleType('gi.repository')
    gi.repository = repository
    repository.GObject = types.SimpleNamespace(Object=object, SignalFlags=types.SimpleNamespace(RUN_FIRST=None))
    repository.GLib = types.SimpleNamespace()
    repository.Gtk = types.SimpleNamespace()
    sys.modules['gi'] = gi
    sys.modules['gi.repository'] = repository
    sys.modules['gi.repository.GObject'] = repository.GObject
    sys.modules['gi.repository.GLib'] = repository.GLib
    sys.modules['gi.repository.Gtk'] = repository.Gtk
