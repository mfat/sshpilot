"""The authentication section's model: Host-block lines in, lines out.

Both forms are read: the order line and the per-method switches. A row is
checked when the order lists the method and the block's own lines do not
switch it off; inherited or built-in switches never decide a checkbox.

Saving without an edit writes the block back exactly as authored. An edit
writes one order line; a per-method line goes only when the user checks or
unchecks that method. Like any other field, it edits this Host block only: an earlier block or
Settings deciding the order never blocks or changes the edit.
"""

import pytest

from sshpilot.connection_dialog_auth import (
    DEFAULT_ORDER,
    AuthPolicy,
    join_extra,
    split_policy_lines,
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


def test_the_blocks_own_off_switch_unchecks_a_listed_method():
    policy = _policy("PreferredAuthentications password,publickey", "PubkeyAuthentication no")
    assert policy.checked() == ["password"]
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
    assert policy.shown_checked("keyboard-interactive") is False
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


# -- a per-method line goes only with an explicit change to that method --------

def test_an_edit_keeps_the_lines_of_methods_it_does_not_change():
    policy = _policy(
        "PreferredAuthentications publickey,password",
        "PubkeyAuthentication no",
        "KbdInteractiveAuthentication no",
        "HostbasedAuthentication yes",
        "PasswordAuthentication yes",
    )
    assert policy.set_tried("gssapi-with-mic", True)
    assert policy.to_lines() == [
        "PreferredAuthentications password,gssapi-with-mic",
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


def test_checking_a_method_drops_its_own_off_line():
    policy = _policy("PreferredAuthentications password,publickey", "PubkeyAuthentication no")
    assert policy.set_tried("publickey", True)
    assert policy.to_lines() == ["PreferredAuthentications password,publickey"]
    assert policy.will_try() == ["password", "publickey"]


def test_unchecking_a_method_drops_its_own_on_line():
    policy = _policy("PreferredAuthentications hostbased,password",
                     "HostbasedAuthentication yes")
    assert policy.set_tried("hostbased", False)
    assert policy.to_lines() == ["PreferredAuthentications password"]


def test_every_copy_of_a_dropped_line_goes():
    # A second copy OpenSSH ignores today would take effect otherwise.
    policy = _policy("PreferredAuthentications password,publickey",
                     "PubkeyAuthentication no", "PubkeyAuthentication yes")
    assert policy.set_tried("publickey", True)
    assert policy.to_lines() == ["PreferredAuthentications password,publickey"]


def test_lines_a_linked_profile_sets_are_never_dropped():
    policy = _policy("PreferredAuthentications password", "PubkeyAuthentication no")
    policy.locked_methods = {"publickey"}
    assert not policy.set_tried("publickey", True)
    assert policy.to_lines() == ["PreferredAuthentications password", "PubkeyAuthentication no"]


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


def test_reset_switch_removes_every_copy_of_the_line():
    policy = _policy("PubkeyAuthentication no", "PubkeyAuthentication yes",
                     "GSSAPIAuthentication no")
    assert policy.reset_switch("publickey")
    assert policy.to_lines() == ["GSSAPIAuthentication no"]


def test_the_older_keyword_copy_goes_with_a_reset_too():
    policy = _policy("KbdInteractiveAuthentication no", "ChallengeResponseAuthentication yes")
    policy.reset_switch("keyboard-interactive")
    assert policy.to_lines() == []


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
