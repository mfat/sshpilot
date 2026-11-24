"""The authentication section's model: Host-block lines in, lines out.

Both forms are read: the order line and the per-method switches. A row is
checked when the order lists the method; switches, the block's own or
inherited, never decide a checkbox. A listed method the block switches off
keeps its place and reads as disabled.

Saving without an edit writes the block back exactly as authored. The
checkbox and the moves edit only the order line; a method's own setting
(Inherit / Enable / Disable) edits only that method's line. Like any other
field, both edit this Host block only: an earlier block or Settings
deciding a value never blocks or changes the edit.
"""

import pytest

from sshpilot.connection_dialog_auth import (
    DEFAULT_ORDER,
    AuthPolicy,
    join_extra,
    split_policy_lines,
    switch_caption,
)

SSH_G = {  # what `ssh -G` prints on Ubuntu with no order configured
    "gssapiauthentication": "yes",
    "hostbasedauthentication": "no",
    "pubkeyauthentication": "true",
    "kbdinteractiveauthentication": "yes",
    "passwordauthentication": "yes",
}
USER_LINE = "PreferredAuthentications gssapi-with-mic,hostbased,publickey"


def _policy(*lines, resolved=SSH_G, earlier=None):
    policy = AuthPolicy.from_lines(lines)
    policy.set_inherited(resolved)
    policy.earlier = dict(earlier or {})
    return policy


# -- reading: what the file says, nothing guessed ------------------------------

def test_inherited_switches_do_not_decide_a_checkbox():
    # Ubuntu turns GSSAPI on and OpenSSH leaves host-based off; neither
    # changes what this block's order lists.
    policy = _policy(USER_LINE)
    assert policy.checked() == ["gssapi-with-mic", "hostbased", "publickey"]
    assert policy.shown_checked("password") is False


def test_a_block_without_lines_checks_every_method_in_openssh_order():
    policy = _policy()
    assert policy.checked() == list(DEFAULT_ORDER)
    assert policy.to_lines() == []


def test_an_inherited_order_is_shown_when_the_block_sets_none():
    policy = _policy(resolved={**SSH_G, "preferredauthentications": "publickey,password"})
    assert policy.checked() == ["publickey", "password"]
    assert policy.rows() == [
        "publickey", "password", "gssapi-with-mic", "hostbased", "keyboard-interactive",
    ]


def test_a_listed_method_the_block_switches_off_keeps_its_place():
    policy = _policy("PreferredAuthentications password,publickey", "PubkeyAuthentication no")
    assert policy.checked() == ["password", "publickey"]
    assert policy.disabled_here("publickey")
    assert not policy.disabled_here("password")
    assert policy.will_try() == ["password"]


def test_checkboxes_are_unknown_until_the_order_is():
    policy = AuthPolicy.from_lines([])
    assert policy.shown_checked("password") is None
    assert not policy.can_edit()
    assert not policy.set_tried("password", False)


def test_a_profile_has_no_host_and_is_always_known():
    policy = AuthPolicy.from_lines([])
    policy.standalone = True
    assert policy.checked() == list(DEFAULT_ORDER)
    assert policy.can_edit()


def test_the_older_keyword_spelling_is_read_and_kept():
    policy = _policy("ChallengeResponseAuthentication no")
    assert policy.own_switch("keyboard-interactive") is False
    assert policy.disabled_here("keyboard-interactive")
    assert policy.to_lines() == ["ChallengeResponseAuthentication no"]


# -- saving without an edit ----------------------------------------------------

@pytest.mark.parametrize("lines", [
    [USER_LINE],
    ["PreferredAuthentications publickey,password", "PubkeyAuthentication no",
     "PasswordAuthentication yes"],
    ["preferredauthentications  publickey,password", "pubkeyauthentication=yes"],
    ["PreferredAuthentications publickey,future-method,password",
     "PubkeyAuthentication yes", "PubkeyAuthentication no"],
])
def test_an_unedited_block_is_written_back_exactly(lines):
    assert _policy(*lines).to_lines() == [line.strip() for line in lines]


