"""Draft evaluation: what Advanced Configuration shows is what a launch gets.

The acceptance test runs real OpenSSH twice for the same target: once on the
draft (``evaluate_draft``), once on the configuration after the draft was
saved, the way a launch reads it. Both must give the same effective values.

The default-mode launch passes no ``-F``: OpenSSH reads ``~/.ssh/config``
(found through the password database, not ``$HOME``) and then
``/etc/ssh/ssh_config``. To exercise that for real without touching the
developer's files, the default-mode cases run inside a ``bwrap`` sandbox
that mounts a scratch directory over ``~/.ssh`` and another over
``/etc/ssh``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from sshpilot.api.models.connections import (
    EvaluateSshConfigDraftRequest,
    SshConfigDraftLineState,
)
from sshpilot.core.ssh_config_draft import (
    DraftContext,
    evaluate_draft,
    exec_commands,
    new_exec_commands,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="needs the OpenSSH client"
)

OVERRIDES = ("-o", "ConnectTimeout=7", "-o", "User=forced")


def _ssh_g(args, host):
    out = subprocess.run(
        ["ssh", *args, "-G", host], capture_output=True, text=True, check=True,
        stdin=subprocess.DEVNULL,
    ).stdout
    return [
        [line.strip().split(" ", 1)[0].lower(), *line.strip().split(" ", 1)[1:]]
        for line in out.splitlines() if line.strip()
    ]


def _pairs(evaluation):
    return [[item.key, item.value] for item in evaluation.values]


def _request(text, host="web1", **kwargs):
    return EvaluateSshConfigDraftRequest(text=text, host=host, **kwargs)


# -- isolated mode: the root is launched with -F -------------------------------

def test_isolated_draft_matches_the_saved_launch(tmp_path):
    included = tmp_path / "extra.conf"
    included.write_text("Host web*\n    Port 2200\n    Compression yes\n")
    root = tmp_path / "isolated" / "config"
    root.parent.mkdir()
    root.write_text("Host web1\n    User old\n")
    draft = textwrap.dedent(f"""\
        ServerAliveInterval 30
        Include {included}

        Host web1 !bad
            User deploy
            Port 2222

        Match host web1 user forced
            ForwardAgent yes

        Host *
            User fallback
            ChallengeResponseAuthentication no
        """)
    context = DraftContext(str(root), isolated=True, overrides=OVERRIDES)
    evaluation = evaluate_draft(_request(draft), context, root.read_text())
    assert evaluation.evaluated
    assert evaluation.system_config == ""          # -F: no system-wide file

    root.write_text(draft)                         # save
    launched = _ssh_g(["-F", str(root), *OVERRIDES], "web1")
    assert _pairs(evaluation) == launched

    values = dict(_pairs(evaluation))
    assert values["port"] == "2200"                # the Include comes first
    assert values["user"] == "forced"              # Settings beat the file
    assert values["forwardagent"] == "yes"         # Match user sees the forced user


# -- default mode: no -F, the system-wide file applies -------------------------

_SANDBOX_SCRIPT = r"""
import json, subprocess, sys
sys.path.insert(0, sys.argv[1])
from sshpilot.api.models.connections import EvaluateSshConfigDraftRequest
from sshpilot.core.ssh_config_draft import DraftContext, evaluate_draft
from sshpilot.platform.paths import openssh_user_dir

root = openssh_user_dir() / "config"
draft = open(sys.argv[2]).read()
host = sys.argv[3]
overrides = tuple(sys.argv[4:])
evaluation = evaluate_draft(
    EvaluateSshConfigDraftRequest(text=draft, host=host),
    DraftContext(str(root), overrides=overrides),
    root.read_text(),
)
with open(root, "w") as handle:          # save the draft
    handle.write(draft)
