"""Headless SSH config loader tests (golden fixtures from Task 0)."""

from pathlib import Path

import pytest

from sshpilot.core.connections.ssh_config_loader import (
    load_ssh_configuration,
)
from sshpilot.core.errors import CoreError, ErrorCode

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ssh_config"


def _golden():
    return load_ssh_configuration(FIXTURES / "golden_root.conf", isolated=False)


def _ids(result):
    return [connection.id for connection in result.connections]


def _by_id(result, connection_id):
    for connection in result.connections:
        if connection.id == connection_id:
            return connection
    raise AssertionError(f"missing connection {connection_id}")


# ---------------------------------------------------------------------------
# Golden fixture: loading shapes
# ---------------------------------------------------------------------------
def test_golden_fixture_loads_expected_connections():
    result = _golden()
    assert _ids(result) == [
        "web",
        "two words",
        "plain",
        "db",
        "jump",
        "inline",
        "tail",
        "alpha",
        "beta",
    ]


def test_golden_fixture_loads_rules():
    result = _golden()
    # Match block + wildcard host + negated host.
    assert len(result.rules) == 3


def test_rule_sources_are_attached():
    result = _golden()
    sources = {rule.get("source") for rule in result.rules}
    assert sources == {str(FIXTURES / "golden_root.conf")}


def test_source_paths_cover_include_tree():
    result = _golden()
    assert result.source_paths == frozenset(
        {
            FIXTURES / "golden_root.conf",
            FIXTURES / "fragments" / "alpha.conf",
            FIXTURES / "fragments" / "nested" / "beta.conf",
            FIXTURES / "more" / "inline.conf",
        }
    )


def test_host_alias_is_the_connection_id():
    result = _golden()
    for connection in result.connections:
        assert connection.id == connection.nickname
        assert "uuid" not in (connection.data or {})


def test_exact_host_fields():
    web = _by_id(_golden(), "web")
    assert web.nickname == "web"
    assert web.hostname == "example.com"
    assert web.username == "alice"
    assert web.port == 22
    assert web.data["host"] == "web"
    assert web.data["source"] == str(FIXTURES / "golden_root.conf")


def test_multi_alias_hosts_materialise_each_token():
    result = _golden()
    assert "two words" in _ids(result)
    assert "plain" in _ids(result)
    assert "db" in _ids(result)
    assert "jump" in _ids(result)


