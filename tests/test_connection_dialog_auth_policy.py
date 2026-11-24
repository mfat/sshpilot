"""The authentication section's model: Host-block lines in, lines out.

Both forms are read: the order line and the per-method switches. A row is
checked when OpenSSH tries the method here: the order lists it and its
switch, the block's own or inherited, is not off. A listed method that is
switched off shows unchecked and keeps its place in the line.

Saving without an edit writes the block back exactly as authored. Moves and
unchecking edit only the order line; checking also turns the method's
switch on when it is off. Like any other field, edits change this Host
block only: an earlier block or Settings deciding a value never blocks or
changes the edit.
"""

import pytest

from sshpilot.connection_dialog_auth import (
    DEFAULT_ORDER,
    AuthPolicy,
    join_extra,
    split_policy_lines,
    method_caption,
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

def test_an_inherited_switch_that_is_off_unchecks_a_listed_method():
    # Ubuntu turns GSSAPI on and OpenSSH leaves host-based off: host-based
    # is listed here but not tried, so it is unchecked.
    policy = _policy(USER_LINE)
    assert policy.checked() == ["gssapi-with-mic", "publickey"]
    assert policy.shown_checked("hostbased") is False
    assert policy.shown_checked("password") is False
    assert policy.order == ["gssapi-with-mic", "hostbased", "publickey"]


def test_a_block_without_lines_checks_what_openssh_tries():
    policy = _policy()
    assert policy.checked() == policy.will_try() == [
        "gssapi-with-mic", "publickey", "keyboard-interactive", "password",
    ]
    assert policy.to_lines() == []


def test_an_inherited_order_is_shown_when_the_block_sets_none():
    policy = _policy(resolved={**SSH_G, "preferredauthentications": "publickey,password"})
    assert policy.checked() == ["publickey", "password"]
    assert policy.rows() == [
        "publickey", "password", "gssapi-with-mic", "hostbased", "keyboard-interactive",
    ]


def test_a_listed_method_the_block_switches_off_keeps_its_place():
    policy = _policy("PreferredAuthentications password,publickey", "PubkeyAuthentication no")
    assert policy.checked() == ["password"]
    assert policy.order == ["password", "publickey"]
    assert policy.own_switch("publickey") is False
    assert policy.own_switch("password") is None
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
    assert policy.own_switch("keyboard-interactive") is False
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
    assert policy.set_tried("gssapi-with-mic", False)
    assert policy.to_lines() == ["PreferredAuthentications hostbased,publickey"]


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
    policy.set_tried("password", True)
    policy.set_tried("password", False)
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


def test_moving_a_method_keeps_the_other_methods_settings():
    policy = _policy("PreferredAuthentications password,publickey,keyboard-interactive",
                     "PubkeyAuthentication no", "PasswordAuthentication yes")
    assert policy.move("keyboard-interactive", 0)
    assert policy.to_lines() == [
        "PreferredAuthentications keyboard-interactive,publickey,password",
        "PubkeyAuthentication no", "PasswordAuthentication yes",
    ]
    assert policy.own_switch("publickey") is False


def test_unchecking_never_touches_a_per_method_line():
    policy = _policy("PreferredAuthentications password,publickey",
                     "PubkeyAuthentication yes", "HostbasedAuthentication yes")
    assert policy.set_tried("publickey", False)
    assert policy.to_lines() == [
        "PreferredAuthentications password",
        "HostbasedAuthentication yes",
        "PubkeyAuthentication yes",
    ]
    assert policy.will_try() == ["password"]


def test_checking_touches_only_its_own_methods_line():
    policy = _policy("PreferredAuthentications password", "PubkeyAuthentication no",
                     "HostbasedAuthentication yes")
    assert policy.set_tried("publickey", True)
    assert policy.to_lines() == [
        "PreferredAuthentications password,publickey",
        "HostbasedAuthentication yes",
        "PubkeyAuthentication yes",  # no fallback known: on is written
    ]
    assert policy.will_try() == ["password", "publickey"]


def test_a_methods_setting_never_touches_the_order():
    policy = _policy("PreferredAuthentications password,publickey")
    assert policy.set_switch("password", False)
    assert policy.to_lines() == [
        "PreferredAuthentications password,publickey", "PasswordAuthentication no",
    ]
    assert policy.checked() == ["publickey"]
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
    policy = _policy("PreferredAuthentications publickey,password,keyboard-interactive")
    assert policy.set_tried("password", False)
    assert policy.set_switch("keyboard-interactive", False)
    assert policy.set_tried("password", True)
    assert policy.to_lines() == [
        "PreferredAuthentications publickey,keyboard-interactive,password",
        "KbdInteractiveAuthentication no",
    ]


def test_a_setting_a_linked_profile_owns_cannot_be_changed_here():
    policy = _policy("PreferredAuthentications password", "PubkeyAuthentication no")
    policy.locked_methods = {"publickey"}
    assert not policy.set_switch("publickey", True)
    assert not policy.set_switch("publickey", None)
    assert policy.to_lines() == ["PreferredAuthentications password", "PubkeyAuthentication no"]
    # Checking it would turn it on, which the profile decides.
    assert not policy.set_tried("publickey", True)
    # The order is the connection's own unless the profile sets that too.
    policy = _policy("PreferredAuthentications password", "PubkeyAuthentication yes")
    policy.locked_methods = {"publickey"}
    assert policy.set_tried("publickey", True)
    assert policy.to_lines() == [
        "PreferredAuthentications password,publickey", "PubkeyAuthentication yes",
    ]


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
    assert policy.checked() == ["keyboard-interactive", "password"]
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
    # Keys are switched off here, so they are not moved over.
    (lambda p: p.move("keyboard-interactive", 0), "keyboard-interactive,publickey,password"),
    (lambda p: p.set_tried("password", False), "publickey,keyboard-interactive"),
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
        lambda p: (p.set_fallback({"gssapiauthentication": "no"}),
                   p.set_tried("gssapi-with-mic", True)),
    )
    assert resolved["gssapiauthentication"] == "yes"
    assert reopened.own_switch("gssapi-with-mic") is True
    assert reopened.order is None  # the order was not written
    assert "PreferredAuthentications" not in text


