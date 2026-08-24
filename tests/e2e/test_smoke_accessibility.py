"""Dogtail smoke suite: drive the real SSH Pilot through AT-SPI only.

Every locator here is semantic — an accessible role plus an accessible name.
No screen coordinates, no index paths into the widget tree, no calls into SSH
Pilot's own Python. If a test fails after a UI refactor, that means the
*accessible* contract changed, which is exactly what we want it to catch.
"""

from __future__ import annotations

import pytest

from harness import HarnessError, has_state, names

pytestmark = pytest.mark.e2e


# --- discovery --------------------------------------------------------------


def test_application_is_discoverable_by_name(app):
    """The app announces itself with a stable name, not the launcher script."""
    assert app.app.name == "sshpilot"
    assert app.app.get_process_id() == app.process.pid


def test_main_window_is_present_and_named(app):
    window = app.window
    assert window.roleName == "frame"
    assert window.name == "SSH Pilot"
    assert has_state(window, "showing")


def test_accessible_tree_is_inspectable(app):
    dump = app.describe_tree()
    assert "'frame' name='SSH Pilot'" in dump
    # Named leaf controls prove the a11y backend populated the whole tree, not
    # just the window chrome.
    assert "'button' name='New Connection'" in dump
    assert "'list' name='Connections'" in dump
    assert dump.count("\n") > 100


# --- locating controls semantically ----------------------------------------

#: Controls the harness must be able to find by role + accessible name alone.
#: Several of these were unreachable before: their name was the tooltip, which
#: carried a user-rebindable keyboard shortcut.
EXPECTED_CONTROLS = [
    ("button", "New Connection"),
    ("button", "New Group"),
    ("button", "Search Connections"),
    ("button", "Sort Connections"),
    ("button", "Filter by tag"),
    ("button", "Hide hostnames"),
    ("button", "Settings"),
    ("button", "Main menu"),
    ("button", "New Local Terminal"),
    ("button", "Minimize sidebar to icons"),
]


@pytest.mark.parametrize("role,name", EXPECTED_CONTROLS)
def test_control_is_locatable_by_role_and_name(app, role, name):
    node = app.node(role=role, name=name)
    assert node.name == name
    assert has_state(node, "sensitive")


def test_connections_list_is_named(app):
    """The sidebar has no visible heading, so the list must name itself."""
    listbox = app.node(role="list", name="Connections")
    assert has_state(listbox, "showing")


def test_icon_row_containers_are_labelled(app):
    """GNOME's coding guidelines ask for a label on grouping panels.

    Each of these holds nothing but icon buttons, so without a name there is
    nothing to tell a screen-reader user what the row is for.
    """
    toolbar = app.node(role="tool bar", name="Connection list actions")
    assert app.find(role="button", name="New Connection", root=toolbar)


def test_icon_only_control_names_exclude_keyboard_shortcuts(app):
    """A rebindable accelerator must not leak into an accessible name."""
    for role, name in EXPECTED_CONTROLS:
        node = app.node(role=role, name=name)
        assert "Ctrl+" not in node.name and "Cmd+" not in node.name


def test_toggle_control_reports_its_pressed_state(app):
    """A toggle's on/off state is readable, and changes when it is activated.

    GTK 4 publishes a toggled ``Gtk.ToggleButton`` as AT-SPI ``pressed``, not
    ``checked`` — ``checked`` is reserved for check boxes and radio items.
    """
    toggle = app.node(role="toggle button", name="Filter by tag")
    assert not has_state(toggle, "pressed")

    app.activate(toggle)
    app.wait_until(
        lambda: has_state(toggle, "pressed"),
        description="the tag filter to report itself pressed",
    )

    app.activate(toggle)
    app.wait_until(
        lambda: not has_state(toggle, "pressed"),
        description="the tag filter to report itself released",
    )


def test_window_exposes_its_gactions(app):
    """GTK publishes ``win.*`` actions on the frame's Action interface."""
    actions = set(app.window.actions)
    for action in (
        "win.open-new-connection",
        "win.create-group",
        "win.toggle_sidebar",
        "window.close",
    ):
        assert action in actions, sorted(actions)[:20]


# --- text entry -------------------------------------------------------------


def test_omnisearch_entry_accepts_and_reports_text(app):
    """Type into an editable control and read the value back through AT-SPI."""
    entry = app.node(role="entry", name="Search connections and commands")
    assert has_state(entry, "editable")

    app.set_text(entry, "e2e-probe")
    app.wait_until(
        lambda: entry.text == "e2e-probe",
        description="entry text to reflect the typed value",
    )

    app.set_text(entry, "")
    app.wait_until(
        lambda: entry.text == "",
        description="entry to clear",
    )


