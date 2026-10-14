# Windows port

Status: **experimental, run-from-source only** (branch `windows`). The app
starts on Windows 10/11 under MSYS2 UCRT64 and runs daemon-owned SSH sessions
in an embedded terminal. User setup lives in
[running-on-windows.md](../running-on-windows.md). This page records how the
port works, what stood in the way, and why each piece is built the way it is.
The proposed decisions for each layer (runtime stack, PTY, emulator core,
connection sharing) and their roadmap are in
[windows-decisions.md](windows-decisions.md).

## Ground rules

- **POSIX behaviour must not change.** Every change sits behind
  `sys.platform == "win32"` / `platform_utils.is_windows()`, or behind a
  factory that picks the existing implementation on Linux and macOS. The
  Unix-socket transport, the POSIX PTY runner and VTE are untouched there.
- **Native OpenSSH stays the SSH engine.** Windows ships OpenSSH for Windows
  (`C:\Windows\System32\OpenSSH\ssh.exe`). The daemon drives it exactly as it
  drives OpenSSH elsewhere; no Paramiko, no bundled ssh.
- **The daemon architecture is kept, not bypassed.** The GUI remains a
  client of the daemon. Windows gets its own *transport* and *PTY runner*
  behind the same seams the POSIX code uses, not a separate code path.

## Runtime picture

```text
GTK frontend (MSYS2 Python, GTK 4 + libadwaita)
   │  DaemonClient ── loopback TCP + auth preamble ──┐
   │  TerminalWidget → PyteTerminalBackend           │
   ▼                                                 ▼
                                        sshpilot daemon (same Python)
                                          selector loop / dispatcher
                                          ConPtySessionProcessRunner
                                              │ ConPTY (pywinpty)
                                              ▼
                                           ssh.exe ── SSH_ASKPASS=askpass.cmd
                                                          │ loopback TCP + broker token
                                                          ▼
                                                 daemon InteractionBroker
```

## Obstacles and decisions

### 1. No `AF_UNIX` → loopback TCP with a token

**Obstacle.** CPython on Windows has no `socket.AF_UNIX`; asyncio doesn't
either. The daemon listener, the client connect, the stale-socket probes, the
askpass listener and the askpass helper all failed with `AttributeError`.
Socket-path resolution also failed earlier: there is no `XDG_RUNTIME_DIR` and
no `os.getuid`. The POSIX security model (mode-0700 directory, mode-0600
socket, `SO_PEERCRED`) has nothing to check on Windows.

**Decision.** `api/transport/endpoint.py` owns endpoint creation and
connection:

- The daemon binds `127.0.0.1:0` with `SO_EXCLUSIVEADDRUSE`, listens, then
  atomically publishes `{port, token, pid}` to
  `%LOCALAPPDATA%\sshpilot\run\sshpilotd.json`. The token is
  `secrets.token_urlsafe(32)`. The file is ACL-restricted to the current user
  (and SYSTEM).
- Every client must send `SSHPILOT-AUTH <token>\n` (fixed length) before any
  framed byte. `PreambleGate` checks it incrementally in the non-blocking
  selector loop with `hmac.compare_digest`, and rejects early on the first
  mismatching byte. Bad clients are closed before dispatch.
- The rest of the code still calls this file the "socket path", so
  `daemon/lifecycle.py` keeps one vocabulary. On Windows, "stale socket" means
  the endpoint file exists but nothing accepts on its port. The peer pid comes
  from the file, and is used only after `identify_process` confirms it is
  alive.

**Why not named pipes?** They are the more native choice, and the stdlib can
open them (`multiprocessing.connection` / `_winapi`), but they do not plug
into the existing `selectors` loop, which only waits on sockets. Loopback TCP
keeps the framing, selector loop and blocking client threads byte-for-byte
identical. (Pipes remain a possible hardening; see "What is not done".)

**Why not Windows' own AF_UNIX?** Windows 10 1803+ supports Unix sockets in
the kernel, but CPython on Windows does not expose `socket.AF_UNIX` (checked
on the python.org and MSYS2 builds of 3.14). If it ever does, only
`api/transport/endpoint.py` and the Windows branches in `daemon/lifecycle.py`
need to change. The token plus the user-only file gives the
same-user guarantee that file permissions give on POSIX.

### 2. No POSIX PTY → ConPTY runner

**Obstacle.** `daemon/pty_runner.py` is built on `os.openpty`, `fcntl`,
`termios`, `select` over pipes, `setsid`/`login_tty` (`_pty_child.py`) and
`killpg`.

**Decision.** `daemon/conpty_runner.py` implements the same runner and handle
surface `SessionRuntime` already consumes (`start`, `write`, `resize`,
`terminate`, `kill`, `wait`, `poll_and_notify`):

- **Process.** pywinpty's `PTY` (ConPTY backend) spawns `ssh.exe` directly
  from the resolved executable path, with `subprocess.list2cmdline` quoting.
