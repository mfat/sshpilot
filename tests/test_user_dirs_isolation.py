"""The suite must never resolve sshPilot's directories to the developer's real ones.

tests/conftest.py points home, XDG_* and (on Windows) %APPDATA% / %LOCALAPPDATA%
at a per-process sandbox. If that ever stops covering a resolver, a test that
saves settings writes into the real config.json, as test_config_upgrade.py once
did on Windows.
"""
import os
from pathlib import Path

import pytest

from sshpilot import platform_utils
from sshpilot.platform import paths

_RESOLVERS = [
    paths.get_config_dir,
    paths.get_data_dir,
    paths.get_state_dir,
    paths.get_ssh_dir,
    platform_utils.get_config_dir,
    platform_utils.get_data_dir,
    platform_utils.get_state_dir,
    platform_utils.get_ssh_dir,
    Path.home,
    lambda: os.path.expanduser("~"),
]
_IDS = [
    "paths.get_config_dir",
    "paths.get_data_dir",
    "paths.get_state_dir",
    "paths.get_ssh_dir",
    "platform_utils.get_config_dir",
    "platform_utils.get_data_dir",
    "platform_utils.get_state_dir",
    "platform_utils.get_ssh_dir",
    "Path.home",
    "expanduser",
]


def _inside(path, root):
    path = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    return os.path.commonpath([path, root]) == root


def _assert_sandboxed(resolver, sandbox):
    try:
        resolved = os.fspath(resolver())
    except TypeError:
        # platform_utils asks GLib off Windows, and the stubbed gi has no real
        # get_user_*_dir. Real GLib reads the same HOME / XDG_* variables.
        pytest.skip("GLib is stubbed in this run")

    for real in sandbox.real_dirs:
        assert not _inside(resolved, real), (
            f"resolved to the real {real} during a test run"
        )
    assert _inside(resolved, sandbox.root), (
        f"resolved to {resolved}, outside the test sandbox"
    )


@pytest.mark.parametrize("resolver", _RESOLVERS, ids=_IDS)
def test_user_dirs_resolve_inside_the_test_sandbox(resolver, user_dirs_sandbox):
    _assert_sandboxed(resolver, user_dirs_sandbox)


@pytest.mark.parametrize("resolver", _RESOLVERS, ids=_IDS)
def test_dropping_the_xdg_overrides_still_stays_in_the_sandbox(
    resolver, user_dirs_sandbox, monkeypatch
):
    # A test that deletes XDG_* falls back to home (or %APPDATA% on Windows),
    # which must be sandboxed too.
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    _assert_sandboxed(resolver, user_dirs_sandbox)


@pytest.mark.skipif(os.name != "nt", reason="the endpoint dir is Windows-only")
def test_windows_runtime_dir_resolves_inside_the_test_sandbox(user_dirs_sandbox):
    _assert_sandboxed(paths.get_runtime_dir, user_dirs_sandbox)