def test_search_action_reveals_and_focuses_the_search_entry(app):
    """Focus is driven through the app's own control, not Component.GrabFocus.

    GTK 4 does not implement the AT-SPI ``GrabFocus`` method (see
    ``AppSession.request_focus``), so this is how an automation client moves
    the caret somewhere: activate the control whose job is to focus it.

    The revealed/hidden transition is asserted unconditionally. The FOCUSED
    state is only asserted when the compositor has actually made the window
    active — on a shared desktop another window may hold focus, and no amount
    of AT-SPI can change that.
    """
    app.activate(app.node(role="button", name="Search Connections"))
    entry = app.node(role="entry", name="Search connections")

    if has_state(app.window, "active"):
        app.wait_until(
            lambda: has_state(entry, "focused"),
            description="sidebar search entry to take focus",
        )

    app.set_text(entry, "nothing-matches-this")
    app.wait_until(
        lambda: entry.text == "nothing-matches-this",
        description="sidebar search text",
    )

    # Toggling search off again leaves the window in its original state.
    app.set_text(entry, "")
    app.activate(app.node(role="button", name="Search Connections"))
    app.wait_until(
        lambda: not app.find(role="entry", name="Search connections"),
        description="the search field to be hidden again",
    )
    app.node(role="button", name="New Connection")


# --- activating a control and cancelling out of it -------------------------


def _connection_editor(app):
    """The New/Edit Connection window, found by its own accessible name."""
    return app.dialog("New Connection")


def test_new_connection_opens_editor_and_cancel_restores_window(app):
    app.activate(app.node(role="button", name="New Connection"))
    editor = _connection_editor(app)

    # Fields are named from their visible titles by GTK — nothing to fix there.
    for field in ("Name", "Hostname / IP address", "Username", "Port"):
        assert app.find(role="text", name=field, root=editor), field

    cancel = app.node(role="button", name="Cancel", root=editor)
    app.activate(cancel)

    app.wait_until(
        lambda: not app.find(role="text", name="Hostname / IP address"),
        description="connection editor to close",
    )
    # Back where we started: the main window is usable again.
    assert has_state(app.window, "showing")
    app.node(role="button", name="New Connection")


def test_created_connection_appears_as_a_named_row(app):
    """End-to-end: type into the editor, save, and read the result back.

    The sidebar row is the case AT-SPI could not describe at all before — its
    text lives in child labels, so GTK exposed the list item unnamed.
    """
    # Nothing selected yet: the per-connection actions are not offered at all.
    assert not app.find(role="button", name="Edit Connection")

    app.activate(app.node(role="button", name="New Connection"))
    editor = _connection_editor(app)

    app.set_text(app.node(role="text", name="Name", root=editor), "e2e-host")
    # The SSH alias is a separate required field; the editor does not derive it
    # from Name when the text arrives through EditableText rather than keys.
    app.set_text(
        app.node(
            role="text", name="SSH Alias (no whitespace allowed)", root=editor
        ),
        "e2e-host",
    )
    app.set_text(
        app.node(role="text", name="Hostname / IP address", root=editor),
        "192.0.2.10",
    )
    app.set_text(app.node(role="text", name="Username", root=editor), "tester")
    app.activate(app.node(role="button", name="Save", root=editor))

    row = app.node(role="list item", name="e2e-host", timeout=30)
    assert has_state(row, "selectable")
    # The description carries the target, so a row is identifiable even when
    # two connections share a nickname prefix.
    assert "tester" in (row.description or "") or "192.0.2.10" in (row.description or "")

    # ...and it really was written to the sandbox's ssh config, not the user's.
    app.wait_until(
        lambda: app.sandbox.ssh_config.exists()
        and "Host e2e-host" in app.sandbox.ssh_config.read_text(),
        description="connection to be written to the sandbox ssh config",
    )
    assert app.sandbox.ssh_dir in app.sandbox.ssh_config.parents

    # Selecting the row offers the per-connection actions, and that transition
    # is visible over AT-SPI. (GTK 4 publishes sensitivity as the `sensitive`
    # state only; it never sets AT-SPI's separate `enabled`.)
    app.select(row)
    app.wait_until(
        lambda: has_state(row, "selected"), description="the row to select"
    )
    actions_bar = app.node(role="tool bar", name="Connection actions")
    for action in ("Edit Connection", "Delete Connection"):
        button = app.node(role="button", name=action, root=actions_bar)
        assert has_state(button, "sensitive"), action
        assert has_state(button, "showing"), action


