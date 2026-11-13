"""Text views leave ibus once their window is gone (GH #1374).

The crash itself needs X11, ibus and GTK 4.24, so these tests pin what the
guard does to a real ``Gtk.TextView``: an unrealized view uses GTK's built-in
input method, and a view realized again gets its own input method back.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROBE = r"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk
if not Gtk.init_check():
    raise SystemExit(77)
from sshpilot.gtk.text_view_im_guard import PARKED_IM_MODULE, install_text_view_im_guard

assert install_text_view_im_guard() is True
assert install_text_view_im_guard() is True  # idempotent

def settle():
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)

view = Gtk.TextView()
custom = Gtk.TextView()
custom.set_property('im-module', 'gtk-im-context-wayland')

win = Gtk.Window()
box = Gtk.Box()
box.append(view)
box.append(custom)
win.set_child(box)
# Realized but never mapped: no window appears on the desktop.
view.realize()
custom.realize()
settle()
assert view.get_realized()
assert view.get_property('im-module') is None

win.destroy()
settle()
assert not view.get_realized()
assert view.get_property('im-module') == PARKED_IM_MODULE
assert custom.get_property('im-module') == PARKED_IM_MODULE

box.remove(view)
box.remove(custom)
again = Gtk.Window()
box2 = Gtk.Box()
box2.append(view)
box2.append(custom)
again.set_child(box2)
view.realize()
custom.realize()
settle()
assert view.get_realized()
assert view.get_property('im-module') is None
assert custom.get_property('im-module') == 'gtk-im-context-wayland'
again.destroy()
settle()
print('OK')
"""


def test_unrealized_text_view_is_parked_and_restored():
    probe = subprocess.run(
        [sys.executable, "-c", "import gi; gi.require_version('Gtk', '4.0')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        pytest.skip("real PyGObject/GTK 4 not available")
    import sshpilot

    env = dict(os.environ)
    src = str(Path(sshpilot.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=60, env=env
    )
    if done.returncode == 77:
        pytest.skip("no display for GTK")
    assert done.returncode == 0, done.stderr
    assert "OK" in done.stdout


def test_hooks_ignore_other_widgets_and_stay_installed():
    from sshpilot.gtk import text_view_im_guard as guard

    class NotATextView:
        def set_property(self, *_args):
            raise AssertionError("must not touch non-text-view widgets")

    try:
        from gi.repository import Gtk  # noqa: F401
    except Exception:
        pytest.skip("gi not importable")
    assert guard._on_unrealize(NotATextView()) is True
    assert guard._on_realize(NotATextView()) is True