- **Threads.** One reader thread per session forwards output. A shared reaper
  thread notices exit and gives the reader up to 1 s to deliver the last frame
  before declaring EOF, so EOF arrives before exit as on POSIX.
- **Kill.** Kill and terminate end the process tree via psutil, so ProxyJump
  and ProxyCommand children die too. Windows has no SIGTERM for console
  programs.
- **Selection.** `server.py` chooses the runner by platform at the single
  construction site. `pty_runner.py` is never imported on Windows.

**pywinpty API drift.** pywinpty 3.x (PyPI) reads and writes `str`; 2.x
(the MSYS2 package) reads and writes UTF-8 `bytes`. The runner accepts both.
It feeds input through an incremental UTF-8 decoder, so a multi-byte
character split across frames is not corrupted. The MSYS2 `python-pywinpty`
package also fails to declare its runtime dependency on `winpty`; the DLL load
fails until `mingw-w64-ucrt-x86_64-winpty` is installed (the setup doc lists
it).

**ConPTY behaviour.** At start, ConPTY emits `ESC[c` (a DA1 query),
`ESC[?9001h` (win32-input-mode) and a full redraw. The terminal answers DA1
and ignores 9001, so ConPTY keeps sending plain VT input.

### 2a. A ctypes ConPTY runner and the bundled conpty.dll (spike)

**Why.** pywinpty 2.0.15 (MSYS2) links `CreatePseudoConsole` straight from
`KERNEL32` and passes no flags. So the daemon is stuck with Windows 10's
inbox conhost, which never forwards mouse modes or `?1049h` and strips the
bracketed-paste markers from input. Its 1000-byte read buffer also needed the
drain workaround in `_read_loop`.

**What.** `daemon/conpty_ctypes.py` follows Microsoft's "Creating a
Pseudoconsole session" recipe:
1. two `CreatePipe` pairs;
2. `CreatePseudoConsole(size, input_read, output_write, flags, &hpc)`;
3. a `STARTUPINFOEXW` whose attribute list is sized and then initialised by
   two `InitializeProcThreadAttributeList` calls, plus
   `UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE)`;
4. `CreateProcessW` with `EXTENDED_STARTUPINFO_PRESENT`. It also sets
   `STARTF_USESTDHANDLES`, because the daemon's own stdio is redirected.

After `CreateProcessW`, the binding closes the console's ends of the pipes.

`CtypesPty` exposes the pywinpty 2.x surface (`spawn`, `read`, `write`,
`set_size`, `isalive`, `get_exitstatus`, `iseof`, `pid`) with UTF-8 bytes.
So `ConPtyProcessHandle` and the runner are unchanged: the same reader
thread, reaper, EOF-before-exit order, incremental UTF-8 input decoder and
psutil tree kill. `handle.close()` now also calls the PTY's `close()` when it
has one, which pywinpty's `PTY` does not.

**Close without deadlock.** Per session, a watcher thread waits on the process
handle. Once the client has exited and the output has been quiet for 100 ms
(capped at 0.5 s), it runs `ClosePseudoConsole` on its own thread. The
session's reader keeps draining meanwhile. conhost flushes the last frame and
exits, and the reader then gets `ERROR_BROKEN_PIPE`, which it reports as EOF.
The reaper's 1 s EOF wait is therefore normally not needed.

**Selection.**
- `SSHPILOT_CONPTY_BACKEND=ctypes` picks the binding at the single
  construction site in `server.py`. The default is still `pywinpty`. The
  daemon logs `ConPTY backend: …`.
- `SSHPILOT_CONPTY_DLL=<file or directory>` loads a bundled `conpty.dll`
  through its `Conpty`-prefixed exports. `kernel32` forces the inbox console.
- Without an override, a frozen build looks in `_MEIPASS` and next to the exe.
  A source checkout uses kernel32.
- If the DLL is missing or fails to load, the binding falls back to kernel32.
  The log says which console is in use, and it warns if `OpenConsole.exe` is
  not beside the DLL.
- `SSHPILOT_CONPTY_FLAGS` passes `CreatePseudoConsole` flags (default 0).

**The bundled console.** It comes from the `Microsoft.Windows.Console.ConPTY`
NuGet package, version 1.25.260930003. The package is MIT licensed, published
from microsoft/terminal under the verified `Microsoft.Terminal` owner, and both
files are Authenticode-signed by Microsoft. For a dev run:
1. Download it into the git-ignored `vendor/conpty/`.
2. Copy `runtimes/win-x64/native/conpty.dll` and
   `build/native/runtimes/x64/OpenConsole.exe` into one directory. conpty.dll
   only finds OpenConsole.exe beside itself.
3. Point `SSHPILOT_CONPTY_DLL` at that directory.

Sizes: conpty.dll 117 KB, OpenConsole.exe 1.04 MB (x64). The binaries are not
committed.

