# Accessibility and UI automation

How SSH Pilot exposes itself over AT-SPI, what an automation client can drive
through it, and where the toolkit stops. Everything here was measured against
this build — GTK 4.22.4, libadwaita 1.9.3, at-spi2 2.58.2, dogtail 0.9.11,
PyGObject 3.56, on a GNOME/Wayland session.

The executable form of this document is `tests/e2e/` (see its README for how to
run it). The cheap, display-free part is `tests/test_accessibility_metadata.py`,
which parses **every** module under `src/sshpilot/` — so a name added to a
dialog nobody has touched yet is held to the same rules as one in the sidebar.

What that guard does *not* claim is full coverage of icon-only controls. It
checks the names we set; it cannot tell an icon-only button that still has only
a tooltip from a labelled one that legitimately has both. Roughly 180
`set_tooltip_text` calls remain across the package, most of them on controls
that already carry a visible label. Converting the genuinely icon-only
remainder to `label_icon_button` is follow-up work, done screen by screen with
the Dogtail suite to confirm each one; the helper and the guard are in place
for it.

Everything was measured on GNOME/Wayland. X11 and headless runs are untested.

## The rules we follow

* **GTK first.** A `Gtk.Button` with a label, an `Adw.EntryRow` with a title, an
  `Adw.PreferencesGroup` — GTK already gives all of these a correct role and
  name. We add nothing there.