def test_password_is_enabled_over_a_later_wildcard_and_moved_first(tmp_path):
    def edit(policy):
        # What the host inherits: the wildcard's "no".
        policy.set_fallback({"passwordauthentication": "no"})
        assert policy.shown_checked("password") is False
        assert policy.set_tried("password", True)
        assert policy.move_to_top("password")

    resolved, reopened, _text = _saved(
        tmp_path,
        "Host demo\n    HostName demo.example\n\nHost *\n    PasswordAuthentication no\n",
        edit,
    )
    assert resolved["passwordauthentication"] == "yes"
    # Moved to the top, it goes ahead of Kerberos and host-based too
    # (off, so not tried): turning one on later does not put it first.
    assert resolved["preferredauthentications"] == (
        "password,gssapi-with-mic,hostbased,publickey,keyboard-interactive"
    )
    assert resolved["gssapiauthentication"] == resolved["hostbasedauthentication"] == "no"
    assert reopened.own_switch("password") is True
    reopened.set_inherited(resolved)
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



# -- checked means tried --------------------------------------------------------

def _host(lines, resolved=None):
    policy = AuthPolicy.from_lines(lines)
    policy.set_inherited(resolved or SSH_G)
    policy.set_fallback(SSH_G)
    return policy


def test_a_listed_method_switched_off_here_is_unchecked_and_keeps_its_place():
    policy = _host(["PreferredAuthentications publickey,password",
                    "PubkeyAuthentication no"])
    assert policy.shown_checked("publickey") is False
    assert policy.checked() == ["password"]
    assert policy.order == ["publickey", "password"]
    assert policy.will_try() == ["password"]


def test_a_method_off_by_default_is_unchecked():
    # Nothing configured: OpenSSH lists Kerberos and host-based first but
    # leaves both off, so they are not tried.
    policy = _host([], dict(SSH_G, gssapiauthentication="no"))
    assert policy.shown_checked("gssapi-with-mic") is False
    assert policy.shown_checked("hostbased") is False
    assert policy.checked() == policy.will_try()


def test_checking_a_method_switched_off_here_drops_the_no_line():
    policy = _host(["PreferredAuthentications publickey,password",
                    "PubkeyAuthentication no"])
    assert policy.set_tried("publickey", True)
    assert policy.to_lines() == ["PreferredAuthentications publickey,password"]
    assert policy.checked() == ["publickey", "password"]


