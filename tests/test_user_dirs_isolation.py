"""The suite must never resolve sshPilot's directories to the developer's real ones.

tests/conftest.py points XDG_* (and %APPDATA% / %LOCALAPPDATA% on Windows) at a
per-process sandbox. If that ever stops covering a resolver, a test that saves
settings writes into the real config.json, as test_config_upgrade.py once did
on Windows.
"""
import os

import pytest

from sshpilot import platform_utils
from sshpilot.platform import paths


def _inside(path, root):
    path = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    return os.path.commonpath([path, root]) == root


@pytest.mark.parametrize(
    "resolver",
    [
        paths.get_config_dir,
        paths.get_data_dir,
        paths.get_state_dir,
        platform_utils.get_config_dir,
        platform_utils.get_data_dir,
        platform_utils.get_state_dir,
    ],
    ids=lambda f: f"{f.__module__}.{f.__name__}",
)
def test_user_dirs_resolve_inside_the_test_sandbox(resolver, user_dirs_sandbox):
    try:
        resolved = os.fspath(resolver())
    except TypeError:
        # platform_utils asks GLib off Windows, and the stubbed gi has no real
        # get_user_*_dir. Real GLib reads the same XDG_* variables.
        pytest.skip("GLib is stubbed in this run")

    for real in user_dirs_sandbox.real_dirs:
        assert not _inside(resolved, real), (
            f"{resolver.__name__}() resolved to the real {real} during a test run"
        )
    assert _inside(resolved, user_dirs_sandbox.root), (
        f"{resolver.__name__}() resolved to {resolved}, outside the test sandbox"
    )


@pytest.mark.skipif(os.name != "nt", reason="the endpoint dir is Windows-only")
def test_windows_runtime_dir_resolves_inside_the_test_sandbox(user_dirs_sandbox):
    assert _inside(str(paths.get_runtime_dir()), user_dirs_sandbox.root)