# -- edits write one order line ------------------------------------------------

def test_checking_a_method_adds_it_to_the_end_of_the_line():
    policy = _policy(USER_LINE)
    assert policy.set_tried("password", True)
    assert policy.to_lines() == [
        "PreferredAuthentications gssapi-with-mic,hostbased,publickey,password",
    ]


def test_unchecking_a_method_takes_it_out_of_the_line():
    policy = _policy(USER_LINE)
    assert policy.set_tried("hostbased", False)
    assert policy.to_lines() == ["PreferredAuthentications gssapi-with-mic,publickey"]


def test_unchecking_without_an_own_order_writes_the_rest_of_it():
    policy = _policy()
    assert policy.set_tried("password", False)
    assert policy.to_lines() == [
        "PreferredAuthentications "
        "gssapi-with-mic,hostbased,publickey,keyboard-interactive",
    ]


def test_moving_a_row_rewrites_the_line():
    policy = _policy("PreferredAuthentications publickey,password")
    assert policy.move("password", 0)
    assert policy.to_lines() == ["PreferredAuthentications password,publickey"]


def test_move_by_and_its_bounds():
    policy = _policy("PreferredAuthentications publickey,password")
    assert not policy.move_by("publickey", -1)
    assert policy.move_by("publickey", 1)
    assert policy.order == ["password", "publickey"]
    assert not policy.move_by("publickey", 1)


def test_unchecked_rows_do_not_move():
    policy = _policy("PreferredAuthentications publickey,password")
    assert not policy.move("keyboard-interactive", 0)
    assert not policy.edited


def test_unknown_methods_keep_their_place():
    policy = _policy("PreferredAuthentications publickey,future-x,password")
    assert policy.move("password", 0)
    assert policy.to_lines() == ["PreferredAuthentications password,future-x,publickey"]


def test_a_repeated_method_counts_once_at_its_first_place():
    # Review: OpenSSH accepts a repeated method; moving one crashed.
    line = "PreferredAuthentications publickey,password,publickey"
    policy = _policy(line)
    assert policy.checked() == ["publickey", "password"]
    assert policy.rows() == [
        "publickey", "password", "gssapi-with-mic", "hostbased", "keyboard-interactive",
    ]
    assert policy.to_lines() == [line]          # not edited: kept as written
    assert policy.move_by("publickey", 1)
    assert policy.to_lines() == ["PreferredAuthentications password,publickey"]

    policy = _policy(line)
    assert policy.set_tried("password", False)
    assert policy.to_lines() == ["PreferredAuthentications publickey"]


def test_the_last_checked_method_cannot_be_unchecked():
    policy = _policy("PreferredAuthentications password")
    assert not policy.set_tried("password", False)
    assert policy.to_lines() == ["PreferredAuthentications password"]


def test_undoing_an_edit_writes_the_authored_lines_again():
    policy = _policy()
    policy.set_tried("password", False)
    assert policy.set_tried("password", True)
    assert not policy.edited
    assert policy.to_lines() == []

    lines = ("preferredauthentications  publickey", "PubkeyAuthentication yes")
    policy = _policy(*lines)
    policy.set_tried("hostbased", True)
    policy.set_tried("hostbased", False)
    assert policy.to_lines() == [line.strip() for line in lines]


# -- the order and a method's own setting are edited independently -------------

def test_an_order_edit_keeps_every_per_method_line():
    policy = _policy(
        "PreferredAuthentications publickey,password",
        "PubkeyAuthentication no",
        "KbdInteractiveAuthentication no",
        "HostbasedAuthentication yes",
        "PasswordAuthentication yes",
    )
    assert policy.set_tried("gssapi-with-mic", True)
    assert policy.to_lines() == [
        "PreferredAuthentications publickey,password,gssapi-with-mic",
        "HostbasedAuthentication yes",
        "PubkeyAuthentication no",
        "KbdInteractiveAuthentication no",
        "PasswordAuthentication yes",
    ]


