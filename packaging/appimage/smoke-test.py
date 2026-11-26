"""Check a bundled SSH Pilot under AppRun's environment.

Run by build-appimage.sh against the AppDir and by test-appimage.sh against a
finished AppImage (mounted and extracted): the bundled interpreter reads this
from stdin with the AppDir as argv[1], after sourcing AppRun minus its exec
line, and with SSHPILOT_APPIMAGE_LIBC naming the C library AppRun was told to
use. Exits non-zero with an assertion on the first thing that is wrong.
"""
import os
import subprocess
import sys

appdir = sys.argv[1]
assert sys.executable.startswith(appdir), sys.executable

# Which libc.so.6 is really mapped, not which one AppRun meant to pick.
with open("/proc/self/maps", encoding="utf-8") as maps:
    libc_paths = {line.split()[-1] for line in maps if line.rstrip().endswith("/libc.so.6")}
bundled_libc = os.path.join(appdir, "usr", "lib", "libc", "libc.so.6")
# Forced by the caller, or (test-appimage.sh, "auto") whatever AppRun chose.
libc_mode = os.environ.get("SSHPILOT_APPIMAGE_LIBC", "auto")
if libc_mode not in ("host", "bundled"):
    libc_mode = "bundled" if sys.executable.endswith("/python3-bundled-libc") else "host"
print("smoke test: %s C library" % libc_mode)
if libc_mode == "bundled":
    assert libc_paths == {bundled_libc}, libc_paths
    # sys.executable is what the app hands its children (daemon, askpass), so
    # it has to be the wrapper, or they would start on the host's libc again.
    assert sys.executable.endswith("/python3-bundled-libc"), sys.executable
else:
    assert bundled_libc not in libc_paths, libc_paths
child = subprocess.run(
    [sys.executable, "-c",
     "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk"],
    capture_output=True, text=True,
)
assert child.returncode == 0, child.stderr

# Everything below runs under AppRun's environment, so these are AppRun's
# paths, not ones this test invented.
for variable, expected in (
    ("GI_TYPELIB_PATH", "Gtk-4.0.typelib"),
    ("GSETTINGS_SCHEMA_DIR", "gschemas.compiled"),
    ("GIO_MODULE_DIR", None),
    ("WEBKIT_INJECTED_BUNDLE_PATH", "libwebkitgtkinjectedbundle.so"),
    ("PYXTERMJS_ASSETS_DIR", "xterm.js"),
):
    value = os.environ.get(variable)
    assert value and value.startswith(appdir), "%s=%r" % (variable, value)
    assert os.path.isdir(value), "%s=%r is not a directory" % (variable, value)
    if expected:
        assert os.path.exists(os.path.join(value, expected)), \
            "%s has no %s" % (variable, expected)

import gi

for namespace, version in (
    ("Gtk", "4.0"), ("Gdk", "4.0"), ("Adw", "1"), ("Vte", "3.91"),
    ("GtkSource", "5"), ("Secret", "1"), ("GLibUnix", "2.0"),
    ("PangoFT2", "1.0"), ("WebKit", "6.0"),
):
    gi.require_version(namespace, version)
from gi.repository import (  # noqa: F401
    Adw, Gdk, Gio, GLib, Gtk, GtkSource, Secret, Vte, WebKit,
)

# The dependencies the app imports at runtime.
import cairo, certifi, cryptography, keyring, psutil, yaml  # noqa: F401,E401

import sshpilot
from sshpilot.build_config import PKGDATADIR

assert sshpilot.__file__.startswith(appdir), sshpilot.__file__
gresource = os.path.join(PKGDATADIR, "io.github.mfat.sshpilot.gresource")
assert os.path.exists(gresource), gresource
Gio.resources_register(Gio.Resource.load(gresource))

# The app icon is an SVG, so the svg pixbuf loader has to be usable.
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

formats = {fmt.get_name() for fmt in GdkPixbuf.Pixbuf.get_formats()}
assert "svg" in formats, sorted(formats)

# The xterm.js assets the PyXterm.js backend renders. AppRun's
# PYXTERMJS_ASSETS_DIR is what keeps this off the host's libjs-xterm.
from sshpilot.xterm_shell import asset_dir, build_shell_html

assert asset_dir().startswith(appdir), asset_dir()
assert os.path.isfile(os.path.join(asset_dir(), "xterm.js")), asset_dir()
shell_html = build_shell_html()

