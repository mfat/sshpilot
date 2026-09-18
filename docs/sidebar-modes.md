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
modes below drive (`set_show_sidebar` / `set_sidebar` / `set_content` /
`get_sidebar_width` / `get_resting_sidebar_width`). It replaced
`AdwOverlaySplitView`, which computed the width itself and offered no handle.
What changed for callers:

- **The width is the user's.** Dragging the divider sets it; it is remembered,
  persisted as `ui.sidebar_width`, and restored at startup. Before the user has
  ever sized it the sidebar picks a quarter of the window, capped at
  `sidebar_paned.DEFAULT_MAX_WIDTH` (400) — that cap applies only to the width
  the sidebar chooses for itself, never to a dragged one. **There is no
  maximum-width setting any more**: the slider in Settings ▸ Sidebar existed
  because the Adw split views gave no other way to widen the sidebar, and it
  was removed with them (a stale `ui.max-sidebar-width` in an old config is
  simply ignored).
- **The minimum is measured, not configured.** `SidebarPaned._floor()` is the
  sidebar's own content minimum — `sidebar.measure(HORIZONTAL, -1)`, i.e. the
  widest of what the header, the bottom toolbar row and the connection rows
  ask for. Row labels contribute via `sidebar.FULL_LABEL_MIN_CHARS` —
  `width-chars` is a floor GTK never lays a label out below, so the measured
  floor is typically a few hundred pixels. The group row's trailing controls
  are the rest of it, and they are **width-responsive**: the Edit button is
  gone entirely (it is a context-menu item), and the split-view button keeps
  its reserved 34px only while the sidebar is at least
  `window._ROW_ACTIONS_MIN_WIDTH` (230) wide. Below that
  `MainWindow._apply_sidebar_row_actions` calls
  `GroupRow.set_actions_reserved(False)` on every row, the button goes, and the
  measured floor shrinks with it. Hover is still opacity-only, so revealing the
  button never reflows a row; only the width decides whether it is there at
  all. The threshold has to stay above the floor the reservation produces
  (~150) or the two would fight.
  A drag stops at the floor, and the automatic width is lifted to it (a
  content minimum above the 400 cap wins over the cap). There is **no
  `_SIDEBAR_MIN_WIDTH` constant** any more: the old 180 was
  `AdwOverlaySplitView`'s default `min-sidebar-width` carried over, and on top
  of the measured minimum it only held the divider back from widths the content
  was perfectly happy with. The one remaining number is
  `_ABSOLUTE_MIN_WIDTH` (44), which applies when the window is too narrow to
  give both the sidebar its content minimum and the content side the 320px of
  `_CONTENT_MIN_WIDTH`.
- **`get_resting_sidebar_width()`** answers how wide the sidebar rests at for
  the current window size (used by the search popup's panel width).
- **`get_sidebar_width()` is the live width**, not a configured bound.
- The handle is thin (no wide handle) so it draws the same hairline the split
  view did and the panes stay edge to edge; GTK keeps a wider input area than it
  paints, so it is still easy to grab.
- **Squeezing keeps the sidebar's leading edge.** `Gtk.Paned` shrinks a start
  child by allocating its minimum flush against the divider, so the *left* of
  the sidebar is what falls off the screen; a split view clips the other way.
  `sidebar_paned._ClipStart` wraps the sidebar and lays it out from x=0 with
  the overflow hidden.
- **The sidebar header carries no window controls** (`sidebar.py`,
  `_assemble_sidebar_shell`). They are in the content title bar.

## 1. Sidebar presentation mode — Full vs Compact

`ui.sidebar_mode`: `'full'` (default) or `'compact'`.

Set from Preferences ▸ Interface ▸ Sidebar ▸ **Sidebar Mode**.

### Full

The default presentation. Individual toggles under Sidebar control row chrome
(icons, status, user@hostname, port-forwarding badge, flat vs card rows,
hover action buttons, group count, group icon). Nesting and group-color
display (fill / badge / bar / dot) work as configured.

### Compact

A title-only flat list:

- **Title only** on connection rows (no connection icon, status lock, port
  forwarding badge, secondary host label, or Manage Files hover button).
- **Group headers** show the group title and the active group-color treatment
  only (no folder icon, connection count, split-view hover button, or expand
  chevron — activate the row to expand/collapse). Height matches connection
  rows.
- **Always flat** (`navigation-sidebar`); card chrome is never used.
- **Tighter** row margins/spacing than Full (content `4/0` px and
  inter-row gap `1px 4px` via `.sidebar-compact`).
- **Nesting / indentation** still apply (Group Layout preference).
- Group color modes that do not increase row height (fill, badge, bar, dot)
  remain available from Groups ▸ Group Appearance.

While Compact is selected, the other Sidebar chrome toggles are greyed out
(their stored values are kept for when the user switches back to Full). The
**Local Terminal Row** toggle stays interactive.

Icon-strip ("minimal") sidebar mode has been **removed**. Existing installs
are migrated in `ensure_config_defaults`:

- `ui.sidebar_mode: minimal` → `full`
- `ui.sidebar_on_terminal_open: minimize` → `none`
- `ui.sidebar_minimize_on_connect: true` → `false`

Those retired values are no longer part of the default config. Migration still
rewrites them when an old config file still carries them.

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

`SearchPopup` knows nothing about the sidebar. It is constructed with structural
pieces (the overlay, the `home` container, the `content` widget, a
`width_func`) and delegates all behaviour to callbacks the window supplies:
`on_shown` / `on_hidden` (`_on_search_popup_shown/hidden` — search-only list
visibility) and `on_dismiss` (`_dismiss_search_popup` — route through search
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