def test_reordering_keeps_an_unchecked_methods_off_line():
    # Review P1: an earlier Host * decides the order, so this block's
    # "PasswordAuthentication no" is what keeps password off. Moving two
    # other methods must not drop it.
    lines = (
        "PreferredAuthentications publickey,keyboard-interactive",
        "PasswordAuthentication no",
    )
    policy = _policy(*lines, earlier={
        "preferredauthentications": "password,publickey,keyboard-interactive",
    })
    assert policy.move("keyboard-interactive", 0)
    assert policy.to_lines() == [
        "PreferredAuthentications keyboard-interactive,publickey",
        "PasswordAuthentication no",
    ]
    assert "password" not in policy.will_try()


def test_moving_a_method_keeps_its_own_setting():
    policy = _policy("PreferredAuthentications password,publickey", "PubkeyAuthentication no")
    assert policy.move("publickey", 0)
    assert policy.to_lines() == [
        "PreferredAuthentications publickey,password", "PubkeyAuthentication no",
    ]
    assert policy.disabled_here("publickey")


def test_checking_and_unchecking_never_touch_a_per_method_line():
    policy = _policy("PreferredAuthentications password", "PubkeyAuthentication no",
                     "HostbasedAuthentication yes")
    assert policy.set_tried("publickey", True)
    assert policy.set_tried("password", False)
    assert policy.to_lines() == [
        "PreferredAuthentications publickey",
        "HostbasedAuthentication yes",
        "PubkeyAuthentication no",
    ]
    assert policy.will_try() == []


def test_a_methods_setting_never_touches_the_order():
    policy = _policy("PreferredAuthentications password,publickey")
    assert policy.set_switch("password", False)
    assert policy.to_lines() == [
        "PreferredAuthentications password,publickey", "PasswordAuthentication no",
    ]
    assert policy.checked() == ["password", "publickey"]
    assert not policy.edited  # the order line is still the authored one
    assert policy.will_try() == ["publickey"]


def test_enable_writes_yes_and_inherit_removes_the_line():
    policy = _policy()
    assert "gssapi-with-mic" in policy.will_try()  # this machine's ssh -G says yes
    policy = _policy(resolved={**SSH_G, "gssapiauthentication": "no"})
    assert "gssapi-with-mic" not in policy.will_try()
    assert policy.set_switch("gssapi-with-mic", True)
    assert policy.to_lines() == ["GSSAPIAuthentication yes"]
    assert policy.will_try()[0] == "gssapi-with-mic"
    assert not policy.set_switch("gssapi-with-mic", True)  # no change
    assert policy.set_switch("gssapi-with-mic", None)
    assert policy.to_lines() == []
    assert "gssapi-with-mic" not in policy.will_try()


def test_a_setting_needs_no_answer_from_the_configuration():
    policy = AuthPolicy.from_lines([])
    assert not policy.can_edit()
    assert policy.set_switch("password", False)
    assert policy.to_lines() == ["PasswordAuthentication no"]


def test_an_unchanged_setting_keeps_its_authored_spelling():
    policy = _policy("pubkeyauthentication=unbound")
    assert not policy.set_switch("publickey", True)
    assert policy.to_lines() == ["pubkeyauthentication=unbound"]


def test_every_copy_of_a_changed_setting_is_replaced_by_one_line():
    # A second copy OpenSSH ignores today would take effect otherwise.
    policy = _policy("PreferredAuthentications password,publickey",
                     "PubkeyAuthentication no", "PubkeyAuthentication yes")
    assert policy.set_switch("publickey", True)
    assert policy.to_lines() == [
        "PreferredAuthentications password,publickey", "PubkeyAuthentication yes",
    ]
    policy = _policy("PubkeyAuthentication no", "PubkeyAuthentication yes")
    assert policy.set_switch("publickey", None)
    assert policy.to_lines() == []
    # Same value, but an ignored copy is there: choosing it clears the copy.
    policy = _policy("PubkeyAuthentication no", "PubkeyAuthentication yes")
    assert policy.set_switch("publickey", False)
    assert policy.to_lines() == ["PubkeyAuthentication no"]


