"""The authentication section's model: Host-block lines in, lines out.

One set of settings, no modes. Rows show the block's own order or, when it
sets none, the inherited one; checkboxes show the block's own switch or the
inherited value. Whatever the user does not touch is written back exactly as
it was authored.
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


def _policy(*lines, resolved=SSH_G):
    policy = AuthPolicy.from_lines(lines)
    policy.set_inherited(resolved)
    return policy


# -- loading and showing ------------------------------------------------------

def test_an_unset_block_shows_openssh_order_and_inherited_switches():
    policy = _policy()
    assert not policy.order_is_set
    assert policy.shown_order() == list(DEFAULT_ORDER)
    assert policy.shown_switch("hostbased") is False
    assert policy.shown_switch("gssapi-with-mic") is True
    # Host-based is off, so it is not tried.
    assert policy.will_try() == [
        "gssapi-with-mic", "publickey", "keyboard-interactive", "password",
    ]
    assert policy.to_lines() == []


def test_an_inherited_order_is_shown_when_the_block_sets_none():
    policy = _policy(resolved={**SSH_G, "preferredauthentications": "publickey,password"})
    assert policy.shown_order() == ["publickey", "password"]
    assert policy.left_out() == ["gssapi-with-mic", "hostbased", "keyboard-interactive"]


def test_the_blocks_own_values_are_shown_and_win():
    policy = _policy(
        "PreferredAuthentications password,publickey",
        "PubkeyAuthentication no",
    )
    assert policy.order_is_set
    assert policy.shown_order() == ["password", "publickey"]
    assert policy.switch_is_set("publickey")
    assert policy.will_try() == ["password"]


def test_the_effective_order_is_unknown_until_openssh_has_answered():
    policy = AuthPolicy.from_lines([])
    assert policy.will_try() is None
    assert policy.shown_switch("password") is None


def test_an_earlier_block_decides_what_is_tried():
    policy = _policy("PreferredAuthentications password,publickey")
    policy.earlier = {"preferredauthentications": "publickey",
                      "pubkeyauthentication": "yes"}
    assert policy.shown_order() == ["password", "publickey"]  # the host's own
    assert policy.will_try() == ["publickey"]                  # what is used


def test_the_older_keyword_spelling_is_read_and_kept():
    policy = _policy("ChallengeResponseAuthentication no")
    assert policy.shown_switch("keyboard-interactive") is False
    assert policy.to_lines() == ["ChallengeResponseAuthentication no"]


def test_unknown_methods_and_repeated_lines_are_kept_as_written():
    policy = _policy(
        "PreferredAuthentications publickey,future-method,password",
        "PubkeyAuthentication yes",
        "PubkeyAuthentication no",
    )
    assert policy.unknown_tokens() == ["future-method"]
    assert policy.to_lines() == [
        "PreferredAuthentications publickey,future-method,password",
        "PubkeyAuthentication yes",
        "PubkeyAuthentication no",
    ]


# -- edits --------------------------------------------------------------------

def test_moving_a_row_writes_the_whole_order():
    policy = _policy()
    assert policy.move("password", 0)
    assert policy.to_lines() == [
        "PreferredAuthentications "
        "password,gssapi-with-mic,hostbased,publickey,keyboard-interactive",
    ]


def test_move_by_and_its_bounds():
    policy = _policy("PreferredAuthentications publickey,password")
    assert not policy.move_by("publickey", -1)
    assert policy.move_by("publickey", 1)
    assert policy.order == ["password", "publickey"]
    assert not policy.move_by("publickey", 1)


def test_settings_forced_options_decide_ahead_of_every_block():
    # Settings ▸ SSH Options pass these on the command line: they beat the
    # host's own lines and any earlier block.
    policy = _policy("PreferredAuthentications password,publickey")
    policy.earlier = {"preferredauthentications": "keyboard-interactive"}
    policy.forced = {"preferredauthentications": "publickey,password",
                     "passwordauthentication": "no"}
    assert policy.will_try() == ["publickey"]


def test_reset_order_removes_the_line_and_waits_for_the_inherited_one():
    policy = _policy("PreferredAuthentications password,publickey")
    assert policy.reset_order()
    assert policy.to_lines() == []
    # ssh -G answered for the block's own order, not what it would inherit.
    assert policy.will_try() is None


def test_a_switch_set_writes_its_line_and_reset_removes_it():
    policy = _policy()
    assert policy.set_switch("publickey", False)
    assert policy.to_lines() == ["PubkeyAuthentication no"]
    assert policy.reset_switch("publickey")
    assert policy.to_lines() == []
    # Never authored: the inherited value is known and shown again.
    assert policy.shown_switch("publickey") is True


def test_setting_the_inherited_value_explicitly_still_writes_it():
    policy = _policy()
    assert policy.set_switch("password", True)
    assert policy.to_lines() == ["PasswordAuthentication yes"]


def test_a_reset_authored_switch_is_unknown_until_saved():
    policy = _policy("GSSAPIAuthentication no")
    policy.reset_switch("gssapi-with-mic")
    assert policy.shown_switch("gssapi-with-mic") is None


# -- every method has a row; one the order leaves out is unchecked -------------

def test_every_method_has_a_row_and_a_left_out_one_is_unchecked():
    policy = _policy("PreferredAuthentications publickey,password")
    assert policy.rows() == [
        "publickey", "password", "gssapi-with-mic", "hostbased", "keyboard-interactive",
    ]
    assert policy.shown_checked("publickey") is True
    # Switched on by ssh -G, but not tried: the order leaves it out.
    assert policy.shown_checked("keyboard-interactive") is False


def test_checking_a_left_out_method_adds_it_to_the_end_of_the_order():
    policy = _policy("PreferredAuthentications publickey")
    assert policy.set_tried("password", True)
    assert policy.order == ["publickey", "password"]
    assert policy.shown_checked("password") is True
    # Its switch was already on: only the order line changes.
    assert policy.to_lines() == ["PreferredAuthentications publickey,password"]


def test_checking_a_left_out_switched_off_method_switches_it_on_too():
    policy = _policy("PreferredAuthentications publickey")
    assert policy.set_tried("hostbased", True)
    assert policy.to_lines() == [
        "PreferredAuthentications publickey,hostbased",
        "HostbasedAuthentication yes",
    ]


def test_unchecking_a_method_just_added_restores_the_authored_lines():
    lines = ("preferredauthentications  publickey",)
    policy = _policy(*lines)
    policy.set_tried("hostbased", True)
    assert policy.set_tried("hostbased", False)
    assert policy.to_lines() == [lines[0].strip()]
    assert policy.shown_checked("hostbased") is False


def test_unchecking_a_listed_method_switches_it_off():
    policy = _policy("PreferredAuthentications publickey,password")
    assert policy.set_tried("password", False)
    assert policy.to_lines() == [
        "PreferredAuthentications publickey,password",
        "PasswordAuthentication no",
    ]
    assert not policy.set_tried("keyboard-interactive", False)  # already not tried


def test_moving_a_left_out_method_lists_it_and_keeps_it_unchecked():
    policy = _policy("PreferredAuthentications publickey,password")
    assert policy.move("keyboard-interactive", 1)
    assert policy.order == ["publickey", "keyboard-interactive", "password"]
    assert policy.shown_checked("keyboard-interactive") is False
    assert "KbdInteractiveAuthentication no" in policy.to_lines()


def test_moving_a_left_out_method_among_the_left_out_ones_changes_nothing():
    policy = _policy("PreferredAuthentications publickey,password")
    assert not policy.move_by("hostbased", 1)
    assert policy.to_lines() == ["PreferredAuthentications publickey,password"]


def test_untouched_authored_text_is_kept_verbatim():
    lines = ["preferredauthentications  publickey,password", "pubkeyauthentication=yes"]
    assert _policy(*lines).to_lines() == [line.strip() for line in lines]


def test_a_changed_value_is_written_canonically():
    policy = _policy("pubkeyauthentication=yes")
    policy.set_switch("publickey", False)
    assert policy.to_lines() == ["PubkeyAuthentication no"]


# -- extra options text ---------------------------------------------------------

def test_policy_lines_are_split_from_the_other_options_and_joined_back():
    extra = "Compression yes\nPreferredAuthentications password\nGSSAPIAuthentication no"
    policy, other = split_policy_lines(extra)
    assert policy == ["PreferredAuthentications password", "GSSAPIAuthentication no"]
    assert other == "Compression yes"
    assert join_extra(other, policy).splitlines() == extra.splitlines()[:1] + policy


@pytest.mark.parametrize("value, expected", [
    ("yes", True), ("true", True), ("host-bound", True), ("no", False), ("false", False),
])
def test_switch_values_openssh_prints(value, expected):
    policy = _policy(resolved={**SSH_G, "pubkeyauthentication": value})
    assert policy.shown_switch("publickey") is expected


def test_reset_removes_every_copy_of_the_line():
    # Review: a second, ignored copy must not take effect after Reset.
    policy = _policy("PreferredAuthentications password",
                     "PreferredAuthentications publickey",
                     "PubkeyAuthentication no", "PubkeyAuthentication yes",
                     "GSSAPIAuthentication no", "GSSAPIAuthentication yes")
    assert policy.reset_order()
    assert policy.reset_switch("publickey")
    assert policy.to_lines() == ["GSSAPIAuthentication no", "GSSAPIAuthentication yes"]


def test_the_older_keyword_copy_goes_with_a_reset_too():
    policy = _policy("KbdInteractiveAuthentication no", "ChallengeResponseAuthentication yes")
    policy.reset_switch("keyboard-interactive")
    assert policy.to_lines() == []


# -- what a reset value, or a host not saved yet, inherits --------------------

def test_a_reset_order_shows_what_the_host_inherits_without_its_line():
    policy = _policy("PreferredAuthentications password,publickey")
    policy.reset_order()
    assert policy.will_try() is None            # ssh -G answered with the own line
    policy.set_fallback({"preferredauthentications": "publickey,keyboard-interactive"})
    assert policy.shown_order() == ["publickey", "keyboard-interactive"]
    assert policy.will_try() == ["publickey", "keyboard-interactive"]


def test_a_reset_switch_falls_back_to_the_inherited_line_or_the_default():
    policy = _policy("GSSAPIAuthentication yes", "PasswordAuthentication no")
    policy.reset_switch("gssapi-with-mic")
    policy.reset_switch("password")
    policy.set_fallback({"gssapiauthentication": "no"})
    assert policy.shown_switch("gssapi-with-mic") is False   # from another block
    assert policy.shown_switch("password") is True           # OpenSSH's default


def test_a_host_not_saved_yet_shows_what_a_new_block_inherits():
    policy = AuthPolicy.from_lines([])                       # ssh -G never answers
    policy.set_fallback({"pubkeyauthentication": "no"})
    assert policy.shown_switch("publickey") is False
    assert policy.shown_switch("hostbased") is False
    assert policy.shown_order() == list(DEFAULT_ORDER)
    # Kerberos and host-based are off by default, keys by the inherited line.
    assert policy.will_try() == ["keyboard-interactive", "password"]
