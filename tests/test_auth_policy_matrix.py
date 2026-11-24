"""Every authentication edit, on every kind of block, saved and asked of ssh.

``test_connection_dialog_auth_policy.py`` states each rule on a chosen case.
This is the same rules as a grid: every way a block can already be written,
crossed with every edit the section offers, in three positions relative to a
``Host *`` that has its own authentication lines. For each cell:

* the edit changes only its own lines: a move or a reset leaves every
  per-method line as written; checking or unchecking a method may change
  that method's own line and no other; nothing touches keys, certificates
  or agent options;
* a checked method is tried here, an unchecked one is not;
* the wildcard block is never touched;
* the edited block keeps the user's choice even when an earlier block wins;
* real ``ssh -G`` resolves what first-wins over the three blocks predicts;
* reopening shows the saved choice, and saving again changes nothing.

The model-only half runs the whole grid; the half that saves through the
store and runs ``ssh`` takes a spread of edits per block.
"""

import itertools
import shutil
import subprocess

import pytest

from sshpilot.connection_dialog_auth import (
    KEYWORD_OF,
    METHOD_OF_KEYWORD,
    METHODS,
    AuthPolicy,
    join_extra,
    parse_order,
    parse_switch,
    split_policy_lines,
)
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore

ORDER = "preferredauthentications"

# How a block can already be written.
STARTS = {
    "nothing": [],
    "key-then-code": ["PreferredAuthentications publickey,keyboard-interactive"],
    "password-first": [
        "PreferredAuthentications keyboard-interactive,password,gssapi-with-mic,"
        "hostbased,publickey",
    ],
    "one-method": ["PreferredAuthentications password"],
    "unknown-method": ["PreferredAuthentications publickey,my-method,password"],
    "repeated-method": ["PreferredAuthentications publickey,password,publickey"],
    "keys-off": ["PubkeyAuthentication no"],
    "password-off": ["PasswordAuthentication no"],
    "kerberos-on": ["GSSAPIAuthentication yes"],
    "older-spelling": ["ChallengeResponseAuthentication no"],
    "odd-case": ["preferredauthentications   PUBLICKEY,Password", "pubkeyauthentication YES"],
    "host-bound": ["PubkeyAuthentication host-bound"],
    "off-but-listed": [
        "PreferredAuthentications publickey,password",
        "PubkeyAuthentication no",
    ],
    "set-but-not-listed": [
        "PreferredAuthentications publickey",
        "PasswordAuthentication yes",
        "KbdInteractiveAuthentication no",
    ],
    "two-orders": [
        "PreferredAuthentications password,publickey",
        "PreferredAuthentications publickey",
    ],
    "conflicting-switch": ["PasswordAuthentication no", "PasswordAuthentication yes"],
    "both-spellings": [
        "KbdInteractiveAuthentication yes",
        "ChallengeResponseAuthentication no",
    ],
    "everything": [
        "PreferredAuthentications publickey,keyboard-interactive,password",
        "GSSAPIAuthentication no",
        "HostbasedAuthentication no",
        "PubkeyAuthentication yes",
        "KbdInteractiveAuthentication yes",
        "PasswordAuthentication no",
    ],
}

# Every edit the section offers.
EDITS = {"reset-order": lambda p: p.reset_order()}
for _method in METHODS:
    for _index in range(len(METHODS)):
        EDITS[f"move-{_method}-to-{_index}"] = (
            lambda p, m=_method, i=_index: p.move(m, i)
        )
    for _value in (True, False):
        EDITS[f"{'check' if _value else 'uncheck'}-{_method}"] = (
            lambda p, m=_method, v=_value: p.set_tried(m, v)
        )

ORDER_EDITS = [n for n in EDITS if n.startswith(("move-", "reset-"))]
CHECK_EDITS = [n for n in EDITS if n.startswith("check-")]
UNCHECK_EDITS = [n for n in EDITS if n.startswith("uncheck-")]

