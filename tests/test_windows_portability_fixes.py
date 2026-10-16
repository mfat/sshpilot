"""Regression tests for Windows path, process, and quoting bugs.

Each test names a behavior that failed on Windows and is safe on POSIX.
"""
from __future__ import annotations

import ast
import faulthandler
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "sshpilot"

# POSIX-only creators. The daemon socket directory still has to be mode 0700,
# and Flatpak host import never runs on Windows.
_MODE_700_ALLOW = frozenset({
    "platform/fs.py",
    "daemon/lifecycle.py",
    "flatpak_ssh_import.py",
})


def test_managed_bw_binary_name_is_executable():
    from sshpilot.platform_utils import (
        _legacy_managed_bw_cli_path,
        get_managed_bw_cli_path,
    )

    names = {
        os.path.basename(get_managed_bw_cli_path()),
        os.path.basename(_legacy_managed_bw_cli_path()),
    }
    if sys.platform == "win32":
        assert names == {"bw.exe"}
    else:
        assert names == {"bw"}


def test_manual_bw_download_page_matches_this_os():
    from sshpilot.bitwarden_setup import BW_CLI_DOWNLOAD

    if sys.platform == "win32":
        assert "platform=windows" in BW_CLI_DOWNLOAD
    elif sys.platform == "darwin":
        assert "platform=macos" in BW_CLI_DOWNLOAD
    else:
        assert "platform=linux" in BW_CLI_DOWNLOAD


def test_custom_socket_accepts_windows_pipes_and_drive_paths():
    from sshpilot.core.settings.identity import normalize_custom_socket

    assert normalize_custom_socket(r"\\.\pipe\openssh-ssh-agent") == r"\\.\pipe\openssh-ssh-agent"
    assert normalize_custom_socket("//./pipe/keepassxc-agent") == "//./pipe/keepassxc-agent"
    assert normalize_custom_socket(r"C:\Users\me\agent.sock") == r"C:\Users\me\agent.sock"
    assert normalize_custom_socket("C:/Users/me/agent.sock") == "C:/Users/me/agent.sock"
    with pytest.raises(ValueError):
        normalize_custom_socket(r"relative\agent.sock")
    with pytest.raises(ValueError):
        normalize_custom_socket("C:relative")


@pytest.mark.skipif(sys.platform != "win32", reason="C:\\ is not absolute on POSIX")
def test_include_rewrite_keeps_windows_paths_under_the_source_home():
    import sshpilot.backup_manager as bm

    line = r"Include C:\Users\alice\.ssh\conf.d\extra.conf" + "\n"
    out = bm._rewrite_include_line(line, r"C:\Users\alice\.ssh")
    assert not out.lstrip().startswith("#")
    assert "conf.d/extra.conf" in out.replace("\\", "/")


def test_fragment_include_uses_forward_slashes(tmp_path):
    import sshpilot.backup_manager as bm

    root = tmp_path / "ssh"
    fragment = root / "conf.d" / "sshpilot-imported.conf"
    fragment.parent.mkdir(parents=True)
    fragment.write_text("Host x\n", encoding="utf-8")
    main = root / "config"
    main.write_text("Host y\n", encoding="utf-8")
    manager = bm.BackupManager.__new__(bm.BackupManager)
    manager._ensure_include(str(main), str(fragment))
    text = main.read_text(encoding="utf-8")
    assert "conf.d/sshpilot-imported.conf" in text
    assert "\\" not in text


def test_rebase_home_path_across_path_flavours():
    import sshpilot.backup_manager as bm

    assert bm._rebase_home_path(
        "/home/alice/.ssh/id", "/home/alice", "/home/bob"
    ) == "/home/bob/.ssh/id"
    assert bm._rebase_home_path(
        r"C:\Users\alice\.ssh\id_ed25519",
        r"C:\Users\alice",
        "/home/bob",
    ) == "/home/bob/.ssh/id_ed25519"
    assert bm._rebase_home_path(
        "/home/alice/.ssh/id_ed25519",
        "/home/alice",
        r"C:\Users\bob",
    ) == r"C:\Users\bob\.ssh\id_ed25519"
    assert bm._rebase_home_path(
        "/etc/ssh/id", "/home/alice", "/home/bob"
    ) == "/etc/ssh/id"


