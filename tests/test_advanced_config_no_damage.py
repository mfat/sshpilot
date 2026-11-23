"""Advanced Configuration must not damage a configuration it edits.

It rewrites the user's whole ssh config, so the failure that matters is not
a wrong label but a file that says something else afterwards. The other
suites check chosen edits on chosen text; this one sweeps every block move,
every switch and long random runs of edits over awkward configurations, and
holds four things true throughout:

* an edit touches only its own lines: a move reorders lines, it rewrites none;
* what the editor shows is what OpenSSH reads: no option changes owner;
* what it writes reads back as the same blocks (save, close, reopen);
* undo returns the exact text, byte for byte.

One test asks the real ``ssh`` rather than the app's own reading of the file.
"""

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from sshpilot.advanced_config_model import DraftEditor, Refusal
from sshpilot.ssh_config_blocks import GLOBAL, HOST, MATCH, parse, scan_blocks

GOLDEN = (
    Path(__file__).parent / "fixtures" / "ssh_config" / "golden_root.conf"
).read_text(encoding="utf-8")

CORPUS = {
    "golden": GOLDEN,
    "plain": "Host a\n    User x\n\nHost b\n    Port 1\n\nHost c\n    Port 2\n",
    "globals": (
        "# top\nCompression yes\nServerAliveInterval 30\n\n# web\n"
        "Host web *.prod !bad   # note\n\tUser deploy # who\n    Port=2222\n\n\n"
        'Match host x exec "test -f /a b"\n  ProxyJump j\n# tail\n'
    ),
    "crlf": "Host a\r\n  User x\r\n\r\nHost b\r\n  Port 1\r\n\r\nHost c\r\n  Port 3",
    "mixed-endings": "Host a\r\n  User x\n\nHost b\n  Port 1\r\n",
    "bom": "﻿Host a\n  User b\n\nHost c\n  User d\n",
    "no-final-newline": "Host a\n  User x\nHost b\n  Port 1",
    "no-blank-lines": "Compression yes\nHost a\n  User x\nHost b\n  Port 1\nMatch all\n  Port 9\n",
    "switched-off": (
        "# [disabled] Host old\n# [disabled]     User o\n# plain comment\n"
        "Host new\n    User n\n\n# [disabled] Host later\n# [disabled]     Port 5\n"
    ),
    "off-global": "# [disabled global section]\n# [disabled] Compression yes\nHost a\n  User b\n",
    "include": "Include conf.d/*\nHost *\n  SendEnv LANG\n\nHost a\n  Include extra\n  User z\n",
    "quoted": '"Match" exec "true"\n  "User" x\nhost lower\n\tuser=y\nHOST UPPER\n  PORT   22\n',
    "comments-inside": (
        "Host a\n    # about the user\n    User x\n\n    # about the port\n    Port 1\n"
        "# between\n\nHost b\n    User y   # trailing\n"
    ),
    "repeated": (
        "Host a\n    IdentityFile ~/.ssh/one\n    IdentityFile ~/.ssh/two\n"
        "    LocalForward 1 h:1\n    LocalForward 2 h:2\n\nHost a\n    User second\n"
    ),
}
NAMES = sorted(CORPUS)


def _editor(text):
    editor = DraftEditor()
    editor.load(text)
    return editor


