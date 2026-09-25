"""Unit tests for SSH identity-provider settings normalization."""
from __future__ import annotations

import pytest

from sshpilot.core.settings.identity import (
    AUTO_PROVIDER,
    CUSTOM_PROVIDER,
    DEFAULT_IDENTITY_SETTINGS,
    normalize_custom_socket,
    normalize_identity_settings,
    normalize_provider,
)


def test_normalize_provider_defaults_and_validation():
    assert normalize_provider("") == AUTO_PROVIDER
    assert normalize_provider("  auto  ") == "auto"
    assert normalize_provider("onepassword") == "onepassword"
    assert normalize_provider("custom") == "custom"

    with pytest.raises(TypeError):
        normalize_provider(123)

    with pytest.raises(ValueError):
        normalize_provider("-starts-with-dash")

    with pytest.raises(ValueError):
        normalize_provider("inv@lid!")


def test_normalize_custom_socket_valid_and_invalid():
    assert normalize_custom_socket(None) == ""
    assert normalize_custom_socket("") == ""
    assert normalize_custom_socket("   ") == ""
    assert normalize_custom_socket("/run/user/1000/agent.sock") == "/run/user/1000/agent.sock"
    assert normalize_custom_socket("~/.1password/agent.sock") == "~/.1password/agent.sock"

    with pytest.raises(TypeError):
        normalize_custom_socket(123)

    with pytest.raises(ValueError, match="must not contain NUL"):
        normalize_custom_socket("/tmp/agent\x00.sock")

    with pytest.raises(ValueError, match="too long"):
        normalize_custom_socket("/" + "a" * 513)

    with pytest.raises(ValueError, match="must be an absolute or ~-relative path"):
        normalize_custom_socket("SSH_AUTH_SOCK=0")

    with pytest.raises(ValueError, match="must be an absolute or ~-relative path"):
        normalize_custom_socket("relative/path/agent.sock")


def test_normalize_identity_settings_defaults():
    assert normalize_identity_settings({}) == DEFAULT_IDENTITY_SETTINGS
    assert normalize_identity_settings({"provider": None, "agent_socket": None}) == {
        "provider": "auto",
        "custom_socket": "",
    }


def test_normalize_identity_settings_resets_invalid_socket_when_not_custom():
    # When provider is "auto", invalid custom_socket strings or types reset to ""
    result = normalize_identity_settings({
        "provider": "auto",
        "agent_socket": "SSH_AUTH_SOCK=0",
    })
    assert result == {
        "provider": "auto",
        "custom_socket": "",
    }

    result_bad_type = normalize_identity_settings({
        "provider": "auto",
        "agent_socket": 9999,
    })
    assert result_bad_type == {
        "provider": "auto",
        "custom_socket": "",
    }

    # Same for other non-custom providers like "onepassword"
    result_op = normalize_identity_settings({
        "provider": "onepassword",
        "agent_socket": "SSH_AUTH_SOCK=0",
    })
    assert result_op == {
        "provider": "onepassword",
        "custom_socket": "",
    }


def test_normalize_identity_settings_uses_default_socket_when_not_custom():
    # 'auto' means default socket, ignoring any valid or invalid custom socket
    result_valid = normalize_identity_settings({
        "provider": "auto",
        "agent_socket": "/run/user/1000/agent.sock",
    })
    assert result_valid == {
        "provider": "auto",
        "custom_socket": "",
    }

    # Same for onepassword or other non-custom providers
    result_op = normalize_identity_settings({
        "provider": "onepassword",
        "agent_socket": "/run/user/1000/agent.sock",
    })
    assert result_op == {
        "provider": "onepassword",
        "custom_socket": "",
    }