def test_both_spellings_of_keyboard_interactive_are_one_setting():
    policy = _policy("ChallengeResponseAuthentication no", "KbdInteractiveAuthentication yes")
    assert policy.own_switch("keyboard-interactive") is False
    assert policy.set_switch("keyboard-interactive", None)
    assert policy.to_lines() == []
    policy = _policy("ChallengeResponseAuthentication no", "KbdInteractiveAuthentication yes")
    assert policy.set_switch("keyboard-interactive", True)
    assert policy.to_lines() == ["KbdInteractiveAuthentication yes"]


def test_undoing_an_order_edit_keeps_a_setting_changed_meanwhile():
    policy = _policy("PreferredAuthentications publickey,password")
    assert policy.set_tried("password", False)
    assert policy.set_switch("publickey", False)
    assert policy.set_tried("password", True)
    assert not policy.edited
    assert policy.to_lines() == [
        "PreferredAuthentications publickey,password", "PubkeyAuthentication no",
    ]


def test_a_setting_a_linked_profile_owns_cannot_be_changed_here():
    policy = _policy("PreferredAuthentications password", "PubkeyAuthentication no")
    policy.locked_methods = {"publickey"}
    assert not policy.set_switch("publickey", True)
    assert not policy.set_switch("publickey", None)
    assert policy.to_lines() == ["PreferredAuthentications password", "PubkeyAuthentication no"]
    # The order is the connection's own unless the profile sets that too.
    assert policy.set_tried("publickey", True)


# -- the block is edited the same way whatever wins ---------------------------

def test_an_earlier_order_does_not_change_how_the_block_is_edited():
    # OpenSSH decides which value a connection uses; the dialog edits the
    # block, and the order row is marked as not used (overrides module).
    lines = ("PreferredAuthentications password", "PubkeyAuthentication no")
    policy = _policy(*lines, earlier={"preferredauthentications": "publickey"})
    assert policy.will_try() == []  # publickey, switched off here
    assert policy.set_tried("keyboard-interactive", True)
    assert policy.to_lines() == [
        "PreferredAuthentications password,keyboard-interactive",
        "PubkeyAuthentication no",
    ]
    assert policy.will_try() == []


def test_settings_forced_options_do_not_block_edits_either():
    policy = _policy("PreferredAuthentications password,publickey")
    policy.forced = {"preferredauthentications": "publickey,password",
                     "passwordauthentication": "no"}
    assert policy.can_edit()
    assert policy.move("publickey", 0)
    assert policy.to_lines() == ["PreferredAuthentications publickey,password"]
    assert policy.will_try() == ["publickey"]


# -- Reset ----------------------------------------------------------------------

def test_reset_order_removes_the_line_and_waits_for_the_inherited_one():
    policy = _policy("PreferredAuthentications password,publickey")
    assert policy.reset_order()
    assert policy.to_lines() == []
    # ssh -G answered for the block's own order, not what it would inherit.
    assert policy.will_try() is None
    assert not policy.can_edit()
    policy.set_fallback({"preferredauthentications": "publickey,keyboard-interactive"})
    assert policy.checked() == ["publickey", "keyboard-interactive"]


# -- what OpenSSH will try (the line below the list) ---------------------------

def test_will_try_counts_inherited_switches():
    policy = _policy()
    # Host-based is off by default, so it is not tried although checked.
    assert policy.will_try() == [
        "gssapi-with-mic", "publickey", "keyboard-interactive", "password",
    ]


def test_a_host_not_saved_yet_shows_what_a_new_block_inherits():
    policy = AuthPolicy.from_lines([])                       # ssh -G never answers
    policy.set_fallback({"pubkeyauthentication": "no"})
    assert policy.checked() == list(DEFAULT_ORDER)
    assert policy.will_try() == ["keyboard-interactive", "password"]


@pytest.mark.parametrize("value, expected", [
    ("yes", True), ("true", True), ("host-bound", True), ("no", False), ("false", False),
])
def test_switch_values_openssh_prints(value, expected):
    policy = _policy(resolved={**SSH_G, "pubkeyauthentication": value})
    assert policy.shown_switch("publickey") is expected