def _lines(text):
    """The lines of *text* as a bag. Order and terminators are the edit's."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    return sorted(line for line in text.split("\n") if line)


def _shown(draft):
    """The active blocks as the editor shows them."""
    shown = []
    for block in draft.blocks:
        if block.disabled:
            continue
        options = [o.keyword for o in block.options if o.keyword]
        if block.kind == GLOBAL and not options:
            continue
        shown.append((block.kind, options))
    return shown


def _read(text):
    """The active blocks as OpenSSH reads *text* (the daemon's scan)."""
    return [(b.kind, [line.keyword for line in b.lines]) for b in scan_blocks(text)]


def _structure(draft):
    """Everything a reopened editor would show."""
    out = []
    for block in draft.blocks:
        options = [
            (o.keyword, o.value.strip()) for o in block.options if o.keyword
        ]
        if block.kind == GLOBAL and not options:
            continue
        default = "*" if block.kind == HOST else "all"
        patterns = "" if block.kind == GLOBAL else (block.patterns.strip() or default)
        out.append((block.kind, patterns, block.disabled, options))
    return out


def _check(editor, context):
    """What is shown is what is read, and what is written reads back."""
    assert _shown(editor.draft) == _read(editor.text), (
        f"an option changed owner after {context}:\n{editor.text}"
    )
    assert _structure(parse(editor.text)) == _structure(editor.draft), (
        f"the text does not read back as the draft after {context}:\n{editor.text}"
    )


@pytest.mark.parametrize("name", NAMES)
def test_opening_changes_nothing(name):
    editor = _editor(CORPUS[name])
    assert editor.text == CORPUS[name]
    assert not editor.dirty
    _check(editor, "loading")


@pytest.mark.parametrize("name", NAMES)
def test_every_block_move_reorders_lines_and_rewrites_none(name):
    text = CORPUS[name]
    count = len(_editor(text).draft.blocks)
    moved = 0
    for source in range(count):
        for target in range(count):
            editor = _editor(text)
            block = editor.draft.blocks[source]
            result = editor.move_block(block.uid, target)
            context = f"moving block {source} to {target}"
            if result is not True:
                assert editor.text == text, f"a refused move changed the text ({context})"
                continue
            moved += 1
            assert _lines(editor.text) == _lines(text), context
            _check(editor, context)
            assert editor.undo()
            assert editor.text == text, f"undo did not restore the text after {context}"
    if count > 2:
        assert moved, "no move was accepted; the sweep checked nothing"


@pytest.mark.parametrize("name", NAMES)
def test_switching_a_block_off_and_on_restores_the_exact_text(name):
    text = CORPUS[name]
    for index in range(len(_editor(text).draft.blocks)):
        editor = _editor(text)
        uid = editor.draft.blocks[index].uid
        first = editor.toggle_block(uid)
        if isinstance(first, Refusal):
            assert editor.text == text
            continue
        assert first is True
        _check(editor, f"switching block {index}")
        # Nothing is added or dropped; switched-off global options alone
        # gain the line that marks them, having no header to carry it.
        marker = 1 if editor.draft.blocks[index].kind == GLOBAL else 0
        grown = len(editor.text.split("\n")) - len(text.split("\n"))
        assert abs(grown) == marker, f"switching block {index}"
        second = editor.toggle_block(uid)
        if isinstance(second, Refusal):
            # It may not come back on where it now stands; undo still must.
            assert editor.undo()
        assert editor.text == text, f"block {index} did not come back as it was"


@pytest.mark.parametrize("name", NAMES)
def test_editing_one_value_changes_one_line_and_back(name):
    text = CORPUS[name]
    draft = _editor(text).draft
    for b_index, block in enumerate(draft.blocks):
        for o_index, _option in enumerate(block.options):
            editor = _editor(text)
            option = editor.draft.blocks[b_index].options[o_index]
            original = option.value
            assert editor.set_option(option.uid, "value", "changed-value")
            before, after = text.split("\n"), editor.text.split("\n")
            assert len(before) == len(after)
            differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
            assert len(differing) == 1, (name, b_index, o_index, differing)
            assert editor.set_option(option.uid, "value", original)
            assert editor.text == text, "putting the value back did not restore the line"


def _random_edit(rng, editor):
    """One edit a user could make; returns a word for the failure message."""
    blocks = editor.draft.blocks
    options = [(b, o) for b in blocks for o in b.options]
    kind = rng.choice([
        "move", "move", "toggle", "duplicate", "delete", "add-block",
        "add-option", "delete-option", "move-option", "value", "key", "patterns",
    ])
    if kind == "add-block":
        made = editor.add_block(rng.choice([HOST, MATCH, GLOBAL]))
        if made is not None and made.kind != GLOBAL:
            editor.set_patterns(made.uid, "all" if made.kind == MATCH else "made-here")
        if made is not None:
            editor.set_option(made.options[0].uid, "key", "Port")
            editor.set_option(made.options[0].uid, "value", "2200")
    elif not blocks:
        return "nothing"
    elif kind == "move":
        editor.move_block(rng.choice(blocks).uid, rng.randrange(len(blocks) + 1))
    elif kind == "toggle":
        editor.toggle_block(rng.choice(blocks).uid)
    elif kind == "duplicate":
        editor.duplicate_block(rng.choice(blocks).uid)
    elif kind == "delete":
        editor.delete_block(rng.choice(blocks).uid)
    elif kind == "add-option":
        editor.add_option(rng.choice(blocks).uid, "ServerAliveInterval", "9")
    elif kind == "patterns":
        hosts = [b for b in blocks if b.kind == HOST]
        if hosts:
            editor.set_patterns(rng.choice(hosts).uid, rng.choice(["x y", "*.z", "one"]))
    elif not options:
        return "nothing"
    elif kind == "delete-option":
        editor.delete_option(rng.choice(options)[1].uid)
    elif kind == "move-option":
        target = rng.choice(blocks)
        editor.move_option(
            rng.choice(options)[1].uid, target.uid,
            rng.choice([None, rng.randrange(len(target.options) + 1)]),
        )
    elif kind == "value":
        editor.set_option(rng.choice(options)[1].uid, "value", rng.choice(["a", "b c", "~/k"]))
    elif kind == "key":
        editor.set_option(rng.choice(options)[1].uid, "key", rng.choice(["User", "Port"]))
    editor.commit_typing()
    return kind


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("seed", range(8))
def test_a_long_run_of_edits_stays_readable_and_fully_undoable(name, seed):
    text = CORPUS[name]
    rng = random.Random(f"{name}-{seed}")
    editor = _editor(text)
    trail = []
    for _step in range(40):
        trail.append(_random_edit(rng, editor))
        _check(editor, " > ".join(trail))
    while editor.can_undo:
        assert editor.undo()
        _check(editor, "undo after " + " > ".join(trail))
    assert editor.text == text, "undoing everything did not return the loaded text"


# -- the real client ----------------------------------------------------------

# Hosts that share no option, so the order of the blocks cannot matter: any
# difference in what ssh resolves is an option that ended up somewhere else.
INDEPENDENT = (
    "ServerAliveCountMax 7\n"
    "\n"
    "# the web host\n"
    "Host web\n"
    "    HostName web.example.com\n"
    "    User alice\n"
    "\n"
    "Host db\n"
    "    Port 2222\n"
    "    ProxyJump web\n"
    "# [disabled] Host parked\n"
    "# [disabled]     ForwardAgent yes\n"
    "\n"
    "Host files\n"
    "    Compression yes\n"
    "    IdentityFile ~/.ssh/id_files\n"
    "\n"
    "Host *\n"
    "    ConnectTimeout 13\n"
)
ASKED = ("web", "db", "files", "other")


def _ssh_g(path):
    resolved = {}
    for alias in ASKED:
        out = subprocess.run(
            ["ssh", "-G", "-F", str(path), alias],
            capture_output=True, text=True, timeout=20,
        )
        assert out.returncode == 0, out.stderr
        resolved[alias] = out.stdout
    return resolved


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the ssh client")
def test_ssh_resolves_every_host_the_same_after_any_move_or_switch(tmp_path):
    path = tmp_path / "config"
    path.write_text(INDEPENDENT, encoding="utf-8")
    before = _ssh_g(path)
    assert "port 2222" in before["db"] and "connecttimeout 13" in before["other"]

    count = len(_editor(INDEPENDENT).draft.blocks)
    checked = 0
    for source in range(count):
        for target in range(count):
            editor = _editor(INDEPENDENT)
            if editor.move_block(editor.draft.blocks[source].uid, target) is not True:
                continue
            path.write_text(editor.text, encoding="utf-8")
            assert _ssh_g(path) == before, f"moving block {source} to {target}"
            checked += 1
    assert checked >= count

    # Switching the parked block on and off is invisible to the other hosts.
    editor = _editor(INDEPENDENT)
    parked = next(b for b in editor.draft.blocks if b.disabled)
    assert editor.toggle_block(parked.uid) is True
    path.write_text(editor.text, encoding="utf-8")
    assert _ssh_g(path) == before
