"""
Complex SSH config parsing tests.

This module builds a realistic ~/.ssh/config hierarchy with:
  - Main config that uses `Include ~/.ssh/conf.d/*`
  - Several conf.d fragments covering work hosts, personal hosts, tunnels, and
    edge-case syntax variations

Then it exercises the full parsing pipeline and systematically probes every
identified edge case, including ones that expose real bugs in the current
implementation.  Each failing test is marked with the bug description so it
can be tracked separately.
"""

import asyncio
import os
import pytest

asyncio.set_event_loop(asyncio.new_event_loop())

from sshpilot.connection_manager import ConnectionManager
from sshpilot.ssh_config_utils import resolve_ssh_config_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cm():
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.rules = []
    return cm


def load_config(tmp_path, main_text, conf_d_files=None):
    """Write config files and return a loaded ConnectionManager."""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir(exist_ok=True)

    for filename, content in (conf_d_files or {}).items():
        (conf_d / filename).write_text(content)

    main_cfg = tmp_path / "config"
    main_cfg.write_text(main_text)

    cm = make_cm()
    cm.ssh_config_path = str(main_cfg)
    cm.load_ssh_config()
    return cm, main_cfg, conf_d


def conn_by_nickname(cm, name):
    """Return the first Connection with the given nickname, or None."""
    return next((c for c in cm.connections if c.nickname == name), None)


# ---------------------------------------------------------------------------
# Complex config fixture
# ---------------------------------------------------------------------------

MAIN_CONFIG = """\
# Main SSH config
Include conf.d/*

Host bastion
    HostName bastion.example.com
    User ec2-user
    IdentityFile ~/.ssh/keys/bastion_key
    Port 22

Host *
    ServerAliveInterval 60
    AddKeysToAgent yes
"""

# 10-work.conf ---------------------------------------------------------------
WORK_CONFIG = """\
# Work hosts
Host work-db work-app
    HostName 192.168.1.100
    User workuser
    Port 2222
    IdentityFile ~/.ssh/work_rsa
    LocalForward 5432 localhost:5432
    LocalForward 8080 localhost:80
    ForwardAgent yes

Host work-ci
    HostName ci.work.internal
    ProxyJump bastion
    User deploy
    ForwardAgent yes
"""

# 20-personal.conf -----------------------------------------------------------
PERSONAL_CONFIG = """\
# Personal hosts
Host personal-dev
    HostName dev.personal.com
    User myuser
    IdentityFile ~/.ssh/id_ed25519
    CertificateFile ~/.ssh/id_ed25519-cert.pub

Host "quoted host"
    HostName quoted.example.com
    User quoteduser
"""

# 30-tunnels.conf ------------------------------------------------------------
TUNNELS_CONFIG = """\
# Tunnel hosts
Host socks-proxy
    HostName tunnel.example.com
    DynamicForward 1080
    User tunneluser

Host socks-proxy-bind
    HostName tunnel2.example.com
    DynamicForward 127.0.0.1:1081
    User tunneluser

Host reverse-tunnel
    HostName rtunnel.example.com
    RemoteForward 2222 localhost:22
    User rtuser
"""

# 40-match.conf --------------------------------------------------------------
MATCH_CONFIG = """\
Match host *.corp.example.com User admin
    IdentityFile ~/.ssh/corp_key

Host corp-server
    HostName corp.example.com
    User admin
"""

# 50-edge.conf (intentional edge cases) ------------------------------------
# NOTE: Several items here expose parser bugs documented in the tests below.
EDGE_CONFIG = """\
# Edge case: tab-separated key and value (valid in OpenSSH, bug in parser)
Host tab-host
\tHostName\ttab.example.com
\tPort\t2222

# Edge case: = as key/value separator (valid in OpenSSH, bug in parser)
Host eq-host
    Port=2222
    HostName=eq.example.com

# Edge case: multiple IdentityFile lines (only last survives in parser)
Host multi-key
    HostName multi.example.com
    IdentityFile ~/.ssh/id_rsa
    IdentityFile ~/.ssh/id_ed25519

# Edge case: ForwardAgent with socket path (treated as falsy by parser)
Host agent-path
    HostName agent.example.com
    ForwardAgent /tmp/ssh-agent.sock

# Edge case: RemoteForward with only a port (no destination – silently dropped)
Host tunnel-no-dest
    HostName nodest.example.com
    RemoteForward 9999

# Edge case: empty Host block (no options at all)
Host empty-block

# Edge case: Host block at end of file without trailing newline
Host no-trailing-newline
    HostName ntnl.example.com"""