def test_imported_leading_dash_host_alias_fails_closed(tmp_path):
    config = tmp_path / "config"
    config.write_text(
        "Host -danger\n    HostName example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(CoreError):
        load_ssh_configuration(config, isolated=False)


def test_hostname_equals_form_is_parsed():
    db = _by_id(_golden(), "db")
    assert db.hostname == "db.internal"
    assert db.username == "dbuser"


def test_inline_include_options_still_belong_to_host():
    inline = _by_id(_golden(), "inline")
    # The inline fragment's second Host inline block merges into the same
    # connection; options after the Include belong to the pending host.
    assert inline.hostname == "inline.example.com"
    assert inline.data.get("username") == "inline-user"


def test_multiple_identity_files_accumulate():
    web = _by_id(_golden(), "web")
    identity_files = web.data.get("identity_files") or []
    assert len(identity_files) == 2
    assert identity_files[0].endswith("id_ed25519")
    assert identity_files[1].endswith("id_rsa")


def test_multiple_certificate_files_accumulate():
    web = _by_id(_golden(), "web")
    certificate_files = web.data.get("certificate_files") or []
    assert len(certificate_files) == 1
    assert certificate_files[0].endswith("web-cert.pub")


def test_forwarding_rules_parsed():
    web = _by_id(_golden(), "web")
    rules = web.data.get("forwarding_rules") or []
    types = [rule["type"] for rule in rules]
    assert types == ["local", "local", "remote", "dynamic"]


def test_proxy_and_forwarding_directives():
    web = _by_id(_golden(), "web")
    assert web.data.get("proxy_jump") == ["bastion"]
    assert web.data.get("forward_agent") is True
    assert web.data.get("request_tty") == "force"
    assert web.data.get("local_command") == "echo connected"
    assert web.data.get("remote_command") == "tmux attach"


def test_unrecognized_directives_kept_as_extra_config():
    web = _by_id(_golden(), "web")
    extra = web.data.get("extra_ssh_config") or ""
    # Directives are lowercased during parsing (matching the legacy loader);
    # the authored casing is preserved for editing by the raw document.
    assert "unknowndirective hello world" in extra
    assert "hello world" in extra


def test_match_block_is_a_preservation_rule_not_connection():
    result = _golden()
    assert "*.internal" not in _ids(result)
    raw = next(rule["raw"] for rule in result.rules if "Match" in rule["raw"])
    assert "Match host *.internal" in raw


def test_wildcard_host_is_a_rule():
    result = _golden()
    assert "*.wild" not in _ids(result)


# ---------------------------------------------------------------------------
# Loading edge cases
# ---------------------------------------------------------------------------
def test_empty_root_config(tmp_path):
    path = tmp_path / "config"
    path.write_text("", encoding="utf-8")
    result = load_ssh_configuration(path, isolated=False)
    assert result.connections == ()
    assert result.rules == ()


def test_missing_root_config_is_empty(tmp_path):
    result = load_ssh_configuration(tmp_path / "config", isolated=False)
    assert result.connections == ()


def test_root_level_host_star(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host *\n    User global\n", encoding="utf-8")
    result = load_ssh_configuration(path, isolated=False)
    assert result.connections == ()


def test_negated_host_is_a_rule(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host !never\n    User x\n", encoding="utf-8")
    result = load_ssh_configuration(path, isolated=False)
    assert result.connections == ()
    assert len(result.rules) == 1


def test_crlf_and_lf_input(tmp_path):
    path = tmp_path / "config"
    path.write_bytes(b"Host crlf\n    HostName a.example.com\r\n\r\nHost lf\n    HostName b.example.com\n")
    result = load_ssh_configuration(path, isolated=False)
    assert _ids(result) == ["crlf", "lf"]
    assert _by_id(result, "crlf").hostname == "a.example.com"


def test_identity_file_none_suppresses(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host none\n    IdentityFile none\n", encoding="utf-8")
    result = load_ssh_configuration(path, isolated=False)
    none = _by_id(result, "none")
    assert none.data.get("identity_file_none") is True
    assert none.data.get("identity_files") == []


def test_quoted_values_unwrapped(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        'Host "quoted host"\n    User "bob"\n    HostName "q.example.com"\n',
        encoding="utf-8",
    )
    result = load_ssh_configuration(path, isolated=False)
    quoted = _by_id(result, "quoted host")
    assert quoted.username == "bob"
    assert quoted.hostname == "q.example.com"


def test_forwardagent_no_flag(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host fa\n    ForwardAgent no\n", encoding="utf-8")
    result = load_ssh_configuration(path, isolated=False)
    fa = _by_id(result, "fa")
    assert fa.data.get("forward_agent") is False
    assert fa.data.get("forward_agent_explicit_no") is True


def test_proxy_command_preserved(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "Host pc\n    ProxyCommand ssh gateway nc %h %p\n", encoding="utf-8"
    )
    result = load_ssh_configuration(path, isolated=False)
    assert _by_id(result, "pc").data.get("proxy_command") == "ssh gateway nc %h %p"


def test_duplicate_aliases_merge_into_one_connection(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "Host dup\n    User first\nHost dup\n    Port 2222\n", encoding="utf-8"
    )
    result = load_ssh_configuration(path, isolated=False)
    assert _ids(result) == ["dup"]
    dup = _by_id(result, "dup")
    # First-value-wins for scalars; the second block's port is retained.
    assert dup.port == 2222


def test_nested_and_inline_includes(tmp_path):
    (tmp_path / "frag").mkdir()
    (tmp_path / "frag" / "a.conf").write_text(
        "Include b.conf\nHost from_a\n    HostName a.example.com\n",
        encoding="utf-8",
    )
    (tmp_path / "frag" / "b.conf").write_text(
        "Host from_b\n    HostName b.example.com\n", encoding="utf-8"
    )
    root = tmp_path / "config"
    root.write_text(
        "Host root_host\n    Include frag/*.conf\n    HostName root.example.com\n",
        encoding="utf-8",
    )
    result = load_ssh_configuration(root, isolated=False)
    assert _ids(result) == ["root_host", "from_a", "from_b"]


def test_missing_include_is_tolerated(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host ok\n    Include missing/*.conf\n    HostName ok.example.com\n",
        encoding="utf-8",
    )
    result = load_ssh_configuration(root, isolated=False)
    assert _ids(result) == ["ok"]


def test_unreadable_include_raises(tmp_path):
    root = tmp_path / "config"
    fragment = tmp_path / "frag.conf"
    fragment.write_text("Host x\n    HostName x.example.com\n", encoding="utf-8")
    root.write_text(f"Include frag.conf\n", encoding="utf-8")
    fragment.chmod(0o000)
    try:
        with pytest.raises(CoreError) as excinfo:
            load_ssh_configuration(root, isolated=False)
        assert excinfo.value.code is ErrorCode.CONFIG_PARSE_ERROR
        assert "frag.conf" not in str(excinfo.value)
    finally:
        fragment.chmod(0o644)


def test_include_cycle_does_not_loop(tmp_path):
    a = tmp_path / "a.conf"
    b = tmp_path / "b.conf"
    a.write_text("Include b.conf\nHost from_a\n    HostName a.example.com\n", encoding="utf-8")
    b.write_text("Include a.conf\nHost from_b\n    HostName b.example.com\n", encoding="utf-8")
    result = load_ssh_configuration(a, isolated=False)
    assert _ids(result) == ["from_a", "from_b"]


def test_invalid_quoting_in_host_header_aborts(tmp_path):
    path = tmp_path / "config"
    path.write_text('Host "unbalanced\n    HostName x.example.com\n', encoding="utf-8")
    with pytest.raises(CoreError) as excinfo:
        load_ssh_configuration(path, isolated=False)
    assert excinfo.value.code is ErrorCode.CONFIG_PARSE_ERROR


def test_no_partial_state_on_invalid_header(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "Host good\n    HostName good.example.com\nHost \"bad\n    User x\n",
        encoding="utf-8",
    )
    with pytest.raises(CoreError):
        load_ssh_configuration(path, isolated=False)


def test_error_messages_never_contain_paths(tmp_path):
    path = tmp_path / "secret-config"
    path.write_text('Host "unbalanced\n', encoding="utf-8")
    with pytest.raises(CoreError) as excinfo:
        load_ssh_configuration(path, isolated=False)
    message = str(excinfo.value)
    assert "secret-config" not in message
    assert str(tmp_path) not in message


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------
def test_revision_is_stable_across_loads(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host a\n    HostName a.example.com\n", encoding="utf-8")
    first = load_ssh_configuration(path, isolated=False)
    second = load_ssh_configuration(path, isolated=False)
    assert first.root_revision == second.root_revision
    assert len(first.root_revision) == 64


def test_revision_changes_when_root_changes(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host a\n    HostName a.example.com\n", encoding="utf-8")
    before = load_ssh_configuration(path, isolated=False).root_revision
    path.write_text("Host a\n    HostName a.example.com\n    Port 2222\n", encoding="utf-8")
    after = load_ssh_configuration(path, isolated=False).root_revision
    assert before != after


def test_revision_changes_when_include_changes(tmp_path):
    (tmp_path / "frag").mkdir()
    fragment = tmp_path / "frag" / "a.conf"
    fragment.write_text("Host a\n    HostName a.example.com\n", encoding="utf-8")
    root = tmp_path / "config"
    root.write_text("Include frag/*.conf\n", encoding="utf-8")
    before = load_ssh_configuration(root, isolated=False).root_revision
    fragment.write_text("Host a\n    HostName a.example.com\n    Port 2222\n", encoding="utf-8")
    after = load_ssh_configuration(root, isolated=False).root_revision
    assert before != after


def test_revision_is_deterministic_across_configs(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host a\n    HostName a.example.com\n", encoding="utf-8")
    result = load_ssh_configuration(path, isolated=False)
    assert result.root_revision == "0" * 0 or len(result.root_revision) == 64
    # Same bytes on disk must hash identically regardless of access time.
    result2 = load_ssh_configuration(path, isolated=False)
    assert result.root_revision == result2.root_revision


# --- authorship evidence ----------------------------------------------------
#
# OpenSSH resolves with first-obtained-value-wins, so a `Host *` block earlier
# in the file beats a specific block. The loader therefore records what each
# block *authored* and must never fabricate a value for a directive the block
# omitted — a fabricated value gets written back on the next save and really
# does override the global the user relied on.


def test_absent_user_is_not_filled_in_with_the_local_account(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "Host *\n    User tom\n\nHost bare\n    HostName bare.example.com\n",
        encoding="utf-8",
    )
    record = _only_record(load_ssh_configuration(path, isolated=False), "bare")
    assert record.username == ""
    assert "user" not in record.authored_directives


def test_authored_directives_report_exactly_the_blocks_own_options(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "Host *\n"
        "    User tom\n"
        "    Port 2222\n"
        "\n"
        "Host web\n"
        "    HostName web.example.com\n"
        "    User alice\n",
        encoding="utf-8",
    )
    record = _only_record(load_ssh_configuration(path, isolated=False), "web")
    assert record.username == "alice"
    assert record.authored_directives == frozenset({"hostname", "user"})
    # The global's Port is not this block's, so the default must stay unauthored.
    assert record.port == 22
    assert "port" not in record.authored_directives


def test_wildcard_block_never_becomes_authorship_evidence(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "Host prod-*\n    User deploy\n\nHost prod-a\n    HostName a.example.com\n",
        encoding="utf-8",
    )
    record = _only_record(load_ssh_configuration(path, isolated=False), "prod-a")
    assert "user" not in record.authored_directives
    assert record.username == ""


def _only_record(loaded, nickname):
    matches = [r for r in loaded.connections if r.nickname == nickname]
    assert len(matches) == 1, f"expected one {nickname!r} record, got {matches!r}"
    return matches[0]


def test_explicitly_set_port_is_authored_even_when_it_equals_the_default(tmp_path):
    """Pinning `Port 22` under a global `Port 2222` must be written and emitted.

    Authorship cannot come from comparing values: the editor shows the
    inherited port, so pinning it changes nothing. The request carries the
    intent, and the daemon records it.
    """
    from sshpilot.api.models.connections import UpdateConnectionRequest
    from sshpilot.api.models.identity import ConnectionId
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    path = tmp_path / "config"
    path.write_text(
        "Host *\n    User tom\n    Port 2222\n\nHost web\n    HostName web.example.com\n",
        encoding="utf-8",
    )
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(path),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=True,
    )
    service = ConnectionApplicationService(
        repo, client_name="test", allow_cross_thread_commands=True
    )

    before = _only_record(load_ssh_configuration(path, isolated=True), "web")
    assert "port" not in before.authored_directives

    service.update_connection(ConnectionId("web"), UpdateConnectionRequest(port=22))

    after = _only_record(load_ssh_configuration(path, isolated=True), "web")
    assert "port" in after.authored_directives
    assert "    Port 22\n" in path.read_text(encoding="utf-8")
    # The global block is untouched.
    assert "Host *\n    User tom\n    Port 2222\n" in path.read_text(encoding="utf-8")


def test_explicitly_cleared_username_returns_to_inheriting(tmp_path):
    from sshpilot.api.models.connections import UpdateConnectionRequest
    from sshpilot.api.models.identity import ConnectionId
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    path = tmp_path / "config"
    path.write_text(
        "Host *\n    User tom\n\nHost web\n    HostName web.example.com\n    User alice\n",
        encoding="utf-8",
    )
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(path),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=True,
    )
    service = ConnectionApplicationService(
        repo, client_name="test", allow_cross_thread_commands=True
    )

    service.update_connection(ConnectionId("web"), UpdateConnectionRequest(username=""))

    after = _only_record(load_ssh_configuration(path, isolated=True), "web")
    assert "user" not in after.authored_directives
    assert "User alice" not in path.read_text(encoding="utf-8")


def test_clearing_the_port_returns_the_host_to_the_inherited_one(tmp_path):
    """An emptied Port field must remove the line, like an emptied username.

    Forcing a port on every save is what stopped a global ``Port`` from ever
    applying: the block always carried one, and the launch path always emitted
    ``-p`` for it.
    """
    from sshpilot.api.models.connections import UpdateConnectionRequest
    from sshpilot.api.models.identity import ConnectionId
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    path = tmp_path / "config"
    path.write_text(
        "Host *\n    Port 2323\n\nHost web\n    HostName web.example.com\n    Port 2200\n",
        encoding="utf-8",
    )
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(path),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=True,
    )
    service = ConnectionApplicationService(
        repo, client_name="test", allow_cross_thread_commands=True
    )

    service.update_connection(ConnectionId("web"), UpdateConnectionRequest(port=""))

    text = path.read_text(encoding="utf-8")
    assert "Port 2200" not in text
    assert "Host *\n    Port 2323\n" in text  # the global is untouched
    after = _only_record(load_ssh_configuration(path, isolated=True), "web")
    assert "port" not in after.authored_directives