def test_normalize_identity_settings_strictly_validates_socket_when_custom():
    # Valid socket is preserved
    valid = normalize_identity_settings({
        "provider": CUSTOM_PROVIDER,
        "agent_socket": "/custom/agent.sock",
    })
    assert valid == {
        "provider": CUSTOM_PROVIDER,
        "custom_socket": "/custom/agent.sock",
    }

    # Empty socket is permitted (unset custom socket)
    empty = normalize_identity_settings({
        "provider": CUSTOM_PROVIDER,
        "agent_socket": "",
    })
    assert empty == {
        "provider": CUSTOM_PROVIDER,
        "custom_socket": "",
    }

    # Invalid socket strictly raises ValueError when provider is custom
    with pytest.raises(ValueError, match="must be an absolute or ~-relative path"):
        normalize_identity_settings({
            "provider": CUSTOM_PROVIDER,
            "agent_socket": "SSH_AUTH_SOCK=0",
        })

    with pytest.raises(TypeError):
        normalize_identity_settings({
            "provider": CUSTOM_PROVIDER,
            "agent_socket": 12345,
        })


def test_identity_state_service_tolerates_invalid_socket_in_auto_mode(tmp_path):
    import json

    from sshpilot.core.identity_service import IdentityStateService
    from sshpilot.core.settings import CONFIG_VERSION

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "config_version": CONFIG_VERSION,
            "identity": {
                "provider": "auto",
                "agent_socket": "SSH_AUTH_SOCK=0",
            }
        }),
        encoding="utf-8",
    )
    service = IdentityStateService(
        config_path,
        environ={"SSH_AUTH_SOCK": "/run/user/1000/agent.sock"},
        ssh_copy_id_probe=lambda: True,
    )
    state = service.get_state()
    assert state.provider == "auto"
    assert state.custom_socket == ""
    assert state.agent_available is True
    assert state.ssh_copy_id_available is True


def test_update_configuration_rejects_socket_change_when_not_custom(tmp_path):
    import json

    from sshpilot.api.errors import ErrorCode, SshPilotError
    from sshpilot.api.models.identity import (
        CUSTOM_SOCKET_NOT_APPLICABLE,
        UpdateIdentityConfigurationRequest,
    )
    from sshpilot.core.identity_service import IdentityStateService
    from sshpilot.core.settings import CONFIG_VERSION

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "config_version": CONFIG_VERSION,
            "identity": {"provider": "auto", "agent_socket": ""},
        }),
        encoding="utf-8",
    )
    service = IdentityStateService(
        config_path,
        environ={"SSH_AUTH_SOCK": "/run/user/1000/agent.sock"},
        ssh_copy_id_probe=lambda: True,
    )
    before = service.get_state()

    # Matching the canonical empty socket is a no-op while on auto.
    noop = service.update_configuration(
        UpdateIdentityConfigurationRequest(
            custom_socket="",
            expected_revision=before.revision,
        )
    )
    assert noop.revision == before.revision
    assert noop.custom_socket == ""

    with pytest.raises(SshPilotError) as excinfo:
        service.update_configuration(
            UpdateIdentityConfigurationRequest(
                custom_socket="/new/agent.sock",
                expected_revision=before.revision,
            )
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details.get("code") == CUSTOM_SOCKET_NOT_APPLICABLE

    disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert disk["identity"]["agent_socket"] == ""
    assert service.get_state().revision == before.revision


def test_update_configuration_writes_socket_when_custom(tmp_path):
    import json

    from sshpilot.api.models.identity import UpdateIdentityConfigurationRequest
    from sshpilot.core.identity_service import IdentityStateService
    from sshpilot.core.settings import CONFIG_VERSION

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "config_version": CONFIG_VERSION,
            "identity": {"provider": "custom", "agent_socket": ""},
        }),
        encoding="utf-8",
    )
    service = IdentityStateService(
        config_path,
        environ={"SSH_AUTH_SOCK": "/run/user/1000/agent.sock"},
        ssh_copy_id_probe=lambda: True,
    )
    before = service.get_state()
    after = service.update_configuration(
        UpdateIdentityConfigurationRequest(
            custom_socket="/custom/agent.sock",
            expected_revision=before.revision,
        )
    )
    assert after.provider == "custom"
    assert after.custom_socket == "/custom/agent.sock"
    assert after.revision != before.revision
    disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert disk["identity"]["agent_socket"] == "/custom/agent.sock"

