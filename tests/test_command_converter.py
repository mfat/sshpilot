"""Tests for the SSH command converter (sshpilot.command_converter)."""

from sshpilot.command_converter import parse_ssh_command


def test_bare_user_at_host():
    parsed = parse_ssh_command("user@host")
    assert parsed["host"] == "host"
    assert parsed["hostname"] == "host"
    assert parsed["username"] == "user"
    assert parsed["port"] == 22
    assert parsed["nickname"] == "host"


def test_full_command_with_proxy_jump_and_option():
    command = "ssh -J bastion -o Foo=bar user@host"
    parsed = parse_ssh_command(command)

    assert parsed["host"] == "host"
    assert parsed["username"] == "user"
    # -J maps to proxy_jump and -o Foo=bar goes to extra_ssh_config
    assert parsed["proxy_jump"] == ["bastion"]
    assert "Foo bar" in parsed["extra_ssh_config"]
    assert parsed["unparsed_args"] == []


def test_custom_port():
    parsed = parse_ssh_command("ssh -p 2222 root@192.168.8.1")
    assert parsed["host"] == "192.168.8.1"
    assert parsed["username"] == "root"
    assert parsed["port"] == 2222


def test_identity_file_sets_key_select_mode():
    parsed = parse_ssh_command("ssh -i ~/.ssh/id_ed25519 user@host")
    assert parsed["keyfile"] == "~/.ssh/id_ed25519"
    assert parsed["key_select_mode"] == 2


def test_x11_and_agent_forwarding():
    parsed = parse_ssh_command("ssh -X -A user@host")
    assert parsed["x11_forwarding"] is True
    assert parsed["forward_agent"] is True


def test_local_forward_rule():
    parsed = parse_ssh_command("ssh -L 8080:internal:80 user@host")
    rules = parsed["forwarding_rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["type"] == "local"
    assert rule["listen_port"] == 8080
    assert rule["remote_host"] == "internal"
    assert rule["remote_port"] == 80


def test_non_ssh_command_is_rejected():
    result = parse_ssh_command("scp file user@host:/tmp")
    assert "error" in result


def test_unparseable_returns_none():
    assert parse_ssh_command("") is None
    # 'ssh' with no host token cannot be turned into a connection
    assert parse_ssh_command("ssh -v") is None
