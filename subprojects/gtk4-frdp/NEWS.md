# gtk4-frdp releases

## 0.2.1 (2026-11-05)

- A password entered in the authentication prompt sets auto-logon, the same
  as a password known before connect. Without that, Windows authenticated
  and then stayed on its own logon screen, which some hosts draw black.

## 0.2.0 (2026-11-04)

- `private_libdir` option: install the library and typelib into an
  application's own directory under libdir, without headers, pkg-config file
  or GIR XML, for applications that bundle gtk4-frdp in their package.
- The display can move to another window while connected (a detached tab):
  the session carries on, and system shortcuts inhibited on the old window
  are restored on that window rather than the new one.
- Windows build: FreeRDP no longer takes gtk4-frdp's git tag as its own
  version, and the DLL is installed to bin/ again.

## 0.1.0 (2026-11-04)

First release as a standalone GTK 4 library, derived from gtk-frdp
(GNOME, GTK 3) at commit 83854a2.

- GTK 4 port of the display widget: event controllers, `snapshot()`
  rendering through `GdkMemoryTexture`, GTK 4 cursors, system-shortcut
  inhibition instead of keyboard grabs.
- Each connection runs on its own session thread; `open_host()` and
  `close()` no longer block, and `close()` cancels a pending connect.
- Certificate and login prompts are answered asynchronously, without a
  nested main loop.
- Public API reduced to `FrdpDisplay`; the session is internal.
  GObject Introspection namespace `GtkFrdp-4.0`, pkg-config
  `gtk4-frdp-4.0`, header `<gtk4-frdp.h>`.
- `password` is write-only.
- FreeRDP is set up on the session thread: the first `open_host()` in a
  process no longer blocks the main loop while FreeRDP initialises
  (0.4-1 s on macOS). Settings are copied when `open_host()` is called.
- `rdp-disconnected` is emitted once per connection attempt (a failed
  connect emitted it twice).
- Builds on macOS (Homebrew) and with MSVC/clang-cl against gvsbuild GTK;
  the public API is exported from Windows DLLs.
- FreeRDP 3.11 or newer is required; FreeRDP 2 support was removed.
- New clipboard implementation for GTK 4 (gtk-frdp's GTK 3/FUSE one is
  replaced): text, images and HTML in both directions, fully asynchronous,
  on the session-thread model. Images are converted without depending on
  WinPR's optional PNG support. No file transfer yet.
- Fixed from gtk-frdp: the widget, its session and the FreeRDP context were
  never freed; use-after-free after a failed connection; a certificate
  prompt answered from its signal handler hung; a hidden widget stopped
  processing network events.
- Logs in directly when a password is given (auto-logon), instead of
  stopping at Windows' own logon screen; some Windows 11 hosts showed a
  black screen and dropped the session after ~55 s without it.
- New `clipboard` (default on) and `audio-playback` (default off)
  properties; audio is played by FreeRDP's rdpsnd client (PulseAudio,
  PipeWire, ALSA).
- The remote cursor is scaled together with the desktop.
- Keyboard layout detection beyond GNOME: Windows, the GTK keyboard device
  (GTK 4.18+, any desktop), KDE, XKB_DEFAULT_LAYOUT and the system locale,
  using FreeRDP's XKB layout table; new `keyboard-layout` property to set
  it explicitly. Persian (`ir`) maps to Windows' default Persian layout.
- New: test suite (no server needed, plus an optional live suite), API
  reference (gi-docgen), C and Python example viewers.
