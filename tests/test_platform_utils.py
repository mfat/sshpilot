import os

import pytest

from sshpilot import platform_utils


@pytest.fixture(autouse=True)
def _reset_bw_cli_cache():
    # These tests populate the module-global bw-CLI discovery caches (often with
    # tmp_path bindings). Reset around each test so state never leaks to another
    # test/file (e.g. describe() reading a stale legacy binding).
    platform_utils.invalidate_bw_cli_cache()
    yield
    platform_utils.invalidate_bw_cli_cache()


def test_is_flatpak_env(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "io.github.mfat.sshpilot")
    monkeypatch.setattr(platform_utils.os.path, "exists", lambda path: False)
    assert platform_utils.is_flatpak() is True


def test_is_flatpak_file(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(platform_utils.os.path, "exists", lambda path: path == "/.flatpak-info")
    assert platform_utils.is_flatpak() is True


def test_is_flatpak_false(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(platform_utils.os.path, "exists", lambda path: False)
    assert platform_utils.is_flatpak() is False


def test_resolve_host_binary_prefers_sandbox(monkeypatch):
    monkeypatch.setattr(platform_utils.shutil, "which", lambda name: "/app/bin/bw" if name == "bw" else None)
    assert platform_utils.resolve_host_binary("bw") == ["/app/bin/bw"]


def test_resolve_host_binary_flatpak_host_fallback(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: True)
    monkeypatch.setattr(platform_utils.shutil, "which", lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None)

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/flatpak-spawn", "--host", "which"]
        class R:
            returncode = 0
            stdout = "/home/u/.npm-global/bin/bw\n"
        return R()

    monkeypatch.setattr(platform_utils.subprocess, "run", fake_run)
    assert platform_utils.resolve_host_binary("bw") == ["/usr/bin/flatpak-spawn", "--host", "bw"]


def _read_env_fd(argv):
    """Decode the ``env -0`` payload behind an ``--env-fd=N`` flag, if any."""
    flag = next((a for a in argv if a.startswith("--env-fd=")), None)
    if flag is None:
        return None
    data = os.pread(int(flag[len("--env-fd="):]), 65536, 0)
    return dict(item.split("=", 1) for item in data.decode().split("\0") if item)


def test_inject_flatpak_host_env_inserts_env_fd_after_host():
    argv = ["/usr/bin/flatpak-spawn", "--host", "bw", "--nointeraction", "unlock"]
    out, fds = platform_utils.inject_flatpak_host_env(
        argv, {"BW_PASSWORD": "secret", "BW_SESSION": "tok", "IGNORED": "x"},
    )
    try:
        assert out[0:2] == ["/usr/bin/flatpak-spawn", "--host"]
        assert len(fds) == 1
        flag = f"--env-fd={fds[0]}"
        assert out.index("--host") < out.index(flag) < out.index("bw")
        assert _read_env_fd(out) == {"BW_PASSWORD": "secret", "BW_SESSION": "tok"}
        # The whole point of --env-fd: no secret may appear in the argv itself.
        assert all("secret" not in a and "tok" not in a for a in out)
    finally:
        for fd in fds:
            os.close(fd)


def test_inject_flatpak_host_env_noop_for_direct_binary():
    argv = ["/usr/bin/bw", "--nointeraction", "unlock"]
    out, fds = platform_utils.inject_flatpak_host_env(argv, {"BW_PASSWORD": "secret"})
    assert out == argv
    assert fds == ()


def test_inject_flatpak_host_env_skips_empty_and_existing():
    argv = [
        "/usr/bin/flatpak-spawn", "--host",
        "--env=BW_PASSWORD=already",
        "bw", "unlock",
    ]
    out, fds = platform_utils.inject_flatpak_host_env(
        argv, {"BW_PASSWORD": "new", "BW_SESSION": "", "BITWARDENCLI_APPDATA_DIR": "/data"},
    )
    try:
        assert out.count("--env=BW_PASSWORD=already") == 1
        assert _read_env_fd(out) == {"BITWARDENCLI_APPDATA_DIR": "/data"}
    finally:
        for fd in fds:
            os.close(fd)


def test_get_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_user_config_dir",
        lambda: str(tmp_path / "conf"),
        raising=False,
    )
    expected = os.path.join(str(tmp_path / "conf"), "sshpilot")
    assert platform_utils.get_config_dir() == expected


def test_get_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_user_data_dir",
        lambda: str(tmp_path / "data"),
        raising=False,
    )
    expected = os.path.join(str(tmp_path / "data"), "sshpilot")
    assert platform_utils.get_data_dir() == expected


