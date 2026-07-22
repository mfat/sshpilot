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

A floating panel that hosts the **live** sidebar (`sidebar_box`) over the work
area without affecting layout — so the terminal never resizes.

### API

```python
window.show_sidebar_popup()      # detach sidebar_box into the floating panel
window.hide_sidebar_popup()      # re-attach it to the split view
window.sidebar_popup_visible()   # -> bool
```

### How it works

- On show, `sidebar_box` is **reparented** out of `_sidebar_toolbar_view` into an
  overlay panel (`_sidebar_popup`) sized to the configured sidebar width,
  left-aligned and full height. Because it is the *same* widget tree, the popup
  is pixel-identical to the expanded sidebar and every behaviour (selection,
  drag-and-drop, context menus, search, tags) works with zero duplication.
- The split view's sidebar column is left in place (its `ToolbarView` just loses
  its content) — the terminal is never resized. This is the whole reason the
  popup exists instead of collapsing the split view to an overlay.
- The panel always shows the full sidebar, even when the resting state is the
  minimal strip; `hide_sidebar_popup()` re-collapses the strip if minimal mode
  is active.

### Layers (`_build_sidebar_popup`)

The work UI is wrapped in a `Gtk.Overlay` (`_content_overlay`) with two hidden
overlay children:

- `_sidebar_popup_scrim` — a transparent, full-area box that captures a click
  *outside* the panel to dismiss (a `Gtk.GestureClick`). Transparent so the
  terminal stays fully visible.
- `_sidebar_popup` — the panel itself, styled by the `.sidebar-popup` CSS class
  (opaque background + right-edge shadow). An `Esc` key controller dismisses it.

### Dismissal

`hide_sidebar_popup()` is called on:

- **Esc** and **click outside** the panel — via `_dismiss_sidebar_popup()`,
  which routes through the search teardown (`_close_search_if_open()`) when
  search is active so the filter and entry are cleaned up too.
- **Search stopped** (Esc in the entry / the toolbar search toggle).
- **A search result opened** — `_close_search_if_open()` runs on the shared
  connection-open path (`_cycle_connection_tabs_or_open` /
  `_focus_most_recent_tab_or_open_new`).

### Reuse

`show_sidebar_popup()` / `hide_sidebar_popup()` are generic — search is just the
first caller. Any trigger can detach the sidebar into the floating panel; the
auto-dismiss on Esc / click-outside applies regardless, and the search-specific
teardown only runs when search happens to be open.