# AppRun starts the app in the directory the relocated helper path resolves
# against; anything else and no WebView ever loads.
webkit_workdir = os.path.join(appdir, "usr", "lib")
if libc_mode == "bundled":
    webkit_workdir = os.path.join(webkit_workdir, "bundled-libc-workdir")
assert os.getcwd() == webkit_workdir, os.getcwd()
assert os.access(os.path.join(webkit_workdir, "webkitgtk-6.0", "WebKitWebProcess"), os.X_OK)

# A host program the app starts gets the host's environment and the directory
# the AppImage was started from -- not AppRun's bundled paths or the WebKit
# working directory. ``import sshpilot`` above installed the guard that does it.
probe = subprocess.run(
    ["/bin/sh", "-c", 'pwd; env'], capture_output=True, text=True, check=True,
)
probe_cwd, _, probe_env = probe.stdout.partition("\n")
assert probe_cwd == os.environ["SSHPILOT_APPIMAGE_CWD"], probe_cwd
for variable in ("GI_TYPELIB_PATH", "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR",
                 "WEBKIT_INJECTED_BUNDLE_PATH", "GDK_PIXBUF_MODULE_FILE", "APPDIR",
                 "SSHPILOT_APPIMAGE_VARS"):
    assert "\n%s=" % variable not in "\n" + probe_env, \
        "%s leaked into a host program" % variable
# Local shells start through VTE, which merges the env it is given into the
# app's own unless told not to -- the flag the app passes has to work.
gi_probe = __import__("gi")
gi_probe.require_version("Vte", "3.91")
from gi.repository import GLib as _GLib, Vte as _Vte  # noqa: E402
from sshpilot.appimage_env import host_environment, vte_spawn_flags  # noqa: E402

shell_out = os.path.join(os.environ.get("TMPDIR", "/tmp"), "sshpilot-smoke-vte-%d" % os.getpid())
_pty = _Vte.Pty.new_sync(_Vte.PtyFlags.DEFAULT)
_loop = _GLib.MainLoop()
_pty.spawn_async(
    os.environ["SSHPILOT_APPIMAGE_CWD"],
    ["/bin/sh", "-c", "env > '%s'" % shell_out],
    ["%s=%s" % item for item in host_environment(os.environ).items()],
    vte_spawn_flags(_GLib, _Vte), None, None, -1, None,
    lambda pty, result, *_: (_Vte.Pty.spawn_finish(pty, result),
                             _GLib.timeout_add(500, _loop.quit)),
)
_GLib.timeout_add_seconds(20, _loop.quit)
_loop.run()
with open(shell_out, encoding="utf-8") as handle:
    vte_env = handle.read()
os.unlink(shell_out)
for variable in ("GI_TYPELIB_PATH", "GIO_MODULE_DIR", "WEBKIT_INJECTED_BUNDLE_PATH",
                 "APPDIR", "SSHPILOT_APPIMAGE_VARS"):
    assert "\n%s=" % variable not in "\n" + vte_env, \
        "%s leaked into a VTE-spawned shell" % variable

# ...while a bundled one (the daemon, askpass) keeps what it needs.
bundled_probe = subprocess.run(
    [sys.executable, "-c", "import os; print(os.environ.get('GI_TYPELIB_PATH', ''))"],
    capture_output=True, text=True, check=True,
)
assert bundled_probe.stdout.strip() == os.environ["GI_TYPELIB_PATH"], bundled_probe.stdout


