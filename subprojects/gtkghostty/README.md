# gtkghostty

A GTK 4 terminal widget on [libghostty-vt](https://lib.ghostty.org/vt/), Ghostty's terminal
core. Written in C, usable from any GObject language, on Linux, macOS and Windows.
Used by [SSH Pilot](https://github.com/mfat/sshpilot).

- Ghostty's emulation, scrollback, selection and search
- Kitty and legacy keyboard protocols, input methods, mouse reporting
- OSC 8 links, OSC 52 copy (programs can set the clipboard, never read it), themes, zoom
- Box drawing, Powerline, braille and block glyphs drawn on the grid
- Feed it any byte stream; `GtkGhostty.Pty` optionally runs a local program (PTY or ConPTY)

## Use

```python
import gi
gi.require_version("GtkGhostty", "1")
from gi.repository import GtkGhostty

terminal = GtkGhostty.Terminal()
pty = GtkGhostty.Pty.spawn(["bash"], None, None, terminal.get_columns(), terminal.get_rows())
pty.attach(terminal)
```

For other streams (SSH, serial, sockets), pass output to `feed_data()` and write the `input`
and `reply` signals back. See [`examples/`](examples/); Python type stubs are in
[`stubs/`](stubs/gi-stubs/repository/GtkGhostty.pyi).

## Install

Windows: unzip a [release](https://github.com/mfat/gtkghostty/releases) zip over its gvsbuild GTK.
Linux and macOS: [build from source](docs/BUILDING.md).

## More

- [Building](docs/BUILDING.md)
- [Design](docs/DESIGN.md): how it works, platform notes, what is not done yet

LGPL-2.1-or-later ([`COPYING`](COPYING)); credits in [`NOTICE`](NOTICE).
