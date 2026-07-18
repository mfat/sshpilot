"""apply_headless_askpass_env forces graphical askpass for no-TTY SSH."""

import types

from sshpilot.ssh_connection_builder import apply_headless_askpass_env


def test_forces_askpass_when_resolver_omits_it(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder._askpass_env_for_connection",
        lambda *a, **k: {
            "SSH_ASKPASS": "/forced",
            "SSH_ASKPASS_REQUIRE": "prefer",
        },
    )
    env = apply_headless_askpass_env(
        {}, types.SimpleNamespace(nickname="h", password=None),
        base_env={"PATH": "/bin", "SSH_ASKPASS": "/desktop/askpass"},
    )
    assert env["SSH_ASKPASS"] == "/forced"
    assert env["SSH_ASKPASS_REQUIRE"] == "prefer"


def test_upgrades_require_to_prefer():
    env = apply_headless_askpass_env(
        {"SSH_ASKPASS": "/resolver", "SSH_ASKPASS_REQUIRE": "force"},
        types.SimpleNamespace(nickname="h"),
        base_env={},
    )
    assert env["SSH_ASKPASS"] == "/resolver"
    assert env["SSH_ASKPASS_REQUIRE"] == "prefer"


def test_honors_resolver_deletions(monkeypatch):
    # prepared omitted SSH_AUTH_SOCK → drop desktop sock from base.
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder._askpass_env_for_connection",
        lambda *a, **k: {
            "SSH_ASKPASS": "/a",
            "SSH_ASKPASS_REQUIRE": "prefer",
        },
    )
    env = apply_headless_askpass_env(
        {},  # no SSH_AUTH_SOCK in prepared
        types.SimpleNamespace(nickname="h"),
        base_env={"SSH_AUTH_SOCK": "/tmp/agent.sock", "PATH": "/bin"},
    )
    assert "SSH_AUTH_SOCK" not in env