# -- extra options text ---------------------------------------------------------

def test_policy_lines_are_split_from_the_other_options_and_joined_back():
    extra = "Compression yes\nPreferredAuthentications password\nGSSAPIAuthentication no"
    policy, other = split_policy_lines(extra)
    assert policy == ["PreferredAuthentications password", "GSSAPIAuthentication no"]
    assert other == "Compression yes"
    assert join_extra(other, policy).splitlines() == extra.splitlines()[:1] + policy


# -- saved through the config store: what OpenSSH actually reads ---------------

_DUPLICATES = (
    "Host demo\n"
    "    HostName 127.0.0.1\n"
    "    PreferredAuthentications password,publickey,keyboard-interactive\n"
    "    PreferredAuthentications publickey\n"
    "    PubkeyAuthentication no\n"
    "    PubkeyAuthentication yes\n"
)


def _ssh_g(path):
    import subprocess

    out = subprocess.run(
        ["ssh", "-G", "-F", str(path), "demo"], capture_output=True, text=True, check=True,
    ).stdout
    resolved = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    return resolved.get("preferredauthentications")


@pytest.mark.parametrize("edit, expected", [
    # An edit writes one order line; the copy OpenSSH ignored must not end
    # up above it (the writer keeps unchanged lines where they were).
    (lambda p: p.move("keyboard-interactive", 0), "keyboard-interactive,password,publickey"),
    (lambda p: p.set_tried("publickey", False), "password,keyboard-interactive"),
    (lambda p: p.reset_order(), None),
    (lambda p: True, "password,publickey,keyboard-interactive"),  # no auth edit
])
def test_a_saved_order_edit_is_the_order_openssh_uses(tmp_path, edit, expected):
    import shutil

    if shutil.which("ssh") is None:
        pytest.skip("needs the OpenSSH client")
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    config = tmp_path / "config"
    config.write_text(_DUPLICATES)
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(config),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    record = repo.get_record("demo")
    lines, other = split_policy_lines(record.data.get("extra_ssh_config") or "")
    policy = AuthPolicy.from_lines(lines)
    policy.set_inherited({**SSH_G, "preferredauthentications": _ssh_g(config)})
    assert edit(policy)
    repo.update_connection(
        "demo", {**record.data, "extra_ssh_config": join_extra(other, policy.to_lines())},
    )
    assert _ssh_g(config) == expected
    if expected is not None:
        # Edited: one order line. Untouched: the block as authored.
        count = config.read_text().count("PreferredAuthentications")
        assert count == (1 if policy.edited else 2)


# -- edit -> save -> ssh -G -> reopen ------------------------------------------

def _saved(tmp_path, config_text, edit):
    """Apply *edit* to host ``demo``'s policy, save, and return
    ``(ssh -G answers, the reopened policy, the file's text)``."""
    import shutil
    import subprocess

    if shutil.which("ssh") is None:
        pytest.skip("needs the OpenSSH client")
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    config = tmp_path / "config"
    config.write_text(config_text)

    def repo():
        return ConnectionRepository(
            ssh_store=SshConfigStore(config),
            state_path=tmp_path / "connections.json",
            legacy_config_path=tmp_path / "config.json",
            isolated=False,
        )

    def load(repository):
        record = repository.get_record("demo")
        lines, other = split_policy_lines(record.data.get("extra_ssh_config") or "")
        return record, AuthPolicy.from_lines(lines), other

    repository = repo()
    record, policy, other = load(repository)
    edit(policy)
    repository.update_connection(
        "demo", {**record.data, "extra_ssh_config": join_extra(other, policy.to_lines())},
    )
    out = subprocess.run(
        ["ssh", "-G", "-F", str(config), "demo"], capture_output=True, text=True, check=True,
    ).stdout
    resolved = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    return resolved, load(repo())[1], config.read_text()