def test_checking_a_method_off_by_inheritance_writes_yes_at_its_place():
    policy = _host([], dict(SSH_G, hostbasedauthentication="no"))
    assert policy.set_tried("hostbased", True)
    # It was listed already, so the order is left to OpenSSH.
    assert policy.to_lines() == ["HostbasedAuthentication yes"]
    assert policy.shown_checked("hostbased") is True


def test_checking_an_unlisted_method_off_here_lists_it_and_turns_it_on():
    policy = _host(["PreferredAuthentications publickey",
                    "PasswordAuthentication no"])
    assert policy.set_tried("password", True)
    assert policy.to_lines() == ["PreferredAuthentications publickey,password"]
    assert policy.will_try() == ["publickey", "password"]


def test_unchecking_writes_no_switch_line():
    policy = _host(["PreferredAuthentications publickey,password"])
    assert policy.set_tried("password", False)
    assert policy.to_lines() == ["PreferredAuthentications publickey"]


def test_unchecking_a_method_only_its_own_yes_turns_on_drops_that_line():
    policy = _host(["GSSAPIAuthentication yes"], dict(SSH_G, gssapiauthentication="no"))
    policy.set_fallback(dict(SSH_G, gssapiauthentication="no"))
    assert policy.shown_checked("gssapi-with-mic") is True
    assert policy.set_tried("gssapi-with-mic", False)
    assert policy.to_lines() == []
    assert "gssapi-with-mic" not in policy.will_try()


def test_moving_skips_a_listed_method_that_is_switched_off():
    policy = _host(["PreferredAuthentications publickey,password,keyboard-interactive",
                    "PasswordAuthentication no"])
    assert policy.checked() == ["publickey", "keyboard-interactive"]
    assert policy.move("keyboard-interactive", 0)
    assert policy.order == ["keyboard-interactive", "password", "publickey"]


def test_a_switch_a_profile_sets_off_cannot_be_checked_here():
    policy = _host(["PubkeyAuthentication no"])
    policy.locked_methods = {"publickey"}
    assert not policy.can_set_tried("publickey")
    assert not policy.set_tried("publickey", True)


def test_a_listed_method_is_unknown_until_its_switch_arrives():
    policy = AuthPolicy.from_lines(["PreferredAuthentications publickey,password"])
    assert policy.shown_checked("publickey") is None
    assert not policy.set_tried("publickey", False)


def test_only_a_restricted_switch_gets_a_caption():
    assert method_caption("", False) == ""
    assert method_caption("host-bound", False) == "Enabled for this host (host-bound)"
    assert method_caption("unbound", True) == "Enabled in this profile (unbound)"


def test_move_to_top_puts_the_method_ahead_of_switched_off_ones():
    policy = _host([], dict(SSH_G, gssapiauthentication="no"))
    assert policy.checked()[0] == "publickey"   # Kerberos, host-based: off
    assert policy.move_to_top("password")
    assert policy.to_lines() == [
        "PreferredAuthentications "
        "password,gssapi-with-mic,hostbased,publickey,keyboard-interactive",
    ]


def test_check_then_uncheck_keeps_a_method_that_was_listed():
    # Review: the undo took password out of the order because an order
    # edit (the move) was pending; only checking's own insertion goes.
    policy = _host(["PreferredAuthentications publickey,password,keyboard-interactive",
                    "PasswordAuthentication no"])
    assert policy.move("keyboard-interactive", 0)
    assert policy.order == ["keyboard-interactive", "password", "publickey"]
    assert policy.set_tried("password", True)
    assert policy.set_tried("password", False)
    assert policy.order == ["keyboard-interactive", "password", "publickey"]
    assert "PasswordAuthentication no" in policy.to_lines()


def test_a_row_says_when_an_earlier_setting_overrules_its_checkbox():
    # Review: an earlier Host * turns password off; this block turns it on.
    policy = _host(["PasswordAuthentication yes"])
    policy.earlier = {"passwordauthentication": "no"}
    assert policy.shown_checked("password") is True
    assert "password" not in policy.will_try()
    assert policy.overruled("password") is True
    assert method_caption("", False, True) == (
        "Checked, but not tried: an earlier setting turns it off"
    )
    # And the other way: Settings force an order that lists it.
    policy = _host(["PreferredAuthentications publickey"])
    policy.forced = {"preferredauthentications": "publickey,password"}
    assert policy.shown_checked("password") is False
    assert policy.overruled("password") is False
    assert policy.overruled("publickey") is None