# A method's own line, which checking writes through: not offered by the
# section on its own, but every way of setting it must hold.
SWITCH_EDITS = {}
for _method in METHODS:
    for _name, _value in (("inherit", None), ("enable", True), ("disable", False)):
        SWITCH_EDITS[f"{_name}-{_method}"] = (
            lambda p, m=_method, v=_value: p.set_switch(m, v)
        )

# What a default installation answers when nothing is configured.
DEFAULTS = {
    "gssapiauthentication": "no",
    "hostbasedauthentication": "no",
    "pubkeyauthentication": "true",
    "kbdinteractiveauthentication": "yes",
    "passwordauthentication": "yes",
}


def _keyword(line):
    return line.split(None, 1)[0].lower()


def _norm(lines):
    """Lines with the keyword's case and the spacing set aside: the loader
    hands keywords over in lower case; the file keeps them as written."""
    return [(_keyword(line), " ".join(line.split()[1:])) for line in lines]


def _method_of(line):
    return METHOD_OF_KEYWORD.get(_keyword(line))


def _order_lines(lines):
    return [line for line in lines if _keyword(line) == ORDER]


def _switch_lines(lines, method=None):
    """Per-method lines. Lines for one method keep their order (the first
    is the one in force); which method comes first means nothing."""
    found = [
        line for line in lines
        if _method_of(line) is not None and (method is None or _method_of(line) == method)
    ]
    return sorted(found, key=lambda line: METHODS.index(_method_of(line)))


def _loaded(start):
    policy = AuthPolicy.from_lines(STARTS[start])
    policy.set_inherited(DEFAULTS)
    policy.set_fallback(DEFAULTS)
    return policy


# -- the whole grid, model only ------------------------------------------------

@pytest.mark.parametrize("start", sorted(STARTS))
def test_an_unedited_block_is_written_back_as_authored(start):
    lines = _loaded(start).to_lines()
    assert _order_lines(lines) == _order_lines(STARTS[start])
    assert _switch_lines(lines) == _switch_lines(STARTS[start])
    assert sorted(lines) == sorted(STARTS[start])


@pytest.mark.parametrize("start", sorted(STARTS))
@pytest.mark.parametrize("edit", ORDER_EDITS)
def test_an_order_edit_leaves_every_per_method_line_as_written(start, edit):
    policy = _loaded(start)
    changed = EDITS[edit](policy)
    lines = policy.to_lines()
    assert _switch_lines(lines) == _switch_lines(STARTS[start])
    if not changed:
        assert sorted(lines) == sorted(STARTS[start]), "an edit that did nothing changed the lines"
    assert len(_order_lines(lines)) <= max(1, len(_order_lines(STARTS[start])))
    if policy.edited or edit == "reset-order" and changed:
        # An edited order is one line, or none: an ignored copy must not
        # be left to take its place.
        assert len(_order_lines(lines)) <= 1


@pytest.mark.parametrize("start", sorted(STARTS))
@pytest.mark.parametrize("edit", CHECK_EDITS)
def test_checking_changes_only_its_own_methods_line_and_it_is_tried(start, edit):
    method = edit.split("-", 1)[1]
    policy = _loaded(start)
    changed = EDITS[edit](policy)
    lines = policy.to_lines()
    for other in METHODS:
        if other != method:
            assert _switch_lines(lines, other) == _switch_lines(STARTS[start], other)
    assert policy.shown_checked(method) is True
    assert method in policy.will_try()
    if not changed:
        assert sorted(lines) == sorted(STARTS[start]), "an edit that did nothing changed the lines"
    assert len(_order_lines(lines)) <= max(1, len(_order_lines(STARTS[start])))


@pytest.mark.parametrize("start", sorted(STARTS))
@pytest.mark.parametrize("method", METHODS)
def test_checking_then_unchecking_writes_the_authored_lines(start, method):
    policy = _loaded(start)
    if not policy.set_tried(method, True):
        return
    if policy.set_tried(method, False):
        assert sorted(policy.to_lines()) == sorted(STARTS[start])


