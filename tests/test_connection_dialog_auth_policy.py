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
    assert policy.not_tried() == ["gssapi-with-mic", "hostbased", "keyboard-interactive"]


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


def test_add_back_puts_a_left_out_method_at_the_end():
    policy = _policy("PreferredAuthentications publickey")
    assert policy.add_back("password")
    assert policy.order == ["publickey", "password"]


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