def test_move_to_top_passes_switched_off_methods_when_already_first_tried():
    policy = _host([], dict(SSH_G, gssapiauthentication="no"))
    assert policy.checked()[0] == "publickey"
    assert policy.move_to_top("publickey")
    assert policy.to_lines() == [
        "PreferredAuthentications "
        "publickey,gssapi-with-mic,hostbased,keyboard-interactive,password",
    ]
    assert not policy.move_to_top("publickey")   # already first in the line


# -- review: reset, undo and round trips ----------------------------------------

def test_unchecking_after_a_reset_does_not_fail():
    # Checking listed password and turned it on; Reset discards the order,
    # and the uncheck that follows only puts the switch line back.
    policy = AuthPolicy.from_lines(["PreferredAuthentications publickey",
                                    "PasswordAuthentication no"])
    policy.set_inherited(dict(SSH_G, preferredauthentications="publickey"))
    policy.set_fallback({"preferredauthentications": "publickey,password"})
    assert policy.set_tried("password", True)
    assert policy.reset_order()
    assert policy.shown_checked("password") is True
    assert policy.set_tried("password", False)
    assert policy.to_lines() == ["PasswordAuthentication no"]


def test_move_to_top_past_a_switched_off_method_survives_other_edits():
    policy = _host(["PreferredAuthentications password,publickey,keyboard-interactive",
                    "PasswordAuthentication no"])
    assert policy.move_to_top("publickey")
    moved = "PreferredAuthentications publickey,password,keyboard-interactive"
    assert moved in policy.to_lines()
    assert policy.set_tried("keyboard-interactive", False)
    assert policy.set_tried("keyboard-interactive", True)
    assert moved in policy.to_lines()
    assert policy.move_by("keyboard-interactive", -1)
    assert policy.move_by("keyboard-interactive", 1)
    assert moved in policy.to_lines()


def test_only_move_to_top_passes_switched_off_methods():
    policy = _host(["PreferredAuthentications publickey,gssapi-with-mic,password,"
                    "keyboard-interactive", "GSSAPIAuthentication no"])
    assert policy.move_by("keyboard-interactive", -1)
    assert policy.order == ["publickey", "gssapi-with-mic", "keyboard-interactive", "password"]
    # Moving it back is an exact undo, also into the first place.
    assert policy.move_by("keyboard-interactive", 1)
    assert not policy.edited
    policy = _host([], dict(SSH_G, gssapiauthentication="no"))
    assert policy.move("password", 0) and policy.move("password", 2)
    assert policy.to_lines() == []


def test_edits_that_cancel_out_write_nothing():
    policy = _host([], dict(SSH_G, gssapiauthentication="no"))
    policy.set_fallback(dict(SSH_G, gssapiauthentication="no"))
    assert policy.set_tried("password", False)
    assert policy.set_tried("gssapi-with-mic", True)
    assert policy.set_tried("password", True)
    assert policy.set_tried("gssapi-with-mic", False)
    assert policy.to_lines() == [] and not policy.edited


def test_check_then_uncheck_leaves_the_blocks_own_yes_line():
    policy = _host(["PreferredAuthentications publickey", "PasswordAuthentication yes"])
    policy.set_fallback(dict(SSH_G, passwordauthentication="no"))
    assert policy.set_tried("password", True)
    assert policy.set_tried("password", False)
    assert policy.to_lines() == ["PreferredAuthentications publickey",
                                 "PasswordAuthentication yes"]


def test_uncheck_then_check_restores_a_restricted_switch_as_written():
    policy = _host(["PubkeyAuthentication host-bound"])
    policy.set_fallback(dict(SSH_G, pubkeyauthentication="no"))
    assert policy.set_tried("publickey", False)
    assert policy.to_lines() == []
    assert policy.set_tried("publickey", True)
    assert policy.to_lines() == ["PubkeyAuthentication host-bound"]
    assert policy.restriction("publickey") == "host-bound"


def test_a_switch_openssh_does_not_report_is_at_its_default():
    # An OpenSSH built without GSSAPI prints no gssapiauthentication line.
    resolved = {k: v for k, v in SSH_G.items() if k != "gssapiauthentication"}
    policy = AuthPolicy.from_lines([])
    policy.set_inherited(resolved)
    assert policy.shown_checked("gssapi-with-mic") is False
    assert "gssapi-with-mic" not in policy.checked()


