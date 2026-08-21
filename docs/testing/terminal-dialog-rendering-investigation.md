# Terminal dialog rendering investigation (2026-08-21)

## Outcome

The reported visual symptom is reproducible without tmux when GTK terminal
output consumption is stalled across `GtkTerminalBinding`'s pending-byte
limit. It does **not** reproduce in the no-pressure path.

The first incorrect boundary is
`GtkTerminalBinding._receive_output()` in `src/sshpilot/gtk_client_bridge.py`.
The PTY read, daemon `TerminalOutput`, controller, widget, and VTE feed paths
are byte-identical while the pending limit is not exceeded. On overflow the
binding clears every queued output frame, drops the frame which crossed the
limit, reports one continuity event, and later resumes at an arbitrary raw
byte boundary. That is unsafe for a stateful terminal stream.

The 1 MiB overflow is therefore a proven causal reproducer of the symptom. It
cannot be proven to have occurred in the original user's session because no
sequence/continuity log from that session exists.

## Environment

- Branch/commit: `dev`, `20c13f88 fix(terminal): release unhandled VTE mouse gestures`
- Host: Fedora Linux 44, Python 3.14.6
- GTK 4.22.4, libadwaita 1.9.3, VTE 0.84.1
- Real mapped GTK/VTE surface on Weston 15.0.1 headless Wayland
- Disposable Alpine 3.20 OpenSSH target from
  `tests/fixtures/temporary_openssh.py`, bound to localhost only
- `dialog` 1.3-20240307 and `whiptail` installed only inside the disposable
  target
- `$TERM=xterm-256color`, `LC_ALL=C`, 24 rows by 80 columns for baseline
- No tmux

The repository's documented Phase 14 full-window GUI harness could not be used
unchanged: `tests/gui/_phase14_harness.py` currently passes the removed
`group_manager=` argument to `ConnectionApplicationService`, and after removing
that argument its GTK `ConnectionPresentationStore` still does not implement
the core repository's `add_listener` contract. The focused investigation used
the production PTY owner, real OpenSSH, the production VTE backend, and the
production GTK binding directly. No harness compatibility change was retained.

## Reproduction

1. Start the disposable target with `scripts/phase13-openssh-fixture.sh`.
2. In the target, install `dialog` and `newt` (which provides `whiptail`).
3. Connect with OpenSSH using the fixture key, `IdentitiesOnly=yes`, a forced
   TTY, `TERM=xterm-256color`, locale `C`, and an 80x24 PTY.
4. Run each baseline command:

   ```sh
   dialog --yesno "Can you see and select both buttons?" 10 50
   whiptail --yesno "Can you see and select both buttons?" 10 50
   dialog --menu "Select an item" 15 60 4 \
     one "First item" two "Second item" three "Third item"
   ```

5. Feed every raw PTY chunk, unchanged, to `VTETerminalBackend.feed()` on a
   mapped real VTE widget. The dialogs render correctly.
6. For live pressure, stop draining the GTK dispatcher, run 100,000 49-byte
   output lines followed immediately by `dialog --yesno`, and resume GTK only
   after sending Tab and Enter.
7. For the smallest reliable visual reproducer, queue a valid flood prefix so
   the pending count is exactly 1,048,576 after byte 1,553 of the captured
   dialog stream. Queue the next 181-byte PTY chunk. Current dev clears the
   prefix and that crossing chunk. It resumes at the dialog cleanup tail, so
   VTE shows neither prompt nor buttons although the remote process accepted
   input.
8. For the smallest parser-state proof, use a 64-byte test limit and these
   output chunks while GTK is stalled:

   ```python
   (b"x" * 63, b"\x1b[", b"2J\x1b[8;16HPOST-FLOOD DIALOG")
   ```

   The first bytes VTE receives after continuity loss are `b"2J\x1b[8;16H..."`.
   The missing `ESC [` changes `CSI 2 J` into printable text.

## Results by hypothesis

### A. Baseline rendering

All three screens rendered with borders, labels, rows/buttons, selected state,
and cursor placement visible. Exact PTY captures were:

| Case | Bytes | SHA-256 | Interaction result |
| --- | ---: | --- | --- |
| `dialog --yesno` | 1,833 | `6df8431a981979d5d82a4fb54b5a95d0645a37ee92de2634d68b1c9ab49e0697` | Tab `09`, Enter `0d`, result 1 |
| `whiptail --yesno` | 1,547 | `e2aa5ae5de7045caf03c8463bda85f799fcaf9a3954e936abc8ed316d9e7e7da` | Tab `09`, Enter `0d`, result 1 |
| `dialog --menu` | 3,266 | `a6137e56fceb9918da36040005bd5d95d78be75ac4270944d1a67e9286837dd2` | Down `1b 4f 42`, Enter `0d`, result `0:two` |

No-pressure conclusion: transport, ordering, input, VTE parsing, and normal
dialog rendering are sound. `dialog` enables DECCKM, so the observed VTE arrow
sequence is SS3 (`ESC O B`), not CSI (`ESC [ B`).

### B. Palette

The same raw 1,833-byte dialog stream was rendered using `default`, `light`,
`dark`, `solarized_dark`, and `dracula`. The selected No button was visible in
every screenshot. Default/light use indexed blue `#3465A4`; dark/Dracula use
`#BD93F9`; Solarized Dark uses `#268BD2`. The configured foreground,
background, 16 indexed colors, cursor, and highlight colors were recorded in
`/tmp/sshpilot-terminal-evidence/report.json`.

Conclusion: the tested themes do not make the selected line
foreground/background indistinguishable. VTE's selection-highlight colors are
also not what draws ncurses' selected button; `dialog` emits indexed SGR colors.

### C. GTK backlog