**Comparison (Windows 10 19045, OpenSSH_for_Windows 9.5p1, OpenWrt host
`Router`).** A harness drove `ConPtySessionProcessRunner` and recorded every
output chunk. It ran three times: with pywinpty, with ctypes + kernel32, and
with ctypes + conpty.dll. The router has no vim, so vim 9 ran under `wsl.exe`
through the same runner. That is the same conhost path: a console client
writing VT. pywinpty and ctypes + kernel32 produced the same streams, as
expected with the same inbox conhost. Raw excerpts:

- **Alternate screen (`?1049h`).** The remote ran
  `printf 'A\e[?1049hB\e[?1000h\e[?1002h\e[?1006h\e[?2004hC\n'`.
  - kernel32: `\e[25l\e[?2004h\n\e[HBC\e[K\r\n\e[K…`. conhost swallows
    1049 and the mouse modes, then repaints its own alternate buffer.
  - conpty.dll:
    `A\e[?1049hB\e[?1000h\e[?1002h\e[?1006h\e[?2004hC\r\n`, verbatim.
  - htop and vim show the same picture. Across a whole session, the only
    `CSI ? … h/l` that kernel32 emits are `?25`, `?7`, `?2004` and `?1004`.
    conpty.dll forwards `?1049h/l`, `?1006;1000h` (ncurses' combined form,
    from htop and vim), `?1002h`, `?2004h` and `?1h`.
- **Mouse.**
  - Modes: see above. Through conpty.dll, both models switch on SGR
    click/drag tracking when the recorded htop and vim streams are
    replayed.
  - Input reaching the remote: the remote ran
    `timeout 6 cat > f; hexdump -C f`, and the harness wrote a paste, an SGR
    click, an SGR wheel event and an X10 click.
    - kernel32: `50 41 53 54 45 44 20 23 25 0a 5a 0a  |PASTED #%.Z.|`.
      The SGR reports vanish entirely, and the X10 report loses `ESC [ M`.
    - conpty.dll:
      `1b 5b 32 30 30 7e 50 41 53 54 45 44 1b 5b 32 30 31 7e 1b 5b 3c 30 3b
      31 30 3b 35 4d …  |.[200~PASTED.[201~.[<0;10;5M.[<0;10;5m.[<64;10;5M.[M #%.Z.|`.
      Every byte arrives.
  - vim (`set mouse=a ttymouse=sgr`): after an SGR click at row 3, column 3
    and a wheel-down, `:echo line('.') col('.') &mouse` printed `1 1 a`
    under kernel32 and `4 3 a` under conpty.dll.
  - htop: an SGR wheel-down produced no output under kernel32. Under
    conpty.dll, the process list redrew (1446 B).
- **Bracketed paste.** In both cases the remote's `?2004h` reaches the
  emulator. Only conpty.dll delivers `ESC[200~`/`ESC[201~` to the remote
  (see the hexdump above).
- **Resize inside htop and vim** (80x24 → 110x32 → 70x20 → 80x24): both
  consoles repaint correctly.
  - kernel32 redraws from its own buffer (`\e[H\e[K…`).
  - conpty.dll starts every resize with `\e[6n` (a cursor-position query,
    answered by both models). It then forwards the application's own
    repaint (`\e[H\e[2J…` from vim, incremental updates from htop).
  - In the real app, a `SetWindowPos` shrink/grow while htop ran, followed
    by `q`, left a correct main screen.
- **Stream size.** conpty.dll forwards the remote's bytes instead of
  re-rendering them: 2.4 KB vs 3.4 KB for the probe, 20 KB vs 33 KB for htop.
- **Start/stop sequences.** At start, conpty.dll sends
  `\e[1t\e[c\e[?1004h\e[?9001h`. The terminal answers DA1, and 9001
  (win32-input-mode) stays off, so input is plain VT. At close, it sends
  `\e[?1004l\e[?9001l`.

**Real app.** `SSHPILOT_CONPTY_BACKEND=ctypes`, conpty.dll and the vterm
model, with an SSH session to Router:
- the daemon logged
  `ConPTY: using …\vendor\conpty\x64\conpty.dll`;
- the login banner, typing, htop, a window resize and `exit` all worked;
- the session went `running → exited → closed` with exit code 0;
- no OpenConsole.exe was left behind.

Mouse and wheel in the GUI could not be driven. GTK4 ignores posted
`WM_*BUTTON*`/`WM_MOUSEWHEEL` messages, and real input would have taken over
the desktop. The view-side SGR encoding was verified in the earlier live
check (section 3a). The runner captures above prove that the rest of the path
works.

**Tests.** `tests/daemon/test_conpty_ctypes.py` covers:
- DLL selection: override file, directory or `kernel32`; frozen paths;
  fallback on a missing or broken DLL; the OpenConsole warning;
- the two-call attribute-list setup and `STARTUPINFOEXW`, through a
  recording fake;
- real pipes and an attribute list, on Windows;
- a real `cmd.exe` session through the runner, which must deliver output,
  then EOF, then exit code 3. The kernel32 case always runs on Windows. The
  bundled case runs when `SSHPILOT_CONPTY_DLL` is set.