def _descendants(root):
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, encoding="utf-8") as stat:
                parent = int(stat.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(parent, []).append(int(entry))
    found, queue = {}, [root]
    while queue:
        pid = queue.pop()
        for child in children.get(pid, ()):
            found[child] = pid
            queue.append(child)
    return found


def _process(pid):
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except OSError:
        exe = ""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as cmdline:
            argv = cmdline.read().split(b"\0")
    except OSError:
        argv = [b""]
    return exe, [os.fsdecode(arg) for arg in argv if arg]


HELPERS = ("WebKitWebProcess", "WebKitNetworkProcess")


def _helper_name(exe, argv):
    """Which WebKit helper *exe*/*argv* is, if any.

    On the host C library the helper is argv[0]. On the bundled one the
    launcher execs the bundled loader, whose argv[0] in /proc stays the loader
    (``--argv0`` only changes what the helper itself sees), with the helper a
    few arguments later. bwrap names the helper after its options too, but it
    is not one.
    """
    if os.path.basename(exe) == "bwrap":
        return None
    for arg in argv[:7]:
        if os.path.basename(arg) in HELPERS:
            return os.path.basename(arg)
    return None


# Importing WebKit only proves the library loads. The renderer runs in helper
# processes that WebKit exec's from the relocated path, so the checks that
# matter are that a real WebView finishes a load AND that the helpers doing it
# are the AppImage's own, run inside WebKit's bubblewrap sandbox. A host with
# its own WebKit at the compiled-in path would otherwise pass the load with the
# host's helpers -- which is how this check once passed while the AppImage was
# blank everywhere else. Needs a display, so it is skipped when there is none
# (the build still fails on a WebKit that cannot even be imported).
if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
    from sshpilot.appimage_env import prepare_webkit

    assert not os.environ.get("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS")
    Gtk.init()
    window = Gtk.Window()
    prepare_webkit(WebKit)

    def render(html, base_uri, probe_js, what):
        """Load *html* in a fresh WebView and return *probe_js*'s result.

        A finished load alone is not proof the page ran: the script result
        has to come back from the WebProcess.
        """
        webview = WebKit.WebView()
        window.set_child(webview)
        window.present()
        outcome = {}
        loop = GLib.MainLoop()

        def _on_probe(view, result):
            try:
                outcome["value"] = view.evaluate_javascript_finish(result).to_string()
            except GLib.Error as error:
                outcome["value"] = "error: %s" % error.message
            loop.quit()

        def _on_load_changed(view, event):
            if event == WebKit.LoadEvent.FINISHED:
                view.evaluate_javascript(probe_js, -1, None, None, None, _on_probe)

        def _on_timeout():
            loop.quit()
            return False

        webview.connect("load-changed", _on_load_changed)
        timeout = GLib.timeout_add_seconds(90, _on_timeout)
        webview.load_html(html, base_uri)
        loop.run()
        if "value" in outcome:
            GLib.source_remove(timeout)
        assert "value" in outcome, (
            "the bundled WebKit never finished loading the %s -- check the "
            "relocated helper path (step 8c) and AppRun's working directory" % what
        )
        return outcome["value"]

    # The app's two WebKit pages: the PyXterm.js terminal and Host Info.
    xterm = render(shell_html, "file:///", "String(typeof Terminal)", "xterm.js shell")
    assert xterm == "function", "xterm.js did not run: %r" % xterm
    from sshpilot.host_info_shell import build_host_info_html

    host_info = render(build_host_info_html(), "http://localhost/",
                       "String(document.querySelectorAll('*').length)", "Host Info page")
    assert int(host_info) > 20, "the Host Info page did not build its DOM: %r" % host_info
    print("smoke test ok: rendered the xterm.js shell and the Host Info page")

    helpers = {}
    tree = _descendants(os.getpid())
    for pid in tree:
        exe, argv = _process(pid)
        name = _helper_name(exe, argv)
        if name:
            helpers.setdefault(name, []).append((pid, exe))
    assert set(helpers) == {"WebKitWebProcess", "WebKitNetworkProcess"}, helpers
    for name, processes in helpers.items():
        for pid, exe in processes:
            # On the bundled libc the helper runs through the bundled loader,
            # which lives in the AppImage too.
            assert exe.startswith(appdir + "/"), \
                "%s %d runs %s, not the AppImage's helper" % (name, pid, exe)
    for pid, _exe in helpers["WebKitWebProcess"]:
        ancestor, sandboxed = tree.get(pid), False
        while ancestor and ancestor != os.getpid():
            if os.path.basename(_process(ancestor)[0]) == "bwrap":
                sandboxed = True
            ancestor = tree.get(ancestor)
        assert sandboxed, "WebKitWebProcess %d is not inside bubblewrap" % pid
    print("smoke test ok: the AppImage's own WebKit helpers rendered the "
          "xterm.js shell, sandboxed: %s" % sorted(
              (name, exe) for name, procs in helpers.items() for _pid, exe in procs))
else:
    print("smoke test: no display, WebKit checked by import only")

print("smoke test ok: sshpilot %s" % sshpilot.__version__)
