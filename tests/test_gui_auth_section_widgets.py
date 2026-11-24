"""The authentication section as drawn: real widgets, no daemon.

``test_connection_dialog_auth_policy.py`` states the rules on the model.
These are the same rules as the user meets them: a row dragged to a place,
the row menu, the "OpenSSH will try" line that appears (red) only when it
has something to say, and the states the section shows when OpenSSH gave
no answer, no method is left, or the order names a method it does not know.
"""

import pytest

pytestmark = pytest.mark.gui

SSH_G = {  # what ``ssh -G`` prints with no order configured
    "gssapiauthentication": "no",
    "hostbasedauthentication": "no",
    "pubkeyauthentication": "true",
    "kbdinteractiveauthentication": "yes",
    "passwordauthentication": "yes",
}


@pytest.fixture
def gtk():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"GTK unavailable: {exc}")
    if not isinstance(getattr(Gtk, "MAJOR_VERSION", None), int):
        pytest.skip("gi is stubbed in this environment (no real GTK)")
    if not Gtk.init_check():
        pytest.skip("GTK cannot initialise (no display)")
    Adw.init()
    return Gtk


def _pump(seconds=0.3):
    import time

    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def _section(*lines, resolved=SSH_G, earlier=None, changed=None):
    from sshpilot.connection_dialog_auth import AuthPolicy, AuthPolicyGroup

    section = AuthPolicyGroup.create(
        (lambda: changed.append(1)) if changed is not None else (lambda: None)
    )
    policy = AuthPolicy.from_lines(lines)
    if resolved is not None:
        policy.set_inherited(resolved)
    policy.earlier = dict(earlier or {})
    section.load(policy)
    return section


def _shown(gtk, section):
    """The section in a window, its list open, laid out."""
    window = gtk.Window(default_width=640, default_height=560)
    window.set_child(section.group)
    section.order_row.set_expanded(True)
    window.present()
    _pump()
    return window


def _row_middle(section, method):
    """The y of the middle of a method's row, in the expander's coordinates."""
    from gi.repository import Graphene

    row = section.rows[method]
    ok, point = row.compute_point(section.order_row, Graphene.Point())
    assert ok, "the row is not laid out"
    return point.y + row.get_height() / 2


def _drag(section, method, y):
    """What a drag does, without a pointer: begin on *method*, move to *y*,
    drop there. Returns what the drop answered."""
    from sshpilot.connection_dialog_auth import _DRAG_PREFIX

    row = section.rows[method]
    assert section._on_drag_prepare(None, 4, 4, row) is not None
    section._drag_method = method
    section._drag_height = section._row_height(row)
    section._drag_home = section.policy.rows().index(method)
    assert section._on_drop_motion(None, 0, y)
    return section._on_drop(None, _DRAG_PREFIX + method, 0, y)


# -- dragging a row -------------------------------------------------------------

def test_a_row_dropped_on_the_first_place_is_tried_first(gtk):
    changed = []
    section = _section(
        "PreferredAuthentications publickey,keyboard-interactive,password",
        changed=changed,
    )
    window = _shown(gtk, section)
    try:
        assert section.policy.checked()[0] == "publickey"
        top = _row_middle(section, section.policy.rows()[0])
        assert _drag(section, "password", top - 40)
        assert section.policy.checked()[0] == "password"
        assert section.policy.to_lines() == [
            "PreferredAuthentications password,publickey,keyboard-interactive"
        ]
        assert changed, "the dialog was not told the lines changed"
        # The list is redrawn in the new order, and nothing is left of the drag.
        assert section._shown_rows[0] is section.rows["password"]
        assert section._drag_method is None and section._drag_gap is None
        assert all(
            row.get_margin_top() == 0 and row.get_margin_bottom() == 0
            and row.get_visible()
            for row in section._shown_rows
        )
    finally:
        window.destroy()


def test_a_row_dropped_past_the_last_one_is_tried_last(gtk):
    section = _section("PreferredAuthentications publickey,keyboard-interactive,password")
    window = _shown(gtk, section)
    try:
        last = _row_middle(section, section.policy.rows()[-1])
        assert _drag(section, "publickey", last + 200)
        assert section.policy.checked() == [
            "keyboard-interactive", "password", "publickey",
        ]
    finally:
        window.destroy()


