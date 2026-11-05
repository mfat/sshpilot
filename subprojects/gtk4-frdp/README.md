# gtk4-frdp

A GTK 4 widget that embeds a remote desktop over RDP, using
[FreeRDP 3](https://www.freerdp.com/). Drop `FrdpDisplay` into any GTK 4
window — in C, Python, JavaScript or any other language with GObject
Introspection — and get the remote desktop, keyboard and mouse input,
dynamic resolution, and certificate and login prompts.

- **Non-blocking:** each connection runs on its own thread, which owns
  FreeRDP. Connecting, network I/O and decoding never freeze the UI, and
  `close()` cancels a connection that is still in progress.
- **GTK 4 native:** event controllers, `snapshot()` rendering with
  `GdkMemoryTexture`, Wayland and X11.
- **Full FreeRDP:** NLA/TLS, the graphics pipeline, Display Control
  (the server follows the widget size), and FreeRDP's certificate store.
- **Clipboard** in both directions: text (Unicode), images, HTML.
- **Bindings for free:** GObject Introspection namespace `GtkFrdp-4.0`.

gtk4-frdp is derived from GNOME's GTK 3 library
[gtk-frdp](https://gitlab.gnome.org/GNOME/gtk-frdp). See [NOTICE](NOTICE)
for what changed.

> Status: 0.1 — works on Linux and Windows (MSYS2), tested against Windows 10
> Pro and Windows 11 hosts. macOS is untested. The API may still change
> before 1.0.

## Quick start

### Python

```python
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GtkFrdp", "4.0")
from gi.repository import Gtk, GtkFrdp

def on_activate(app):
    window = Gtk.ApplicationWindow(application=app, default_width=1280, default_height=800)
    display = GtkFrdp.Display(allow_resize=True)
    display.props.username = "alice"
    display.props.password = "secret"            # write-only
    # Accept the server certificate for this session only (show a dialog in real code).
    display.connect("rdp-needs-certificate-verification",
                    lambda d, *cert: d.certificate_verify_ex_finish(2))
    display.connect("rdp-disconnected", lambda d: window.destroy())
    window.set_child(display)
    window.present()
    display.open_host("rdp.example.com", 3389)   # returns immediately

app = Gtk.Application()
app.connect("activate", on_activate)
app.run()
```

### C

```c
#include <gtk4-frdp.h>

GtkWidget *display = frdp_display_new ();
g_object_set (display, "username", "alice", "password", password,
              "allow-resize", TRUE, NULL);
g_signal_connect (display, "rdp-needs-certificate-verification",
                  G_CALLBACK (on_certificate), window);
gtk_window_set_child (window, display);
frdp_display_open_host (FRDP_DISPLAY (display), "rdp.example.com", 3389);
```

Compile with `pkg-config --cflags --libs gtk4-frdp-4.0`.

On Windows, build in an MSYS2 UCRT64 shell (`pacman -S mingw-w64-ucrt-x86_64-{gtk4,freerdp,meson,gobject-introspection,python-gobject}`);
see `.github/workflows/ci.yml`.

Complete viewers with a certificate dialog and a login prompt are in
[`examples/`](examples/) (C and Python).

## API overview

| | |
|---|---|
| `frdp_display_new()` | create the widget |
| `open_host(host, port)` | connect (asynchronous) |
| `close()` | disconnect or cancel (asynchronous) |
| `is_open()` | whether a session is established |
| properties | `username`, `password` (write-only), `domain`, `keyboard-layout` (0 = detect), `clipboard`, `audio-playback`, `scaling`, `allow-resize`, `resize-supported` (read-only), `last-error-code` (read-only) |
| signals | `rdp-connected`, `rdp-disconnected`, `rdp-error`, `rdp-auth-failure`, `rdp-needs-certificate-verification`, `rdp-needs-certificate-change-verification`, `rdp-needs-authentication` |
| answering prompts | `certificate_verify_ex_finish()`, `certificate_change_verify_ex_finish()`, `authenticate_finish()` |

`rdp-error` reports a connection failure (unreachable host, DNS, TLS, the
security negotiation, the transport breaking); `rdp-auth-failure` only the
server refusing the logon. `last-error-code` carries FreeRDP's code for
either (for example `FREERDP_ERROR_CONNECT_LOGON_FAILURE`), so an application
does not have to parse FreeRDP's English sentence.

The keyboard layout announced to the server is detected automatically: the
active layout on Windows, GNOME's input sources, the GTK keyboard device
(GTK 4.18+, any desktop), KDE's configuration, `XKB_DEFAULT_LAYOUT`, then the
system language. Set `keyboard-layout` to override it.

All signals are emitted on the GTK main thread. Prompts can be answered from
the signal handler or later; the connection waits, the UI does not. The full
reference is built with `-Ddocumentation=true` (gi-docgen) and includes a
[design and threading page](docs/design.md).

## Building

Requirements: GTK ≥ 4.14, GLib ≥ 2.66, FreeRDP 3 ≥ 3.11 (`freerdp3`,
`freerdp-client3`, `winpr3`), Meson ≥ 0.62. Optional: GObject Introspection
(on by default), gi-docgen (docs).

```sh
# Debian/Ubuntu
sudo apt install meson ninja-build libgtk-4-dev freerdp3-dev libwinpr3-dev \
                 gobject-introspection libgirepository1.0-dev
# Fedora
sudo dnf install meson gtk4-devel freerdp-devel gobject-introspection-devel

meson setup _build
meson compile -C _build
xvfb-run -a meson test -C _build      # tests need a display
sudo meson install -C _build
```

Options (`meson configure _build`):

| Option | Default | |
|---|---|---|
| `introspection` | true | GIR/typelib for Python, JavaScript, ... |
| `documentation` | false | API reference (needs gi-docgen) |
| `examples` | false | build the C example viewer |
| `tests` | true | register the test suite |
| `private_libdir` | (empty) | install for one application only (below) |

### Using it as a Meson subproject

```meson
gtk4_frdp_dep = dependency('gtk4-frdp-4.0',
  fallback: ['gtk4-frdp', 'gtk4_frdp_dep'])
```

### Bundling it inside an application package

`-Dprivate_libdir=myapp` puts the library in `<libdir>/myapp` and the typelib
in `<libdir>/myapp/girepository-1.0`, and installs no headers, pkg-config
file or GIR XML, so an application package can carry its own copy without
clashing with a system gtk4-frdp. The application adds both directories
before loading the namespace. In Python:

```python
import gi
repo = gi._gi.Repository.get_default()        # PyGObject >= 3.52
repo.prepend_search_path(f"{private}/girepository-1.0")
repo.prepend_library_path(private)
# Older PyGObject: the same two calls are static methods of
# GIRepository.Repository (gi.require_version("GIRepository", "2.0")).
gi.require_version("GtkFrdp", "4.0")
```

### Running from a build tree

```sh
LD_LIBRARY_PATH=_build/src GI_TYPELIB_PATH=_build/src \
  examples/gtk4-frdp-viewer.py rdp.example.com alice
```

## Testing against a real server

```sh
GTK4_FRDP_TEST_HOST=rdp.example.com GTK4_FRDP_TEST_USER=alice \
GTK4_FRDP_TEST_PASSWORD_FILE=~/.rdp-test-password \
xvfb-run -a meson test -C _build --suite live
```

`tests/live-session.py` can also be run by hand for longer soak runs
(`--cycles`, `--hold`, `--idle-cpu`, `--abort-after`).

## Known gaps

- Clipboard: text and images work in both directions; HTML is implemented
  but not yet tested against Windows applications; copying files is not
  supported.
- On Windows, the clipboard and cursor live tests have not been run (they
  use xdotool). macOS: untested.

## License

GPL-3.0-or-later. See [COPYING](COPYING) and [NOTICE](NOTICE). The original
gtk-frdp files keep their authors' copyright notices.