def test_the_overruled_caption_names_the_order_when_the_order_is_the_cause():
    policy = _host(["PreferredAuthentications publickey,password"])
    policy.earlier = {"preferredauthentications": "publickey"}
    assert policy.overruled("password") is True
    assert policy.overruled_by_order("password")
    assert method_caption("", False, True, True) == (
        "Checked, but not tried: an earlier setting leaves it out of the order"
    )
    policy = _host(["PasswordAuthentication yes"])
    policy.earlier = {"passwordauthentication": "no"}
    assert policy.overruled("password") is True
    assert not policy.overruled_by_order("password")


# -- rows stay where they are while editing --------------------------------------

DEFAULT_OFF = dict(SSH_G, gssapiauthentication="no")


def _default_host():
    policy = _host([], DEFAULT_OFF)
    policy.set_fallback(DEFAULT_OFF)
    return policy


def test_rows_open_with_the_checked_methods_first():
    assert _default_host().rows() == [
        "publickey", "keyboard-interactive", "password", "gssapi-with-mic", "hostbased",
    ]


def test_an_unchecked_row_stays_where_it_is():
    policy = _default_host()
    before = policy.rows()
    assert policy.set_tried("keyboard-interactive", False)
    assert policy.rows() == before
    assert policy.checked() == ["publickey", "password"]


def test_checking_it_again_puts_it_back_in_the_order_at_its_row():
    policy = _default_host()
    assert policy.set_tried("keyboard-interactive", False)
    assert policy.set_tried("keyboard-interactive", True)
    assert policy.checked() == ["publickey", "keyboard-interactive", "password"]
    assert policy.to_lines() == [] and not policy.edited   # an exact undo


def test_a_listed_method_checked_goes_to_its_place_in_the_line():
    # Kerberos is first in OpenSSH's order: checked, it is tried first, and
    # its row says so. The line is not rewritten for it.
    policy = _default_host()
    assert policy.set_tried("password", False)
    assert policy.set_tried("gssapi-with-mic", True)
    assert policy.rows()[0] == "gssapi-with-mic"
    assert policy.rows().index("password") == 3   # still where it was left


def test_a_row_can_be_dragged_anywhere():
    policy = _default_host()
    # A checked row: the order follows.
    assert policy.place_row("password", 0)
    assert policy.rows()[:3] == ["password", "publickey", "keyboard-interactive"]
    assert policy.checked() == ["password", "publickey", "keyboard-interactive"]
    # An unchecked row: only the list changes, nothing to save.
    lines = policy.to_lines()
    assert policy.place_row("hostbased", 1)
    assert policy.rows()[1] == "hostbased" and policy.to_lines() == lines
    # Checked there, an unlisted method joins the order at that place.
    policy = _host(["PreferredAuthentications publickey,password"])
    assert policy.place_row("keyboard-interactive", 1)
    assert policy.to_lines() == ["PreferredAuthentications publickey,password"]
    assert policy.set_tried("keyboard-interactive", True)
    assert policy.order == ["publickey", "keyboard-interactive", "password"]
    assert not policy.place_row("keyboard-interactive", 1)   # already there


def test_a_reset_sorts_the_rows_again():
    policy = _host(["PreferredAuthentications publickey,keyboard-interactive,password"])
    assert policy.set_tried("keyboard-interactive", False)
    assert policy.rows()[1] == "keyboard-interactive"
    assert policy.reset_order()
    assert policy.rows()[:len(policy.checked())] == policy.checked()


def test_a_listed_method_dragged_while_unchecked_is_tried_where_it_was_dropped():
    # Review: dropped above keys and then checked, password jumped back
    # behind them, because the line already listed it there.
    lines = ["PreferredAuthentications publickey,password", "PasswordAuthentication no"]
    policy = _host(lines)
    assert policy.place_row("password", 0)
    assert policy.to_lines() == lines            # an unchecked row: nothing to save
    assert policy.set_tried("password", True)
    assert policy.rows()[:2] == ["password", "publickey"]
    assert policy.to_lines() == ["PreferredAuthentications password,publickey"]
    # Unchecking takes both back: the switch line and the place in the line.
    assert policy.set_tried("password", False)
    assert policy.to_lines() == lines and not policy.edited
    assert policy.rows()[0] == "password"        # the row stays where it was put