def test_rebase_home_path_ignores_case_only_for_windows_homes():
    import sshpilot.backup_manager as bm

    assert bm._rebase_home_path(
        r"c:\users\alice\.ssh\id_ed25519", r"C:\Users\alice", "/home/bob"
    ) == "/home/bob/.ssh/id_ed25519"
    assert bm._rebase_home_path(
        "c:/USERS/Alice/.ssh/id", r"C:\Users\alice", "/home/bob"
    ) == "/home/bob/.ssh/id"
    assert bm._rebase_home_path(
        "/home/Alice/.ssh/id", "/home/alice", "/home/bob"
    ) == "/home/Alice/.ssh/id"


def test_home_alias_uses_forward_slashes(tmp_path, monkeypatch):
    from sshpilot.secret_storage import home_alias_for_path

    home = tmp_path / "home"
    key = home / ".ssh" / "id_ed25519"
    key.parent.mkdir(parents=True)
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert home_alias_for_path(str(key)) == "~/.ssh/id_ed25519"


def test_windows_drive_path_counts_as_a_key_path():
    from sshpilot.secret_storage import _looks_like_key_path

    assert _looks_like_key_path(r"C:\keys\my_key")
    assert _looks_like_key_path("C:/keys/my_key")
    assert _looks_like_key_path(r"~\.ssh\id_ed25519")
    assert not _looks_like_key_path("alice@host")


@pytest.mark.skipif(sys.platform != "win32", reason="cmd quoting")
def test_pre_command_tokens_are_quoted_for_cmd():
    from sshpilot.api.models.pre_command import expand_pre_command_tokens as expand

    assert expand("knock %u", username=r"DOMAIN\user") == r"knock DOMAIN\user"
    assert expand("knock %h", hostname="two words") == 'knock "two words"'
    assert expand("knock %h", hostname="") == 'knock ""'


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_download_metadata_leaves_the_temp_file_deletable(tmp_path):
    from sshpilot.daemon.transfer_runtime import _apply_downloaded_metadata

    temp = tmp_path / "part"
    temp.write_bytes(b"new")
    missing = tmp_path / "absent"
    attr = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_mtime=None)
    _apply_downloaded_metadata(str(temp), str(missing), attr)
    os.unlink(temp)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_download_replaces_a_readonly_destination(tmp_path):
    from sshpilot.platform.fs import replace_local_file

    dest = tmp_path / "file"
    dest.write_bytes(b"old")
    os.chmod(dest, 0o444)
    temp = tmp_path / "part"
    temp.write_bytes(b"new")
    replace_local_file(str(temp), str(dest))
    assert dest.read_bytes() == b"new"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_a_readonly_remote_file_downloads_readonly(tmp_path):
    from sshpilot.daemon.transfer_runtime import (
        _apply_downloaded_metadata,
        _finish_downloaded_file,
    )

    temp = tmp_path / "part"
    temp.write_bytes(b"new")
    dest = tmp_path / "file"
    attr = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_mtime=None)
    mode = _apply_downloaded_metadata(str(temp), str(dest), attr)
    _finish_downloaded_file(str(temp), str(dest), mode)
    try:
        assert dest.read_bytes() == b"new"
        assert not os.stat(dest).st_mode & stat.S_IWRITE
    finally:
        os.chmod(dest, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive filesystem")
def test_case_only_local_rename(tmp_path):
    from sshpilot.platform.fs import rename_local_file

    source = tmp_path / "Foo.txt"
    source.write_text("hi", encoding="utf-8")
    target = tmp_path / "foo.txt"
    rename_local_file(str(source), str(target))
    names = list(tmp_path.iterdir())
    assert len(names) == 1
    assert names[0].name == "foo.txt"
    assert names[0].read_text(encoding="utf-8") == "hi"


def test_ssh_config_probes_hide_the_console(monkeypatch, tmp_path):
    from sshpilot.platform.process import hidden_console_kwargs
    from sshpilot.ssh_config_utils import validate_ssh_config_text
    import sshpilot.core.ssh_config_effective as effective

    seen = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="hostname example\n", stderr="")

    monkeypatch.setattr("sshpilot.ssh_config_utils.subprocess.run", fake_run)
    monkeypatch.setattr(effective.subprocess, "run", fake_run)
    validate_ssh_config_text("Host x\n")
    config = tmp_path / "config"
    config.write_text("Host x\n", encoding="utf-8")
    effective.get_effective_ssh_config("x", config_file=str(config))
    expected = hidden_console_kwargs()
    assert seen, "ssh -G was not spawned"
    for kwargs in seen:
        for key, value in expected.items():
            assert kwargs.get(key) == value