CONF_D_FILES = {
    "10-work.conf": WORK_CONFIG,
    "20-personal.conf": PERSONAL_CONFIG,
    "30-tunnels.conf": TUNNELS_CONFIG,
    "40-match.conf": MATCH_CONFIG,
    "50-edge.conf": EDGE_CONFIG,
}


@pytest.fixture
def complex_cm(tmp_path):
    cm, main_cfg, conf_d = load_config(tmp_path, MAIN_CONFIG, CONF_D_FILES)
    return cm, main_cfg, conf_d, tmp_path


# ===========================================================================
# 1. Basic loading
# ===========================================================================

class TestComplexConfigLoading:
    def test_all_regular_hosts_loaded(self, complex_cm):
        cm, *_ = complex_cm
        names = {c.nickname for c in cm.connections}
        # Note: tab-host, eq-host, and empty-block are NOT loaded due to parser
        # limitations documented in TestEdgeCases below.
        expected = {
            "bastion",
            "work-db", "work-app", "work-ci",
            "personal-dev", "quoted host",
            "socks-proxy", "socks-proxy-bind", "reverse-tunnel",
            "corp-server",
            "multi-key", "agent-path",
            "tunnel-no-dest", "no-trailing-newline",
        }
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_wildcard_all_stored_as_rule(self, complex_cm):
        cm, *_ = complex_cm
        names = {c.nickname for c in cm.connections}
        assert "*" not in names, "Host * should be a rule, not a connection"

    def test_wildcard_all_appears_in_rules(self, complex_cm):
        cm, *_ = complex_cm
        rule_hosts = [r.get("host", "") for r in cm.rules if isinstance(r, dict)]
        assert "*" in rule_hosts

    def test_match_block_stored_as_rule(self, complex_cm):
        cm, *_ = complex_cm
        raw_rules = [r.get("raw", "") for r in cm.rules if "raw" in r]
        assert any("Match" in raw for raw in raw_rules)

    def test_total_connection_count(self, complex_cm):
        cm, *_ = complex_cm
        # 1 bastion + 2 work (work-db/work-app) + 1 work-ci + 2 personal +
        # 3 tunnels + 1 corp + 4 edge (multi-key, agent-path, tunnel-no-dest,
        # no-trailing-newline) = 14
        # tab-host, eq-host, and empty-block are excluded due to parser bugs.
        assert len(cm.connections) == 14


# ===========================================================================
# 2. Source file tracking
# ===========================================================================

class TestSourceTracking:
    def test_bastion_source_is_main_config(self, complex_cm):
        cm, main_cfg, *_ = complex_cm
        c = conn_by_nickname(cm, "bastion")
        assert c.source == str(main_cfg)

    def test_work_host_source_is_work_conf(self, complex_cm):
        cm, _, conf_d, _ = complex_cm
        c = conn_by_nickname(cm, "work-db")
        assert c.source == str(conf_d / "10-work.conf")

    def test_multi_host_block_both_have_same_source(self, complex_cm):
        cm, _, conf_d, _ = complex_cm
        src_db = conn_by_nickname(cm, "work-db").source
        src_app = conn_by_nickname(cm, "work-app").source
        expected = str(conf_d / "10-work.conf")
        assert src_db == expected
        assert src_app == expected

    def test_personal_source_is_personal_conf(self, complex_cm):
        cm, _, conf_d, _ = complex_cm
        c = conn_by_nickname(cm, "personal-dev")
        assert c.source == str(conf_d / "20-personal.conf")


# ===========================================================================
# 3. Include resolution ordering
# ===========================================================================

