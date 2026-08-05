
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.ssh_config_formatter import format_ssh_config_entry


def _load(tmp_path, text: str):
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return load_ssh_configuration(path, isolated=False)


def test_host_token_used_when_hostname_missing(tmp_path):
    """Entries without HostName should fall back to the Host token for the hostname."""
    result = _load(tmp_path, "Host example.com\n    User testuser\n")

    assert len(result.connections) == 1
    record = result.connections[0]
    assert record.nickname == "example.com"
    assert record.data["hostname"] == ""
    assert record.data["host"] == "example.com"


def test_multiple_labels_without_hostname_have_no_aliases(tmp_path):
    """Multiple labels without HostName produce separate entries without aliases."""
    cfg = "Host primary alias1 alias2\n    User testuser\n"
    result = _load(tmp_path, cfg)

    assert sorted(c.nickname for c in result.connections) == ['alias1', 'alias2', 'primary']
    for record in result.connections:
        assert record.data["hostname"] == ""
        assert record.data["host"] == record.nickname
        assert "aliases" not in record.data

    primary = next(c for c in result.connections if c.nickname == "primary")

    entry = format_ssh_config_entry(dict(primary.data))
    assert 'HostName' not in entry
    assert entry.splitlines()[0] == 'Host primary'
    assert 'alias1' not in entry and 'alias2' not in entry


def test_alias_labels_with_hostname(tmp_path):
    """Alias groups with HostName create independent entries for each label."""
    cfg = "Host app1 app2\n    HostName 192.168.1.50\n    User testuser\n"
    result = _load(tmp_path, cfg)

    assert sorted(c.nickname for c in result.connections) == ['app1', 'app2']
    for record in result.connections:
        assert record.data["hostname"] == "192.168.1.50"
        assert record.data["username"] == "testuser"
        assert record.data["aliases"] == []