**Found on the way.** `shutil.which` under MSYS2 Python returns
`C:\Windows\system32/cmd.exe` (MSYS2's `os.sep` is `/`). cmd.exe then parses
the `/cmd.exe` in its own argv[0] as a switch. `CtypesPty.spawn` converts `/`
to `\`. ssh.exe was never affected, and the pywinpty path still has the
quirk.

**Recommendation.**
1. *Replace pywinpty with the ctypes runner: yes, after a short soak.*
   - With kernel32 it behaves like pywinpty.
   - It drops two MSYS2 packages (`python-pywinpty` and the undeclared
     `winpty`) and pywinpty's 1000-byte read quirk.
   - It is the only way to choose the console DLL or pass flags.
   - Make `ctypes` the default on Windows, and keep `pywinpty` behind the
     switch for one release.
2. *Ship conpty.dll and OpenConsole.exe: yes, in frozen builds, next to the
   exe.* They fix mouse, bracketed paste and the alternate screen on
   Windows 10. They cost about 1.2 MB, are MIT licensed and signed by
   Microsoft. Before shipping them:
   - **pyte breaks on the forwarded stream.** vim's `CSI > 4 ; 2 m`
     (modifyOtherKeys) raises `TypeError` from `select_graphic_rendition`
     inside `view.feed`. This is the same bug noted in section 3a, now
     reachable on Windows. Fix it, or make libvterm the Windows default
     first. libvterm parsed every recorded stream without errors.
   - **Recheck the grow and alt-screen decisions.** `pull_history_on_grow`
     is off under ConPTY because conhost repaints from the top on a grow,
     and libvterm's alt-resize workaround was moot because `?1049h` never
     arrived. Both assumptions came from the inbox conhost and need
     re-measuring with OpenConsole.
   - **Ship the right architecture.** The package also carries x86 and
     arm64 builds.


**Obstacle.** Neither VTE nor WebKitGTK exists for GTK 4 on Windows, so both
existing backends (VTE, xterm.js-in-WebKit) were unavailable. `Vte` was also
imported at module level across `main.py`, `terminal.py`,
`terminal_backends.py` and `xterm_pty_bridge.py`.

**Options considered.**
- An external terminal only (Windows Terminal running ssh.exe) loses the
  embedded experience the app is built around.
- WebView2 hosting xterm.js has to live outside GTK's widget tree.
- A pure-GTK renderer was chosen.

**Decision.** The backend is split in two:

- **`pyte_terminal_model.py`** is GTK-free and unit-tested. It holds a
  `pyte.Screen` subclass that adds:
  - scrollback that survives full-screen scrolling, shrinking resizes and
    `ED 3`;
  - pull-back of history when the window grows;
  - the alternate screen (DECSET 47/1047/1049, with cursor save for 1049);
  - DA/DSR replies routed to the remote;
  - xterm key and mouse (SGR and legacy) encoding;
  - selection text and search over scrollback plus screen.
- **`terminal_backend_pyte.py`** is a `Gtk.DrawingArea` that draws with
  Pango/cairo and implements the existing `BaseTerminalBackend` protocol:
  - `commit` / `grid-size-changed` signals with VTE's shapes;
  - `Gtk.IMMulticontext` input;
  - a scrollbar bound to its own adjustment;
  - selection, PRIMARY, bracketed paste;
  - mouse reporting with Shift to bypass;
  - alternate-scroll in full-screen apps.

  `TerminalWidget` needed no changes in its daemon I/O path. The `commit`
  payload is `bytes`, because legacy mouse reports are not valid UTF-8, and
  `commit_payload_to_bytes` already passes `bytes` through.

**Wiring.**
- `terminal_backends.VTE_AVAILABLE` gates VTE.
- `TerminalWidget._create_backend` picks `pyte` when VTE is missing or when
  `terminal.backend = "pyte"` is set, so the backend can be tested on Linux
  too.
- Preferences → Terminal → Backend lists it as "pyte" and "libvterm", one
  entry per emulator model.

**Windows input detail.** GDK on Windows reports AltGr as Ctrl+Alt. A
Ctrl+Alt press that produces a printable non-letter is therefore sent as text.
Otherwise AltGr+Q (`@` on a German layout) would become Ctrl+@ (NUL).

**Limits.** pyte has no hyperlinks (OSC 8), no reflow on resize, no sixel,
and no true-blink. Rendering is per-run Pango layouts, which is fine for
interactive use but slower than VTE on bulk output.

### 3a. libvterm as the emulator core (proof of concept)

**Why.** pyte parses at about 0.35 MB/s on the GTK main thread, its upstream
is dormant, it raises `TypeError` on private-marker SGR (`CSI ? 4 m`, the
XTQMODKEYS query vim 9 sends at startup), and it loses the main screen's
bottom rows when the window is resized while a full-screen app runs.

**What.** `vterm_terminal_model.VtermTerminalModel` binds libvterm 0.3.x with
`ctypes` (no compiler, no cffi build step). It has the same surface the view
uses from the pyte `TerminalModel`. That covers feed, resize, absolute-row
`line()` cells with pyte's `Char` fields and colour names, the cursor,
scrollback through `sb_pushline`/`sb_popline`/`sb_clear`, the alternate
screen, the OSC 0/2 title, DA/DSR replies through the output callback, and
the mouse/focus/paste/app-cursor flags. libvterm keeps DECCKM, bracketed
paste and the SGR mouse encoding private, so the model asks libvterm to
encode an Up key, a paste start or a wheel event and checks which form it
chose. The model-independent parts (mode constants, text/selection/search,
`encode_key`, `encode_mouse`) moved to `terminal_model_common.py`, and
`pyte_terminal_model` re-exports them.

Scrollback rows are stored as raw 40-byte `VTermScreenCell` runs. They are
decoded only when drawn or searched, through a memo of distinct cells.
Screen rows are cached until libvterm's damage callback (merged with
`VTERM_DAMAGE_SCROLL` and flushed once per `feed`) invalidates them.

**Switch.** `PyteTerminalView(model_name)` takes `"pyte"` (default) or
`"vterm"`. The backend reads `terminal.builtin_model` from the config, and
the `SSHPILOT_TERMINAL_MODEL` environment variable overrides it. If the
requested model cannot load, the view falls back to the other one. The
backend module raises `ImportError` only when neither model can load, so
`TerminalWidget`'s fall-back-to-VTE path is unchanged. POSIX defaults are
unchanged, because both models sit behind the opt-in built-in backend.

**libvterm's own resize bug, and the workaround.** In libvterm 0.3.3, resizing
while the alternate screen is shown resizes the hidden main screen, but:
- the rows it pushes to scrollback are read from the *active* buffer, so
  pager or editor lines land in history;
- the cursor saved by DECSET 1049 is not moved when rows are pushed or
  pulled;
- the main screen is fitted around the alternate cursor, so the blank row
  under the command line is dropped.

`_AltResize` works around all three. `feed` splits the input at
`CSI ? 1049/1047/47 h|l` and snapshots the main screen just before entering.
During a resize it hands out the snapshot rows in place of the wrong ones and
counts the net shift. On leaving, it sends a CUP, plus one LF for each row
that overflowed, so the next prompt lands under the command line.

**Results** (Windows 10 19045, MSYS2 Python 3.14, `scripts/bench_terminal_models.py`,
1.5 MB fed in 4 KiB chunks into 80x24 with 10 000 lines of scrollback):

| model | plain text | SGR-heavy | reading all 24 rows for a redraw |
|---|---|---|---|
| pyte | 0.35 MB/s | 0.37 MB/s | 0.2–0.3 ms |
| libvterm | 6.7 MB/s | 14.4 MB/s | 2.9 ms cold, one row ≈ 0.12 ms |

Plain text is slower than SGR-heavy text on libvterm because the Python
`sb_pushline` callback runs once per scrolled line (about 5 µs). The parsing
itself happens in C.

`tests/test_pyte_terminal_model.py` runs every model test against both
models through a `make_model` fixture. The alternate-screen resize tests are
`xfail` for pyte.

**Full-screen apps.** vim 9, GNU less and htop were recorded under a Linux
PTY in WSL with 80x24 → larger → smaller resizes inside the alternate screen,
then replayed into both models and drawn by `PyteTerminalView._draw`:
- Frames inside the apps are pixel-identical between the models.
- After quitting, libvterm keeps the whole transcript: `seq 1 40`, the
  command, then the next prompt.
- For vim and htop, pyte loses 8–10 lines: the end of the `seq` output, the
  marker line and the command line. The next prompt follows line 32 or 34.

Through **ConPTY** (the daemon's path on Windows 10) the picture is
different: conhost keeps the alternate screen itself and never forwards
`?1049h`. It repaints the main screen instead, and both models then show the
same screens for vim, htop, busybox vi and busybox less. So on Windows 10 the
alternate-screen code is only used when the remote stream reaches the
emulator unrendered. That happens on Linux/macOS with the built-in backend,
and with a ConPTY passthrough mode if one becomes available.

**Growing under ConPTY.** After a resize conhost repaints its own viewport
from the top row. If the model had pulled history back onto the screen on a
grow (as xterm and VTE do), conhost would paint over those rows and they would
drop out of the transcript: `seq 1000 1120`, shrink, grow lost 1084–1098.
Both models therefore have a `pull_history_on_grow` flag.
`PyteTerminalBackend.prepare_pty_less_emulation()`, which only daemon sessions
call, turns it off when the daemon runs under ConPTY (Windows; the daemon is
always local). The pyte screen then skips the pull and libvterm's
`sb_popline` returns 0, so the new rows are added at the bottom. Raw streams
(the POSIX daemon) keep the pull. Leftover: through ConPTY, libvterm still
pushes 2–3 rows of a full-screen app into history on a shrink, because it
never sees `?1049h`.

**Packaging cost.**
- *MSYS2:* `pacman -S mingw-w64-ucrt-x86_64-libvterm` installs
  `libvterm-0.dll` (80 KB). It imports only system DLLs (ucrtbase, msvcrt,
  kernel32, advapi32), with no MinGW runtime. It sits in `ucrt64/bin`, which
  the loader finds as `sys.base_prefix/bin`, also from a
  `--system-site-packages` venv. Python 3.8+ no longer searches `PATH` for
  DLLs, so a bare-name `CDLL("libvterm-0.dll")` is only the last resort.
- *Frozen build:* PyInstaller does not see a `ctypes` load from a list of
  names. A Windows spec needs `binaries=[("<ucrt64>/bin/libvterm-0.dll", ".")]`.
  The loader checks `sys._MEIPASS` and the exe's directory first.
  `SSHPILOT_LIBVTERM=<path>` overrides every lookup.
- *Linux/macOS:* `libvterm.so.0` (Debian/Ubuntu `libvterm0`, 0.3.x since
  Debian 12 / Ubuntu 24.04) or Homebrew `libvterm` is found through
  `ctypes.util.find_library`.

**Not done in the proof of concept.**
- Reflow on resize: `vterm_screen_enable_reflow` exists, but continuation
  flags would also have to be stored with the scrollback.
- X10 mouse mode (DECSET 9), which libvterm does not implement.

**Live GUI check (Windows 10 19045, ConPTY, OpenWrt host).** The real app ran
with `SSHPILOT_TERMINAL_MODEL=vterm`, beside a pyte tab on the same host.
Input went through the view's own key, IM, click, drag and scroll handlers,
and the window was resized with `SetWindowPos`. Findings:
- Same as pyte: typing, UTF-8 and wide characters, 16/256/truecolor SGR,
  selection (drag, word, line), copy, search, OSC title, and busybox
  vi/less and htop through shrink and grow. For vi and less the screens
  matched pyte line for line, before and after quitting.
- Bracketed paste: ConPTY passes `?2004h` through, and the view wraps the
  paste. conhost then drops the markers on input, so the remote gets the
  bare text.
- Mouse: ConPTY does not forward `?1000h`/`?1006h`, so the models never
  report the mouse. With the mode set locally, the view sends correct SGR
  reports, but htop does not react. On this path, the mouse does not work
  in full-screen apps with either model.
- Wheel and Shift+PageUp/PageDown scrolled the wrong way, for both models.
  Fixed in `PyteTerminalView.scroll_lines`.
- Both models drop a screenful of scrollback when the window grows. A
  shrink pushes the top rows into scrollback. A grow pulls them back onto
  the screen, and conhost then repaints its top-anchored viewport over
  them. libvterm also pushes rows from a full-screen app into scrollback
  on a shrink, because it never sees `?1049h`.
- Not model-specific: daemon output sometimes stalls mid-frame until the
  next keystroke (seen in the login banner and in `seq 1 400`).

### 4. Askpass without a shebang

**Obstacle.** The broker writes a `#!/bin/sh` (frozen build) or
`#!python` launcher and points `SSH_ASKPASS` at it, but Windows ignores
shebangs. The helper also connected over `AF_UNIX`.

**Decision.**
- On Windows the broker writes `askpass.cmd` beside a copy of
  `askpass_helper.py`. Win32-OpenSSH starts `SSH_ASKPASS` with
  CreateProcess, which runs `.cmd` through cmd.exe. The launcher runs the
  helper as `python -I` (isolated mode: nothing from the cwd or `PYTHON*`).
- The listener is loopback TCP, advertised as `tcp:127.0.0.1:PORT` in
  `SSHPILOT_DAEMON_ASKPASS_SOCKET`. No extra preamble is needed: every
  askpass request already carries its per-launch broker token, which was
  always this channel's authority. `_same_user_peer` only applies to
  `AF_UNIX`.
- Verified end to end: a password prompt from `ssh.exe` reached the GTK
  dialog and the session authenticated.

### 5. ControlMaster does not exist

**Obstacle.** OpenSSH for Windows has no connection multiplexing. A
`ControlMaster` option makes launches fail. Host Info forced a master with
`require_master=True`.

**Decision.**
- On Windows, `ssh_multiplex.controlmaster_args()` returns
  `-o ControlMaster=no -o ControlPath=none`. Because OpenSSH takes the first
  value given, a forced master or an authored Host block can no longer break
  a launch.
- `socket_dir()` points at an always-empty `%LOCALAPPDATA%\sshpilot\run\cm`,
  so the existing master surveys and the shutdown sweep resolve it and find
  nothing.
- `expire_all_masters` is a no-op, and the preference switch is hidden.

### 6. Atomic writes fsync the parent directory

**Obstacle.** This is the bug that broke saving. The connection-state,
`~/.ssh/config` and `known_hosts` writers fsync the parent directory after
`os.replace` by `os.open()`-ing it. Windows refuses to open a directory that
way (`PermissionError`). The connection repository then classified its own
state file as unusable and disabled every save ("Connection state file is
unusable … Permission was denied").

**Decision.** The directory fsync is skipped on Windows in
`state_file._fsync_parent`, `known_hosts/file_io.py` and
`ssh_config_store.py`. NTFS journals the rename metadata itself, and
`os.replace` is `MoveFileEx(MOVEFILE_REPLACE_EXISTING)`. Fixing this took the
Windows failures in `tests/daemon`, `tests/core` and `tests/api` from 484 to
174.

### 7. `os.kill(pid, 0)` is not a probe on Windows

**Obstacle.** This is the bug behind having to quit twice. Python's
`os.kill` on Windows calls `TerminateProcess` for every signal except the
console CTRL events, so `os.kill(pid, 0)` kills a live process. On a dead
pid it raises a plain `OSError`, which `process_registry._pid_exists` read as
"alive". The quit path's "daemon identity gone" check therefore never became
true, and quit waited out its 10 s timeout until a second click escalated.

**Decision.** `_pid_exists` uses `psutil.pid_exists` on Windows. A regression
test forbids `os.kill` there. The remaining `os.kill(pid, 0)` calls in
`terminal.py` / `ssh_process_manager.py` belong to local-shell paths that
cannot run on Windows today. Fix them before enabling local shells.

### 8. The ssh-agent is a named pipe, and ssh-add needs `PROGRAMDATA`

**Obstacle.** Two separate problems stopped the agent features working on
Windows.

- **Detection.** OpenSSH for Windows runs ssh-agent as a Windows service
  listening on the fixed named pipe `\\.\pipe\openssh-ssh-agent`, and does
  not set `SSH_AUTH_SOCK`. `ssh.exe` and `ssh-add.exe` use that pipe by
  default. sshPilot decided "is there a system agent?" purely from
  `SSH_AUTH_SOCK`, so on Windows the agent always looked absent (identity
  state, provider list, plugin `is_agent_available()`).
- **Environment.** The daemon runs `ssh-add` and `ssh-keygen` with an
  allowlisted environment (`PATH`, `HOME`, `USER`, ...). Without
  `PROGRAMDATA`, `ssh-add.exe` exits 255 with no output: OpenSSH for Windows
  reads its system configuration from `%PROGRAMDATA%\ssh`. A greedy
  minimisation of the full environment showed `PROGRAMDATA` is the one it
  cannot run without.

**Decision.**
- `providers/system_agent.system_agent_socket()` is the one answer to "where is
  the system agent". `SSH_AUTH_SOCK` wins everywhere. On Windows, the service
  pipe counts when it exists. The core identity service and the system-agent
  provider both use it. Nothing new is injected into child environments,
  because OpenSSH finds the pipe by itself.
- The pipe is detected by listing `\\.\pipe\`, never by opening it, since
  opening a pipe consumes one of the server's instances.
- On Windows the daemon's allowlist also keeps the variables Windows programs
  need: `SystemRoot`, `WINDIR`, `PROGRAMDATA`, `TEMP`, `USERPROFILE` and so on.
  Names are matched case-insensitively. The POSIX allowlist is unchanged.

**A third problem: the wrong OpenSSH build.** Git for Windows (like MSYS2
and Cygwin) ships its own MSYS-based OpenSSH, and its `usr\bin` commonly
comes *before* `System32\OpenSSH` on PATH. A plain `ssh` or `ssh-add` then
runs that copy. It has a different agent (a Unix-socket emulation that
never sees the Windows service), different config and key locations, and
cannot run `askpass.cmd`. On the development machine, `ssh-add -l` listed
nothing until the built-in `ssh-add.exe` was used, and then listed the key.
The GUI happened to work only because the MSYS2 shell drops the Windows PATH.
At startup (`run.py` and the daemon's `main`), `prefer_builtin_openssh()` now
moves `System32\OpenSSH` to the front of PATH, so every child the GUI or
daemon starts comes from one build.

Verified with the service running: the system agent is detected on the pipe,
and the daemon's `ssh-add -l` (with its filtered environment) lists the loaded
key.

The agent service is disabled by default on Windows. Users enable it with
`Set-Service ssh-agent -StartupType Manual` and `Start-Service ssh-agent` (as
administrator). Adding and removing keys through the UI is still untested.

### 9. Smaller incompatibilities

| Problem | Fix |
| --- | --- |
| `import resource`, `pwd`, `grp` at module level | Made lazy or guarded (`main.py`, `terminal.py`, serial plugin) |
| `require_version('PangoFT2')` (not in MSYS2's GTK) | Guarded; the font dialog falls back to `PangoCairo.FontMap` |
| `ctypes.CDLL(None)` (inotify probe) raises `TypeError` on Windows | Caught; the monitor reports itself unavailable, as on macOS |
| GUI used `GLib.get_user_config_dir()` (`%LOCALAPPDATA%`), daemon used `~/.config` | `platform/paths.py` is the single source: config in `%APPDATA%\sshpilot`, data/state/runtime in `%LOCALAPPDATA%\sshpilot` |
| `fcntl.flock` file locks silently became a no-op | `platform/locking.py` uses `msvcrt.locking` |
| `os.fchmod` (absent before Python 3.13), `os.uname`, `os.getuid`, `os.mkfifo` | Guarded per call site |
| `chmod 0600` does nothing; OpenSSH rejects keys others can read ("UNPROTECTED PRIVATE KEY FILE") | `platform/windows/acl.py` uses `icacls` for owner-only (+SYSTEM) ACLs on generated keys and the endpoint file |
| `start_new_session` ignored; console windows flashing | The daemon spawns with `CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW`; its console children inherit the hidden console |
| `SIGALRM` watchdog at quit, `SIGKILL` references | `threading.Timer` / `getattr(signal, "SIGKILL", SIGTERM)` |
| `sh -lc` for pre-connection commands | `cmd /d /s /c` on Windows |
| Secret backend "auto" tried libsecret first | Windows uses `keyring` first (Credential Manager); KeePass stays available |
| Several OpenSSH builds on PATH (see section 8) | `platform/windows/openssh.prefer_builtin_openssh()` puts `System32\OpenSSH` first |
| Windows OpenSSH has no `ssh-copy-id` (Git for Windows' copy is a shell script `shutil.which` cannot resolve, and it would run the MSYS `ssh`), so key deployment reported "unavailable" | `platform/windows/copy_id.py` runs `ssh-copy-id`'s own remote install script (including the OpenWrt/Dropbear path) over the built-in `ssh`, with the key embedded in the command |

### 10. A self-inflicted bug worth remembering

While adding `detached_process_options()` to `daemon/launcher.py`, the new
function went in between `@dataclass(frozen=True)` and
`class DaemonProcessHandle`. That decorated the function and crashed the GUI
on first daemon connect. It was only caught by actually launching the GUI;
the headless tests passed around it. Launch the real app after touching
module-level code.

## What is not done

- **libvterm as the Windows default.** The libvterm model (section 3a) is
  opt-in. Plan: use it day to day, then make it the default on Windows once
  the frozen build ships `libvterm-0.dll`. pyte stays as the fallback.
- **Local shell tabs.** They need a frontend PTY. The action is disabled on
  Windows, and `setup_local_shell` refuses with the pyte backend. A
  ConPTY-backed local shell (cmd/PowerShell) is the natural follow-up.
- **Packaging.** There is no installer, PyInstaller spec or Windows CI yet.
  The unmerged `origin/cursor/windows-support-abbc` branch has a Windows spec
  and MSYS2 workflow that may be reusable; its runtime claims predate this
  work.
- **1Password agent on Windows.** The 1Password preset points at
  `~/.1password/agent.sock`. On Windows, 1Password serves the same
  `\\.\pipe\openssh-ssh-agent` pipe instead, so the preset needs a
  Windows-specific socket.
- **Named pipes for the daemon channel.** These are a possible hardening of the
  loopback-TCP transport. Windows checks pipe ACLs at connect, so other users
  could not even reach the endpoint. `GetNamedPipeClientProcessId` gives a
  reliable peer pid. The stdlib can do it (`multiprocessing.connection` /
  `_winapi`). The cost is that the selector loop only waits on sockets, so
  each pipe client needs a bridge thread to a socketpair. Not urgent: TCP is
  loopback-only and token-authenticated.
- **Plugins.** RDP (FIFO arguments) and serial are not usable on Windows.
  Docker and Kubernetes should work if their CLIs are on PATH; untested.
- **Unverified on real hosts:** SFTP file manager, key-based auth with the
  ACL fix, port forwards, Host Info without a master, AltGr input, and
  full-screen apps (`vim`/`htop`) under resize.
- **Cosmetic issues.**
  - Startup diagnostics report `gtk4` / `libadwaita` as NOT FOUND, because
    they look for Linux library names.
  - At quit, open tabs log "daemon transport is closed" as errors.
- **Tests.**
  - New: `tests/api/test_transport_endpoint.py`,
    `tests/test_pyte_terminal_model.py`, `tests/daemon/test_conpty_runner.py`,
    `tests/daemon/test_conpty_ctypes.py`,
    `tests/daemon/test_process_registry_windows.py`,
    `tests/test_platform_paths_windows.py`.
  - The rest of the suite still assumes POSIX (Unix sockets, `/home/...`
    paths, file modes); about 170 such tests fail on Windows.
  - The baseline tree cannot even collect on Windows (the `CDLL(None)`
    import error), so there is no before/after diff there.
  - **The Linux suite has not yet been re-run against this branch.**