def test_enabling_kerberos_reaches_openssh_and_reopens(tmp_path):
    resolved, reopened, text = _saved(
        tmp_path,
        "Host demo\n    HostName demo.example\n\nHost *\n    GSSAPIAuthentication no\n",
        lambda p: p.set_switch("gssapi-with-mic", True),
    )
    assert resolved["gssapiauthentication"] == "yes"
    assert reopened.own_switch("gssapi-with-mic") is True
    assert reopened.order is None  # the order was not written
    assert "PreferredAuthentications" not in text


def test_password_is_enabled_over_a_later_wildcard_and_moved_first(tmp_path):
    def edit(policy):
        policy.set_fallback({})
        assert policy.set_switch("password", True)
        assert policy.move("password", 0)

    resolved, reopened, _text = _saved(
        tmp_path,
        "Host demo\n    HostName demo.example\n\nHost *\n    PasswordAuthentication no\n",
        edit,
    )
    assert resolved["passwordauthentication"] == "yes"
    assert resolved["preferredauthentications"].startswith("password,")
    assert reopened.own_switch("password") is True
    assert reopened.checked()[0] == "password"


@pytest.mark.parametrize("choice, expected, own", [
    (None, "yes", None),   # Inherit: OpenSSH's built-in value is back
    (True, "yes", True),
    (False, "no", False),
])
def test_conflicting_duplicate_lines_become_one_setting(tmp_path, choice, expected, own):
    resolved, reopened, text = _saved(
        tmp_path,
        "Host demo\n    HostName demo.example\n"
        "    PasswordAuthentication no\n    PasswordAuthentication yes\n"
        "    PreferredAuthentications publickey,password\n",
        lambda p: p.set_switch("password", choice),
    )
    assert resolved["passwordauthentication"] == expected
    assert text.count("PasswordAuthentication") == (0 if choice is None else 1)
    assert reopened.own_switch("password") is own
    # Changing the setting kept the order.
    assert resolved["preferredauthentications"] == "publickey,password"
    assert reopened.order == ["publickey", "password"]


def _caption(lines, method, resolved=None):
    policy = AuthPolicy.from_lines(lines)
    policy.set_inherited(resolved or SSH_G)
    return switch_caption(
        policy.own_switch(method), policy.restriction(method),
        policy.shown_checked(method), False,
    )


@pytest.mark.parametrize("lines, method, caption", [
    # No line of its own.
    (["PreferredAuthentications publickey,password"], "publickey", "Not set"),
    # Enabled and listed: it is tried.
    (["PreferredAuthentications publickey,password", "PubkeyAuthentication yes"],
     "publickey", "Enabled for this host"),
    # Enabled but left out of the order: ssh never offers it.
    (["PreferredAuthentications password", "PubkeyAuthentication yes"],
     "publickey", "Enabled for this host, but not tried (unticked)"),
    (["PreferredAuthentications password", "PubkeyAuthentication host-bound"],
     "publickey", "Enabled for this host (host-bound), but not tried (unticked)"),
    (["PreferredAuthentications publickey,password", "PubkeyAuthentication host-bound"],
     "publickey", "Enabled for this host (host-bound)"),
    # Listed but switched off: it keeps its place and is skipped.
    (["PreferredAuthentications publickey,password", "PubkeyAuthentication no"],
     "publickey", "Disabled for this host, so it is skipped"),
    (["PreferredAuthentications password", "PubkeyAuthentication no"],
     "publickey", "Disabled for this host"),
])
def test_a_method_caption_reads_its_switch_with_its_tick(lines, method, caption):
    resolved = dict(SSH_G, preferredauthentications=parse_order_line(lines))
    assert _caption(lines, method, resolved) == caption


def parse_order_line(lines):
    return next(line.split(None, 1)[1] for line in lines
                if line.startswith("PreferredAuthentications"))


def test_a_caption_says_nothing_about_the_tick_while_the_order_is_unknown():
    assert switch_caption(True, "", None, False) == "Enabled for this host"
    assert switch_caption(False, "", None, True) == "Disabled in this profile"


def test_a_profile_caption_speaks_of_the_profile():
    assert (switch_caption(True, "", False, True)
            == "Enabled in this profile, but not tried (unticked)")
    assert (switch_caption(False, "", True, True)
            == "Disabled in this profile, so it is skipped")
