# Windows port: implementation plan

Status: **plan, October 2026.** This turns
[windows-decisions.md](windows-decisions.md) (the research) and the ConPTY
spike (windows-port.md §2a on branch `claude/ctypes-conpty-771129`,
merged in step 1.1) into
ordered work. [windows-port.md](windows-port.md) describes how the port works
today.

The ground rules from windows-port.md still hold. POSIX behaviour does not
change, and native OpenSSH stays the SSH engine.

## Status (October 2026)

| Phase | State |
|---|---|
| 0. Make shipped code safe | Done: libvterm CSI guard, pyte CSI normaliser, Linux suite matches `main` |
| 1. Modern ConPTY | Done: ctypes runner default, bundled console fetched and verified; gate measurements in windows-port.md §2a. Mouse via the GUI still needs a person |
| 2. libvterm default | Done: patched build, ABI self-test, Windows default, synchronized output, output slicing. **2.6 reflow deliberately not done** (windows-port.md, "What is not done") |
| 3. python.org + gvsbuild | Done: setup/run scripts, Windows CI, POSIX-only test list |
| 4. Frozen build, installer | Done and verified locally; signing waits for a SignPath account |
| 5. Auth without ControlMaster | Done: keyboard-interactive was already brokered; session-only remembered passwords (API 0.82) |
| 6. Local shells | Done: frontend-owned, on Windows and for the built-in backend everywhere |
| 7. Remaining gaps | Done, plus fixes found by live checks (ACLs, selectors, Include paths, D-Bus); open items in windows-port.md |
| 8. Retire MSYS2 | Waits for the first shipped release |

## How to work through this plan

The port so far was built one blocker at a time, and layer choices showed
their cost late. To stop that repeating:

- **Each phase starts with a research gate.** Before any code is written,
  answer the gate's questions from documentation, from how other terminals
  and GTK apps solved it, and from a measurement on this machine. Record the
  answers in the phase's section of windows-port.md. If an answer contradicts
  this plan, change the plan first.
- **Look at the whole data path, not only the layer in the question.** For
  the terminal that path is ssh.exe → ConPTY → runner → daemon → model →
  view.
- **Spike, then build.** Throwaway measurement harnesses stay out of the
  tree (`.agent_terminal_investigation/` is git-ignored for this).
- **Launch the real app after touching module-level code** (windows-port.md
  §10). Headless tests have missed crashes before.
- **One writer per set of files.** The parallel tracks below are split so
  that no two sessions edit the same modules.

## Decisions this plan makes

The research and the spike agree on every layer except one.

**ConPTY binding: the ctypes runner, not pywinpty 3.x.** The research
recommends pywinpty 3.x from PyPI, which bundles the modern ConPTY but only
exists for python.org Python. The spike recommends our ctypes runner. This
plan takes the ctypes runner, because:

1. **It works on both stacks.** We get the modern ConPTY (mouse, paste
   markers, the alternate screen) now, on MSYS2, instead of only after the
   stack migration in phase 3. That removes the hardest ordering dependency
   in the research roadmap.
2. **It is written and measured.** Raw byte captures show the same streams
   as pywinpty with kernel32, plus verbatim mouse, paste and `?1049h` with
   conpty.dll (windows-port.md §2a).
3. **It is the only binding that takes flags.** That keeps the open
   `RESIZE_QUIRK` question answerable, and it lets us pin the conpty.dll and
   OpenConsole.exe pair ourselves, independent of pywinpty's release cadence.
4. **Its costs are known.** It is about 650 lines of Win32 code we own:
   pipes, the Windows 10 `ClosePseudoConsole` hang, exit codes. Neovim,
   WezTerm and Alacritty use the same design (windows-decisions.md §2).

pywinpty stays behind `SSHPILOT_CONPTY_BACKEND=pywinpty` for one release, and
pywinpty 3.x stays the documented fallback if owning the binding goes badly.

**Local shells: owned by the frontend, not the daemon.** windows-decisions.md
§5 proposed running local shells as daemon sessions. That contradicts a
boundary the repo already documents: frontend-closure-audit.md classifies
the local shell tab as "legitimate frontend-only", owned by the terminal
presentation, and on Linux and macOS local shells are frontend VTE spawns.
Making them daemon sessions would add local-process RPCs to a daemon that
otherwise only owns SSH Pilot state and remote operations, and Windows would
behave differently from POSIX. So the frontend owns them on Windows too
(phase 6). The ConPTY binding moves to `sshpilot.platform.windows`, which
both the daemon and the frontend may import (`core/package_graph.py`). The
cost is that a local shell does not survive a frontend restart, which is
the same as on POSIX today.

