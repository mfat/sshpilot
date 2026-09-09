# Sidebar presentation modes

The connection sidebar has a few orthogonal, reusable presentation modes. They
are all implemented on `MainWindow` (`src/sshpilot/window.py`) and compose with
each other. This document is the reference for what each mode is and how to
reuse it.

The sidebar's widget tree is a single `Gtk.Box` (`window._sidebar_box`) holding
the header actions, search bar, connection list and bottom toolbar. It normally
lives inside the split view's `Adw.ToolbarView` (`window._sidebar_toolbar_view`),
which is the split view's sidebar widget.

## 0. The split view itself — a resizable paned

`window.split_view` is a `SidebarPaned` (`src/sshpilot/sidebar_paned.py`): a
`Gtk.Paned` whose divider the user can drag, wearing the split-view API the
modes below drive (`pin_width` / `release_width` / `set_show_sidebar` /
`set_sidebar` / `set_content`). It replaced `AdwOverlaySplitView`, which
computed the width itself and offered no handle. What changed for callers:

- **The width is the user's.** Dragging the divider sets it; it is remembered,
  persisted as `ui.sidebar_width`, restored at startup, and restored again when
  the icon strip animates back open. Before the user has ever sized it the
  sidebar picks a quarter of the window, capped at
  `sidebar_paned.DEFAULT_MAX_WIDTH` (400) — that cap applies only to the width
  the sidebar chooses for itself, never to a dragged one. **There is no
  maximum-width setting any more**: the slider in Settings ▸ Sidebar existed
  because the Adw split views gave no other way to widen the sidebar, and it
  was removed with them (a stale `ui.max-sidebar-width` in an old config is
  simply ignored).
- **The minimum is measured, not configured.** `SidebarPaned._floor()` is the
  sidebar's own content minimum — `sidebar.measure(HORIZONTAL, -1)`, i.e. the
  widest of what the header, the bottom toolbar row and the connection rows
  ask for. Measured today: **195px, all of it the group row** (a connection row
  asks 128, the header toolbar 98, and the bottom toolbar sits in a scroller
  that asks 0). Row labels add nothing to it — `sidebar.FULL_LABEL_MIN_CHARS`
  is 0, because `width-chars` is a floor GTK never lays a label out below and
  these labels ellipsize; at the old 10 characters they were ~80px each and the
  floor was 263. The group row's trailing controls are the rest of it, and they
  are **width-responsive**: the Edit button is gone entirely (it is a
  context-menu item), and the split-view button keeps its reserved 34px only
  while the sidebar is at least `window._ROW_ACTIONS_MIN_WIDTH` (180) wide.
  Below that `MainWindow._apply_sidebar_row_actions` calls
  `GroupRow.set_actions_reserved(False)` on every row, the button goes, and the
  measured floor goes 149 → 128 with it — which is what lets the divider carry
  on instead of stopping at a width the group name has already been ellipsised
  out of. Hover is still opacity-only, so revealing the button never reflows a
  row; only the width decides whether it is there at all. The threshold has to
  stay above the floor the reservation produces (~150) or the two would fight.
  A drag stops at the floor, and the automatic width is lifted to it (a
  content minimum above the 400 cap wins over the cap). There is **no
  `_SIDEBAR_MIN_WIDTH` constant** any more: the old 180 was
  `AdwOverlaySplitView`'s default `min-sidebar-width` carried over, and on top
  of the measured minimum it only held the divider back from widths the content
  was perfectly happy with. The one remaining number is
  `_ABSOLUTE_MIN_WIDTH` (44), which applies when the window is too narrow to
  give both the sidebar its content minimum and the content side the 320px of
  `_CONTENT_MIN_WIDTH`.
- **`pin_width(w)` freezes the width, `release_width()` hands it back.** That
  pair is what the icon strip and every tick of its animation use, and a pin
  (`_pinned_width`) overrides the measured floor — the only state in which the
  sidebar may shrink below its own content minimum, which is what makes the
  64px strip reachable. `get_resting_sidebar_width()`
  answers "how wide once released?" even while pinned, which is what the
  animation's endpoint and the search popup's panel width need.