def test_rbw_pinentry_helper_is_launchable(tmp_path):
    from sshpilot.secret_storage import RbwBackend

    backend = RbwBackend.__new__(RbwBackend)
    backend._pinentry_helper_dir = lambda: str(tmp_path)
    backend._pinentry_from_config = lambda: ""
    backend._using_our_pinentry_helper = lambda _current: False
    backend._host_file_executable = lambda _path: False
    written = {}

    def write(path, data, mode):
        written[path] = data
        Path(path).write_bytes(data)

    backend._write_host_file = write
    backend._run = lambda *args: SimpleNamespace(returncode=0, stderr=b"")
    helper, changed = backend._ensure_pinentry_helper()
    assert changed is True
    if sys.platform == "win32":
        assert helper.endswith("rbw-pinentry.cmd")
        assert b"python" in written[helper].lower()
        assert b" -I " in written[helper]
        assert any(path.endswith("rbw-pinentry.py") for path in written)
    else:
        assert helper.endswith("rbw-pinentry")
        assert written[helper].startswith(b"#!")


def test_frozen_pinentry_launcher_uses_the_console_exe(tmp_path, monkeypatch):
    from sshpilot.platform.process import FROZEN_WINDOWS_CONSOLE_EXE
    from sshpilot.secret_storage import RbwBackend

    console = tmp_path / "100%" / FROZEN_WINDOWS_CONSOLE_EXE
    console.parent.mkdir()
    console.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(console.parent / "sshPilot.exe"))
    launcher = RbwBackend._windows_pinentry_launcher().decode("utf-8")
    expected_exe = str(console).replace("%", "%%")
    assert f'@"{expected_exe}" --internal-rbw-pinentry "%~dp0rbw-pinentry.py" %*' in launcher


@pytest.mark.skipif(sys.platform != "win32", reason="Windows .cmd launcher")
def test_a_stale_pinentry_launcher_is_rewritten_without_resetting_rbw(tmp_path):
    from sshpilot.secret_storage import RbwBackend

    backend = RbwBackend.__new__(RbwBackend)
    backend._pinentry_helper_dir = lambda: str(tmp_path)
    helper_path = str(tmp_path / "rbw-pinentry.cmd")
    backend._pinentry_from_config = lambda: helper_path
    (tmp_path / "rbw-pinentry.cmd").write_bytes(b'@echo off\r\n@"C:\\old\\python.exe" x %*\r\n')
    (tmp_path / "rbw-pinentry.py").write_bytes(b"old")

    def refuse_config_set(*args):
        raise AssertionError("rbw config set drops the agent's unlocked keys")

    backend._run = refuse_config_set
    helper, changed = backend._ensure_pinentry_helper()
    assert (helper, changed) == (helper_path, False)
    assert Path(helper).read_bytes() == RbwBackend._windows_pinentry_launcher()
    assert b"GETPIN" in (tmp_path / "rbw-pinentry.py").read_bytes()
    assert backend._ensure_pinentry_helper() == (helper_path, False)


def _write_pinentry_script(tmp_path) -> Path:
    from sshpilot.secret_storage import _RBW_PINENTRY_HELPER

    script = tmp_path / "rbw-pinentry.py"
    script.write_text(
        _RBW_PINENTRY_HELPER.format(fallback=json.dumps("pinentry")), encoding="utf-8"
    )
    return script