Everything else follows windows-decisions.md:
- python.org Python with gvsbuild GTK 4 and PyInstaller;
- a patched libvterm built from source as the Windows default emulator;
- one login per channel, softened through the broker, until upstream
  ControlMaster ships;
- loopback TCP for the daemon transport.

## Phase 0: make what is already shipped safe (now, on MSYS2)

Small fixes. The opt-in libvterm backend can be crashed by a remote host
today, and phase 1 makes pyte's known `TypeError` reachable on Windows.

| Step | Work | Files | Done when |
|---|---|---|---|
| 0.1 | Cap CSI parameters before libvterm sees them. Split `feed` input at an over-long CSI and drop or truncate parameters beyond 16, so the stock 0.3.3 DLL cannot overflow `args[16]`. | `vterm_terminal_model.py`, `tests/test_pyte_terminal_model.py` | `ESC [ ;×17 m`, and 17+ real parameters, feed without a crash. A regression test runs against the model. |
| 0.2 | Stop pyte raising on private-marker SGR (`CSI ? 4 m`, vim's `CSI > 4 ; 2 m`): ignore those sequences instead of letting `select_graphic_rendition` throw. | `pyte_terminal_model.py`, tests | Recorded vim 9 streams replay through pyte without an exception. |
| 0.3 | Run the Linux and macOS test suites against `windows`, which has never been done (windows-port.md, "Tests"). Fix any regressions. | `tests.yml` runs on pull requests only, so open a draft PR from `windows`, or run the suite in WSL | The suite is green on Linux, and on macOS through CI. |

## Phase 1: the modern ConPTY (on MSYS2)

**Research gate.**
- Which NuGet `Microsoft.Windows.Console.ConPTY` version do we pin, and what
  are its SHA-256 hashes?
- Does `RESIZE_QUIRK` change anything for us? Measure with
  `SSHPILOT_CONPTY_FLAGS`.
- Under OpenConsole, do `pull_history_on_grow = False` and
  `prepare_pty_less_emulation` still help, or do they now hurt, since the
  remote stream arrives verbatim?
- Does the startup `ESC[c` (DA1) get answered before `ssh.exe` switches to VT
  input? A late answer reaches the child as input.

| Step | Work | Files | Done when |
|---|---|---|---|
| 1.1 | Merge `claude/ctypes-conpty-771129` and make `ctypes` the Windows default. Keep `pywinpty` behind the switch. Move the binding from `daemon/conpty_ctypes.py` to `platform/windows/conpty.py` (stdlib-only, already allowed for the daemon by `core/package_graph.py`), so phase 6 can reuse it from the frontend without a second move. | `platform/windows/conpty.py`, `daemon/conpty_runner.py`, `daemon/server.py`, `core/package_graph.py` | Daemon log names the backend and the DLL; existing ConPTY tests pass with both backends; the package-graph test passes. |
| 1.2 | A fetch script that downloads the pinned NuGet package from nuget.org into the git-ignored `vendor/conpty/`, checks its SHA-256 and **fails on a mismatch**, and lays out `conpty.dll` with `OpenConsole.exe` beside it. Source checkouts find it there automatically. | `scripts/fetch-conpty.py`, `docs/running-on-windows.md` | A fresh checkout gets the bundled ConPTY with one command. |
| 1.3 | Fold in the reference hosts' behaviour: answer DA1 immediately; the orphan-conhost guard (terminate a child that produced no output before closing, as Neovim does); keep `ClosePseudoConsole` off the reader thread (the spike already does this). | `daemon/conpty_runner.py`, `daemon/conpty_ctypes.py` | Opening and closing a tab at once leaves no `OpenConsole.exe`; tests cover both. |
| 1.4 | Focus events: send `ESC[I`/`ESC[O` while `?1004h` is set. The bundled host turns it on at start. | `terminal_backend_pyte.py`, `terminal_model_common.py` | Unit test, plus vim's `FocusGained` firing over ssh. |
| 1.5 | Apply the gate's results on growing and the alternate screen. Under OpenConsole, `_AltResize` and pyte's alternate-screen code become live on Windows; drop the inbox-only workarounds if the measurement says so. | `terminal_backend_pyte.py`, both models | The grow/shrink transcripts from windows-port.md §3a are lossless under OpenConsole with libvterm. |
| 1.6 | **Live check by the user** (GTK4 ignores simulated mouse input): clicks and wheel in htop and vim (`set mouse=a`), paste into a shell and into vim, resize inside both. | none | Checklist results recorded in windows-port.md §2a. |

## Phase 2: libvterm becomes the Windows default (can overlap phase 1)

**Research gate.**
- Which commit of the `github.com/neovim/libvterm` mirror do we pin? Which
  upstream or Neovim patch is the `CSI_ARGS_MAX` fix?
- Does a `cl` build keep `VTermScreenCell` at 40 bytes, with the same
  attribute bitfield packing `_decode_cell` assumes? Do the same check for a
  MinGW `gcc` build. Do not take Neovim's internal `src/nvim/vterm/`: its
  cells hold an `schar_T` index instead of `chars[6]`, so it is not
  ABI-compatible with our model (checked in the Neovim source).
- Does the bundled ConPTY forward synchronized output (`?2026h`/`?2026l`)?
- Does the window stay responsive while bulk output (a large `cat`) is fed
  on the GTK main thread through libvterm? If not, measure Neovim's
  approach: it repaints on a 10 ms timer and pauses reading while it
  refreshes (`REFRESH_DELAY`, `read_pause_cb` in `terminal.c`/`channel.c`).

| Step | Work | Files | Done when |
|---|---|---|---|
| 2.1 | A build script for `libvterm-0.dll` from the pinned mirror, with `CSI_ARGS_MAX = 32` and a bounded `argi`. It produces an MSVC build (for phase 3) and a MinGW build (for MSYS2 meanwhile). | `scripts/build-libvterm.py` or `packaging/windows/libvterm/` | The DLL builds reproducibly; the source hash is pinned. |
| 2.2 | An ABI self-test at load: feed known attributes, read a cell back, and refuse a DLL whose layout differs, falling back to pyte with a logged reason. | `vterm_terminal_model.py`, tests | A deliberately mismatched layout falls back cleanly. |
| 2.3 | Make libvterm the default on Windows (`DEFAULT_MODEL` per platform; Preferences shows "libvterm (default)"). Keep pyte as the fallback. POSIX defaults are unchanged. | `terminal_backend_pyte.py`, `preferences.py` | New Windows tabs use libvterm; the Preferences test covers the label. |
| 2.4 | Synchronized output (mode 2026): hold repaints between `?2026h` and `?2026l`, with a timeout, as Neovim does. libvterm 0.3.3 and pyte ignore the mode, so the view tracks it. Without it, modern TUIs tear while they redraw. | `terminal_backend_pyte.py`, `terminal_model_common.py` | Unit test; a redraw inside a 2026 block paints once. |
| 2.5 | Apply the gate's responsiveness result: coalesce output into frame-sized feeds, and pause reading if feeding falls behind. | `terminal_backend_pyte.py`, the daemon output path | A 10 MB `cat` keeps the window responsive. |
| 2.6 | Later: the mirror's `sb_pushline4` for continuation-aware scrollback, then reflow on the primary screen only (Neovim #38812). | `vterm_terminal_model.py` | Long lines rejoin on widen; alternate-screen apps are unaffected. |

## Phase 3: move to python.org Python and gvsbuild GTK 4

**Research gate.**
- Which gvsbuild release (and its SHA-256) do we pin? Do its PyGObject and
  pycairo wheels match the Python 3.14 we pick?
- Is GTK 4.22 / libadwaita 1.9 enough for every API the app feature-detects?
- Does `PangoFT2` exist there (windows-port.md §9)?
- How does keyring behave on that stack?

| Step | Work | Files | Done when |
|---|---|---|---|
| 3.1 | Developer setup on the new stack: python.org 3.14, the pinned gvsbuild zip (hash-checked, **failing on a mismatch**, unlike Gaphor's check), PyPI wheels, `conpty`/`libvterm` from phases 1–2. Keep MSYS2 as the alternative section. | `docs/running-on-windows.md`, `pyproject.toml`, `scripts/run-windows.*` | A clean Windows VM runs the app from these instructions alone. |
| 3.2 | A Windows CI workflow on the new stack that runs the Windows test files. Mark the ~170 POSIX-only tests with an explicit `posix_only` marker and a reason, instead of letting them fail. | `.github/workflows/windows.yml`, `tests/` | CI is green and lists what it skips. |
| 3.3 | Re-run the phase 1 live checklist on the new stack. | none | Same results as on MSYS2. |

## Phase 4: frozen build, installer, signing

**Research gate.**
- Is sshPilot eligible for SignPath Foundation, and what is the lead time?
  This needs the maintainer to apply, so start it during phase 1.
- Bootloader build steps: copy Gaphor's.
- Does Microsoft's signature on `OpenConsole.exe` survive bundling?

| Step | Work | Files | Done when |
|---|---|---|---|
| 4.1 | Three entry points in one bundle: the windowed GUI, a console-subsystem daemon, and `sshpilot-askpass.exe`. The askpass launcher and the daemon launcher use them when frozen. | `daemon/interaction_broker.py`, `askpass_utils.py`, `daemon/launcher.py` | A frozen build logs in with a password prompt and runs a session. |
| 4.2 | A PyInstaller spec with a self-built bootloader. It lists `libvterm-0.dll`, `conpty.dll` and `OpenConsole.exe` as binaries, and leaves out winpty. | `packaging/pyinstaller/sshpilot-windows.spec` | The bundle runs on a clean VM with no Python. |
| 4.3 | Bundle assertions: typelibs, pixbuf loaders, schemas, the three DLL/exe files. | `scripts/verify_pyinstaller_bundle.py` | CI fails on a missing piece. |
| 4.4 | An NSIS installer and a portable build (adapted from Gaphor), SignPath signing of every exe, and a winget manifest job. | `packaging/windows/`, `release.yml` | A signed installer on a GitHub release; SmartScreen does not warn. |
| 4.5 | Measure the size (estimated 50–60 MiB) and decide on the portable single-exe variant. | none | Recorded in windows-decisions.md "Open questions". |

## Phase 5: logging in without ControlMaster (independent; also helps POSIX)

**Research gate.** What does a keyboard-interactive prompt look like through
`SSH_ASKPASS_REQUIRE=force`, for the common 2FA setups (Google
Authenticator PAM, Duo)?

| Step | Work | Files | Done when |
|---|---|---|---|
| 5.1 | A single-field keyboard-interactive prompt in the broker for headless channels, so SFTP and Host Info stop failing on 2FA hosts. | `daemon/interaction_broker.py`, `askpass_utils.py` | SFTP opens on a 2FA host after one code prompt. |
| 5.2 | "Remember for this session": the broker keeps a password in daemon memory only, never on disk. | `daemon/interaction_broker.py`, the prompt dialog | A second tab to the same host does not ask again. |
| 5.3 | A note on SFTP and Host Info that, on Windows, they open a second login. | UI | Shown only on Windows. |
| 5.4 | Track PowerShell/openssh-portable#863. When a Win32-OpenSSH release has ControlMaster, add a version probe and a named-pipe `ControlPath`, and re-enable the preference. | `ssh_multiplex.py` | Masters work on that release; older ones behave as today. |

## Phase 6: local shell tabs (after phase 1)

**Research gate.** Read `setup_local_shell`, the VTE and xterm.js spawn
paths, and frontend-closure-audit.md, and list everything a local tab relies
on today (child-exit signal, pid and pgid for "process running" checks, cwd
tracking, plugin `open_local_command_terminal`). The frontend runner must
cover each one or say why it does not apply on Windows.

The built-in backend (pyte or libvterm) has no local shell on any platform
today: `setup_local_shell` refuses it. So this phase fixes Linux and macOS
for that backend too, not only Windows.

| Step | Work | Files | Done when |
|---|---|---|---|
| 6.1 | A frontend local-shell runner for the built-in backend, with the shape the daemon runner already has: bytes out, bytes in, resize, exit. On Windows it uses `platform/windows/conpty.py` (with the bundled ConPTY) to spawn `pwsh.exe`, `powershell.exe`, `cmd.exe` or `wsl.exe`. On POSIX it uses `os.openpty` and the shell from `$SHELL`. Output is read on a thread and handed to the GTK main loop. libvterm only parses. The runner owns the process, as in emacs-libvterm and Neovim. | a new GI-free runner module, `terminal.py` (`setup_local_shell`), `terminal_backend_pyte.py` (`spawn_async`) | A local PowerShell tab, and a local bash tab with the built-in backend on Linux, work and close cleanly. No daemon RPC is added. |
| 6.2 | A shell picker in Preferences, with detection of installed shells. | `preferences.py` | Only installed shells are offered. |
| 6.3 | Replace the remaining `os.kill(pid, 0)` calls with psutil (windows-port.md §7). | `terminal.py`, `ssh_process_manager.py` | The regression test forbidding `os.kill` covers them. |

## Phase 7: remaining gaps

Independent, small items. Pick them up between phases.

- **Tab title:** filter the `C:\Windows\System32\…` console title before the
  remote shell sets its own. Check whether OpenConsole still sends it.
- **Links in the built-in view:** plain-text URL detection in the view, and
  OSC 8 if the libvterm build exposes it.
- **1Password on Windows:** point the preset at `\\.\pipe\openssh-ssh-agent`.
- **Agent keys through the UI:** test adding and removing keys.
- **Startup diagnostics:** stop reporting GTK 4 and libadwaita as NOT FOUND.
- **Quit:** stop logging "daemon transport is closed" as errors.
- **Plugins:** serial over COM ports, RDP through `mstsc`, a smoke test of
  Docker and Kubernetes.
- **Unverified on real hosts:** SFTP file manager, port forwards, key login
  after the ACL fix, Host Info, AltGr input. Run this list on the first
  frozen build.

## Phase 8: retire MSYS2

Once phase 4 ships:
- remove the MSYS2 setup section and the pywinpty 2.x notes;
- drop the pywinpty backend switch;
- re-evaluate libghostty-vt when it is tagged. It would be a
  `ghostty_terminal_model.py` behind the same model surface, and pyte is
  removed only after it.

## Order and parallel tracks

```text
Phase 0 ──┬── Phase 1 (daemon, ConPTY) ─────┬── Phase 6 (local shells)
          │                                 │
          ├── Phase 2 (model, libvterm) ────┼── Phase 3 (stack, CI) ── Phase 4 (freeze, sign) ── Phase 8
          │                                 │
          └── Phase 5 (broker, auth) ───────┘
Phase 7: any time
```

- Phases 1, 2 and 5 can run as parallel sessions: the daemon's ConPTY
  runner, the emulator models and the interaction broker. The exception is
  the view, `terminal_backend_pyte.py`: steps 1.4, 1.5, 2.3–2.5 and 6.1 all
  edit it, so those steps go one at a time.
- Phase 3 can start its research gate and CI workflow during phases 1–2,
  but it switches the developer stack only after phases 1 and 2 have landed.
- Phase 4 needs phase 3. The SignPath application should go in early,
  because it is the one step with an outside lead time.

## Review of outside research (2026-10-15)

`libvterm-local-shell-research.md`, written by another agent, traced
emacs-libvterm, libvtty and Neovim against sshpilot. Each claim was checked
against the source before it changed this plan.

Confirmed, and used:
- **The local shell is frontend-owned** (frontend-closure-audit.md). This
  overturned phase 6's daemon-session design.
- **libvterm only parses; the host owns the process.** emacs-libvterm starts
  the shell from Elisp with `make-process :connection-type 'pty`. Neovim's
  terminal is fed through byte callbacks. Phase 6.1 follows this.
- **Neovim's ConPTY host** creates named pipes, loads `conpty.dll` and falls
  back to `kernel32.dll`, and passes flags 0 (`os/pty_conpty_win.c`). This
  matches the ctypes runner.
- **Neovim repaints on a 10 ms timer, pauses reading while it refreshes, and
  supports synchronized output (mode 2026)** (`terminal.c`, `channel.c`).
  This became steps 2.4 and 2.5 and a phase 2 gate question.
- **libvtty is a different library** (Bryan Christ's, formerly also named
  libvterm, LGPL, POSIX-only). It is not a candidate.

Rejected, because they are outdated or wrong:
- *"Under ConPTY, keep `pull_history_on_grow=false` and expect no
  `?1049h`."* That holds only for the inbox ConPTY. The bundled ConPTY
  forwards `?1049h` and the mouse modes verbatim (windows-port.md §2a, on the
  spike branch). Step 1.5 re-measures it instead.
- *"Vendor libvterm like Neovim's `src/nvim/vterm/`."* Neovim's fork stores an
  `schar_T` index in each cell instead of `chars[6]`, so our ctypes model
  cannot read it. Phase 2 builds the `neovim/libvterm` mirror, which keeps
  the upstream layout.
- *"emacs-libvterm and libvtty have no Windows code."* Close, but
  emacs-libvterm's header has a `_WIN32` export macro. No effect on the plan.
- *"sshpilot's ConPTY runner is pywinpty."* True on `windows` today, but the
  ctypes runner replaces it in step 1.1.
- Its "missing evidence" list (WezTerm and Windows Terminal hosts, byte
  traces) is already covered by windows-decisions.md §2 and the spike.

## What only the maintainer can do

- Apply to SignPath Foundation (phase 4), or choose a paid certificate.
- Drive real mouse input in the live checks (phases 1.6 and 3.3).
- Decide if ARM64 is a target. pywinpty and NuGet cover it; gvsbuild's
  release does not.
- Approve each merge into `windows`, and into `main` when the port leaves
  "experimental".
