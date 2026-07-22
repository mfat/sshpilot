# Sidebar presentation modes

The connection sidebar has a few orthogonal, reusable presentation modes. They
are all implemented on `MainWindow` (`src/sshpilot/window.py`) and compose with
each other. This document is the reference for what each mode is and how to
reuse it.

The sidebar's widget tree is a single `Gtk.Box` (`window._sidebar_box`) holding
the header actions, search bar, connection list and bottom toolbar. It normally
lives inside the split view's `Adw.ToolbarView` (`window._sidebar_toolbar_view`),
which is the split view's sidebar widget.

## 1. Full vs. Minimal (icon strip)

`set_sidebar_minimal(minimal: bool, animate: bool = True)`

- **Full** — the normal sidebar rows.
- **Minimal** — a ~64px icon strip: each connection collapses to a coloured
  avatar (initials) or icon, groups to a folder avatar. The width animates
  between the two states.

Driven by the `ui.sidebar_mode` setting (`full` / `minimal`, applied at startup)
and optionally by the "When a Terminal Opens" behaviour. Minimal mode is a
side-by-side column, so the terminal is `window − strip_width`.

## 2. Default vs. Overlay presentation

`set_sidebar_overlay(overlay: bool)`

- **Default** (`False`) — side-by-side: the sidebar takes its own column.
- **Overlay** (`True`) — `AdwOverlaySplitView.collapsed = True`: the sidebar is
  drawn as an overlay *above* the content.

This is a pure presentation switch and only affects the `OverlaySplitView`
backend. Note the libadwaita semantics: collapsing **resizes the content by the
sidebar width** (the column disappears) and auto-hides the sidebar. Because of
that resize, overlay mode is **not** used for the search flow — the detachable
popup below is used instead.

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
