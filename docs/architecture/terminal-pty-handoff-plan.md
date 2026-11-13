# Plan: hand the SSH terminal's PTY to VTE

Status: **not pursued (option C), November 2026.** Nothing is implemented.
The handover would lose OSC 52 clipboard writes (on by default), output
highlighting and the selection feed pause, and the resize gain turned out not
to address a live bug: GH #1270 was a Flatpak local-tab relay bug, fixed on
2026-09-22 and confirmed by the reporter on 6.3.9. SSH tabs keep the
daemon streaming path. This page records the survey and the design in case
the trade-off changes.

## Goal

On Linux and macOS, let the VTE widget own the PTY of an SSH tab, as it does
for local shells, instead of receiving terminal bytes streamed from the daemon.
The daemon keeps everything else: launching OpenSSH through `SshLauncher`,
askpass and keyring autofill, host-key trust, readiness, transfers, forwards,
secrets, config, and the shared state the CLI and MCP servers use.

Windows, the PyXterm backend and the pyte backend keep today's streaming path
(see [Out of scope](#out-of-scope)).

Expected gains:

* VTE gets a real PTY. Legacy mouse reports work natively, so the GH #1212
  workaround (`MouseTrackingState` SGR translation) is no longer needed for
  these tabs.
* Resize is a local `TIOCSWINSZ` by VTE; no RPC round trip and no
  `_resize_in_flight` coalescing. (Streaming resize works: GH #1270 was a
  different bug, and the stuck-flag path cannot occur in practice because
  resize requests are never cancelled individually.)
* Keystrokes and output stop crossing the daemon socket.

## What reads the terminal stream today

Everything below would stop seeing terminal bytes once VTE reads the PTY
directly. This is the cost side of the change.

### In the daemon

| Consumer | Where | Needs bytes when | Impact |
|---|---|---|---|
| Connection-evidence gate (promotes STARTING → RUNNING, classifies auth failures) | `daemon/session_runtime.py` `_terminal_output`, `_connection_evidence_locked` | Before authentication | **None** if the handover happens at RUNNING (below) |
| PTY-evidence fallback when OpenSSH `-E` diagnostics are unavailable (always on macOS: no inotify) | `daemon/ssh_readiness.py`, `session_runtime.py` | Before authentication | None, same reason |
| Replay buffer (reattach, recovery, GTK restart) | `daemon/terminal_stream.py` `TerminalReplayBuffer` | After authentication | Ends at handover. Reattach has nothing to replay |
| Multi-attachment and input ownership | `session_runtime.py` `attach_session`, `claim_input` | After authentication | Handover is exclusive to one client |
| Broadcast input (`terminal.broadcast_input`) | `session_runtime.py` `broadcast_terminal_input`, called from `terminal_manager.py` | After authentication | **Writes only.** Keeps working if the daemon keeps a write-only duplicate of the master |

The MCP runtime server does not read terminal output (it only lists, opens and
closes sessions).

### In the GTK process

These run in `TerminalWidget._on_daemon_output` / `_paint_display`
(`terminal.py`) on the bytes **before** VTE sees them. VTE has no hook that
replaces them.

| Feature | Where | Default | Impact |
|---|---|---|---|
| **OSC 52 clipboard writes** (GH #1297) | `scan_remote_clipboard`, `terminal_osc52.py` | **On** (policy `ask`) | **Lost.** The code notes that neither emulator acts on OSC 52 itself |
| **Output highlighting** (IPs, MACs, VLANs for network devices) | `terminal_highlight.py`, `backend.feed(self._highlighter.feed(data))` | Off (`terminal.highlight_enabled`) | **Lost.** It rewrites the byte stream; a PTY read by VTE cannot be rewritten |
| Pause output while drag-selecting (selection survives redraws) | `terminal_display_pause.py` | On | **Lost.** VTE reads the PTY whether or not a drag is in progress |
| Pending startup commands, flushed on the first output after RUNNING | `_flush_pending_shell_ready_feeds` | On when configured | Adaptable: watch VTE `contents-changed` instead |
| Plugin `read_terminal` | `plugins/host.py` (daemon replay) | Plugin API | Must read VTE's text instead after handover |
| Plugin `send_terminal` | `plugins/host.py` | Plugin API | Keeps working through the daemon's write-only master, or via `feed_child` |

### User-visible settings tied to detach

| Setting | Default | Impact |
|---|---|---|
| Tab close policy (`terminal.daemon_tab_close_policy`) | `terminate` | `detach` loses meaning: nothing can be replayed |
| App close policy (`terminal.daemon_app_close_policy`) | `ask` | The quit prompt offers to keep sessions running. Drop the option for handed-over tabs |
| Restore sessions on startup (`terminal.daemon_restore_sessions`) | Off in Preferences, `True` fallback in `window.py:1168` | Remove or limit to streaming tabs |

Side finding: the restore setting defaults to `False` in Preferences but
`True` in `window.py`. That mismatch exists today and should be fixed either
way.

## Decisions needed before coding

1. **OSC 52 and highlighting.** Options:
   * **A.** Accept the loss for VTE SSH tabs. OSC 52 is on by default, so this
     is a visible regression (remote tmux/vim copy to local clipboard stops
     working).
   * **B.** Use the handover only when neither feature is active for the tab
     (OSC 52 policy `never` and highlighting off), and stream otherwise. Since
     OSC 52 defaults to `ask`, most tabs would keep streaming; the change
     would mostly help users who opt out.
   * **C.** Don't do the handover. Keep streaming and fix its remaining pain
     directly (resize round trip, GH #1270), keeping all byte-level features.
2. **Detach and restore.** You said sessions don't need to outlive the
   window. Recommended: for handed-over tabs, closing the tab or the app
   terminates the session; the detach and restore options apply only to
   streaming tabs (or are removed if streaming tabs are rare after this).
3. **Selection pause.** Accept the loss, or keep streaming on backends where
   it matters. VTE with a real PTY behaves like GNOME Terminal here.

Option C is a legitimate outcome. The survey shows the streaming path is not
only plumbing; it is also where OSC 52 and highlighting live.

## Design (if A or B is chosen)

### Hand over after authentication, not at launch

The daemon keeps reading the PTY while the session is STARTING, so the
evidence gate, the `-E` diagnostics readiness, the macOS PTY fallback and auth
failure classification stay exactly as they are. Login banners and errors keep
streaming to the tab during that phase.

When the session reaches RUNNING and the client asked for a handover:

1. The daemon stops reading: `PtyIoManager` unregisters the master **without
   closing it**.
2. It flushes every byte it already read (including `deferred_live_output`) as
   ordinary terminal frames, and records the final sequence number.
3. It passes the master to the client with `SCM_RIGHTS`, together with that
   final sequence.
4. The client feeds every streamed byte up to the final sequence into VTE,
   then adopts the fd (`VTETerminalBackend.adopt_pty`). From then on VTE reads,
   writes and resizes it.

Ordering is the risky part: no byte may be lost or shown twice across the
switch. The final sequence number makes that checkable in tests.

**Fd transport.** The main `DaemonClient` socket has one reader thread and a
framing codec that uses `recv`. Rather than teach it ancillary data, the client
opens a short-lived second connection, authenticates as usual, sends
`sessions.take_pty(session_id, attachment_id)`, and receives one frame plus the
fd via `recvmsg`. `terminal.py` already receives an fd this way from the
Flatpak agent (`SCM_RIGHTS`, around line 4174) and can share that helper.

### What the daemon keeps

* A duplicate of the master, used **only for writing** (broadcast input,
  plugin `send_terminal`). It must never read it after the handover, or it
  would steal bytes from VTE.
* The process: `SessionProcessHandle`, process group, reaper and exit events.
  `close_session` and terminate work as now.
* Session state and events. VTE cannot watch the ssh child (it is not the GTK
  process's child), so exit comes from the daemon's `session` events, with
  VTE's EOF on the master as a secondary signal.

On client disconnect (GTK crash), the daemon terminates handed-over sessions;
with no reader, they cannot be resumed.

### API changes

* New capability `terminal.pty_handoff`, advertised only on POSIX and only when
  the PTY runner exists.
* `OpenSessionRequest` gains an opt-in (e.g. `pty_handoff: bool`). The client
  sets it only when its backend has `supports_pty_adoption` (VTE today).
* New method `sessions.take_pty` and a handover frame carrying the final
  sequence.
* Session snapshots report the terminal mode (`streaming` / `handed_over`) so
  the sessions dialog and reattach logic can tell.
* Per `AGENTS.md`: models, client, codec, dispatch, capabilities, errors,
  contract tests, version baseline, `scripts/generate_api_artifacts.py`, API
  changelog. Any new failure gets a stable error code and wording in a
  `gtk/*_messages.py` presenter, translated in every `po/LINGUAS` catalogue.

### GTK changes

* `TerminalSessionController`: a `HANDED_OVER` state. Input, resize and
  replay calls stop; `send_input` and `resize` become no-ops with a debug log.
* `TerminalWidget`: after adoption, skip `_paint_display`, the mouse-mode
  translation (already gated on `vte.get_pty() is None`, so it turns off by
  itself) and the selection feed pause.
* `adopt_pty` currently takes a `watch_pid`; add a variant without one.
* Pending startup commands: flush on VTE `contents-changed` after the
  handover.
* Plugin `read_terminal`: read VTE's text for handed-over sessions.
* Close and quit dialogs: hide the detach choice for handed-over tabs.

### Unchanged

* `SshLauncher` and every launch policy. The handover happens after launch.
* Askpass, OTP dialogs, the interaction broker, host-key trust.
* Local shell tabs (already VTE-owned) and the Flatpak agent.

## Out of scope

* **Windows.** ConPTY's pseudo-console handle has no documented way to move
  between processes, so resize would still go through the daemon, and the
  emulator (libvterm or pyte) is fed bytes either way. Streaming stays.
* **PyXterm and pyte backends.** They cannot adopt a PTY
  (`supports_pty_adoption = False`). Streaming stays.
* Removing the streaming path. It remains for the cases above and for any
  future UI that cannot take a PTY.

## Steps

1. **Settle the decisions above.**
2. **Headless spike.** In the headless daemon rig, open a session against a
   throwaway sshd, take the PTY over the second connection after RUNNING, and
   verify byte continuity across the switch under load (`yes`, `cat` of a
   large file) with a 2-core `taskset` run. Verify the daemon never reads
   after the handover.
3. **Real VTE check** under Xvfb: htop mouse on legacy mode, resize, vim,
   OSC 52 absent (or present on streaming tabs under option B).
4. **API** (capability, request field, method, frame, artifacts, tests).
5. **Daemon** (`session_runtime.py`, `pty_runner.py`): stop-read, flush,
   duplicate, pass, disconnect policy.
6. **GTK** (controller state, widget, plugin read, dialogs), plus the
   restore-setting default mismatch.
7. **Flatpak check.** The daemon is expected to run inside the sandbox with
   the GUI (same devpts), so the fd should pass. Verify on a Flatpak build.
8. **macOS check** on the Homebrew build: VTE adoption and the PTY-evidence
   readiness fallback.
9. Update `terminal-streaming.md`, `session-runtime.md` and
   `terminal-input-routing.md`.

## Risks

* Lost or duplicated bytes at the switch. Mitigated by the final sequence and
  the spike in step 2.
* The daemon accidentally reading the duplicate. One read steals output from
  VTE. Needs an explicit guard and test.
* Two terminal paths to maintain for good.
* Interactive prompts after RUNNING (e.g. `sudo` in the shell) are ordinary
  terminal I/O and need nothing special, but anything that today relies on
  the daemon seeing post-login output needs re-checking during step 5.