class TestIncludeOrdering:
    def test_conf_d_files_sorted_alphabetically(self, complex_cm):
        cm, main_cfg, conf_d, _ = complex_cm
        files = resolve_ssh_config_files(str(main_cfg))
        # Main file comes first
        assert files[0] == str(main_cfg)
        # The rest are the conf.d files in alphabetical order
        conf_d_files = [f for f in files if str(conf_d) in f]
        assert conf_d_files == sorted(conf_d_files)

    def test_include_at_top_resolves_all_fragments(self, complex_cm):
        cm, main_cfg, conf_d, _ = complex_cm
        files = resolve_ssh_config_files(str(main_cfg))
        for name in CONF_D_FILES:
            assert str(conf_d / name) in files

    def test_no_duplicate_files_in_resolution(self, complex_cm):
        cm, main_cfg, *_ = complex_cm
        files = resolve_ssh_config_files(str(main_cfg))
        assert len(files) == len(set(files)), "resolve_ssh_config_files returned duplicates"


# ===========================================================================
# 4. Multi-host block handling
# ===========================================================================

class TestMultiHostBlock:
    def test_work_db_and_work_app_share_hostname(self, complex_cm):
        cm, *_ = complex_cm
        db = conn_by_nickname(cm, "work-db")
        app = conn_by_nickname(cm, "work-app")
        assert db.hostname == "192.168.1.100"
        assert app.hostname == "192.168.1.100"

    def test_work_db_and_work_app_share_port(self, complex_cm):
        cm, *_ = complex_cm
        db = conn_by_nickname(cm, "work-db")
        app = conn_by_nickname(cm, "work-app")
        assert db.port == 2222
        assert app.port == 2222

    def test_multi_host_block_independent_connection_objects(self, complex_cm):
        cm, *_ = complex_cm
        db = conn_by_nickname(cm, "work-db")
        app = conn_by_nickname(cm, "work-app")
        assert db is not app


# ===========================================================================
# 5. Port forwarding rules
# ===========================================================================

class TestPortForwarding:
    def test_multiple_localforward_rules_parsed(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "work-db")
        local_rules = [r for r in c.forwarding_rules if r["type"] == "local"]
        assert len(local_rules) == 2

    def test_localforward_5432_correct_ports(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "work-db")
        rule = next((r for r in c.forwarding_rules if r["listen_port"] == 5432), None)
        assert rule is not None
        assert rule["remote_port"] == 5432
        assert rule["remote_host"] == "localhost"

    def test_dynamicforward_plain_port(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "socks-proxy")
        dyn = [r for r in c.forwarding_rules if r["type"] == "dynamic"]
        assert len(dyn) == 1
        assert dyn[0]["listen_port"] == 1080

    def test_dynamicforward_with_bind_address(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "socks-proxy-bind")
        dyn = [r for r in c.forwarding_rules if r["type"] == "dynamic"]
        assert len(dyn) == 1
        assert dyn[0]["listen_port"] == 1081
        assert dyn[0]["listen_addr"] == "127.0.0.1"

    def test_remoteforward_parsed(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "reverse-tunnel")
        remote = [r for r in c.forwarding_rules if r["type"] == "remote"]
        assert len(remote) == 1
        assert remote[0]["listen_port"] == 2222


# ===========================================================================
# 6. ProxyJump
# ===========================================================================