def test_the_space_follows_the_pointer_and_returns_when_it_leaves(gtk):
    section = _section("PreferredAuthentications publickey,keyboard-interactive,password")
    window = _shown(gtk, section)
    try:
        row = section.rows["password"]
        section._drag_method = "password"
        section._drag_height = section._row_height(row)
        section._drag_home = section.policy.rows().index("password")
        section._on_drop_motion(None, 0, _row_middle(section, "publickey") - 40)
        assert section._drag_gap == 0
        others = section._other_rows()
        assert others[0].get_margin_top() == section._drag_height
        # Outside the list a drop does nothing: the space is at the row's
        # own place again, and a cancelled drag leaves the order alone.
        section._on_drop_leave(None)
        assert section._drag_gap == section._drag_home
        section._on_drag_cancel(None, None, None, row)
        assert section._drag_method is None
        assert all(r.get_margin_top() == 0 for r in section._shown_rows)
        assert section.policy.to_lines() == [
            "PreferredAuthentications publickey,keyboard-interactive,password"
        ]
    finally:
        window.destroy()


def test_a_drop_from_elsewhere_or_on_a_locked_order_is_refused(gtk):
    from sshpilot.connection_dialog_auth import _DRAG_PREFIX

    section = _section("PreferredAuthentications publickey,password")
    window = _shown(gtk, section)
    try:
        before = section.policy.to_lines()
        # Not one of this list's rows.
        assert section._on_drop(None, "some other text", 0, 10) is False
        # A row that is not the one being dragged.
        section._drag_method = "publickey"
        assert section._on_drop(None, _DRAG_PREFIX + "password", 0, 10) is False
        section._drag_method = None
        # A linked profile sets the order: nothing can be picked up.
        section.set_profile_locks(order=True, methods=(), note="Set by a profile")
        assert section._on_drag_prepare(None, 4, 4, section.rows["password"]) is None
        section._drag_method = "password"
        assert section._on_drop_motion(None, 0, 10) == 0
        assert section._on_drop(None, _DRAG_PREFIX + "password", 0, 10) is False
        assert section.policy.to_lines() == before
    finally:
        window.destroy()


# -- the row menu ---------------------------------------------------------------

def test_move_up_and_move_down_reorder_the_line(gtk):
    section = _section("PreferredAuthentications publickey,keyboard-interactive,password")
    actions = section.rows["keyboard-interactive"]._actions
    assert actions.lookup_action("up").get_enabled()
    assert actions.lookup_action("down").get_enabled()
    actions.activate_action("up", None)
    assert section.policy.order == ["keyboard-interactive", "publickey", "password"]
    # First now: it cannot go further up.
    assert not actions.lookup_action("up").get_enabled()
    actions.activate_action("down", None)
    actions.activate_action("down", None)
    assert section.policy.order == ["publickey", "password", "keyboard-interactive"]
    assert not actions.lookup_action("down").get_enabled()
    assert section.order_row.get_subtitle() == "publickey, password, keyboard-interactive"


def test_an_unchecked_row_cannot_be_moved_from_the_menu(gtk):
    section = _section("PreferredAuthentications publickey,password")
    actions = section.rows["hostbased"]._actions
    assert not section.rows["hostbased"]._check.get_active()
    for name in ("up", "down", "top"):
        assert not actions.lookup_action(name).get_enabled()


# -- the "OpenSSH will try" line --------------------------------------------------

def test_the_line_is_hidden_while_it_repeats_the_header(gtk):
    section = _section("PreferredAuthentications publickey,password")
    assert section.order_row.get_subtitle() == "publickey, password"
    assert section.effective_row.get_subtitle() == "publickey, password"
    assert not section.preview.get_visible()
    assert not section.effective_row.has_css_class("error")
    # An edit of the order keeps both in step: still nothing to say.
    section.rows["password"]._actions.activate_action("top", None)
    assert section.order_row.get_subtitle() == "password, publickey"
    assert not section.preview.get_visible()


def test_the_line_shows_in_red_with_its_reason_when_another_setting_wins(gtk):
    section = _section(
        "PreferredAuthentications password,publickey", "PasswordAuthentication yes",
        earlier={"passwordauthentication": "no"},
    )
    section.set_preview_notes([("Password", "Not used: “Host *” <sets> it & wins.")])
    assert section.order_row.get_subtitle() == "password, publickey"
    assert section.effective_row.get_subtitle() == "publickey"
    assert section.preview.get_visible()
    assert section.effective_row.has_css_class("error")
    # The reason names the row it is about; the note's text is not markup.
    assert section.effective_row.get_tooltip_markup() == (
        "<b>Password</b>\nNot used: “Host *” &lt;sets&gt; it &amp; wins."
    )
    # Unchecking the overruled method ends the conflict: the line goes.
    section.rows["password"]._check.set_active(False)
    assert section.order_row.get_subtitle() == "publickey"
    assert not section.preview.get_visible()
    assert not section.effective_row.has_css_class("error")
    assert section.effective_row.get_tooltip_markup() is None


