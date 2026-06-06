"""Tests for platform_utils._build_restart_command (app restart re-exec)."""

from types import SimpleNamespace

from sshpilot.platform_utils import _build_restart_command


def test_module_launch_reexecs_with_dash_m():
    """A `python -m sshpilot.main` launch (Flatpak) must restart the same way.

    argv[0] is a bare file path; re-running it directly would break relative
    imports, so we must re-exec via `-m <spec.name>` and keep user flags.
    """
    spec = SimpleNamespace(name="sshpilot.main")
    argv = ["/app/lib/python3.13/site-packages/sshpilot/main.py", "--isolated"]
    assert _build_restart_command("py", argv, spec) == [
        "py",
        "-m",
        "sshpilot.main",
        "--isolated",
    ]


def test_script_launch_reexecs_argv_unchanged():
    """A plain-script launch (run.py / console entry point) keeps argv as-is."""
    argv = ["run.py", "-v"]
    assert _build_restart_command("py", argv, None) == ["py", "run.py", "-v"]


def test_spec_without_name_falls_back_to_script():
    """A spec lacking a usable name falls back to the script form."""
    spec = SimpleNamespace(name=None)
    argv = ["run.py"]
    assert _build_restart_command("py", argv, spec) == ["py", "run.py"]