* **Fix only what GTK cannot infer.** Four shapes need help, and they are the
  only ones `sshpilot/accessibility.py` exists for: icon-only buttons,
  composite rows whose text is in child widgets, entries whose only text is a
  placeholder, and containers that group controls
  ([GNOME's coding guidelines](https://developer.gnome.org/documentation/guidelines/accessibility/coding-guidelines.html)
  ask for a label on "image-only buttons, panels that provide logical
  groupings, text areas" — all four are that list).
* **No test-only hierarchy, no test-only attributes.** Accessible names are
  what a screen-reader user hears. They happen to be what automation matches
  on. If those two ever disagree, the screen-reader user wins.

## Locators: what is actually stable

The locator is **accessible role + accessible name**. There is nothing better
available:

| Candidate | Verdict |
| --- | --- |
| role + accessible name | **Use this** — but see "Accessible names are translated" below: stable across layout changes, and the same information assistive technology uses, yet only stable *within a locale*. |
| `gtk_widget_set_name()` (the CSS name) | Not exposed. GTK 4 publishes only `toolkit` and `keyshortcuts` as AT-SPI object attributes, and application code cannot add its own. |
| AT-SPI `HelpText` (`GTK_ACCESSIBLE_PROPERTY_HELP_TEXT`) | Exposed and settable, but it is user-facing help a screen reader reads aloud. Using it as a hidden test id would be abusing it. |
| index path into the tree | Rejected. Breaks on any layout change; the tree is 20+ levels deep before it reaches content. |
| screen coordinates | Rejected, and impossible anyway — see Wayland below. |

**There is no hidden automation-id facility in GTK 4 / AT-SPI.** That is the
documented limitation; we did not invent an abstraction to work around it. In
practice it has not been a problem: every control the smoke suite needs is
reachable by role + name.

Two supplementary locators are worth knowing:

* **`win.*` GActions on the frame.** GTK publishes every window action on the
  main window's AT-SPI `Action` interface (`win.create-group`,
  `win.move-to-group`, `window.close`, …). This is the accessible route to
  anything that only has a menu item or a keyboard shortcut.
* **The `keyshortcuts` AT-SPI attribute.** GTK publishes a control's
  accelerator here, separately from its name — which is precisely why the name
  must not repeat it.

## Wayland

The target environment is Wayland, and that rules out a whole class of
automation:

* Dogtail's `Node.click()`, `Node.doubleClick()`, `Node.point()`,
  `Node.typeText()` and `Node.keyCombo()` all synthesise X11 events through
  XTEST. Under Wayland they are silently inert. **The harness never calls
  them.**
* `Component.getPosition()` returns `(0, 0)` for every widget: a Wayland client
  is not told where its own window is, so absolute coordinates do not exist.
  Any coordinate-driven approach is a non-starter regardless of preference.

What *does* work is the semantic side of AT-SPI, which is compositor-agnostic:

| Intent | Mechanism | Works |
| --- | --- | --- |
| Activate a control | `Action` interface | yes |
| Run a command with no button | `win.*` GAction on the frame | yes |
| Type into a field | `EditableText.setTextContents` | yes |
| Read a field | `Text` interface | yes |
| Select a list row | `Selection.selectChild` | yes (where the list has a selection mode) |
| Read state (sensitive, selected, expanded, checked…) | `StateSet` | yes |
| Move keyboard focus | `Component.GrabFocus` | **no** |
| Type into the terminal | — | **no** |
| Perform a drag | — | **no** |

### `Component.GrabFocus` is not implemented in GTK 4

`Atspi.Component.grab_focus()` returns `atspi_error (1)` for **every** GTK 4
widget, in SSH Pilot and in a minimal test app alike. This is a toolkit gap,
not something the application can fix.

The consequence for automation: focus must be moved by activating the control
whose job is to move it (in SSH Pilot, the *Search Connections* button focuses
the search entry), not demanded from outside. In practice this is rarely
needed, because `EditableText` writes do not require focus.

Note also that the `focused` state only appears when the compositor has made
the window active. On a shared desktop another window may hold focus, so tests
assert on it conditionally (see
`test_search_action_reveals_and_focuses_the_search_entry`).

Two states to know about. GTK 4 publishes an insensitive widget as **not**
`SENSITIVE`, but never sets AT-SPI's separate `ENABLED` state on anything —
assert on `sensitive`; `enabled` is always absent. And a toggled
`Gtk.ToggleButton` comes through as `PRESSED`, not `CHECKED`; `checked` is
reserved for check boxes and radio items.

### A container needs a role before it can have a name

Labelling a bare `Gtk.Box` does nothing: GTK does not publish an accessible
name for a widget left on the default *generic* role, so the label is accepted
and silently dropped. Measured side by side:

| widget | exposed as |
| --- | --- |
| `Gtk.Box` + label | `'panel' ''` — name discarded |
| `Gtk.Box(accessible_role=TOOLBAR)` + label | `'tool bar' 'Connection actions'` |
| `Gtk.Box(accessible_role=GROUP)` + label | `'grouping' 'C group role'` |

This is the practical form of the
[custom-widget guidelines'](https://developer.gnome.org/documentation/guidelines/accessibility/custom-widgets.html)
second step, "determine which accessible role a custom widget should provide":
the role is not decoration, it is what makes the rest of the metadata visible.
SSH Pilot's three action rows are toolbars and now say so. `accessible-role` is
construct-only, so it has to be passed to the constructor.

### In-window dialogs need an active window

`Adw.Dialog` (used for *Create New Group*, *Move to Group*, …) is presented
inside its parent window rather than as a separate toplevel. When that window
is **not active**, the dialog renders on screen but publishes an almost empty
accessible subtree — two nodes, no fields, no buttons. A screen-reader user
whose window lost focus would find the dialog unreadable; an automation client
sees the same thing.

Nothing outside the app can correct this on Wayland: `Component.GrabFocus` is
unimplemented, and `org.freedesktop.Application.Activate` returns success
without raising the window (focus-stealing prevention). Note how narrow the
effect is, though — structure is unaffected. Roles, names, states, actions and
the whole tree shape stay readable; it is only *live content* that stops being
published. The suite is split along that line: `AppSession.wait_for_content()`
skips when the window is inactive, and every other assertion runs regardless.

## The terminal (VTE)

The terminal widget was inspected, not changed. What AT-SPI sees:

* **Role** `terminal`; **states** `focusable`, `multi line`, `sensitive`,
  `showing`, and `focused` when it holds focus.
* **Accessible name**: VTE reports every terminal as simply "Terminal", which
  is ambiguous once a window holds several. `TerminalWidget` now names it after
  its connection (`Terminal: production-db`); a local shell keeps the plain
  "Terminal". The tab page also carries the connection name.
* **Description**: the terminal's current title (e.g. `mahdi@fedora:~`), which
  tracks the remote working directory.
* **Interfaces**: `Text` and `Component`. **No `EditableText`, no `Action`,
  no `Value`, no `Selection`, no `Hypertext`.**

So:

* **Terminal text is readable.** `Text.getText(0, -1)` returns the visible
  screen including the prompt, and `caretOffset` tracks the cursor. Scrollback
  beyond the visible screen is not exposed. `getTextAtOffset` is deprecated in
  at-spi 2.58 and errors — use `getStringAtOffset`.
* **Terminal input is not possible through AT-SPI.** Setting `.text` raises
  `AttributeError` (no `EditableText`), and there is no action to invoke.
  Driving a shell session therefore needs **real keyboard input**: XTEST under
  X11, or the `org.gnome.Mutter.RemoteDesktop` / `xdg-desktop-portal`
  RemoteDesktop interface under Wayland. Neither is wired up here.
* **Focus semantics**: the widget is focusable and reports `focused`, but
  cannot be focused through AT-SPI (see above). Activate a tab, or use the
  window action that opens the terminal, and focus follows.

`test_terminal_text_is_readable_but_not_writable` pins all of this down so a
future VTE or GTK update that changes it is noticed.

## Sidebar drag-and-drop

Production drag-and-drop is unchanged. What is true of it over AT-SPI:

* **Rows are identifiable.** A `ConnectionRow` is a `list item` named after the
  connection, described by `user@host`. A `GroupRow` is a `list item` named
  `Connection group: <name>`, described by its member count, carrying
  `expandable` and `expanded`. Both source and target of a drag can therefore
  be located precisely.
* **The gesture cannot be performed.** AT-SPI has no drag-and-drop API at all,
  rows expose no drag/drop actions, and Wayland removes the coordinate fallback.
  Testing the *gesture* — the drop indicators, the mid-drag auto-expand, the
  mixed-group drop rules — will require real pointer automation
  (`libei`/RemoteDesktop portal, or an X11 session with XTEST).
* **The outcome has an accessible equivalent, and it is tested.** Select the
  row, invoke `win.move-to-group`, name the target group, activate *Move*. That
  is proven end to end in
  `test_connection_can_be_moved_into_a_group_without_a_drag`.
* One gap inside that dialog: its **existing-group** rows are activatable
  `Adw.ActionRow`s, and GTK 4 exposes no action for an activatable list row, so
  AT-SPI can neither activate nor select them. Choosing an *existing* group is
  therefore out of reach; naming a *new* one is not. The rows now at least
  publish a `selected` state so a screen reader can tell which is chosen. Fixing
  activation would mean giving the rows a real `activatable-widget` (a check or
  radio button), which is a UI change and out of scope here.

## Dependencies

Production dependencies are untouched. Nothing in `requirements.txt` changed,
and `sshpilot/accessibility.py` imports only `gi.repository.Gtk`, which the app
already depends on.

GUI-test dependencies live in their own file, `requirements-gui-tests.txt`, and
are distro packages rather than PyPI ones (`python3-dogtail`,
`python3-pyatspi`, `at-spi2-core`, and the GTK 3 typelib that dogtail imports).
See that file and `tests/e2e/README.md`.

One note that saves an hour: dogtail refuses to import unless the GNOME
`toolkit-accessibility` GSetting is on. That gate belongs to GTK 3's
atk-bridge. GTK 4 publishes its AT-SPI tree whenever the accessibility bus
exists, regardless of the setting — verified with the setting off. The harness
therefore disables dogtail's check instead of rewriting the developer's desktop
settings.

## PyGObject quirks worth remembering

Both were found the hard way and are now guarded by unit tests:

* **Tristate accessible states must be passed as plain `int`.**
  `update_state([Gtk.AccessibleState.EXPANDED], [True])` trips a
  `g_value_get_int` assertion; `[Gtk.AccessibleTristate.TRUE]` publishes
  `EXPANDABLE` *without* `EXPANDED` — no error, just a state assistive
  technology never sees. `[1]` / `[0]` is correct.
* **`labelled-by` relations cannot be set from Python.** GNOME's coding
  guidelines ask for `GTK_ACCESSIBLE_RELATION_LABELLED_BY` to tie a control to
  the label that names it, but
  `update_relation([Gtk.AccessibleRelation.LABELLED_BY], [[label]])` trips a
  `g_value_get_pointer` assertion and publishes no relation. Five argument
  shapes were tried (nested list, flat list, tuple, a `GObject.Value`, and
  `update_relation_value`), and `DESCRIBED_BY` behaves the same way, so this is
  the reference-valued relations as a class, not one call spelled wrongly.
  Where the guidelines would use a relation, set the accessible name directly —
  which is why, for instance, the broadcast entry names itself instead of
  pointing at the banner title beside it.

A third one bites on the reading side: **libatspi caches an object's children**
and invalidates that cache only from events the client has subscribed to.
Dogtail does not subscribe, so a container that gained children after the
client first touched it can keep reporting zero of them — which looks exactly
like a control that never appeared. `harness.refresh()` clears the cached
subtree before each retry, and `AppSession.find()` falls back to a direct
`Atspi` walk when the cached search comes back empty — which is why the suite
does not need a sleep here either.

## Accessible names are translated

Every accessible name here goes through `gettext`, exactly like the visible
strings — a German screen reader says "Neue Verbindung". That is correct and
deliberate, and it has one consequence for automation: **an accessible name is
only a stable locator within a known locale.** Any client matching on names has
to pin the language or read the user's.

The Dogtail suite pins `LANGUAGE=C` (`harness.TEST_LOCALE`) so its contract is
the untranslated msgids. It pinned nothing at first and passed anyway, because
`tests/conftest.py` sets `LANGUAGE=en` at import for the unit suite and the
sandbox inherited it — a pin about modules that bake labels at import time,
which says nothing about a subprocess. Inheriting it worked; depending on it
was the bug. `test_accessible_names_are_translated` covers the other direction,
asserting the translated path still works, so the pin cannot quietly become
"we hardcoded English".

## Test isolation

Reused wherever SSH Pilot already had a mechanism; one small hook was added.

| Variable | Existing? | Purpose |
| --- | --- | --- |
| `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME` / `XDG_CACHE_HOME` | yes | settings, groups, logs |
| `SSHPILOT_SSH_DIR` | yes | the `~/.ssh` the app reads and writes |
| `XDG_RUNTIME_DIR` | yes | **the daemon socket** |
| `SSHPILOT_APP_ID` | **new** | a distinct GApplication id per test run |

`XDG_RUNTIME_DIR` is the one that matters most and is easiest to miss. The
daemon socket is `$XDG_RUNTIME_DIR/sshpilot/sshpilotd.sock`, so redirecting
only the XDG data directories leaves a "sandboxed" app talking to the
developer's live daemon — the first measurement taken for this work showed the
developer's real connections in a supposedly empty sandbox. The harness gives
each run its own runtime dir (symlinking the Wayland, session-bus and a11y-bus
sockets in so a window can still open), so the app starts its own private
daemon and tears it down afterwards.

`SSHPILOT_APP_ID` is the one addition to the application: without a distinct
application id, GApplication hands a second launch off to a copy the developer
already has open and no test process ever appears. It is read in `main()` in
exactly the same shape as the pre-existing `SSHPILOT_SSH_DIR` hook, and has no
effect when unset.

## Next step: letting an agent consume this

The foundation is in place; the planner is deliberately not. When it is built,
the shape that fits what exists today is:

1. **Observation.** `AppSession.describe_tree()` already produces the state an
   agent would reason over. Give it a structured (JSON) mode emitting, per
   node, `{role, name, description, states, actions, text}` plus a path — and
   prune the ~20 levels of unnamed `panel`/`grouping` chrome, which carry no
   information and would dominate a prompt. A pruned tree of the main window is
   on the order of 60 nodes.
2. **Action space.** The agent's vocabulary is exactly the harness's semantic
   verbs: `activate(role, name)`, `set_text(role, name, value)`,
   `select(role, name)`, `activate_window_action(name)`, `wait_until(...)`.
   Each maps to one AT-SPI interface call, so a plan is directly executable and
   directly loggable.
3. **Grounding.** Have the agent name its target by role + name and let the
   harness resolve it, rather than letting it pick tree indices. A miss then
   produces a legible error ("no button named X"; the tree dump is right there)
   instead of a wrong click.
4. **Verification.** Every step should assert a resulting accessible state, the
   way the smoke tests do — that is what makes a run self-checking rather than
   a recording.
5. **Known dead ends to encode up front**, so the agent does not burn turns:
   no terminal input, no drag gesture, no `GrabFocus`, no coordinates.

Real pointer/keyboard automation (the RemoteDesktop portal or an X11 session
with XTEST) is the separate piece of work that would lift items 5's first three
restrictions; it is not needed for anything the current suite covers.