def test_the_line_keeps_its_state_while_an_edit_waits_for_openssh(gtk):
    section = _section("PreferredAuthentications publickey,password")
    policy = section.policy
    policy.set_draft(
        {"preferredauthentications": "publickey,password",
         "passwordauthentication": "yes", "pubkeyauthentication": "yes"},
        {}, policy.to_lines(),
    )
    section.sync()
    assert not section.preview.get_visible()
    section.rows["password"]._actions.activate_action("top", None)
    assert not policy._draft_current()
    assert not section.preview.get_visible()          # no answer yet: unchanged
    # The answer: an earlier block's order wins over the one just set.
    policy.set_draft(
        {"preferredauthentications": "publickey",
         "passwordauthentication": "yes", "pubkeyauthentication": "yes"},
        {}, policy.to_lines(),
    )
    section.sync()
    assert section.preview.get_visible()
    assert section.effective_row.has_css_class("error")
    assert section.effective_row.get_subtitle() == "publickey"


def test_a_profiles_section_never_shows_the_line(gtk):
    from sshpilot.connection_dialog_auth import AuthPolicy, AuthPolicyGroup

    section = AuthPolicyGroup.create(lambda: None, profile=True)
    section.load(AuthPolicy.from_lines(["PreferredAuthentications password"]))
    assert not section.preview.get_visible()
    section.rows["publickey"]._check.set_active(True)
    assert not section.preview.get_visible()


# -- states the section shows -----------------------------------------------------

def test_before_openssh_answers_nothing_is_claimed(gtk):
    section = _section(resolved=None)
    waiting = "Shown once OpenSSH has read the configuration."
    assert section.order_row.get_subtitle() == waiting
    assert not section.preview.get_visible()          # a wait says nothing
    for method, row in section.rows.items():
        assert row._check.get_inconsistent(), method
        assert not row._check.get_sensitive(), method
        assert row._check.get_tooltip_text() == waiting


def test_a_failed_lookup_is_shown_as_unknown_not_as_a_default(gtk):
    section = _section(resolved=None)
    section.policy.set_lookup_failed()
    section.sync()
    unknown = "Unknown: OpenSSH could not read the configuration."
    assert section.preview.get_visible()
    assert section.effective_row.get_subtitle() == unknown
    # Not knowing is not a conflict: no red, no reason.
    assert not section.effective_row.has_css_class("error")
    assert section.effective_row.get_tooltip_markup() is None
    for method, row in section.rows.items():
        assert row._check.get_inconsistent(), method
        assert row._check.get_tooltip_text() == unknown


def test_no_method_left_is_an_error_on_screen(gtk):
    section = _section(
        "PreferredAuthentications password", "PasswordAuthentication yes",
        earlier={"passwordauthentication": "no"},
    )
    assert section.preview.get_visible()
    assert section.effective_row.get_subtitle() == "No method: the login cannot succeed."
    assert section.effective_row.has_css_class("error")


def test_a_method_whose_switch_is_unanswered_is_marked_unsure(gtk):
    # The block answers for keys itself; nothing has answered for Kerberos.
    section = _section(
        "PreferredAuthentications gssapi-with-mic,publickey",
        "PubkeyAuthentication yes",
        resolved=None,
    )
    assert section.rows["gssapi-with-mic"]._check.get_inconsistent()
    assert section.preview.get_visible()
    assert section.effective_row.get_subtitle() == (
        "gssapi-with-mic (unknown if enabled), publickey"
    )
    assert not section.effective_row.has_css_class("error")


def test_a_method_this_dialog_does_not_know_is_kept_and_shown(gtk):
    section = _section("PreferredAuthentications publickey,quantum-token,password")
    assert section.extra_row in section._shown_rows
    assert section.extra_row.get_title() == "quantum-token"
    assert section.extra_row.get_subtitle() == "Not known to this dialog; kept as written."
    assert section.order_row.get_subtitle() == "publickey, quantum-token, password"
    # It is in what OpenSSH is given too, so the line has nothing to add.
    assert not section.preview.get_visible()
    # An edit of the known rows keeps it in the line.
    section.rows["password"]._check.set_active(False)
    assert section.policy.to_lines() == [
        "PreferredAuthentications publickey,quantum-token"
    ]
    # Without it the extra row is gone.
    assert _section("PreferredAuthentications publickey").extra_row not in (
        _section("PreferredAuthentications publickey")._shown_rows
    )


def test_the_last_checked_method_says_why_it_cannot_be_unchecked(gtk):
    section = _section("PreferredAuthentications publickey")
    check = section.rows["publickey"]._check
    assert check.get_active() and not check.get_sensitive()
    assert check.get_tooltip_text() == "At least one method must stay checked."
