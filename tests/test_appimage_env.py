"""The AppImage's bundled environment must not reach host programs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sshpilot import appimage_env

# AppImages exist only on Linux, and these tests run /bin/sh and look up
# programs on PATH the POSIX way.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="AppImages are Linux-only"
)

SRC = Path(__file__).resolve().parents[1] / "src"


def _appimage_env(appdir, launch_dir, **host):
    """An environment shaped like the one AppRun hands the interpreter."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "APPDIR": str(appdir),
        "GI_TYPELIB_PATH": f"{appdir}/usr/lib/girepository-1.0",
        "XDG_DATA_DIRS": f"{appdir}/usr/share:/usr/share",
        "PWD": f"{appdir}/usr/lib",
        "SSHPILOT_APPIMAGE_CWD": str(launch_dir),
        "SSHPILOT_APPIMAGE_VARS": " APPDIR GI_TYPELIB_PATH XDG_DATA_DIRS PWD",
        "SSHPILOT_APPIMAGE_HOST_XDG_DATA_DIRS": "/host/share",
        "SSHPILOT_APPIMAGE_HOST_PWD": str(launch_dir),
    }
    env.update(host)
    return env


def test_host_environment_restores_replaced_and_drops_added(tmp_path):
    env = _appimage_env(tmp_path / "app", tmp_path, SSH_AUTH_SOCK="/agent")

    host = appimage_env.host_environment(env)

    assert host["XDG_DATA_DIRS"] == "/host/share"
    assert host["PWD"] == str(tmp_path)
    assert "GI_TYPELIB_PATH" not in host
    assert "APPDIR" not in host
    assert not [key for key in host if key.startswith("SSHPILOT_APPIMAGE_")]
    # What the app adds on purpose survives.
    assert host["SSH_AUTH_SOCK"] == "/agent"
    # The input is never mutated.
    assert env["GI_TYPELIB_PATH"].endswith("girepository-1.0")


def test_host_environment_is_identity_outside_the_appimage():
    env = {"PATH": "/usr/bin", "GI_TYPELIB_PATH": "/opt/typelibs"}
    assert appimage_env.host_environment(env) == env
    assert not appimage_env.active(env)


def test_is_bundled_follows_symlinks_and_path(tmp_path, monkeypatch):
    appdir = tmp_path / "app"
    (appdir / "usr" / "bin").mkdir(parents=True)
    helper = appdir / "usr" / "bin" / "helper"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(helper)
    monkeypatch.setenv("APPDIR", str(appdir))

    assert appimage_env.is_bundled(str(helper))
    assert appimage_env.is_bundled(str(link))
    assert appimage_env.is_bundled("helper", {"PATH": str(appdir / "usr" / "bin")})
    assert not appimage_env.is_bundled("/bin/sh")
    # A sibling directory sharing the prefix is not inside the AppDir.
    assert not appimage_env.is_bundled(str(tmp_path / "app-other" / "x"))


GUARD_PROBE = r"""
import json, os, subprocess, sys
import sshpilot  # installs the guard, as every bundled process does
from sshpilot import appimage_env

os.chdir(os.environ["APPDIR"])  # stand-in for AppRun's WebKit working directory
host = subprocess.run(["/bin/sh", "-c", "pwd; env"], capture_output=True, text=True, check=True)
explicit = subprocess.run(["/bin/sh", "-c", "env"], capture_output=True, text=True, check=True,
                          env=dict(os.environ, EXTRA="1"), cwd="/")
cwd, _, env_text = host.stdout.partition("\n")
print(json.dumps({
    "cwd": cwd,
    "env": dict(l.split("=", 1) for l in env_text.splitlines() if "=" in l),
    "explicit": dict(l.split("=", 1) for l in explicit.stdout.splitlines() if "=" in l),
    "guard": appimage_env._guard_installed,
}))
"""


def test_guard_gives_host_programs_the_host_environment(tmp_path):
    appdir = tmp_path / "app"
    appdir.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    env = _appimage_env(appdir, launch, PYTHONPATH=str(SRC))

    result = subprocess.run(
        [sys.executable, "-c", GUARD_PROBE],
        env=env, capture_output=True, text=True, check=True, cwd=tmp_path,
    )
    probe = json.loads(result.stdout)

    assert probe["guard"] is True
    # No cwd given: the launch directory, not the AppRun working directory.
    assert probe["cwd"] == str(launch)
    assert "GI_TYPELIB_PATH" not in probe["env"]
    assert probe["env"]["XDG_DATA_DIRS"] == "/host/share"
    # An explicit env and cwd are honoured, minus the AppImage's own values.
    assert probe["explicit"]["EXTRA"] == "1"
    assert "GI_TYPELIB_PATH" not in probe["explicit"]


def test_guard_is_not_installed_outside_the_appimage(tmp_path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c",
         "import subprocess, sshpilot; from sshpilot import appimage_env;"
         "print(appimage_env._guard_installed, subprocess.Popen.__init__.__module__)"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.split() == ["False", "subprocess"]


def test_local_shell_env_gets_host_values(tmp_path):
    from sshpilot.terminal import sanitize_local_shell_env

    env = _appimage_env(tmp_path / "app", tmp_path)
    shell_env = sanitize_local_shell_env(env)

    assert "GI_TYPELIB_PATH" not in shell_env
    assert shell_env["XDG_DATA_DIRS"] == "/host/share"
    assert shell_env["TERM"] == "xterm-256color"


def test_vte_spawns_do_not_inherit_inside_the_appimage(tmp_path, monkeypatch):
    """VTE merges envv into the parent environment unless told not to."""
    from types import SimpleNamespace

    # The suite stubs gi; the real flag is exercised by the AppImage smoke test.
    GLib = SimpleNamespace(SpawnFlags=type("SpawnFlags", (int,), {}))
    GLib.SpawnFlags.DEFAULT = GLib.SpawnFlags(0)
    Vte = SimpleNamespace(SPAWN_NO_PARENT_ENVV=1 << 25)

    for key, value in _appimage_env(tmp_path / "app", tmp_path).items():
        monkeypatch.setenv(key, value)
    flags = appimage_env.vte_spawn_flags(GLib, Vte)
    assert int(flags) & int(Vte.SPAWN_NO_PARENT_ENVV)

    monkeypatch.delenv("SSHPILOT_APPIMAGE_VARS")
    assert int(appimage_env.vte_spawn_flags(GLib, Vte)) == int(GLib.SpawnFlags.DEFAULT)