@pytest.mark.parametrize("start", sorted(STARTS))
@pytest.mark.parametrize("method", METHODS)
def test_unchecking_means_not_tried(start, method):
    policy = _loaded(start)
    if policy.set_tried(method, False):
        assert policy.shown_checked(method) is False
        assert method not in policy.will_try()
        lines = policy.to_lines()
        for other in METHODS:
            if other != method:
                assert _switch_lines(lines, other) == _switch_lines(STARTS[start], other)
        # Never an "off" line: at most this block's own "yes" goes.
        own = _switch_lines(lines, method)
        assert own == _switch_lines(STARTS[start], method) or own == []
    else:
        # Refused only when nothing else would be tried.
        assert policy.shown_checked(method) is False or policy.checked() == [method]


@pytest.mark.parametrize("start", sorted(STARTS))
@pytest.mark.parametrize("edit", SWITCH_EDITS)
def test_a_methods_setting_leaves_the_order_and_the_other_methods(start, edit):
    method = edit.split("-", 1)[1]
    policy = _loaded(start)
    changed = SWITCH_EDITS[edit](policy)
    lines = policy.to_lines()
    assert _order_lines(lines) == _order_lines(STARTS[start])
    for other in METHODS:
        if other != method:
            assert _switch_lines(lines, other) == _switch_lines(STARTS[start], other)
    own = _switch_lines(lines, method)
    if not changed:
        assert own == _switch_lines(STARTS[start], method)
    elif edit.startswith("inherit-"):
        assert own == [], "Not set must leave no line of any spelling"
    else:
        wanted = edit.startswith("enable-")
        assert len(own) == 1 and parse_switch(own[0].split(None, 1)[1]) is wanted


@pytest.mark.parametrize("start", sorted(STARTS))
@pytest.mark.parametrize("edit", sorted(EDITS))
def test_what_is_written_reopens_as_the_same_choice(start, edit):
    policy = _loaded(start)
    EDITS[edit](policy)
    lines = policy.to_lines()
    again = AuthPolicy.from_lines(lines)
    assert again.order == policy.order
    assert again.switches == policy.switches
    assert again.to_lines() == lines, "saving again would change the block"


@pytest.mark.parametrize("start", sorted(STARTS))
def test_undoing_an_order_edit_writes_the_authored_lines(start):
    for method, index in itertools.product(METHODS, range(len(METHODS))):
        policy = _loaded(start)
        before = policy.checked()
        if method not in before or not policy.move(method, index):
            continue
        assert policy.move(method, before.index(method))
        assert sorted(policy.to_lines()) == sorted(STARTS[start]), (method, index)


# -- through the store, asked of ssh --------------------------------------------

OTHER_LINES = [
    "IdentityFile ~/.ssh/id_one",
    "IdentityFile ~/.ssh/id_two",
    "IdentitiesOnly yes",
    "CertificateFile ~/.ssh/id_one-cert.pub",
    "IdentityAgent ~/.agent.sock",
    "AddKeysToAgent 1h",
    "Compression yes",
]
WILDCARD = [
    "PreferredAuthentications hostbased,password",
    "PasswordAuthentication no",
    "GSSAPIAuthentication yes",
]
POSITIONS = ("alone", "wildcard-first", "wildcard-last")

# A spread of edits: one of each kind, on the methods that matter most.
SAVED_EDITS = [
    "move-password-to-0",
    "move-publickey-to-4",
    "check-keyboard-interactive",
    "uncheck-publickey",
    "reset-order",
    "check-gssapi-with-mic",
    "check-hostbased",
    "check-password",
    "check-publickey",
    "uncheck-password",
]


def _config(start, position):
    host = ["Host demo", "    HostName 192.0.2.9"]
    host += [f"    {line}" for line in OTHER_LINES + STARTS[start]]
    wildcard = ["Host *"] + [f"    {line}" for line in WILDCARD]
    blocks = {
        "alone": [host],
        "wildcard-first": [wildcard, host],
        "wildcard-last": [host, wildcard],
    }[position]
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


def _block(text, header):
    lines = text.split("\n")
    start = lines.index(header)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("Host ")),
        len(lines),
    )
    return [line.strip() for line in lines[start + 1:end] if line.strip()]


