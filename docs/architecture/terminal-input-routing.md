# Terminal input routing

How pointer and scroll events are shared between VTE and SSH Pilot's own
controllers, and why each controller is attached where it is. Read this before
adding, moving or removing a controller on the terminal widget: the ordering
rules below are not visible in the code that depends on them, and getting them
wrong has produced four separate user-facing bugs (#1178, #1212, #1251, #1269).

## GTK dispatch, and why attachment point is a behavioural choice

Two GTK 4 facts decide everything here:

* `gtk_widget_add_controller()` **prepends**. The most recently added
  controller is first in the list.
* `gtk_widget_run_controllers()` iterates that list forward and stops at the
  first **non-gesture** controller that returns `TRUE`. Gestures do not stop
  the walk; plain event controllers do.

VTE installs its own controllers in its constructor, so anything SSH Pilot adds
to the VTE widget afterwards lands *ahead* of them. Observed on VTE 0.84:

```
0  EventControllerScroll   sshpilot-scroll         bubble   <- added by us
1  GestureLongPress        vte-long-press-gesture  bubble
2  GestureClick            vte-click-gesture       bubble
3  EventControllerScroll   vte-scroll-controller   bubble
4  EventControllerMotion   vte-motion-controller   bubble
5  EventControllerFocus    vte-focus-controller    bubble
6  EventControllerKey      vte-key-controller      bubble
```

The rule that follows:

| To run… | Attach to | Phase |
| --- | --- | --- |
| **ahead of** VTE | the terminal widget (`controller_host()`) | `CAPTURE` |
| **behind** VTE | `terminal_container`, the widget's parent `Gtk.Box` | `BUBBLE` (default) |

Attaching to the widget in `BUBBLE` also runs ahead of VTE, which is the trap:
it looks like deferral and is not. `CAPTURE` on the widget is the honest way to
say "before VTE", because capture propagates root-to-target ahead of every
bubble handler and does not depend on insertion order.

Bubbling to the parent works because the VTE widget is a direct child of
`terminal_container`, with only the scrollbar as a sibling. That is deliberate:
VTE's own docs forbid putting a `VteTerminal` inside a `Gtk.ScrolledWindow`
(see the comment at `terminal.py:320`), so there is no intervening scroll
container to swallow the event first.

## What VTE still owns after `set_enable_fallback_scrolling(False)`

Disabling fallback scrolling does **not** hand SSH Pilot the wheel. It only
switches off VTE's unit-blind history scroll. `Terminal::widget_mouse_scroll()`
checks two other things *above* the fallback check and keeps them:

* **alternate screen** (`less`, `vim`, `htop`, `mc`) — emits `ESC[A` / `ESC[B`,
  or `ESC O A` / `ESC O B` in application cursor-key mode.
* **mouse tracking** (`tmux`, anything with DECSET 9/1000–1016) — emits button
  4/5 reports.

So a handler on the VTE widget that returns `TRUE` for every delta silently
destroys both. This is not recoverable at the adjustment either: in the
alternate screen `upper == page_size`, so there is nothing to scroll even as a
fallback. That was the #1269 regression — the wheel did nothing at all in
`less` until the history controller moved to the container.

`MouseTrackingState` (`terminal_input.py`) tracks the DECSET modes from the
display byte stream, but only on the daemon path, where `_paint_display()`
sees the bytes. Prefer letting VTE decide by attachment point over re-deriving
its screen and mouse state here.

## Current controllers

| Controller | Host | Phase | Why there |
| --- | --- | --- | --- |
| `_zoom_controller` | widget | `CAPTURE` | Ctrl/Cmd+wheel must zoom even while a full-screen app owns the wheel. Skipped entirely in pass-through mode: zoom is a shortcut. |
| `_scroll_controller` | `terminal_container` | `BUBBLE` | Unit-aware history scroll. Must see only what VTE declined. Not a shortcut, so it survives pass-through and backend swaps. |
| `_shortcut_controller` | widget | `BUBBLE` | Copy/paste and friends. Removed in pass-through mode. |
| `_latin_fallback_controller` | widget | `CAPTURE` | Matches accelerators through the layout's Latin group; must not reach VTE, which would turn an unmatched Ctrl+Shift+C into a plain `^C`. |
| context-menu `GestureClick` | widget | `CAPTURE` | Records coordinates before VTE handles the click; claims only when SSH Pilot handles it. |
| `SelectionFeedPauseController` | widget | `CAPTURE` | `EventControllerLegacy`, **not** a gesture — see below. |

## Gestures lose the pointer to VTE

VTE claims the pointer sequence for its own drag-select the moment the button
goes down. A `Gtk.GestureClick` on the same widget therefore sees `pressed`
followed by `cancel`; the `released` half never arrives. Pausing anything on a
signal whose partner never fires wedges permanently — that was the drag-select
freeze.

`Gtk.EventControllerLegacy` is not a gesture, so it never competes for the
sequence. It still sees the release, including one delivered outside the window
through the implicit pointer grab, and VTE's mouse reports reach the remote
unchanged. Use it whenever both halves of a press/release pair are needed.
`SelectionFeedPauseController` (`terminal_display_pause.py`) is the worked
example.

## Teardown must match attachment

`_remove_custom_shortcut_controllers()` detaches from `controller_host()`, so
it covers only the widget-hosted controllers. The history-scroll controller
lives on the container and has its own `_remove_scroll_controller()`; removing
it from the wrong widget fails silently and leaves the controller attached.

Two consequences worth keeping straight:

* Controllers on the **widget** are stranded by a backend swap (VTE ↔ PyXterm)
  and must be reinstalled; `ensure_backend()` tears down before repointing.
* Controllers on the **container** outlive both backend swaps and pass-through
  toggles, and only come off at real teardown.

Install the container-hosted ones from `setup_terminal()` rather than from
`_install_shortcuts()`, which early-returns while pass-through mode is on. A
terminal created with `terminal.pass_through_mode` already set otherwise never
gets them at all.

## Verifying a change

None of the above is reachable from a unit test: a test with widget doubles can
pin *where* a controller is attached and in which phase, but not what GTK and
VTE then do with it. Measure it.

The recipe, using VTE's `commit` signal as the oracle for "did VTE act on this
event":

```python
term.connect("commit", lambda t, text, n: print("VTE->pty", repr(text)))
```

Run a real `Vte.Terminal` under Xvfb (`GDK_BACKEND=x11`), spawn the state you
care about, and inject real input with `xdotool` — `click 4`/`click 5` for the
wheel, `keydown ctrl` around it for modifiers. Cover at least:

| State | Spawn | Expected |
| --- | --- | --- |
| alternate screen | `seq 1 500 \| less` | `VTE->pty '\x1bOA'` / `'\x1bOB'` |
| mouse tracking | `printf '\033[?1000h'; cat` | `VTE->pty '\x1b[Ma..'` |
| normal screen | `seq 1 500; cat` | no commit; the vadjustment moves |
| modifier | any of the above + Ctrl | zoom fires, VTE gets nothing |

`observe_controllers()` on the widget prints the dispatch list shown above and
answers ordering questions directly. Note that the alternate screen reports
`upper == page_size`, so "the adjustment did not move" there means the event
was consumed, not that the scroll was merely clamped.
