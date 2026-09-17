"""ProxyJump + ProxyCommand coexistence: show both, save both, defer to OpenSSH.

OpenSSH applies the first of the two lines in the file and ignores the
second. sshPilot shows both dialog rows exactly as authored, never refuses
a save for the combination, and preserves the block's proxy order on edit
so a save cannot flip the effective route. New blocks render
Jump-then-Command. If the route fails, the file — not the editor — is where
to look.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.errors import CoreError, ErrorCode
from sshpilot.core.ssh.launch import SSHLaunchRequest, build_ssh_process_spec
from sshpilot.ssh_config_formatter import format_ssh_config_entry

SSH = shutil.which("ssh")
needs_ssh = pytest.mark.skipif(SSH is None, reason="openssh client not available")

JUMP = ["bastion-jump"]
COMMAND = "ssh -W %h:%p bastion-cmd"


def _both(nickname="both", hostname="both.example.com"):
    return {
        "nickname": nickname,
        "hostname": hostname,
        "proxy_jump": list(JUMP),
        "proxy_command": COMMAND,
    }


def _proxy_lines(text: str) -> list:    return [
        line.strip().split()[0]
        for line in text.splitlines()
        if line.strip().split() and line.strip().split()[0].lower() in ("proxyjump", "proxycommand")
    ]


def _effective(path: Path, host: str) -> dict:
    out = subprocess.run(
        [SSH, "-F", str(path), "-G", host],
        capture_output=True, text=True, check=True,
    ).stdout
    found = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        key = key.strip().lower()
        if key in ("proxyjump", "proxycommand"):
            found[key] = value.strip()
    return found


# --- saves are never refused -----------------------------------------------


def test_create_writes_both_jump_first_for_new_blocks(tmp_path):
    root = tmp_path / "config"
    root.write_text("", encoding="utf-8")
    store = SshConfigStore(root)
    prepared = store.prepare_create({**_both(nickname="x"), "extra_ssh_config": ""})
    assert _proxy_lines(prepared.target_text) == ["ProxyJump", "ProxyCommand"]


def test_update_unrelated_edit_preserves_command_first_order(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host both\n"
        "    HostName both.example.com\n"
        "    ProxyCommand ssh -W %h:%p bastion-cmd\n"
        "    ProxyJump bastion-jump\n",
        encoding="utf-8",
    )
    store = SshConfigStore(root)
    store.update(
        "both",
        {
            "nickname": "both",
            "hostname": "renamed.example.com",
            "proxy_jump": list(JUMP),
            "proxy_command": COMMAND,
        },
        expected_generation=0,
    )
    text = root.read_text(encoding="utf-8")
    assert _proxy_lines(text) == ["ProxyCommand", "ProxyJump"]


def test_update_unrelated_edit_preserves_jump_first_order(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host both\n"
        "    HostName both.example.com\n"
        "    ProxyJump bastion-jump\n"
        "    ProxyCommand ssh -W %h:%p bastion-cmd\n",
        encoding="utf-8",
    )
    store = SshConfigStore(root)
    store.update(
        "both",
        {
            "nickname": "both",
            "hostname": "renamed.example.com",
            "proxy_jump": list(JUMP),
            "proxy_command": COMMAND,
        },
        expected_generation=0,
    )
    assert _proxy_lines(root.read_text(encoding="utf-8")) == [
        "ProxyJump",
        "ProxyCommand",
    ]


def test_update_proxy_value_keeps_old_order(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host both\n"
        "    HostName both.example.com\n"
        "    ProxyCommand ssh -W %h:%p old\n"
        "    ProxyJump bastion-jump\n",
        encoding="utf-8",
    )
    store = SshConfigStore(root)
    store.update(
        "both",
        {
            "nickname": "both",
            "proxy_jump": list(JUMP),
            "proxy_command": "ssh -W %h:%p new",
        },
        expected_generation=0,
    )
    text = root.read_text(encoding="utf-8")
    assert _proxy_lines(text) == ["ProxyCommand", "ProxyJump"]
    assert "ssh -W %h:%p new" in text


def test_clearing_one_side_writes_only_the_other(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host both\n"
        "    HostName both.example.com\n"
        "    ProxyJump bastion-jump\n"
        "    ProxyCommand ssh -W %h:%p bastion-cmd\n",
        encoding="utf-8",
    )
    store = SshConfigStore(root)
    store.update(
        "both",
        {"nickname": "both", "proxy_jump": [], "proxy_command": COMMAND},
        expected_generation=0,
    )
    text = root.read_text(encoding="utf-8")
    assert "ProxyJump" not in text
    assert f"ProxyCommand {COMMAND}" in text


def test_update_preserves_jump_on_unrelated_edit(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host j\n    HostName old.example.com\n    ProxyJump bastion\n",
        encoding="utf-8",
    )
    store = SshConfigStore(root)
    store.update(
        "j", {"nickname": "j", "hostname": "new.example.com"},
        expected_generation=0,
    )
    assert "ProxyJump bastion" in root.read_text(encoding="utf-8")


# --- launch paths defer to the file -----------------------------------------


def test_authored_options_omit_both_and_defer_to_file():
    """With both authored, no proxy -o is emitted: the -F config on argv
    resolves exactly what plain `ssh host` would do."""
    from types import SimpleNamespace

    from sshpilot.ssh_connection_builder import _authored_ssh_options

    data = {
        "proxy_jump": ["bastion-jump"],
        "proxy_command": "ssh -W %h:%p bastion-cmd",
        "__authored_directives": ("proxyjump", "proxycommand"),
    }
    view = SimpleNamespace(data=dict(data), **{k: v for k, v in data.items()})
    kwargs, _ = _authored_ssh_options(view)
    assert "proxy_jump" not in kwargs
    assert "proxy_command" not in kwargs

    single_jump = SimpleNamespace(
        data={"proxy_jump": ["b"], "__authored_directives": ("proxyjump",)},
        proxy_jump=["b"],
    )
    kwargs, _ = _authored_ssh_options(single_jump)
    assert kwargs["proxy_jump"] == ["b"]

    single_command = SimpleNamespace(
        data={"proxy_command": "ssh cmd", "__authored_directives": ("proxycommand",)},
        proxy_command="ssh cmd",
    )
    kwargs, _ = _authored_ssh_options(single_command)
    assert kwargs["proxy_command"] == "ssh cmd"


def test_resolved_config_probe_omits_both():
    from sshpilot.ssh_connection_builder import _append_identity_and_proxy

    cmd: list = []
    _append_identity_and_proxy(
        cmd,
        {"proxycommand": "ssh -W %h:%p bastion-cmd", "proxyjump": "bastion-jump"},
        is_copy_id=False,
    )
    assert not any(
        part.startswith(("ProxyJump=", "ProxyCommand=")) for part in cmd
    )

    cmd = []
    _append_identity_and_proxy(cmd, {"proxyjump": "bastion-jump"}, is_copy_id=False)
    assert "ProxyJump=bastion-jump" in cmd


def test_explicit_launch_request_with_both_still_refused():
    """The explicit-argv API has no file to defer to: callers pick one."""
    with pytest.raises(CoreError) as caught:
        build_ssh_process_spec(
            SSHLaunchRequest(
                destination="example.com",
                proxy_jump=["bastion-jump"],
                proxy_command="ssh -W %h:%p bastion-cmd",
            )
        )
    assert caught.value.code is ErrorCode.VALIDATION_ERROR


def test_launch_singles_match_openssh_flags():
    jump = build_ssh_process_spec(
        SSHLaunchRequest(destination="example.com", proxy_jump=["b1", "b2"])
    )
    assert "-J" in jump.argv and "b1,b2" in jump.argv
    command = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="example.com", proxy_command="ssh -W %h:%p bastion"
        )
    )
    assert "-o" in command.argv
    assert "ProxyCommand=ssh -W %h:%p bastion" in command.argv


# --- OpenSSH parity ----------------------------------------------------------


@needs_ssh
def test_singles_match_ssh_effective_config(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host only-jump\n"
        "    HostName jump.example.com\n"
        "    ProxyJump bastion-jump\n"
        "\n"
        "Host only-cmd\n"
        "    HostName cmd.example.com\n"
        "    ProxyCommand ssh -W %h:%p bastion-cmd\n",
        encoding="utf-8",
    )
    loaded = {
        record.id: record
        for record in load_ssh_configuration(root, isolated=False).connections
    }
    assert loaded["only-jump"].data["proxy_jump"] == ["bastion-jump"]
    assert loaded["only-cmd"].data["proxy_command"] == "ssh -W %h:%p bastion-cmd"

    rewritten = tmp_path / "rewritten"
    blocks = []
    for record_id, record in loaded.items():
        blocks.append(
            format_ssh_config_entry(
                {
                    "nickname": record_id,
                    "hostname": record.data.get("hostname"),
                    "proxy_jump": record.data.get("proxy_jump"),
                    "proxy_command": record.data.get("proxy_command"),
                    "extra_ssh_config": "",
                }
            )
        )
    rewritten.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    assert _effective(rewritten, "only-jump") == {"proxyjump": "bastion-jump"}
    assert _effective(rewritten, "only-cmd") == {
        "proxycommand": "ssh -W %h:%p bastion-cmd"
    }


@needs_ssh
def test_both_orders_survive_save_with_same_effective_value(tmp_path):
    """Saving an unrelated edit keeps each block's proxy order — and hence
    the route OpenSSH takes — instead of normalizing to Jump-first."""
    root = tmp_path / "config"
    root.write_text(
        "Host cmd-first\n"
        "    HostName example.com\n"
        "    ProxyCommand ssh -W %h:%p bastion-cmd\n"
        "    ProxyJump bastion-jump\n"
        "\n"
        "Host jump-first\n"
        "    HostName example.com\n"
        "    ProxyJump bastion-jump\n"
        "    ProxyCommand ssh -W %h:%p bastion-cmd\n",
        encoding="utf-8",
    )
    assert _effective(root, "cmd-first") == {
        "proxycommand": "ssh -W %h:%p bastion-cmd"
    }
    assert _effective(root, "jump-first") == {"proxyjump": "bastion-jump"}

    store = SshConfigStore(root)
    for host in ("cmd-first", "jump-first"):
        record = next(
            c for c in store.load().connections if c.id == host
        )
        store.update(
            host,
            {
                "nickname": host,
                "hostname": "renamed.example.com",
                "proxy_jump": list(record.data.get("proxy_jump") or []),
                "proxy_command": record.data.get("proxy_command") or "",
            },
            expected_generation=0,
        )

    assert _effective(root, "cmd-first") == {
        "proxycommand": "ssh -W %h:%p bastion-cmd"
    }
    assert _effective(root, "jump-first") == {"proxyjump": "bastion-jump"}


def test_entry_rows_never_receive_unguarded_set_subtitle():
    """Regression: Adw.EntryRow.set_subtitle only exists on newer
    libadwaita — an unguarded call crashes dialog build on older runtimes
    (e.g. libadwaita 1.5), while the stubbed gi in this suite accepts any
    attribute. The file convention is try/except around it; flag only
    unguarded calls on EntryRow rows."""
    import ast

    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "sshpilot"
        / "connection_dialog.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    entry_rows = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "EntryRow"):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                entry_rows.add(target.attr)
    assert entry_rows, "expected Adw.EntryRow rows in the dialog"
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents.setdefault(child, node)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "set_subtitle"):
            continue
        receiver = func.value
        if not (
            isinstance(receiver, ast.Attribute) and receiver.attr in entry_rows
        ):
            continue
        guarded = False
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.Try):
                guarded = True
                break
            parent = parents.get(parent)
        if not guarded:
            bad.append(f"self.{receiver.attr}:{node.lineno}")
    assert not bad, (
        "unguarded set_subtitle on Adw.EntryRow "
        "(crashes older libadwaita; wrap in try/except): " + ", ".join(bad)
    )