def _ssh_g(path):
    out = subprocess.run(
        ["ssh", "-G", "-F", str(path), "demo"],
        capture_output=True, text=True, timeout=20,
    )
    assert out.returncode == 0, out.stderr
    return dict(line.split(" ", 1) for line in out.stdout.splitlines() if " " in line)


def _first_wins(sources, read):
    for source in sources:
        value = read(source)
        if value is not None:
            return value
    return None


@pytest.fixture(scope="module")
def unconfigured(tmp_path_factory):
    """What this machine's ssh answers for a host with no authentication lines."""
    if shutil.which("ssh") is None:
        pytest.skip("needs the OpenSSH client")
    path = tmp_path_factory.mktemp("bare") / "config"
    path.write_text("Host demo\n    HostName 192.0.2.9\n", encoding="utf-8")
    return _ssh_g(path)


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("start", sorted(STARTS))
def test_saved_edits_reach_openssh_and_nothing_else_changes(
    tmp_path, unconfigured, start, position
):
    wildcard = AuthPolicy.from_lines(WILDCARD)
    for number, edit in enumerate(SAVED_EDITS):
        directory = tmp_path / str(number)
        directory.mkdir()
        config = directory / "config"
        original = _config(start, position)
        config.write_text(original, encoding="utf-8")

        def repository():
            return ConnectionRepository(
                ssh_store=SshConfigStore(config),
                state_path=directory / "connections.json",
                legacy_config_path=directory / "config.json",
                isolated=False,
            )

        repo = repository()
        record = repo.get_record("demo")
        lines, other = split_policy_lines(record.data.get("extra_ssh_config") or "")
        assert _norm(lines) == _norm(STARTS[start]), (
            "the block's authentication lines were not all read"
        )
        policy = AuthPolicy.from_lines(lines)
        policy.set_inherited(_ssh_g(config))
        policy.set_fallback(unconfigured)
        changed = EDITS[edit](policy)
        repo.update_connection(
            "demo",
            {**record.data, "extra_ssh_config": join_extra(other, policy.to_lines())},
        )
        saved = config.read_text(encoding="utf-8")
        context = f"{start} / {position} / {edit}"

        if not changed:
            assert saved == original, f"an edit that did nothing rewrote the file ({context})"
        # The wildcard block is not this host's to change.
        if position != "alone":
            assert _block(saved, "Host *") == WILDCARD, context
        # Keys, certificates and agent options are as written, in order.
        block = _block(saved, "Host demo")
        kept = [line for line in block if line in OTHER_LINES]
        assert kept == OTHER_LINES, context
        # The block holds exactly the user's choice, whoever wins.
        written = [
            line for line in block
            if _keyword(line) == ORDER or _method_of(line) is not None
        ]
        assert sorted(_norm(written)) == sorted(_norm(policy.to_lines())), context

        # ssh agrees with first-wins over the blocks, in file order.
        sources = {
            "alone": [policy],
            "wildcard-first": [wildcard, policy],
            "wildcard-last": [policy, wildcard],
        }[position]
        resolved = _ssh_g(config)
        order = _first_wins(sources, lambda p: p.order)
        if order is None:
            assert resolved.get(ORDER) == unconfigured.get(ORDER), context
        else:
            assert parse_order(resolved.get(ORDER)) == order, context
        for method in METHODS:
            keyword = KEYWORD_OF[method].lower()
            value = _first_wins(sources, lambda p, m=method: p.switches.get(m))
            if value is None:
                assert resolved[keyword] == unconfigured[keyword], f"{context} / {keyword}"
            else:
                assert parse_switch(resolved[keyword]) is value, f"{context} / {keyword}"

        # Reopen: the same choice, and a save without an edit writes nothing.
        again = repository()
        record = again.get_record("demo")
        lines, other = split_policy_lines(record.data.get("extra_ssh_config") or "")
        reopened = AuthPolicy.from_lines(lines)
        assert reopened.order == policy.order, context
        assert reopened.switches == policy.switches, context
        again.update_connection(
            "demo",
            {**record.data, "extra_ssh_config": join_extra(other, reopened.to_lines())},
        )
        assert config.read_text(encoding="utf-8") == saved, (
            f"saving again without an edit changed the file ({context})"
        )
