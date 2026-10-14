# Windows port: foundational decisions

Status: **proposal, October 2026.** Research only; no code changed with this
page. It builds on [windows-port.md](windows-port.md) (how the port works
today) and [running-on-windows.md](../running-on-windows.md) (setup), and on
the code on the `windows` branch.

The port was built one blocker at a time, and several layer choices only
showed their cost late: pyte was too slow, so libvterm was added; the inbox
ConPTY strips mouse and paste modes, so a ctypes ConPTY runner is being
spiked. Packaging was never examined, and everything assumes MSYS2. This page
settles each layer before more code is written, so the next rewrites are the
last ones.

The ground rules from windows-port.md still hold: **POSIX behaviour does not
change**, and **native OpenSSH stays the SSH engine**. Nothing below argues
for breaking either.

## Summary

| Layer | Recommendation | Main reason |
|---|---|---|
| 1. Runtime and packaging | **python.org (MSVC) Python + gvsbuild GTK 4**, frozen with PyInstaller, NSIS installer, SignPath signing, winget | PyPI's pywinpty is the only Python ConPTY binding that ships a modern ConPTY, and it only exists for MSVC Python. Gaphor ships exactly this stack today. |
| 2. PTY | **Bundled ConPTY** (`conpty.dll` + `OpenConsole.exe` from Microsoft's NuGet package), through **pywinpty 3.x** | Measured on Windows 10 19045: the bundled ConPTY passes mouse modes, the alternate screen and paste markers through in both directions. The inbox one strips them. |
| 3. Emulator core | **libvterm** as the Windows default, **pyte** as fallback; watch **libghostty-vt** | libvterm is 20–40× faster than pyte, is plain C that builds with MSVC, and the model already exists. libghostty-vt is the better long-term core but has no tagged release yet. |
| 4. Connection sharing | **Accept one login per channel for now**, reduce the cost through the agent and the broker, and track the Win32-OpenSSH ControlMaster PR | A daemon-held SSH library would be a second SSH engine. Upstream multiplexing is in review. |
| 5. Local shells / IPC | Local shells as **daemon sessions** on the same ConPTY runner; **keep loopback TCP** | The daemon is always local on Windows, so no frontend PTY is needed. TCP + token is adequate. |

The order to do them in is at the end ([Roadmap](#roadmap)).

---

## 1. Packaging and runtime stack

### What has to ship

The frozen app needs, on Windows:

- GTK 4, libadwaita (≥ 1.5, per `debian/control`; newer APIs are
  feature-detected), GtkSourceView 5, PyGObject, pycairo.
- Python packages: cryptography, psutil, PyYAML, keyring, certifi, pyte,
  optionally pykeepass (which pulls in lxml).
- A ConPTY binding (today pywinpty) and, for the libvterm model, a
  `libvterm` DLL.
- Nothing from VTE or WebKitGTK; neither exists for GTK 4 on Windows.

### Option A: MSYS2 UCRT64 (what the branch uses now)

GTK, PyGObject and Python all come from MSYS2's `mingw-w64-ucrt-x86_64-*`
packages, and the app is frozen from that environment.

- **Versions are current.** On the development machine: gtk4 4.24.1,
  libadwaita 1.10.0, gtksourceview5 5.22.0, python 3.14.8,
  python-gobject 3.58.1, libvterm 0.3.3.
- **Precedent.** Inkscape builds with the UCRT64 toolchain
  ([wiki](https://wiki.inkscape.org/wiki/Compiling_Inkscape_on_Windows)).
  GIMP builds in MSYS2 (CLANG64 on x64) and packages with Inno Setup and
  MSIX ([developer docs](https://developer.gimp.org/core/setup/build/windows/),
  [MR !1154](https://gitlab.gnome.org/GNOME/gimp/-/merge_requests/1154)).
  Both are C/C++ apps that copy an MSYS2 prefix. Neither freezes a Python
  app.
- **PyInstaller works under MSYS2.** MSYS2 packages it
  ([mingw-w64-pyinstaller-hooks-contrib](https://packages.msys2.org/base/mingw-w64-pyinstaller-hooks-contrib)).
  The unmerged `origin/cursor/windows-support-abbc` branch has an MSYS2
  (MINGW64, not UCRT64) workflow and spec that build a portable zip.
- **The blocker is pywinpty.** MSYS2's `python-pywinpty` is 2.0.15. Its
  extension imports `CreatePseudoConsole` from kernel32 only, so it always
  gets the **inbox** ConPTY (checked by scanning
  `winpty.cp314-mingw_x86_64_ucrt_gnu.pyd`). The inbox ConPTY is the cause of
  the dead mouse and the stripped paste markers (section 2). PyPI wheels
  cannot load into MSYS2 Python (different C runtime ABI and tags), and
  building pywinpty 3 for MinGW means a Rust `-gnu` toolchain plus NuGet
  inside MSYS2. The package also forgets its `winpty` runtime dependency
  (windows-port.md §2).
- **Other costs.**
  - Contributors must install MSYS2 and work in its shell.
  - Every Python dependency must exist as a pacman package, or be built with
    MinGW.
  - The frozen app carries the MinGW runtime DLLs.
  - The stack is whatever pacman has on build day, unless CI pins a
    snapshot. The macOS build already had to pin Homebrew after GTK 4.24.0
    broke keys (`.github/workflows/macos-pyinstaller.yml`).

### Option B: gvsbuild GTK + python.org (MSVC) Python

GTK is built with Visual Studio by [gvsbuild](https://github.com/wingtk/gvsbuild).
Python is the python.org (or NuGet) build, and every other Python package
comes from PyPI wheels.

- **What gvsbuild ships.** The release CI builds `gtk4 libadwaita
  gtksourceview5 pygobject protobuf-c adwaita-icon-theme gtkmm` with
  `--enable-gi --py-wheel`, against Python 3.14
  ([ci.yml](https://github.com/wingtk/gvsbuild/blob/main/.github/workflows/ci.yml)).
  - The release zip `GTK4_Gvsbuild_<tag>_x64.zip` (a 287 MiB zip for
    2026.8.0) carries PyGObject and pycairo wheels in
    `C:\gtk\wheels`.
  - Release 2026.6.0 has GTK 4.22.4 and libadwaita 1.9.1
    ([releases](https://github.com/wingtk/gvsbuild/releases)). That is a
    little behind MSYS2, and above our 1.5 floor.
  - The binaries come "AS IS" and "are not tested", and the maintainers
    advise building your own for distribution (README). In CI that means
    `gvsbuild build ...` with pinned versions, or the release zip pinned by
    hash.
- **PyGObject has no Windows wheels on PyPI** (sdist only,
  [PyPI](https://pypi.org/project/PyGObject/)). The gvsbuild wheels are the
  supported route.
- **Precedent: Gaphor.** Gaphor is a GTK 4 + libadwaita + PyGObject app, and
  it ships exactly this stack.
  - Its CI downloads the pinned gvsbuild zip, caches `C:\gtk`, installs the
    gvsbuild wheels, and freezes with PyInstaller (`_packaging/gaphor.spec`).
    It builds its own PyInstaller bootloader, because the prebuilt one trips
    antivirus scanners.
  - It makes an NSIS installer and a 7-Zip self-extracting portable exe,
    signs both through SignPath, scans them with VirusTotal, and submits to
    winget with `wingetcreate`
    ([full-build.yml](https://github.com/gaphor/gaphor/blob/main/.github/workflows/full-build.yml),
    [windows_executables action](https://github.com/gaphor/gaphor/blob/main/.github/actions/windows_executables/action.yml),
    [build_win_installer.py](https://github.com/gaphor/gaphor/blob/main/scripts/build_win_installer.py)).
  - Gaphor 3.3.2's installer is **44.5 MiB**; the portable exe is 43.4 MiB.
  - One thing not to copy: its SHA-256 check of the gvsbuild zip computes a
    boolean but never fails the step.
- **pywinpty fits.** PyPI pywinpty 3.0.5 (June 2026) has `win_amd64` and
  `win_arm64` wheels for CPython 3.10–3.14
  ([PyPI](https://pypi.org/project/pywinpty/)). Each wheel bundles
  `conpty.dll` and `OpenConsole.exe` from Microsoft's
  `Microsoft.Windows.Console.ConPTY` NuGet package (section 2).
- **Other dependencies.** cryptography, psutil, PyYAML, lxml and keyring's
  `pywin32-ctypes` have ordinary MSVC wheels. keyring's Windows backend is
  pure Python over ctypes, so it works on either stack.

### Other stacks considered

- **Mixing:** MSYS2's GTK DLLs with python.org Python, or PyPI pywinpty in
  MSYS2 Python. Not viable. PyGObject and pywinpty are C extensions tied to
  their interpreter's toolchain and tags. Loading one from the other stack
  fails, or works by accident across CRTs.
- **conda-forge.** It packages GTK and PyGObject, but no shipped GTK 4 app
  uses it on Windows that we found. The installer would also carry conda's
  layout. Not pursued.
- **WebView2/xterm.js or a non-GTK frontend.** Out of scope: they replace
  the frontend instead of packaging it (windows-port.md §3).

### Freezing: PyInstaller or Nuitka

- **PyInstaller** is what the macOS build already uses
  (`packaging/pyinstaller/`), and Gaphor proves it on Windows with gvsbuild.
  Its GI hooks collect typelibs, gdk-pixbuf loaders and schemas. A missing
  `GIRepository` typelib makes the hook skip them silently, so CI should
  assert the bundle contents. `scripts/verify_pyinstaller_bundle.py` is the
  place for that.
- **Nuitka** compiles to C and supports MSVC and MinGW. It would give smaller
  startup and fewer antivirus false positives, but it is a second freezing
  system to learn. No GTK 4 Python app we found ships with it.
- **Recommendation: PyInstaller**, with a self-built bootloader as Gaphor
  does.

Windows-specific freezing work, whatever the stack:

- **Askpass needs an executable.** The askpass launcher runs
  `python -I askpass_helper.py` today (windows-port.md §4). A frozen app has
  no `python.exe`. Ship a small console-subsystem entry point (for example
  `sshpilot-askpass.exe`) in the same PyInstaller bundle (shared `COLLECT`),
  and point `askpass.cmd`, or `SSH_ASKPASS` directly, at it.
- **The daemon needs a console-subsystem exe.** A windowed exe gets no
  usable standard streams, and the daemon's hidden-console child model
  (`CREATE_NO_WINDOW`) wants a console-subsystem process.
- **DLLs PyInstaller cannot see.** `libvterm-0.dll`, and pywinpty's
  `conpty.dll` and `OpenConsole.exe`, are not linked imports, so the spec
  must list them. `winpty.dll` and `winpty-agent.exe` (about 5 MiB) can be
  dropped, because only the ConPTY backend is used.

### Installer, signing, updates

- **Installer.** NSIS, as Gaphor does, or Inno Setup, as GIMP does. Either
  works. NSIS lets us copy Gaphor's tested script and its portable variant.
  MSIX/Store can come later.
- **Size.** Expect about 50–60 MiB: Gaphor's 44.5 MiB, plus cryptography,
  lxml and pywinpty's ConPTY pair (about 1.2 MiB). This is an estimate; the
  first CI build measures it.
- **Signing.** Without a signature, SmartScreen warns on every download.
  [SignPath Foundation](https://signpath.org/) signs open-source projects for
  free and has a GitHub Action (Gaphor uses it). The fallback is a paid
  certificate or Azure's hosted signing service. Sign every exe: the app,
  the daemon, askpass, and the bundled `OpenConsole.exe` (it is already
  Microsoft-signed; check that it keeps its signature).
- **Updates.** Publish to winget from the release workflow (Gaphor's
  `wingetcreate` job), and keep the GitHub release assets. An in-app "new
  version available" notice is enough at first. A self-updater can wait.

### Recommendation

**Option B: python.org Python 3.14 + gvsbuild GTK 4 + PyPI wheels, frozen
with PyInstaller, NSIS installer, SignPath, winget.**

The deciding facts:

1. The only Python ConPTY binding that ships a modern ConPTY is PyPI's
   pywinpty, and it is MSVC-only.
2. A GTK 4 + libadwaita + PyGObject app already ships on this stack with
   working CI.
3. Contributors use an ordinary Python and pip, instead of MSYS2.

The costs:

- GTK is two minor versions behind MSYS2.
- We own a pinned gvsbuild build in CI.
- libvterm must be built with MSVC, because gvsbuild does not include it
  (section 3).

Keep the MSYS2 instructions as an alternative development setup until B is
proven, then retire them.

---

## 2. PTY layer

### How other terminals host ConPTY

| Terminal | ConPTY used | `CreatePseudoConsole` flags | Source |
|---|---|---|---|
| Windows Terminal | its own OpenConsole, through the conpty.dll it ships | (its own) | [1.22 release notes](https://github.com/microsoft/terminal/releases/tag/v1.22.2362.0) |
| WezTerm | prefers a side-loaded `conpty.dll` + `OpenConsole.exe` next to the exe, falls back to kernel32 | `INHERIT_CURSOR \| RESIZE_QUIRK \| WIN32_INPUT_MODE` (0x7); `PASSTHROUGH_MODE` (0x8) defined but unused | [pty/src/win/pseudocon.rs](https://github.com/wezterm/wezterm/blob/main/pty/src/win/pseudocon.rs) |
| Alacritty | `LoadLibraryW("conpty.dll")` from PATH or the exe directory, falls back to the system API | 0 | [tty/windows/conpty.rs](https://github.com/alacritty/alacritty/blob/master/alacritty_terminal/src/tty/windows/conpty.rs) |
| Tabby | node-pty with `useConptyDll` (bundled conpty) on build ≥ 19041, retries with inbox on failure | node-pty: only `INHERIT_CURSOR` when asked | Tabby `tabby-local/src/session.ts:120-132`, [node-pty conpty.cc](https://github.com/microsoft/node-pty/blob/main/src/win/conpty.cc) |
| Zed | ships conpty.dll + OpenConsole.exe; "the pseudoconsole provided by Windows OS is severely outdated" | n/a | [Zed commit 8af3f58](https://git.secluded.site/zed/commit/8af3f583c2776f5b5ab1d1c2ec1bb2e00f151f74) ("Better conpty", #39090) |
| pywinpty 3.x (PyPI) | bundled conpty.dll + OpenConsole.exe in the wheel (winpty-rs `conpty_local`) | 0 | `winpty-rs/src/pty/conpty/pty_impl.rs:402`, `pywinpty/build.rs` |

Every terminal that cares about full-screen apps ships Microsoft's ConPTY
package instead of using the copy in Windows.

**Why it matters.** Windows Terminal 1.22 replaced ConPTY's renderer: it now
translates console API calls to VT directly, instead of rendering a snapshot
of the screen. It also gained a way to pass application VT through unmodified
(release notes above). The inbox ConPTY is the old renderer on Windows 10, and on Windows 11 24H2 it
still strips mouse modes. Reports from other terminals:

- On 24H2, conhost drops `?1000h/?1002h/?1003h/?1006h` and `?7l`
  ([VelaTerm #143](https://github.com/vlinx-io/VelaTerm/issues/143)).
- The passthrough flag alone did not fix mouse loss; shipping the runtime
  did ([oxideterm #6](https://github.com/liansishen/oxideterm/pull/6)).

### Measured on this machine

Windows 10 Pro 19045. A child console program wrote mode sequences, and a
second program read raw VT input.

The bundled ConPTY was pywinpty 3.0.5 on python.org 3.14, with conpty.dll
1.24.2605.12001 and flags 0. The inbox ConPTY was MSYS2's pywinpty 2.0.15.

| Child → terminal | bundled | inbox |
|---|---|---|
| `?1049h` / `?1049l` (alternate screen) | passed through | dropped; conhost repaints instead |
| `?1000h`, `?1006h`, `?1000l` (mouse) | passed through | dropped |
| `?2004h` / `?2004l` (bracketed paste) | passed through | passed through |
| OSC 8 hyperlink | passed through | passed through, but the text moved outside the link |
| Output shape | the child's bytes, verbatim | a full-screen repaint (`ESC[2J`, padded lines) |
| At startup | `ESC[1t ESC[c ESC[?1004h ESC[?9001h` | `ESC[c` and `ESC[?9001h` (windows-port.md §2) |

| Terminal → child (VT input mode) | bundled | inbox |
|---|---|---|
| SGR mouse `ESC[<0;5;6M`/`m` | delivered | dropped |
| Paste `ESC[200~hi ESC[201~` | delivered with markers | `hi` only |

This explains every ConPTY limitation in windows-port.md §3a's live check:
the mouse dead in htop, paste markers dropped, the alternate screen invisible.
They are inbox artefacts, and a bundled ConPTY removes them without any
passthrough flag.

Two new facts to handle:

- **Focus events.** The bundled host turns on focus reporting (`?1004h`).
  The view should send `ESC[I`/`ESC[O`.
- **Answer DA1 at once.** In the probe, a DA1 answer sent late was delivered
  to the child's input, because the child had already switched to VT input
  mode. The terminal must answer the startup `ESC[c` immediately, which the
  daemon path should do before `ssh.exe` reads input.

### Options

1. **Inbox ConPTY (today).** No mouse, no alternate screen, no paste markers,
   repaint-on-resize workarounds. Rejected.
2. **pywinpty 3.x from PyPI.** Bundled ConPTY with no extra work. Flags are
   fixed at 0: no `RESIZE_QUIRK`, no `INHERIT_CURSOR`, no passthrough. The
   API reads and writes `str`, which `conpty_runner.py` already handles. Only
   on Option B.
3. **The ctypes runner being spiked** (`daemon/conpty_ctypes.py` on
   `claude/ctypes-conpty-771129`, not finished when this was written). It
   loads a bundled conpty.dll or falls back to kernel32, has configurable
   flags, and works on either stack. It means owning roughly 400 lines of
   Win32: pipe handling, the `ClosePseudoConsole` hang on Windows 10,
   `STILL_ACTIVE` exit codes, job objects. Those are the pitfalls catalogued
   in [conpty-oxide's notes](https://docs.rs/crate/conpty-oxide/0.1.2/source/docs/conpty-pitfalls.md).

### Recommendation

**Use the bundled ConPTY, through pywinpty 3.x on Option B.**

- Pin pywinpty, and with it the NuGet ConPTY version, because conpty.dll and
  OpenConsole.exe must come from the same release (conpty-oxide's notes,
  [wezterm#7774](https://github.com/wezterm/wezterm/issues/7774)).
- Log at startup which ConPTY loaded.
- **Do not merge the ctypes runner as the primary path.** Keep it as the
  fallback if we need flags pywinpty does not expose, or if pywinpty
  regresses. It already accepts the pywinpty surface, so swapping is local
  to `conpty_runner.py`.
- If the stack stays on MSYS2, the ctypes runner with a vendored NuGet pair
  becomes the only way to get a modern ConPTY, and is then required.

Leave `PSEUDOCONSOLE_PASSTHROUGH_MODE` off until measured. The bundled
runtime already forwards what we need with flags 0. Passthrough changes
which side owns the screen, and WezTerm does not use it either.

---

## 3. Emulator core

Judged against Option B (MSVC Python, PyInstaller), with ConPTY now
forwarding the alternate screen, mouse and paste.

| Core | Speed (1.5 MB, 80x24) | Packaging on B | Features | Risk |
|---|---|---|---|---|
| **pyte** 0.8.2 (pure Python) | 0.35 MB/s | none | no reflow, no OSC 8; `TypeError` on `CSI ? 4 m`; loses lines on alternate-screen resize (windows-port.md §3a) | upstream dormant (last release Nov 2023) |
| **libvterm** 0.3.3 (C, ctypes) | 6.7–14.4 MB/s | one ~80 KB DLL; plain C99, builds with MSVC (Neovim vendors and builds it, [src/nvim/vterm](https://github.com/neovim/neovim/tree/master/src/nvim/vterm)) | alternate screen, mouse, paste, DA/DSR; reflow exists but is not wired; no DECSET 9; alternate-screen resize bug worked around (`_AltResize`) | upstream quiet; Neovim maintains its own fork |
| **libghostty-vt** (Zig, C API) | not measured; Ghostty's core | needs Zig to build a static or shared lib; MSVC builds now in Ghostty CI ([announcement](https://x.com/mitchellh/status/2036169542762897685)) | reflow, Kitty keyboard, OSC 8, modern xterm coverage | "not tagged with a version yet", "API signatures are still in flux" ([README](https://github.com/ghostty-org/ghostty)) |
| **alacritty_terminal / wezterm-term** (Rust, via PyO3) | fast | we would write and own a binding crate, build with maturin per Python version, and ship a Rust toolchain in CI; no published Python binding exists | strong (reflow, modern sequences) | the binding and its API surface become ours to maintain |

Notes:

- Under the bundled ConPTY the emulator now sees raw application streams on
  Windows too. libvterm's alternate-screen code and `_AltResize` become live
  code on Windows, where they were dead under the inbox host.
- pyte's alternate-screen loss and its `CSI ? 4 m` crash would now hit
  Windows users (vim 9 sends it).
- The `pull_history_on_grow = False` workaround exists because the inbox
  conhost repaints its viewport. It should be re-measured under the bundled
  host, and probably dropped there.
- Paneflow is a Rust terminal that moved from alacritty_terminal to a
  statically linked libghostty-vt on Windows x64, pinning the source commit
  and the archive ([blog](https://paneflow.dev/blog/libghostty-windows)).
  That is evidence the C API is usable on Windows. Its own caveat stands:
  every engine update means reviewing the archive and the bindings together.

### Recommendation

**libvterm becomes the Windows default once the frozen build ships an
MSVC-built `libvterm-0.dll`. pyte stays as the fallback. libghostty-vt is
the planned successor.**

- Build libvterm from source in the Windows CI with `cl`. It is a dozen C
  files with no dependencies. Pin the 0.3.3 tarball, or Neovim's vendored
  copy, which carries fixes.
- Re-evaluate libghostty-vt when it is tagged. A ctypes binding of its C API
  slots behind the same `TerminalModel` surface `vterm_terminal_model.py`
  implements, and its build (Zig produces a C ABI DLL) fits Option B.
- Do not take the PyO3 route. It adds a Rust toolchain and a binding we
  would own, for a core that libghostty-vt will offer through a C ABI anyway.

---

## 4. Connection sharing without ControlMaster

### Where it bites

On POSIX a master lets extra tabs, SFTP (`ssh … -s sftp`, driven by the
pure-Python client in `sftp/client.py`), Host Info and broadcast commands
reuse one authenticated connection. On Windows each of those starts its own
`ssh.exe` and authenticates again.

- **Key in the agent** (OpenSSH agent service, 1Password): silent. Nothing to
  fix.
- **Password:** a prompt per channel, unless the password is saved, in which
  case the broker answers it.
- **Keyboard-interactive / 2FA / OTP:**
  - Interactive tabs use `SSH_ASKPASS_REQUIRE=prefer`, so OTP prompts fall
    back to the terminal. The user types a code for every tab.
  - Headless channels (SFTP, Host Info, broadcast) use `force`
    (`daemon/interaction_broker.py:375`). The broker does not support
    unrestricted keyboard-interactive prompts (interaction-broker.md, "Typed
    prompts"), so **SFTP and Host Info fail on 2FA hosts on Windows**.
- **FIDO keys:** a touch per channel.

### Options

1. **Accept one login per channel** (today), and soften it:
   - Recommend agent-held keys in the UI.
   - The broker can remember a password in memory for the daemon's lifetime
     ("remember for this session").
   - Teach the broker a single-field keyboard-interactive prompt, so a
     headless channel can ask the user for an OTP instead of failing.
   - Show "this opens a second login" on SFTP/Host Info for hosts known to
     use 2FA.
2. **Wait for upstream.**
   [PowerShell/openssh-portable#863](https://github.com/PowerShell/openssh-portable/pull/863)
   implements `ControlMaster`/`ControlPath`/`ControlPersist` in `ssh.exe`
   over named pipes.
   - Opened July 2026, rebased September 2026, still open.
   - The author says it was written with Claude and may never merge.
   - The feature request dates back years
     ([Win32-OpenSSH#1328](https://github.com/PowerShell/Win32-OpenSSH/issues/1328)).
   - If it ships, the POSIX master code (`ssh_multiplex.py`,
     `daemon/control_masters.py`) mostly lights up again. The Windows
     branches would need a pipe-style `ControlPath` and a version probe
     instead of today's unconditional `ControlMaster=no`.
3. **The daemon holds connections in a Python SSH library.** Only
   [asyncssh](https://pypi.org/project/asyncssh/) (2.24.1, October 2026)
   fits.
   - It has keyboard-interactive and the Windows OpenSSH agent pipe
     (`asyncssh/agent_win32.py`). paramiko 5.0 also reaches the agent pipe
     (`paramiko/win_openssh.py`).
   - libssh's Python binding has no Windows wheels
     ([ansible-pylibssh](https://pypi.org/project/ansible-pylibssh/)).
   - This is a **second SSH engine**: `ssh_config` semantics (Match,
     ProxyJump chains, Include, certificates, GSSAPI, FIDO, known_hosts
     hashing) would have to agree with OpenSSH's, or users would see
     different behaviour per channel. It breaks the ground rule and would
     move a large piece of the security surface into our code.
4. **Bring our own `ssh.exe`** (MSYS2/Cygwin OpenSSH has ControlMaster).
   - Its Unix-socket emulation cannot see the Windows agent service, it reads
     different config and key locations, and it cannot run `askpass.cmd`
     (windows-port.md §8).
   - Rejected for the same reasons the port moved System32 OpenSSH to the
     front of PATH.

### Recommendation

**Option 1 now, Option 2 when it ships. Do not adopt a second SSH engine.**

The keyboard-interactive work in the broker pays off on POSIX too, for
channels that run without a master. Re-open Option 3 only if upstream
multiplexing is abandoned *and* users report 2FA re-prompts as a blocker.
Even then, scope it to SFTP and Host Info, never terminals.

---

## 5. Local shells and the daemon transport

- **Local shells.** On Windows the daemon always runs on the same machine as
  the GUI. A local shell tab can therefore be an ordinary daemon session:
  `ConPtySessionProcessRunner` spawning `pwsh.exe`, `cmd.exe` or `wsl.exe`
  instead of `ssh.exe`. It reuses `SessionRuntime`, the reattachment and the
  built-in terminal unchanged. There is no need for the frontend PTY that
  windows-port.md lists.
  - It needs a shell picker and the `os.kill(pid, 0)` fixes listed in
    windows-port.md §7.
  - It depends on the bundled ConPTY, because PowerShell and cmd behave far
    better under a modern ConPTY than the inbox one.
- **Daemon transport.** Keep loopback TCP with the token preamble.
  - Named pipes would add connect-time ACLs and a reliable peer pid, at the
    cost of a bridge thread per client into the socket selector loop
    (windows-port.md, "What is not done").
  - Same-user access is already enforced by the ACL-restricted endpoint file
    and the token.
  - Revisit only for multi-user terminal servers.

---

## Roadmap

In order. Each step lists which current modules it **keeps**, **changes** or
**removes**. POSIX code paths are untouched throughout.

1. **Switch the dev and CI stack to Option B** (python.org 3.14 + pinned
   gvsbuild + PyPI wheels).
   - Change: `docs/running-on-windows.md` (python.org setup; MSYS2 kept as an
     alternative section for now); `pyproject.toml` (`pywinpty>=3.0.5` on
     win32).
   - New: a Windows test workflow running the existing Windows test files.
   - Keep: all of `src/`. `conpty_runner.py` already accepts the 3.x `str`
     API.
2. **Adopt the bundled ConPTY through pywinpty 3.x.**
   - Change: `daemon/conpty_runner.py` (log which ConPTY loaded; answer DA1
     immediately; ignore `?9001h` as today).
   - Change: `terminal_backend_pyte.py` (send focus events for `?1004h`;
     re-test mouse, paste and the alternate screen live).
   - Re-measure and probably remove the inbox-only workaround
     `prepare_pty_less_emulation`/`pull_history_on_grow=False` for ConPTY
     sessions.
   - Keep the ctypes spike (`daemon/conpty_ctypes.py`) unmerged as a
     documented fallback.
3. **Build libvterm with MSVC in CI, and make it the Windows default.**
   - Change: `terminal_backends`/`TerminalWidget` default model on Windows;
     Preferences default.
   - Keep: `vterm_terminal_model.py`, `terminal_model_common.py`,
     `pyte_terminal_model.py` (fallback); the tests in
     `tests/test_pyte_terminal_model.py` run both.
4. **Freeze, install, sign.**
   - New: `packaging/pyinstaller/sshpilot-windows.spec`. Reuse the ideas from
     `origin/cursor/windows-support-abbc`, not its MINGW64 paths.
   - New: GUI, daemon and askpass entry points; NSIS script (adapted from
     Gaphor); SignPath and winget jobs; self-built bootloader.
   - Change: the askpass launcher in `daemon/interaction_broker.py` and
     `askpass_utils.py` (frozen: run `sshpilot-askpass.exe`, not `python -I`).
   - Change: `daemon/launcher.py` (frozen daemon exe);
     `scripts/verify_pyinstaller_bundle.py` (assert typelibs, `libvterm-0.dll`,
     `conpty.dll` + `OpenConsole.exe`).
5. **Soften re-authentication.**
   - Change: `daemon/interaction_broker.py` and `askpass_utils.py`
     (single-field keyboard-interactive prompt for headless channels;
     in-memory "remember for this session").
   - Keep: `ssh_multiplex.py`'s Windows `ControlMaster=no` branch and the
     empty `cm` directory.
   - Watch openssh-portable#863. When a Win32-OpenSSH release has it, add a
     version probe and a named-pipe `ControlPath`, and re-enable the
     preference.
6. **Local shell tabs as daemon sessions.**
   - Change: the local-shell action and `setup_local_shell` (Windows path
     through the daemon); the `os.kill(pid, 0)` call sites in `terminal.py`
     and `ssh_process_manager.py`.
   - Keep: `ConPtySessionProcessRunner`, `SessionRuntime`.
7. **Retire MSYS2 as a documented setup.**
   - Remove: the MSYS2 section of `running-on-windows.md` and the MSYS2
     notes in windows-port.md (pywinpty 2.x `bytes` handling can stay; it is
     harmless).
   - Re-evaluate the core when libghostty-vt is tagged: a
     `ghostty_terminal_model.py` behind the same model surface. pyte is
     removed only after it.

Unchanged by every step: `api/transport/endpoint.py` (loopback TCP + token),
`platform/windows/*` (ACLs, OpenSSH PATH preference, `ssh-copy-id`
replacement), `platform/paths.py`, `platform/locking.py`, the system-agent
pipe detection, and the POSIX PTY runner, VTE backend and Unix-socket
transport.

## Open questions

- Does the bundled ConPTY still repaint the viewport on resize, and does
  `RESIZE_QUIRK` matter for us? pywinpty cannot set it. If it matters, step 2
  switches to the ctypes runner.
- ARM64: pywinpty and the NuGet package cover it; gvsbuild's release builds
  x64 only. Not a target yet.
- How large is the frozen build really? Measure in step 4 before committing
  to a portable single-exe variant.