def test_group_row_is_named_and_reports_expansion(app):
    """Group headers say what they are and expose an expanded state."""
    app.activate_window_action("win.create-group")
    dialog = app.dialog("Create New Group")
    # Named after the prompt shown above it, which is the field's visible label.
    entry = app.node(
        role="text", name="Enter a name for the new group:", root=dialog
    )
    app.set_text(entry, "Production")
    app.activate(app.node(role="button", name="Create", root=dialog))

    row = app.node(role="list item", name="Connection group: Production", timeout=30)
    assert has_state(row, "expandable")
    assert has_state(row, "expanded")

    collapse = app.node(role="button", name="Collapse group", root=row)
    app.activate(collapse)
    app.wait_until(
        lambda: not has_state(row, "expanded"),
        description="group row to report itself collapsed",
    )
    assert app.find(role="button", name="Expand group", root=row)


# --- terminal (VTE) boundary ------------------------------------------------


def test_terminal_text_is_readable_but_not_writable(app):
    """Pin down exactly where AT-SPI stops being enough for the terminal.

    VTE publishes the screen through the ``Text`` interface, so an automation
    client can *read* the terminal. It exposes neither ``EditableText`` nor an
    ``Action``, so sending input needs real keyboard events — see
    ``docs/accessibility-automation.md``.
    """
    app.activate(app.node(role="button", name="New Local Terminal"))
    terminal = app.node(role="terminal", timeout=30)

    assert terminal.roleName == "terminal"
    assert has_state(terminal, "multi line")
    assert app.wait_for_content(
        lambda: terminal.text and terminal.text.strip(),
        timeout=30,
        description="the shell prompt to render into the terminal",
    )
    assert terminal.queryText().caretOffset >= 0

    with pytest.raises(AttributeError):
        terminal.text = "echo hello\n"
    assert terminal.actions == {}


# --- the drag-and-drop equivalent -------------------------------------------


def test_connection_can_be_moved_into_a_group_without_a_drag(app):
    """Sidebar drag-and-drop has an accessible equivalent; the gesture does not.

    AT-SPI has no drag API, and under Wayland there is no coordinate injection
    either — so the drop *gesture* is out of reach. The **outcome** is not:
    select the row, invoke ``win.move-to-group``, name the target. Production
    drag-and-drop is untouched by this; the test simply uses the path that is
    reachable.
    """
    app.activate(app.node(role="button", name="New Connection"))
    editor = _connection_editor(app)
    for field, value in (
        ("Name", "movable"),
        ("SSH Alias (no whitespace allowed)", "movable"),
        ("Hostname / IP address", "192.0.2.30"),
        ("Username", "tester"),
    ):
        app.set_text(app.node(role="text", name=field, root=editor), value)
    app.activate(app.node(role="button", name="Save", root=editor))

    row = app.node(role="list item", name="movable", timeout=30)
    app.select(row)
    app.wait_until(
        lambda: has_state(row, "selected"), description="the row to select"
    )

    app.activate_window_action("win.move-to-group")
    dialog = app.dialog("Move to Group")
    app.set_text(app.node(role="text", name="Group name", root=dialog), "Production")
    move = app.node(role="button", name="Move", root=dialog)
    app.wait_until(
        lambda: has_state(move, "sensitive"),
        description="Move to become available once a target is named",
    )
    app.activate(move)

    group = app.node(
        role="list item", name="Connection group: Production", timeout=30
    )
    assert "1" in (group.description or ""), group.description


# --- isolation & shutdown ---------------------------------------------------


def test_session_is_isolated_from_the_developer_environment(app):
    """No developer connections leak in through the shared daemon socket."""
    rows = app.find(role="list item", root=app.node(role="list", name="Connections"))
    assert names(rows) == [], f"sandbox should start empty, saw {names(rows)}"
    assert app.sandbox.daemon_socket.exists(), "the app should run its own daemon"


def test_application_closes_cleanly(app):
    app.activate_window_action("window.close")
    app.wait_until(
        lambda: app.process.poll() is not None,
        timeout=30,
        description="the app process to exit",
    )
    assert app.process.returncode == 0, app.read_log()[-2000:]


def test_missing_control_raises_a_useful_error(app):
    """A failed lookup must say what it looked for, not just time out."""
    with pytest.raises(HarnessError) as excinfo:
        app.node(role="button", name="No Such Button", timeout=1.0)
    assert "No Such Button" in str(excinfo.value)
