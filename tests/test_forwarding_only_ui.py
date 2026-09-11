"""Unit tests for SessionType-none / forwarding-only helpers."""

from types import SimpleNamespace

from sshpilot.forwarding_only_ui import (
    apply_forwarding_only_flag,
    connection_forwarding_only,
    connection_forwarding_rules,
    extra_ssh_config_has_session_type_none,
    format_destination_endpoint,
    format_forwarding_rule_rows,
    format_bind_endpoint,
    forwarding_only_subtitle,
    forwarding_only_tab_title,
    kind_label_for_rule,
)


def test_session_type_none_detected_case_insensitively():
    assert extra_ssh_config_has_session_type_none("SessionType none")
    assert extra_ssh_config_has_session_type_none("sessiontype NONE")
    assert extra_ssh_config_has_session_type_none('SessionType "none"')
    assert extra_ssh_config_has_session_type_none(
        "Compression yes\nSessionType none\n"
    )


def test_session_type_other_values_are_not_forwarding_only():
    assert not extra_ssh_config_has_session_type_none("")
    assert not extra_ssh_config_has_session_type_none(None)
    assert not extra_ssh_config_has_session_type_none("SessionType default")
    assert not extra_ssh_config_has_session_type_none("# SessionType none")
    assert not extra_ssh_config_has_session_type_none("Compression yes")


def test_connection_forwarding_only_prefers_cached_flag():
    conn = SimpleNamespace(forwarding_only=True, extra_ssh_config="")
    assert connection_forwarding_only(conn) is True
    conn.forwarding_only = False
    assert connection_forwarding_only(conn) is False


def test_connection_forwarding_only_parses_extra_when_unflagged():
    conn = SimpleNamespace(data={"extra_ssh_config": "SessionType none"})
    assert connection_forwarding_only(conn) is True
    conn2 = SimpleNamespace(extra_ssh_config="Compression yes")
    assert connection_forwarding_only(conn2) is False
    assert connection_forwarding_only(SimpleNamespace()) is None


def test_apply_forwarding_only_flag_sets_attribute():
    conn = SimpleNamespace()
    assert apply_forwarding_only_flag(conn, "SessionType none") is True
    assert conn.forwarding_only is True
    assert apply_forwarding_only_flag(conn, "") is False
    assert conn.forwarding_only is False


def test_rule_formatting_local_remote_dynamic():
    local = {
        "type": "local",
        "listen_addr": "127.0.0.1",
        "listen_port": 8080,
        "remote_host": "127.0.0.1",
        "remote_port": 80,
    }
    remote = {
        "type": "remote",
        "listen_addr": "0.0.0.0",
        "listen_port": 2222,
        "local_host": "127.0.0.1",
        "local_port": 22,
    }
    dynamic = {"type": "dynamic", "listen_addr": "127.0.0.1", "listen_port": 1080}
    assert kind_label_for_rule(local) == "L"
    assert kind_label_for_rule(remote) == "R"
    assert kind_label_for_rule(dynamic) == "D"
    assert format_bind_endpoint(local) == "127.0.0.1:8080"
    assert format_destination_endpoint(local) == "127.0.0.1:80"
    assert format_destination_endpoint(remote) == "127.0.0.1:22"
    assert format_destination_endpoint(dynamic) == "dynamic"


def test_format_forwarding_rule_rows_shared_status_and_skips_disabled():
    rules = (
        {
            "type": "local",
            "listen_addr": "127.0.0.1",
            "listen_port": 1,
            "remote_host": "h",
            "remote_port": 2,
            "enabled": True,
        },
        {
            "type": "dynamic",
            "listen_addr": "127.0.0.1",
            "listen_port": 3,
            "enabled": False,
        },
    )
    conn = SimpleNamespace(forwarding_rules=rules)
    enabled = connection_forwarding_rules(conn)
    assert len(enabled) == 1
    rows = format_forwarding_rule_rows(enabled, status="active")
    assert rows == [
        {
            "kind": "L",
            "bind": "127.0.0.1:1",
            "destination": "h:2",
            "status": "active",
        }
    ]


def test_tab_title_and_subtitle():
    assert "forwards" in forwarding_only_tab_title("prod-db")
    assert "prod-db" in forwarding_only_tab_title("prod-db")
    assert "SessionType none" in forwarding_only_subtitle("prod-db")