class TestProxyJump:
    def test_proxyjump_single_hop_parsed(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "work-ci")
        assert c.proxy_jump == ["bastion"]

    def test_proxyjump_multi_hop(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host multi-hop\n"
            "    HostName target.example.com\n"
            "    ProxyJump jump1,jump2,jump3\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "multi-hop")
        assert c.proxy_jump == ["jump1", "jump2", "jump3"]


# ===========================================================================
# 7. Authentication options
# ===========================================================================

class TestAuthOptions:
    def test_identityfile_expanded(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "bastion")
        assert "~" not in c.keyfile, "IdentityFile ~ should be expanded"

    def test_certificatefile_parsed(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "personal-dev")
        assert c.certificate != ""
        assert "~" not in c.certificate

    def test_forwardagent_yes_is_true(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "work-db")
        assert c.forward_agent is True

    def test_preferred_authentications_order(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host authtest\n"
            "    HostName auth.example.com\n"
            "    PreferredAuthentications password,publickey\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "authtest")
        # preferred_authentications lives in c.data, not as a direct attribute
        assert c.data.get("preferred_authentications") == ["password", "publickey"]
        assert c.auth_method == 1  # password comes first


# ===========================================================================
# 8. Quoted host name
# ===========================================================================

class TestQuotedHost:
    def test_quoted_host_nickname_stripped(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "quoted host")
        assert c is not None

    def test_quoted_host_hostname_correct(self, complex_cm):
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "quoted host")
        assert c.hostname == "quoted.example.com"


# ===========================================================================
# 9. Wildcard / negated / Match rules
# ===========================================================================

class TestRuleStorage:
    def test_glob_wildcard_not_in_connections(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host *.example.com\n"
            "    User wilduser\n"
            "\n"
            "Host normal\n"
            "    HostName normal.example.com\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        assert all(c.nickname != "*.example.com" for c in cm.connections)
        assert any(r.get("host") == "*.example.com" for r in cm.rules)

    def test_negated_host_stored_as_rule(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host !blocked\n"
            "    User user\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        assert len(cm.connections) == 0
        assert len(cm.rules) == 1

    def test_question_mark_wildcard_is_rule(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host alias?\n"
            "    User user\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        assert len(cm.connections) == 0
        assert any(r.get("host") == "alias?" for r in cm.rules)


# ===========================================================================
# 10. Match block handling
# ===========================================================================

class TestMatchBlock:
    def test_match_block_not_in_connections(self, complex_cm):
        cm, *_ = complex_cm
        for c in cm.connections:
            assert not c.nickname.lower().startswith("match ")

    def test_match_block_raw_preserved(self, complex_cm):
        cm, *_ = complex_cm
        match_rules = [r for r in cm.rules if "raw" in r and "Match" in r["raw"]]
        assert len(match_rules) >= 1
        assert "IdentityFile" in match_rules[0]["raw"]

    def test_match_block_source_tracked(self, complex_cm):
        cm, _, conf_d, _ = complex_cm
        match_rules = [r for r in cm.rules if "raw" in r and "Match" in r["raw"]]
        expected = str(conf_d / "40-match.conf")
        assert match_rules[0]["source"] == expected


# ===========================================================================
# 11. Edge cases – documented parser bugs
# ===========================================================================

class TestEdgeCases:

    # BUG: Tab as key/value separator is valid in OpenSSH config but not
    # recognised by the parser (checks `' ' in line` which misses tabs).
    def test_tab_separated_kv_bug_host_entirely_absent(self, tmp_path):
        """
        KNOWN BUG (severe): When ALL options in a Host block are tab-separated
        (e.g. `HostName\\texample.com`), the parser finds no space-delimited
        key/value pairs. Because `load_ssh_config` only flushes a Host block
        when `current_config` is non-empty, the entire host is silently
        discarded — not just the individual option values.

        The parser condition is `if ' ' in line:` — tabs are not spaces.
        """
        main = tmp_path / "config"
        # Use actual tab characters in the option lines
        main.write_text(
            "Host tab-only\n"
            "\tHostName\ttab.example.com\n"
            "\tPort\t2222\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "tab-only")
        # Document the current (buggy) behaviour: host is NOT created at all
        assert c is None, (
            "BUG: tab-separated options cause the entire Host block to be "
            "silently discarded (current_config stays empty, flush is skipped)"
        )

    def test_tab_separated_kv_mixed_still_parses_space_options(self, tmp_path):
        """
        When a Host block mixes tab-separated and space-separated options,
        only the space-separated ones survive.  The Host block IS created
        because at least one option populates current_config.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host tab-mixed\n"
            "\tHostName\ttabmixed.example.com\n"  # tab – dropped
            "    Port 2222\n"                      # space – kept
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "tab-mixed")
        assert c is not None, "Host block with at least one space option should be created"
        assert c.port == 2222
        # HostName was tab-separated and dropped
        assert c.hostname == "", "BUG: tab-separated HostName is silently dropped"

    # BUG: `=` as key/value separator (e.g. `Port=2222`) is valid in OpenSSH
    # but the parser requires a space character.
    def test_equals_separator_kv_bug_host_entirely_absent(self, tmp_path):
        """
        KNOWN BUG (severe): When ALL options in a Host block use `=` as the
        separator (e.g. `Port=2222`), `' ' in line` is False for every option
        line.  current_config stays empty and the entire Host block is
        silently discarded.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host eq-only\n"
            "    Port=2222\n"
            "    HostName=eq.example.com\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "eq-only")
        assert c is None, (
            "BUG: `=`-separated options cause the entire Host block to be "
            "silently discarded"
        )

    def test_equals_separator_mixed_still_parses_space_options(self, tmp_path):
        """
        When a Host block mixes `=`-separated and space-separated options,
        only the space-separated ones survive.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host eq-mixed\n"
            "    HostName=eqmixed.example.com\n"  # = separator – dropped
            "    Port 2222\n"                       # space – kept
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "eq-mixed")
        assert c is not None
        assert c.port == 2222
        assert c.hostname == "", "BUG: =-separated HostName is silently dropped"

    def test_multiple_identityfile_only_last_survives(self, complex_cm):
        """
        KNOWN LIMITATION: SSH config allows multiple IdentityFile lines and
        OpenSSH tries each in order.  The parser stores only the last value
        because it treats IdentityFile as a single-value option.
        """
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "multi-key")
        assert c is not None
        # Only the last IdentityFile survives
        assert "id_ed25519" in c.keyfile, (
            "LIMITATION: last IdentityFile wins (id_ed25519); "
            "id_rsa is silently discarded"
        )
        assert "id_rsa" not in c.keyfile or "id_ed25519" in c.keyfile

    def test_forwardagent_socket_path_treated_as_false(self, complex_cm):
        """
        KNOWN BUG: When ForwardAgent is set to a socket path
        (`ForwardAgent /tmp/ssh-agent.sock`), OpenSSH treats it as agent
        forwarding enabled with that specific socket.  The parser only
        recognises yes/true/1/on and a path value is therefore treated as
        falsy (forward_agent = False).
        """
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "agent-path")
        assert c is not None
        # Current (buggy) behaviour: forward_agent is False for a path
        assert c.forward_agent is False, (
            "BUG: ForwardAgent /tmp/ssh-agent.sock should be truthy "
            "but the parser returns False"
        )

    def test_remoteforward_single_port_only_silently_dropped(self, complex_cm):
        """
        KNOWN BUG: `RemoteForward 9999` (no destination side) is a valid
        OpenSSH directive for a remote-only tunnel.  The parser expects two
        whitespace-separated parts; a single part is silently ignored.
        """
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "tunnel-no-dest")
        assert c is not None
        remote_rules = [r for r in c.forwarding_rules if r["type"] == "remote"]
        assert remote_rules == [], (
            "BUG: RemoteForward 9999 (single-part) should produce a remote "
            "rule but is silently dropped"
        )

    def test_empty_host_block_silently_discarded(self, tmp_path):
        """
        KNOWN BUG: A Host block with no options is silently discarded.

        `load_ssh_config` flushes the previous block only when both
        `current_hosts` AND `current_config` are truthy.  An empty dict is
        falsy, so a host with zero options is never registered.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host ghost\n"
            "\n"
            "Host real\n"
            "    HostName real.example.com\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        ghost = conn_by_nickname(cm, "ghost")
        real = conn_by_nickname(cm, "real")
        assert ghost is None, "BUG: empty Host block should create a connection but is silently discarded"
        assert real is not None, "Host block with options should still be created"

    def test_host_block_without_trailing_newline_parsed(self, complex_cm):
        """
        The last host block in a file that has no trailing newline must still
        be parsed (handled by the post-loop flush in load_ssh_config).
        """
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "no-trailing-newline")
        assert c is not None
        assert c.hostname == "ntnl.example.com"

    def test_case_insensitive_host_keyword(self, tmp_path):
        """
        SSH config keywords are case-insensitive.  `HOST`, `hOsT`, etc. must
        all be recognised.
        """
        main = tmp_path / "config"
        main.write_text(
            "HOST uppercase-host\n"
            "    HOSTNAME uppercase.example.com\n"
            "    PORT 2222\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "uppercase-host")
        assert c is not None
        assert c.hostname == "uppercase.example.com"
        assert c.port == 2222

    def test_include_with_multiple_patterns_on_one_line(self, tmp_path):
        """Include can list multiple space-separated glob patterns."""
        conf_a = tmp_path / "a.conf"
        conf_b = tmp_path / "b.conf"
        conf_a.write_text("Host a-host\n    HostName a.example.com\n")
        conf_b.write_text("Host b-host\n    HostName b.example.com\n")
        main = tmp_path / "config"
        # Two patterns on the same Include line
        main.write_text(f"Include {conf_a} {conf_b}\n")

        files = resolve_ssh_config_files(str(main))
        assert str(conf_a) in files
        assert str(conf_b) in files

    def test_include_with_quoted_path_containing_spaces(self, tmp_path):
        """Include paths that contain spaces must be quoted."""
        spaced_dir = tmp_path / "my conf.d"
        spaced_dir.mkdir()
        spaced_conf = spaced_dir / "extra.conf"
        spaced_conf.write_text("Host spaced-host\n    HostName sp.example.com\n")
        main = tmp_path / "config"
        main.write_text(f'Include "{spaced_conf}"\n')

        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "spaced-host")
        assert c is not None, "Host from Include with quoted/spaced path should be loaded"

    def test_include_inside_host_block_does_not_crash(self, tmp_path):
        """
        An Include directive that appears inside a Host block is non-standard.
        resolve_ssh_config_files will resolve it (it processes all Include
        lines regardless of context), and load_ssh_config skips it during
        option parsing.  The outer host options around it must still be parsed.
        """
        extra = tmp_path / "extra.conf"
        extra.write_text("Host extra-host\n    HostName extra.example.com\n")
        main = tmp_path / "config"
        main.write_text(
            f"Host outer-host\n"
            f"    HostName outer.example.com\n"
            f"    Include {extra}\n"
            f"    Port 2222\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        outer = conn_by_nickname(cm, "outer-host")
        assert outer is not None
        assert outer.port == 2222
        # The extra host from the included file should also appear
        extra_c = conn_by_nickname(cm, "extra-host")
        assert extra_c is not None

    def test_x11_forwarding_yes_parsed(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host x11host\n"
            "    HostName x11.example.com\n"
            "    ForwardX11 yes\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "x11host")
        assert c is not None
        assert c.x11_forwarding is True

    def test_requesttty_force_parsed(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host ttyhost\n"
            "    HostName tty.example.com\n"
            "    RequestTTY force\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "ttyhost")
        assert c is not None
        # request_tty lives in c.data, not as a direct Connection attribute
        assert c.data.get("request_tty") is True

    def test_serveraliveinterval_stored_as_extra(self, tmp_path):
        """
        Non-standard options end up in extra_ssh_config.
        Note: the key is lowercased by the parser, so the stored string is
        `serveraliveinterval 60`, not `ServerAliveInterval 60`.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host livehost\n"
            "    HostName live.example.com\n"
            "    ServerAliveInterval 60\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "livehost")
        assert c is not None
        extra = c.extra_ssh_config
        # Keys are lowercased; the original casing is NOT preserved
        assert "serveraliveinterval" in extra.lower()
        assert "60" in extra

    def test_pubkeyauthentication_no_sets_password_auth(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host pwdhost\n"
            "    HostName pwd.example.com\n"
            "    PubkeyAuthentication no\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "pwdhost")
        assert c is not None
        assert c.auth_method == 1  # password
        assert c.pubkey_auth_no is True

    def test_localforward_with_ipv6_bind_address(self, tmp_path):
        """LocalForward with an IPv6 bind address should not crash."""
        main = tmp_path / "config"
        main.write_text(
            "Host ipv6fwd\n"
            "    HostName ipv6.example.com\n"
            "    LocalForward [::1]:8080 localhost:80\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "ipv6fwd")
        assert c is not None
        local = [r for r in c.forwarding_rules if r["type"] == "local"]
        assert len(local) == 1
        assert local[0]["listen_port"] == 8080

    def test_port_with_invalid_integer_does_not_crash(self, tmp_path):
        """
        A non-integer Port value should not propagate an unhandled exception.
        The parser should either skip the host or use a default.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host badport\n"
            "    HostName bad.example.com\n"
            "    Port notanumber\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        # Must not raise
        try:
            cm.load_ssh_config()
        except (ValueError, TypeError) as exc:
            pytest.fail(f"Parser raised {type(exc).__name__} on invalid Port value: {exc}")