- **The divider switches mode, too — one way.** Dragging it more than
  `_MODE_SWITCH_SLACK` (40px) below the measured floor used to ask for the icon
  strip; that gesture is behind `sidebar_paned.COLLAPSE_BY_DRAG` and is
  currently **off**, so a drag into the wall simply stops at the floor. The
  answer to "the sidebar is too wide" is a full sidebar that lays out narrower
  (see the floor above), not a mode the user did not ask for. The strip is
  still entered by `ui.sidebar_mode`, by "When a Terminal Opens" and by a
  restored session, and is still left by dragging it open or by its expand
  button. Dragging a pinned strip out to `_expand_threshold()` — the width the full
  sidebar actually needs — asks for the full sidebar back. Until that point
  the strip follows the pointer while staying minimal. The paned reports
  both through `on_mode_switch` and leaves the divider alone when the owner
  takes it (`window._on_sidebar_drag_mode_switch` → persist `ui.sidebar_mode`
  + `set_sidebar_minimal`, never animated). A dragged strip (or a drag back
  to full) is the resting mode and is restored at startup. Transient
  collapses from "When a Terminal Opens" do not use this path and stay
  non-persisted.
- **A mode-switching drag never moves the divider on its own**, which takes
  three rules that are easy to break. Below the full sidebar's floor the strip
  stays in minimal mode but **follows the pointer** up to `_expand_threshold()` —
  exactly the width the full sidebar needs — so the switch there is continuous
  (no jump away from the pointer). And the drag position becomes the remembered
  width *before* the switch, because `release_width()` otherwise restores the
  width the sidebar had before it was collapsed and the divider travels there
  after the user has stopped moving. Collapsing does neither: it leaves
  `user_width` alone, so the strip is never remembered as a width. And a
  collapse keeps the divider **under the pointer** rather than at the width the
  owner pins: `set_sidebar_minimal` pins `_MINIMAL_STRIP_WIDTH` for the resting
  strip, so the switch would otherwise snap the divider there and the next
  motion event would throw it straight back out to the pointer — measured as a
  jump to 117px and back to 208px mid-drag. `_on_position_notify` re-pins to
  the drag position (keeping the pin's floor) as soon as the owner returns.
- **`get_sidebar_width()` is the live width**, not a configured bound.
- The handle is thin (no wide handle) so it draws the same hairline the split
  view did and the panes stay edge to edge; GTK keeps a wider input area than it
  paints, so it is still easy to grab.
- **Squeezing keeps the sidebar's leading edge.** `Gtk.Paned` shrinks a start
  child by allocating its minimum flush against the divider, so the *left* of
  the sidebar — its icons — is what falls off the screen; a split view clips the
  other way. `sidebar_paned._ClipStart` wraps the sidebar and lays it out from
  x=0 with the overflow hidden, which is what keeps the frames of the strip
  animation (narrower than the sidebar's content minimum) readable.
- **The sidebar header carries no window controls** (`sidebar.py`,
  `_assemble_sidebar_shell`). They are in the content title bar, and a copy in
  the sidebar header floors that header at ~126px — twice the strip's width,
  which is what the whole sidebar then had to be. Without them the strip's
  content minimum is 63px and the 64px strip needs no clipping at all.

## 1. Full vs. Minimal (icon strip)

`set_sidebar_minimal(minimal: bool, animate: bool = True)`

- **Full** — the normal sidebar rows.
- **Minimal** — a ~112px label strip at rest: each connection collapses to a
  short ellipsized text label (10 characters at the default width; the budget
  grows as the strip is dragged wider), groups to a bold coloured text
  label (no folder icon) plus their expand/collapse chevron, which stays on
  screen there exactly as in full mode — the split-view and edit buttons are
  the group actions that go. Rows are always flat in the strip (no card chrome),
  regardless of the full-sidebar flat-rows preference. The full name stays on
  the row tooltip. The row's **Manage Files** hover action survives the strip —
  it is the only way to reach the file manager without leaving minimal mode —
  and it keeps the full row's *opacity* reveal there, so its space stays
  reserved and hovering never reflows or resizes a row (revealing it by
  visibility instead would be free at rest but would jump the layout under the
  pointer). To pay for that reservation it wears `.sidebar-compact-action` in
  the strip, which trims it to the bare 16px icon: measured in the 112px strip,
  a row is 80×36px with or without it, and the label goes from 68px to 50px
  (~2 characters) rather than the 34px the untrimmed button would leave. Every
  other row action (status icon, colour widgets, hostname line) stays hidden.
  A compact row's content box is also given `vexpand` (both `ConnectionRow` and
  `GroupRow`): a single-line strip row is shorter than the list row's theme
  minimum (19px of content in a 36px row) and a `Gtk.Box` leaves that slack
  *after* its last child, so without it the label and the hover action sit high
  in the highlighted row instead of centred.
  The width animates between the two states. During that animation the top
  header toolbar uses clip-reveal (buttons stay laid out; the pane clips them)
  instead of reshuffling the overflow menu every frame, the accent tips banner
  is snap-hidden until the width has settled and a short timeout has elapsed
  (so it cannot flash blue beside the top chrome while the content pane
  resizes), and header *margin* compacting is applied only once the width has
  settled.

  **Clip-reveal is frozen on the destination's split**, not on "show
  everything": `set_clip_reveal(True, target_width=…)` runs the overflow
  calculation once for the width the animation ends at (the sidebar's full
  width minus the header's `2 × _SIDEBAR_HEADER_MARGIN_FULL`), and the pane's
  clip reveals exactly those buttons. Revealing every packable item instead
  meant the last frame re-split the row — measured as two buttons appearing
  mid-animation and being replaced by the hide-hostnames control and the "…"
  button once the width settled. For the same reason an expand applies the
  full-mode *button set* up front (`_apply_sidebar_header_items(False)`) even
  though the margins stay deferred: the strip drops the hide-hostnames control,
  and freezing a row without it leaves room for buttons that do not belong in
  the destination.

Driven by the `ui.sidebar_mode` setting (`full` / `minimal`), written when the
user switches mode with the divider (or the strip's expand button) and applied
at startup. Optionally also entered transiently by the "When a Terminal Opens"
behaviour. There is no Preferences toggle for the resting mode — the divider
is the control. Minimal mode is a side-by-side column, so the terminal is
`window − strip_width`.

The strip's header bar shows the **app icon** in place of the title: the "SSH
Pilot" label is hidden there (its natural width alone would floor the strip) and
the title moves to the content header, which would otherwise leave the strip
topped by a blank bar. The icon is built hidden in `_assemble_sidebar_shell` and
swapped with the label by `_apply_sidebar_minimal_chrome`; at 24px it leaves the
strip's header minimum at 36px, well inside the 112px strip. The top action
toolbar is the same `OverflowToolbar` as full mode (same New Connection
button); it keeps as many buttons as fit and moves the rest into a trailing
"…" menu. The hide-hostnames control is omitted in the strip (hostnames are
not shown there). The bottom selection toolbar is hidden and replaced by the
expand control.

**By mouse, the divider is the way out** (section 0): drag the strip open to
restore the full sidebar. Dragging *in* — past the sidebar's minimum — is
`COLLAPSE_BY_DRAG` and is off; the strip's own expand button is the other way
out. There is no
minimize button in the bottom toolbar any more — it sat at the start of that
button row, and the row's minimum width is what held the whole sidebar wide, so
the control that collapsed the sidebar was itself part of why it could not get
narrow.

## 2. Default vs. Overlay presentation

`set_sidebar_overlay(overlay: bool)`

- **Default** (`False`) — side-by-side: the sidebar takes its own column.
- **Overlay** (`True`) — `AdwOverlaySplitView.collapsed = True`: the sidebar is
  drawn as an overlay *above* the content.

**Overlay needs a backend that has no `Gtk.Paned` equivalent, so it is inert
since the split view became a paned**: the call records the request and the
sidebar stays a side-by-side column. It had no callers — the over-the-content
presentation in use is the detachable popup below, which was already preferred
because collapsing an `AdwOverlaySplitView` **resizes the content by the sidebar
width** (the column disappears) and auto-hides the sidebar. Restoring a true
overlay means bringing back an overlay-capable backend behind `split_view`.

## 3. Detachable sidebar popup (search, and reusable)

`src/sshpilot/search_popup.py` — the `SearchPopup` class. A floating panel that
hosts the **live** sidebar (`sidebar_box`) over the work area without affecting
layout — so the terminal never resizes. The window owns one instance,
`window._search_popup`, built in `setup_ui`.

### API

Lifecycle:

```python
popup = window._search_popup     # a search_popup.SearchPopup
popup.show()                     # detach sidebar_box into the floating panel
popup.hide()                     # re-attach it to the split view
popup.visible                    # -> bool (property)
popup.dismiss()                  # Esc / click-outside routing
```

Presentation (composable; placement only — content stays the owner's job):

```python
from sshpilot.search_popup import Position, Backdrop
popup.set_position(Position.LEFT | RIGHT | CENTER | TOP)
popup.set_size(width=None, height=None)   # None -> derive (width_func / fill)
popup.set_backdrop(Backdrop.NONE | DIM)   # scrim behind the panel
popup.set_transparent(enabled)            # subtle panel transparency (code-only)
popup.set_show_groups(enabled)            # group headers vs flat results
popup.apply_preset('sidebar' | 'center' | 'spotlight')
popup.mode          # active preset name
popup.search_only   # bool: the mode wants the list hidden (spotlight)
popup.show_groups   # bool: group headers in the list/results
```

Presets bundle placement:

| preset | position | size | backdrop | search-only | groups |
|--------|----------|------|----------|-------------|--------|
| `sidebar` (default) | left, full height | width = sidebar width | none | no | yes |
| `center` | centered | 520×560 | dim | no | yes |
| `spotlight` | top-centered | 560×auto | dim | yes (list hidden) | no (flat) |

The owner honours `search_only` in `_on_search_popup_shown` (hides
`connection_scrolled`), `show_groups` in `rebuild_connection_list` (a flat
connection list when off), and supplies a `focus_func` so the search entry is
focused on show. **Real backdrop blur is intentionally omitted** — GTK4 has no
`backdrop-filter`; `DIM` is the practical stand-in.

### How it works

- On show, `sidebar_box` is **reparented** out of `_sidebar_toolbar_view` into the
  overlay panel (`SearchPopup._panel`) sized to the effective sidebar width,
  left-aligned and full height. Because it is the *same* widget tree, the popup
  is pixel-identical to the expanded sidebar and every behaviour (selection,
  drag-and-drop, context menus, search, tags) works with zero duplication.
- The split view's sidebar column is left in place (its `ToolbarView` just loses
  its content) — the terminal is never resized. This is the whole reason the
  popup exists instead of collapsing the split view to an overlay.
- The panel always shows the full sidebar, even when the resting state is the
  minimal strip; `popup.hide()` re-collapses the strip if minimal mode
  is active.

### Layers (`SearchPopup._build`)

The work UI is wrapped in a `Gtk.Overlay` (`_content_overlay`) with two hidden
overlay children:

- `SearchPopup._scrim` — a transparent, full-area box that captures a click
  *outside* the panel to dismiss (a `Gtk.GestureClick`). Transparent so the
  terminal stays fully visible.
- `SearchPopup._panel` — the panel itself, styled by the `.sidebar-popup` CSS
  class (opaque background + right-edge shadow). An `Esc` key controller
  dismisses it.

### Dismissal

`popup.hide()` is called on:

- **Esc** and **click outside** the panel — via `popup.dismiss()`,
  which routes through the search teardown (`_close_search_if_open()`) when
  search is active so the filter and entry are cleaned up too.
- **Search stopped** (Esc in the entry / the toolbar search toggle).
- **A search result opened** — `_close_search_if_open()` runs on the shared
  connection-open path (`_cycle_connection_tabs_or_open` /
  `_focus_most_recent_tab_or_open_new`).

### Subtle transparency (programmatic only)

`popup.set_transparent(enabled)` toggles a subtle background
transparency on the panel (the `.sidebar-popup-transparent` CSS class →
`alpha(@window_bg_color, 0.86)`), so the terminal shows faintly through while the
rows stay readable. It is **intentionally not exposed in Preferences** — it is a
code-level toggle only. Default is off (opaque); the setting persists across
show/hide.

### Decoupling & drift

`SearchPopup` knows nothing about the sidebar or minimal mode. It is constructed
with structural pieces (the overlay, the `home` container, the `content` widget,
a `width_func`) and delegates all behaviour to callbacks the window supplies:
`on_shown` / `on_hidden` (`_on_search_popup_shown/hidden` — expand or re-collapse
the rows) and `on_dismiss` (`_dismiss_search_popup` — route through search
teardown). The callbacks are deliberately **not** wrapped in try/except so a
drifted contract fails loudly.

Because the popup moves the *live* `sidebar_box` (never a copy), the sidebar and
its search cannot drift out of sync — there is only one of each. The one place
drift could bite is the owner→popup contract; `tests/test_sidebar_popup_gui.py`
guards it by exercising `show()`/`hide()` on a real window (the mocked unit tests
in `tests/test_sidebar_popup.py` can't, since they mock the callbacks).

### Reuse

`popup.show()` / `popup.hide()` are generic — search is just the first caller.
Any trigger can detach the content into the floating panel; the auto-dismiss on
Esc / click-outside applies regardless, and the search-specific teardown only
runs when search happens to be open.
