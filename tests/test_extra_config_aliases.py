from sshpilot.connection_manager import ConnectionManager


def make_cm():
    return ConnectionManager.__new__(ConnectionManager)


def test_aliases_not_in_extra_config():
    cm = make_cm()
    config = {
        "host": "primary",
        "aliases": ["alias1", "alias2"],
        "hostname": "example.com",
        "compression": "yes",
    }
    parsed = ConnectionManager.parse_host_config(cm, config)
    extra = parsed.get("extra_ssh_config", "") or ""
    assert "aliases" not in extra.lower()
    assert "compression yes" in extra.lower()