def test_get_ssh_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv("SSHPILOT_SSH_DIR", raising=False)
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_home_dir",
        lambda: str(tmp_path),
        raising=False,
    )
    expected = os.path.join(str(tmp_path), ".ssh")
    assert platform_utils.get_ssh_dir() == expected


def test_get_ssh_dir_override(monkeypatch, tmp_path):
    override = tmp_path / "custom_ssh"
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(override))
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_home_dir",
        lambda: "ignored",
        raising=False,
    )
    assert platform_utils.get_ssh_dir() == str(override)


def test_managed_bw_cli_path_uses_data_home(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: False)
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_user_data_dir",
        lambda: str(tmp_path / "share"),
        raising=False,
    )
    path = platform_utils.get_managed_bw_cli_path()
    assert path == os.path.join(str(tmp_path / "share"), "sshpilot", "bin", "bw")


def test_managed_bw_cli_path_flatpak_uses_host_data(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: True)
    monkeypatch.setattr(platform_utils, "_host_env", lambda name: {
        "HOME": "/home/u",
        "XDG_DATA_HOME": "/home/u/.local/share",
    }.get(name))
    path = platform_utils.get_managed_bw_cli_path()
    assert path == "/home/u/.local/share/sshpilot/bin/bw"


def test_discover_managed_bw_when_not_on_path(monkeypatch, tmp_path):
    bw_path = tmp_path / "bw"
    bw_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bw_path.chmod(0o755)

    monkeypatch.setattr(platform_utils, "resolve_host_binary", lambda _b: None)
    monkeypatch.setattr(platform_utils, "get_managed_bw_cli_path", lambda: str(bw_path))
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: False)
    monkeypatch.setattr(platform_utils, "_verify_bw_argv", lambda argv: argv == [str(bw_path)])
    platform_utils.invalidate_bw_cli_cache()

    binding = platform_utils.resolve_bw_cli_binding(force_refresh=True)
    assert binding is not None
    assert list(binding.argv_prefix) == [str(bw_path)]
    assert "sshPilot install" in binding.source
    assert platform_utils.resolve_bw_cli_path(force_refresh=True) == str(bw_path)


def test_discover_legacy_managed_bw_when_new_path_missing(monkeypatch, tmp_path):
    bw_path = tmp_path / "legacy" / "bw"
    bw_path.parent.mkdir(parents=True)
    bw_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bw_path.chmod(0o755)
    new_path = str(tmp_path / "new" / "bw")

    monkeypatch.setattr(platform_utils, "resolve_host_binary", lambda _b: None)
    monkeypatch.setattr(platform_utils, "get_managed_bw_cli_path", lambda: new_path)
    monkeypatch.setattr(platform_utils, "_legacy_managed_bw_cli_path", lambda: str(bw_path))
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: False)
    monkeypatch.setattr(platform_utils, "_verify_bw_argv", lambda argv: argv == [str(bw_path)])
    platform_utils.invalidate_bw_cli_cache()

    binding = platform_utils.resolve_bw_cli_binding(force_refresh=True)
    assert binding is not None
    assert list(binding.argv_prefix) == [str(bw_path)]


def test_resolve_bw_cli_path_flatpak_host_binary(monkeypatch):
    path = "/home/u/.local/share/sshpilot/bin/bw"
    monkeypatch.setattr(
        platform_utils,
        "resolve_bw_cli_binding",
        lambda **kw: platform_utils.BwCliBinding(
            ("/usr/bin/flatpak-spawn", "--host", path),
            f"sshPilot install ({path})",
        ),
    )
    assert platform_utils.resolve_bw_cli_path() == path

