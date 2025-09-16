from sshpilot.connection_manager import ConnectionManager


def make_cm():
    return ConnectionManager.__new__(ConnectionManager)


def test_extra_config_contains_expected_directives():
    cm = make_cm()
    config = {
        "host": "primary",
        "hostname": "example.com",
        "compression": "yes",
    }
    parsed = ConnectionManager.parse_host_config(cm, config)
    extra = parsed.get("extra_ssh_config", "") or ""
    assert "compression yes" in extra.lower()