def test_pinentry_helper_answers_with_bare_newlines(tmp_path):
    script = _write_pinentry_script(tmp_path)
    secret = tmp_path / "secret"
    secret.write_bytes(b"s3cr%et")
    result = subprocess.run(
        [sys.executable, "-I", str(script)],
        input=b"GETPIN\nBYE\n",
        capture_output=True,
        env={**os.environ, "PINENTRY_USER_DATA": str(secret)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"OK Pleased to meet you\nD s3cr%25et\nOK\nOK\n"


def test_run_py_dispatches_the_frozen_pinentry_flag(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text("import sys\nprint(sys.argv[1:])\n", encoding="utf-8")
    run_py = Path(__file__).resolve().parents[1] / "run.py"
    result = subprocess.run(
        [sys.executable, str(run_py), "--internal-rbw-pinentry", str(script), "--display", ":0"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['--display', ':0']"


def test_public_key_comments_are_read_as_utf8(tmp_path):
    from sshpilot.key_sources import KeySourcesMixin

    pub = tmp_path / "id_ed25519.pub"
    pub.write_bytes(b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIComment \xe6\x9d\xb1\xe4\xba\xac\n")
    parts = KeySourcesMixin._read_pub(str(pub))
    assert parts is not None
    assert parts[-1] == "東京"


def test_config_json_round_trips_non_latin1_text(tmp_path, monkeypatch):
    from sshpilot.config import CONFIG_VERSION, Config

    config_file = tmp_path / "config.json"
    config_file.write_bytes(
        ('{"config_version": %d, "nickname": "東京"}\n' % CONFIG_VERSION).encode("utf-8")
    )
    cfg = Config.__new__(Config)
    cfg.config_file = str(config_file)
    cfg._ensure_config_defaults = lambda data: (data, False)
    loaded = Config.load_json_config(cfg)
    assert loaded["nickname"] == "東京"

    cfg.config_data = loaded
    cfg.terminal_themes = {}
    cfg.custom_theme_keys = list
    exported = tmp_path / "export.json"
    assert Config.export_config(cfg, str(exported)) is True
    assert json.loads(exported.read_text(encoding="utf-8"))["nickname"] == "東京"

    imported = {"config_version": CONFIG_VERSION, "nickname": "東京"}
    incoming = tmp_path / "import.json"
    incoming.write_text('{"config_version": 1, "nickname": "東京"}\n', encoding="utf-8")
    cfg.set_setting = lambda key, value: imported.__setitem__(key, value)
    cfg.add_custom_theme = lambda name, theme: None
    assert Config.import_config(cfg, str(incoming)) is True
    assert imported["nickname"] == "東京"


def test_crash_log_is_opened_as_utf8(monkeypatch, tmp_path):
    import sshpilot.main as main

    monkeypatch.setattr(main, "get_state_dir", lambda: str(tmp_path), raising=False)
    # _enable_crash_diagnostics imports get_state_dir inside the function.
    monkeypatch.setattr(
        "sshpilot.platform_utils.get_state_dir", lambda: str(tmp_path)
    )
    main._crash_log_fp = None
    was_enabled = faulthandler.is_enabled()
    main._enable_crash_diagnostics()
    try:
        assert main._crash_log_fp is not None
        main._crash_log_fp.write("東京\n")
        main._crash_log_fp.flush()
        text = (tmp_path / "crash.log").read_text(encoding="utf-8")
        assert "東京" in text
    finally:
        # faulthandler still holds the crash log's descriptor.
        faulthandler.disable()
        if was_enabled:
            faulthandler.enable(file=sys.__stderr__, all_threads=True)
        if main._crash_log_fp is not None:
            main._crash_log_fp.close()
        main._crash_log_fp = None


@pytest.mark.skipif(sys.platform != "win32", reason="os.execv differs on Windows")
def test_restart_app_spawns_a_new_process(monkeypatch):
    from sshpilot import platform_utils

    spawned = {}

    def refuse_execv(*_args, **_kwargs):
        raise AssertionError("os.execv races the single-instance lock on Windows")

    monkeypatch.setattr(platform_utils.os, "execv", refuse_execv)

    def fake_popen(args, **kwargs):
        spawned["args"] = list(args)
        spawned["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(platform_utils.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(platform_utils.os, "_exit", lambda code: spawned.setdefault("exit", code))
    platform_utils.restart_app()
    assert spawned["args"][0] == sys.executable
    assert spawned["exit"] == 0


def _quit_with_one_terminal(monkeypatch):
    import sshpilot.shutdown as shutdown

    window = SimpleNamespace(
        _cleanup_index=0,
        _cleanup_total=1,
        _cleanup_connections=[
            (
                SimpleNamespace(nickname="box"),
                SimpleNamespace(process_pid=2**31 - 1, is_connected=False),
            )
        ],
    )
    monkeypatch.setattr(shutdown, "_update_cleanup_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(shutdown, "_disconnect_terminal_safely", lambda *_a, **_k: None)
    shutdown._perform_cleanup_and_quit(window, window._cleanup_connections)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows has no SIGTERM to send")
def test_quit_kills_the_terminal_process_tree(monkeypatch):
    killed = []
    monkeypatch.setattr(
        "sshpilot.platform.process.kill_process_tree",
        lambda pid, **kwargs: killed.append(pid) or True,
    )
    _quit_with_one_terminal(monkeypatch)
    assert killed == [2**31 - 1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_quit_asks_the_terminal_process_to_exit_on_posix(monkeypatch):
    import signal

    def refuse_tree_kill(*_args, **_kwargs):
        raise AssertionError("SIGKILL gives ssh no chance to clean up")

    monkeypatch.setattr("sshpilot.platform.process.kill_process_tree", refuse_tree_kill)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    _quit_with_one_terminal(monkeypatch)
    assert signals == [(2**31 - 1, signal.SIGTERM)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows netstat")
def test_port_fallback_parses_windows_netstat(monkeypatch):
    from sshpilot.port_utils import PortChecker

    sample = (
        "  Proto  Lokale Adresse         Remoteadresse          Status           PID\n"
        "  TCP    0.0.0.0:22             0.0.0.0:0              ABHÖREN          4321\n"
        "  TCP    127.0.0.1:50000        127.0.0.1:80           HERGESTELLT      1\n"
        "  TCP    [::]:8080              [::]:0                 ABHÖREN          99\n"
        "  UDP    0.0.0.0:53             *:*                                     7\n"
    )
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        assert argv == ["netstat", "-ano"]
        return SimpleNamespace(returncode=0, stdout=sample, stderr="")

    monkeypatch.setattr("sshpilot.port_utils.subprocess.run", fake_run)
    ports = PortChecker()._get_ports_via_netstat()
    assert [(port.port, port.address, port.pid) for port in ports] == [
        (22, "0.0.0.0", 4321),
        (8080, "::", 99),
    ]
    assert seen["encoding"] == "oem"


def test_private_directories_are_not_created_with_mode_0700():
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        rel = path.relative_to(SOURCE).as_posix()
        if rel in _MODE_700_ALLOW:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            values = [keyword.value for keyword in node.keywords if keyword.arg == "mode"]
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("mkdir", "makedirs"):
                receiver = func.value.id if isinstance(func.value, ast.Name) else ""
                # os.mkdir(path, mode) and os.makedirs(path, mode) take the
                # mode second, Path.mkdir(mode) first; an SFTP client's
                # mkdir(path, mode) creates a remote directory.
                if receiver == "os":
                    values += node.args[1:2]
                elif func.attr == "mkdir":
                    values += node.args[:1]
            if any(isinstance(v, ast.Constant) and v.value == 0o700 for v in values):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "mkdir(mode=0o700) adds an OWNER RIGHTS ACE on Windows; use "
        "sshpilot.platform.fs.makedirs_private:\n" + "\n".join(offenders)
    )