launched = subprocess.run(                # a launch: no -F
    ["ssh", *overrides, "-G", host], capture_output=True, text=True, check=True,
    stdin=subprocess.DEVNULL,
).stdout
print(json.dumps({
    "draft": [[v.key, v.value] for v in evaluation.values],
    # ssh -G prints one keyword in mixed case; keywords are compared lower-case.
    "launched": [
        [l.strip().split(" ", 1)[0].lower(), *l.strip().split(" ", 1)[1:]]
        for l in launched.splitlines() if l.strip()
    ],
    "system_config": evaluation.system_config,
    "errors": [e.diagnostic for e in evaluation.errors],
    "sources": [[s.key, s.file, s.line, s.block] for s in evaluation.sources],
    "matched": list(evaluation.matched_blocks),
}))
"""


def _run_in_default_mode_sandbox(tmp_path, *, user_files, system_files, draft, host,
                                 overrides=()):
    from sshpilot.platform.paths import openssh_user_dir

    bwrap = shutil.which("bwrap")
    user_dir = openssh_user_dir()
    if bwrap is None or not user_dir.is_dir() or not os.path.isdir("/etc/ssh"):
        pytest.skip("needs bwrap, ~/.ssh and /etc/ssh to stand in for a real launch")
    fake_user = tmp_path / "user_ssh"
    fake_system = tmp_path / "etc_ssh"
    for base, files in ((fake_user, user_files), (fake_system, system_files)):
        base.mkdir()
        for name, text in files.items():
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text))
            path.chmod(0o600 if base is fake_user else 0o644)
        base.chmod(0o700 if base is fake_user else 0o755)
    draft_file = tmp_path / "draft"
    draft_file.write_text(textwrap.dedent(draft))
    scratch = tmp_path / "tmp"          # the sandbox's root is read-only
    scratch.mkdir()
    script = tmp_path / "in_sandbox.py"
    script.write_text(_SANDBOX_SCRIPT)
    command = [
        bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--bind", str(fake_user), str(user_dir),
        "--ro-bind", str(fake_system), "/etc/ssh",
        "--bind", str(tmp_path), str(tmp_path),
        "--setenv", "TMPDIR", str(scratch),
        # OpenSSH finds ~/.ssh/config through the password database, but
        # expands a relative Include with glob(), which follows $HOME. The
        # test suite runs with a scratch $HOME; a real session does not.
        "--setenv", "HOME", str(user_dir.parent),
        sys.executable, str(script), str(ROOT / "src"), str(draft_file), host,
        *overrides,
    ]
    probe = subprocess.run(command[:command.index(sys.executable)] + ["true"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"bwrap cannot build the sandbox here: {probe.stderr.strip()[:200]}")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


SYSTEM_FILES = {
    # A system-wide default, and a relative Include: on its own OpenSSH
    # looks for it next to the file (/etc/ssh), not in ~/.ssh.
    "ssh_config": """\
        Include ssh_config.d/*.conf
        Host *
            ServerAliveInterval 41
            User system-user
            HashKnownHosts yes
        """,
    "ssh_config.d/10-site.conf": """\
        Host *
            Compression yes
        """,
}
USER_FILES = {
    "config": "Host web1\n    User old\n",
    # Read through a relative Include of the draft, which OpenSSH resolves
    # in ~/.ssh.
    "conf.d/work.conf": """\
        Host web*
            Port 2200
            ProxyJump bastion
        """,
    # Same name as the system's directory: must not be read for its Include.
    "ssh_config.d/10-site.conf": "Host *\n    Compression no\n    LogLevel DEBUG\n",
}
DRAFT = """\
    Include conf.d/*.conf

    Host web1
        Port 2222
        IdentityFile ~/.ssh/id_work

    Match host web1 exec "true"
        ForwardAgent yes

    Host *
        ServerAliveCountMax 5
    """


def test_default_mode_draft_matches_the_saved_launch(tmp_path):
    """The Include and the system-wide default reach the draft as they
    reach a launch (``ssh host`` with no ``-F``)."""
    user_files = dict(USER_FILES, config='Match host web1 exec "true"\n    User old\n')
    result = _run_in_default_mode_sandbox(
        tmp_path, user_files=user_files, system_files=SYSTEM_FILES,
        draft=DRAFT, host="web1",
    )
    assert result["errors"] == []
    assert result["draft"] == result["launched"]

    values = dict(result["launched"])
    assert values["port"] == "2200"                 # the draft's Include
    assert values["proxyjump"] == "bastion"
    assert values["serveraliveinterval"] == "41"    # the system-wide default
    assert values["user"] == "system-user"
    assert values["compression"] == "yes"           # /etc/ssh/ssh_config.d, not ~/.ssh
    assert values["loglevel"] != "debug"
    assert values["forwardagent"] == "yes"
    assert result["system_config"] == "/etc/ssh/ssh_config"
    sources = {key: (file, block) for key, file, _line, block in result["sources"]}
    assert sources["serveraliveinterval"] == ("/etc/ssh/ssh_config", "Host *")
    assert sources["port"][0].endswith("conf.d/work.conf")
    assert sources["serveralivecountmax"] == ("", "Host *")


def test_default_mode_draft_matches_with_settings_overrides(tmp_path):
    result = _run_in_default_mode_sandbox(
        tmp_path, user_files=USER_FILES, system_files=SYSTEM_FILES,
        draft="Host web1\n    User fromfile\n    ConnectTimeout 99\n",
        host="web1", overrides=OVERRIDES,
    )
    assert result["errors"] == []
    assert result["draft"] == result["launched"]
    values = dict(result["launched"])
    assert values["user"] == "forced" and values["connecttimeout"] == "7"


def test_a_user_value_beats_the_system_default_in_the_draft_too(tmp_path):
    result = _run_in_default_mode_sandbox(
        tmp_path, user_files=USER_FILES, system_files=SYSTEM_FILES,
        draft="Host *\n    ServerAliveInterval 5\n", host="anything",
    )
    assert result["draft"] == result["launched"]
    assert dict(result["launched"])["serveraliveinterval"] == "5"


# -- what the evaluation reports ----------------------------------------------

def _isolated(tmp_path, draft, host="web1", saved="", **kwargs):
    root = tmp_path / "config"
    root.write_text(saved)
    return evaluate_draft(
        _request(textwrap.dedent(draft), host, **kwargs),
        DraftContext(str(root), isolated=True), saved,
    )


def test_blocks_that_apply_are_openssh_s_answer(tmp_path):
    evaluation = _isolated(tmp_path, """\
        Host web1 !web1
            User never
        Host web? !bad
            User pattern
        Match host *.example.com
            User domain
        Match all
            Port 2022
        """)
    assert evaluation.matched_blocks == (3, 7)
    states = {line.line: line.state for line in evaluation.lines}
    assert states[2] is SshConfigDraftLineState.INACTIVE
    assert states[4] is SshConfigDraftLineState.APPLIED
    assert states[6] is SshConfigDraftLineState.INACTIVE
    assert states[8] is SshConfigDraftLineState.APPLIED


def test_line_states_and_what_decided_a_shadowed_line(tmp_path):
    evaluation = _isolated(tmp_path, """\
        Compression yes
        Host web1
            User first
            IdentityFile ~/.ssh/a
        Host *
            User second
            Compression no
            IdentityFile ~/.ssh/b
        """)
    lines = {line.line: line for line in evaluation.lines}
    assert lines[1].state is SshConfigDraftLineState.APPLIED
    assert lines[3].state is SshConfigDraftLineState.APPLIED
    assert lines[4].state is SshConfigDraftLineState.ACCUMULATED
    assert lines[8].state is SshConfigDraftLineState.ACCUMULATED
    assert lines[6].state is SshConfigDraftLineState.SHADOWED
    assert (lines[6].decided_line, lines[6].decided_block) == (3, "Host web1")
    assert (lines[7].decided_line, lines[7].decided_block) == (1, "")
    values = dict(_pairs(evaluation))
    assert values["user"] == "first" and values["compression"] == "yes"
    assert "user" not in evaluation.defaults and "addressfamily" in evaluation.defaults


def test_a_rejected_draft_reports_openssh_s_line(tmp_path):
    evaluation = _isolated(tmp_path, "Host a\n    Bogus yes\n", host="a")
    assert not evaluation.evaluated and evaluation.values == ()
    (error,) = evaluation.errors
    assert (error.file, error.line) == ("", 2)
    assert "bogus" in error.diagnostic.lower()


def test_command_line_user_and_port_are_honoured(tmp_path):
    evaluation = _isolated(
        tmp_path, "Match user alice\n    ForwardAgent yes\nHost *\n    Port 2222\n",
        user="alice", port=2200,
    )
    values = dict(_pairs(evaluation))
    assert values["user"] == "alice" and values["port"] == "2200"
    assert values["forwardagent"] == "yes"


# -- Match exec is not run because a key was pressed ---------------------------

def test_exec_commands_are_found_in_every_spelling():
    text = (
        'Match host a exec "test -f /x y"\n'
        "match !exec=/bin/false user b\n"
        "Host exec\n    User exec\n"
        '# Match exec "commented"\n'
    )
    assert exec_commands(text) == ("test -f /x y", "/bin/false")


def test_a_new_exec_command_waits_for_consent(tmp_path):
    marker = tmp_path / "ran"
    draft = f'Match exec "touch {marker}"\n    User x\n'
    pending = _isolated(tmp_path, draft)
    assert pending.pending_exec == (f"touch {marker}",)
    assert not pending.evaluated and not marker.exists()

    allowed = _isolated(tmp_path, draft, run_new_exec=True)
    assert allowed.evaluated and marker.exists()
    assert dict(_pairs(allowed))["user"] == "x"


def test_a_saved_exec_command_is_not_new(tmp_path):
    draft = 'Match exec "true"\n    User x\nHost *\n    Port 2222\n'
    assert new_exec_commands(draft, 'Match exec "true"\n    User y\n') == ()
    evaluation = _isolated(tmp_path, draft, saved='Match exec "true"\n    User y\n')
    assert evaluation.evaluated and evaluation.matched_blocks == (1, 3)


# -- through the application service -------------------------------------------

def test_the_service_evaluates_in_its_own_root_s_context(tmp_path):
    """The daemon's entry point: the saved root tells which Match exec is
    new, and a root that is not ~/.ssh/config gets no system-wide file."""
    from tests.test_auth_method_update import _service

    service, _repository, root = _service(tmp_path, "    User saved\n")
    saved = root.read_text()
    draft = saved + "\nHost *\n    Port 2222\n"
    host = scan_first_host(saved)
    evaluation = service.evaluate_ssh_config_draft(_request(draft, host))
    assert evaluation.evaluated and evaluation.system_config == ""
    values = dict(_pairs(evaluation))
    assert values["user"] == "saved" and values["port"] == "2222"
    assert root.read_text() == saved                       # nothing is written

    marker = tmp_path / "ran"
    risky = draft + f'\nMatch exec "touch {marker}"\n    Compression yes\n'
    pending = service.evaluate_ssh_config_draft(_request(risky, host))
    assert pending.pending_exec == (f"touch {marker}",) and not marker.exists()


def scan_first_host(text):
    from sshpilot.ssh_config_blocks import scan_blocks

    return next(b.patterns.split()[0] for b in scan_blocks(text) if b.kind == "host")