The live flood produced 4,901,820 raw bytes. Four overflows occurred:

| Crossing frame `[sequence,next)` | Queued range cleared | Queued bytes |
| --- | --- | ---: |
| `[1048502,1048600)` | `[0,1048502)` | 1,048,502 |
| `[2097053,2097249)` | `[1048600,2097053)` | 1,048,453 |
| `[3145751,3145849)` | `[2097249,3145751)` | 1,048,502 |
| `[4194351,4194449)` | `[3145849,4194351)` | 1,048,502 |

The crossing frame at each row is also discarded. Only bytes
`[4194449,4901820)` (707,371 bytes) reached VTE. The single continuity callback
visible at drain time was `(4194351, 4194449)` because each later overflow
overwrote the earlier pending continuity report.

This particular 100,000-line timing left the final dialog redraw in the
surviving tail, so it was complete and usable. Moving the same legitimate PTY
chunk boundary into the dialog reliably produced partial borders, buttons
without a dialog, a lone selected No button, or a blank apparent freeze.

### D. Ordering and integrity

The new no-pressure regression records the same chunks at the six requested
logical boundaries and compares concatenated raw bytes and SHA-256 hashes.
It uses the production implementations of
`GtkTerminalBinding._receive_output()`,
`DaemonTerminalSessionController._handle_output()`,
`TerminalWidget._on_daemon_output()`, and
`VTETerminalBackend.feed()`.

There is no decode/re-encode, duplicate, reorder, or missing byte before the
GTK backlog overflow. The first divergence is the first overflow branch at
`gtk_client_bridge.py:309`: daemon frame `[1048502,1048600)` exists, but it and
all queued predecessors are absent from GTK delivery. Later layers faithfully
render the already-damaged stream.

### E. Input

Output loss does not block input. During the 4.9 MiB live flood, Tab (`09`) and
Enter (`0d`) were accepted by `PtyIoManager.write()` and the remote dialog
returned result 1. The frontend regression also verifies raw commit forwarding
for application Up (`1b 4f 41`), Tab (`09`), Shift+Tab (`1b 5b 5a`), Space
(`20`), Enter (`0d`), Escape (`1b`), and Ctrl+C (`03`).

Conclusion: the apparent freeze is output/state failure only.

### F. Resize

A real SSH dialog was resized while active through 40x100, 12x60, 24x80, and
30x90. Remote `stty size` started at `24 80` and ended at `30 90`; dialog
redraw bytes were received after every resize, Escape was accepted, and the
application returned 255. No stale PTY size occurred in this focused path.

Conclusion: sizing is not the reproducer. A stale size could independently
hide a bottom row, but it is not needed for this failure.

## First incorrect boundary and involved code

1. `PtyIoManager._read_pty()` reads and forwards exact bytes.
2. `SessionRuntime._terminal_output()` assigns absolute byte sequences without
   changing `data`.
3. **First divergence:** `GtkTerminalBinding._receive_output()` clears pending
   frames and discards the crossing frame when the 1 MiB bound is exceeded.
4. `_drain()` sends a local marker, then resumes later raw bytes without a
   terminal reset, replay, or parser-state restoration.
5. `DaemonTerminalSessionController._handle_output()` advances
   `expected_sequence` to the resumed frame and forwards its bytes.
6. `TerminalWidget._on_daemon_output()` and
   `VTETerminalBackend.feed()` faithfully hand the discontinuous stream to VTE.

Thus this is not a VTE rendering defect: VTE renders the byte stream it is
given. It is not an ordering defect, input defect, tmux interaction, or PTY-size
defect in the reproduced case. It is arbitrary frontend transport loss followed
by unsafe continuation of a stateful terminal stream.

## Regression coverage

- `tests/test_terminal_dialog_continuity.py`
  - passing no-pressure six-boundary raw-byte/hash integrity test;
  - strict expected failure for large output followed by a dialog;
  - strict expected failure for continuity loss split inside CSI;
  - passing exact input-byte forwarding after output loss.
- `tests/gui/test_terminal_dialog_continuity.py`
  - real GTK4/VTE strict expected failure: the post-flood title and buttons are
    absent on current dev.

Normal focused run:

```sh
PYTHONPATH=src:. pytest -q -n0 tests/test_terminal_dialog_continuity.py
# 2 passed, 2 xfailed
```

Proof that the regressions fail on current dev:

```sh
PYTHONPATH=src:. pytest -q -n0 --runxfail tests/test_terminal_dialog_continuity.py
# 2 failed, 2 passed
```

Real-VTE proof (on a display):

```sh
SSHPILOT_GUI_TESTS=1 PYTHONPATH=src:. \
  pytest -q -n0 -m gui --runxfail \
  tests/gui/test_terminal_dialog_continuity.py
# 1 failed: POST-FLOOD DIALOG absent from VTE text
```

## Evidence artifacts

The one-off raw captures, detailed sequence/chunk hashes, effective palette
report, and screenshots are under `/tmp/sshpilot-terminal-evidence/` in the
investigation workspace. Key files are:

- `report.json`
- `dialog_yesno-default.png`
- `theme-light.png`, `theme-dark.png`, `theme-solarized_dark.png`
- `backlog-current.png` (overflow occurred, final redraw survived)
- `loss-cross-chunk-15.png` (malformed dialog)
- `loss-cross-chunk-18.png` (buttons without dialog)
- `loss-cross-chunk-20.png` (lone selected button)
- `loss-cross-chunk-21.png` (blank/apparent freeze)
- `resize.raw`

GNOME Terminal was launched against the same target and command successfully,
but Weston headless does not implement the screencopy protocol, so an external
GNOME Terminal screenshot could not be saved. The comparison raw stream was
also fed to a separate real VTE reference widget and rendered correctly.
