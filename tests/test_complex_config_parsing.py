"""
Complex SSH config parsing tests.

This module builds a realistic ~/.ssh/config hierarchy with:
  - Main config that uses `Include ~/.ssh/conf.d/*`
  - Several conf.d fragments covering work hosts, personal hosts, tunnels, and
    edge-case syntax variations

Then it exercises the full parsing pipeline and systematically probes every
identified edge case.

These tests originally documented ten parser bugs (silent failures with no
warning to the user). All ten have since been fixed in the parser; the tests
below now assert the spec-correct behaviour and act as regression guards.

Behaviour is verified against the ssh_config(5) man page for OpenSSH 10.2p1
(the version installed in this environment). The relevant quotes:

  Separators: "Configuration options may be separated by whitespace or
  optional whitespace and exactly one ="

  IdentityFile: "Multiple IdentityFile directives will add to the list of
  identities tried (this behaviour differs from that of other configuration
  directives)"

  CertificateFile: "Multiple CertificateFile directives will add to the list
  of certificates used for authentication"

  ForwardAgent: "The argument may be yes, no (the default), an explicit path
  to an agent socket or the name of an environment variable (beginning with $)
  in which to find the path"

  RemoteForward: "if no destination argument is specified then the remote
  forwarding will be established as a SOCKS proxy" (i.e. single-argument form
  is valid)

  Environment variables: "The keywords CertificateFile, ControlPath,
  IdentityAgent, IdentityFile, Include, KnownHostsCommand, and
  UserKnownHostsFile support environment variables."

  TOKENS: IdentityFile/CertificateFile/Include accept the tokens %%, %C, %d,
  %h, %i, %j, %k, %L, %l, %n, %p, %r and %u.

  IdentityFile none: "an argument of none may be used to indicate no identity
  files should be loaded".

Ten fixed parser issues, now covered as regression tests:
  ISSUE 1  – Tab separator           now parsed (whitespace incl. tabs)
  ISSUE 2a – `keyword=value`         now parsed
  ISSUE 2b – `keyword = value`       now parsed (no int() crash)
  ISSUE 3  – Multiple IdentityFile   all preserved (identity_files list)
  ISSUE 4  – Multiple CertificateFile all preserved (certificate_files list)
  ISSUE 5a – ForwardAgent socket path treated as truthy
  ISSUE 5b – ForwardAgent $ENV_VAR    treated as truthy
  ISSUE 6  – RemoteForward single arg  parsed as a SOCKS rule
  ISSUE 7  – ${VAR} in IdentityFile    expanded
  ISSUE 8  – %d token in IdentityFile  expanded (static tokens only)
  ISSUE 9  – IdentityFile none         treated as suppressor, not a path
  ISSUE 10 – %d/%u tokens in Include   expanded so included files load

Not bugs:
  - Empty Host block – valid no-op; silently discarding it is correct UX
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

# 50-edge.conf (edge cases) -------------------------------------------------
# NOTE: Items here exercise spec-valid syntax variations (tab separator,
# = separator, multiple IdentityFile) that the parser must handle, verified
# against the ssh_config(5) man page.
EDGE_CONFIG = """\
# Tab-separated key and value – valid per spec (whitespace includes tabs)
Host tab-host
\tHostName\ttab.example.com
\tPort\t2222

# `=` separator, no spaces – valid per spec ("optional whitespace and
# exactly one =")
Host eq-host
    Port=2222
    HostName=eq.example.com

# Multiple IdentityFile lines – spec says "all tried in sequence";
# all entries must be preserved
Host multi-key
    HostName multi.example.com
    IdentityFile ~/.ssh/id_rsa
    IdentityFile ~/.ssh/id_ed25519

# Host block at end of file without trailing newline (must not be lost)
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
        # tab-host and eq-host now load (spec-valid separators are parsed).
        expected = {
            "bastion",
            "work-db", "work-app", "work-ci",
            "personal-dev", "quoted host",
            "socks-proxy", "socks-proxy-bind", "reverse-tunnel",
            "corp-server",
            "multi-key", "no-trailing-newline",
            "tab-host", "eq-host",
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
        # 3 tunnels + 1 corp + 4 edge (multi-key, no-trailing-newline,
        # tab-host, eq-host) = 14
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
# 11. Edge cases (regression guards for ten fixed parser issues)
#
# Verified against ssh_config(5):
#   ISSUE 1  – Tab separator      (spec: "whitespace" = space or tab)
#   ISSUE 2  – `=` separator      (spec: "optional whitespace and exactly one =")
#   ISSUE 3  – Multiple IdentityFile (spec: "all identities tried in sequence")
#   ISSUE 5  – ForwardAgent path / $VAR (spec: both are valid arguments)
#   ISSUE 6  – RemoteForward single arg  (spec: SOCKS proxy form)
#
# Genuine non-bugs:
#   - Empty Host block: valid no-op; silently discarding it is correct UX
# ===========================================================================

class TestEdgeCases:

    # -----------------------------------------------------------------------
    # ISSUE 1 – Tab separator
    # -----------------------------------------------------------------------

    def test_tab_separated_kv_block_loaded(self, tmp_path):
        """
        The spec says options may be separated by whitespace, which includes
        tabs.  A block whose options all use a tab separator must load with
        every option parsed.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host tab-only\n"
            "\tHostName\ttab.example.com\n"
            "\tPort\t2222\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "tab-only")
        assert c is not None, "tab-separated options must not discard the host"
        assert c.hostname == "tab.example.com"
        assert c.port == 2222

    def test_tab_separated_kv_mixed_all_options_kept(self, tmp_path):
        """A block mixing tab and space separators must keep every option."""
        main = tmp_path / "config"
        main.write_text(
            "Host tab-mixed\n"
            "\tHostName\ttabmixed.example.com\n"  # tab
            "    Port 2222\n"                      # space
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "tab-mixed")
        assert c is not None
        assert c.port == 2222
        assert c.hostname == "tabmixed.example.com"

    # -----------------------------------------------------------------------
    # ISSUE 2 – `=` separator
    # -----------------------------------------------------------------------

    def test_equals_no_spaces_block_loaded(self, tmp_path):
        """`keyword=value` (no surrounding spaces) is spec-valid and must parse."""
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
        assert c is not None, "`keyword=value` options must not discard the host"
        assert c.hostname == "eq.example.com"
        assert c.port == 2222

    def test_equals_no_spaces_mixed_all_options_kept(self, tmp_path):
        """Mixed `=`/space block must keep every option."""
        main = tmp_path / "config"
        main.write_text(
            "Host eq-mixed\n"
            "    HostName=eqmixed.example.com\n"  # no-space =
            "    Port 2222\n"                       # space
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "eq-mixed")
        assert c is not None
        assert c.port == 2222
        assert c.hostname == "eqmixed.example.com"

    def test_equals_with_spaces_parsed(self, tmp_path):
        """
        `keyword = value` (whitespace around the `=`) is spec-valid.  The `=`
        must be consumed as the separator (not folded into the value, which
        previously crashed `int('= 2222')` and discarded the whole host).
        """
        main = tmp_path / "config"
        main.write_text(
            "Host spaced-eq\n"
            "    HostName spacedeq.example.com\n"
            "    Port = 2222\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "spaced-eq")
        assert c is not None, "`Port = 2222` must parse, not discard the host"
        assert c.hostname == "spacedeq.example.com"
        assert c.port == 2222

    # -----------------------------------------------------------------------
    # ISSUE 3 – Multiple IdentityFile
    # -----------------------------------------------------------------------

    def test_multiple_identityfile_all_preserved(self, complex_cm):
        """
        The spec states "all these identities will be tried in sequence".
        Every IdentityFile entry must be preserved (in identity_files); keyfile
        keeps the first as the primary entry.
        """
        cm, *_ = complex_cm
        c = conn_by_nickname(cm, "multi-key")
        assert c is not None
        assert any("id_rsa" in f for f in c.identity_files), (
            "first IdentityFile (id_rsa) must be preserved"
        )
        assert any("id_ed25519" in f for f in c.identity_files), (
            "second IdentityFile (id_ed25519) must be preserved"
        )
        # keyfile is the primary (first) entry.
        assert "id_rsa" in c.keyfile

    # -----------------------------------------------------------------------
    # ISSUE 4 – Multiple CertificateFile
    # -----------------------------------------------------------------------

    def test_multiple_certificatefile_all_preserved(self, tmp_path):
        """
        The man page states "Multiple CertificateFile directives will add to
        the list of certificates used for authentication."  All entries must
        be preserved (in certificate_files); certificate keeps the first.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host multi-cert\n"
            "    HostName multi.example.com\n"
            "    CertificateFile ~/.ssh/cert1-cert.pub\n"
            "    CertificateFile ~/.ssh/cert2-cert.pub\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "multi-cert")
        assert c is not None
        assert any("cert1" in f for f in c.certificate_files), "cert1 must be preserved"
        assert any("cert2" in f for f in c.certificate_files), "cert2 must be preserved"
        assert "cert1" in c.certificate  # primary

    def test_multiple_identityfile_round_trip_preserved(self, tmp_path):
        """
        Writing a connection back to config must preserve every IdentityFile /
        CertificateFile entry (not collapse to the primary).
        """
        cm = make_cm()
        data = {
            'nickname': 'multi',
            'hostname': 'multi.example.com',
            'auth_method': 0,
            'key_select_mode': 2,  # dedicated key mode
            'keyfile': '/home/u/.ssh/id_rsa',
            'identity_files': ['/home/u/.ssh/id_rsa', '/home/u/.ssh/id_ed25519'],
            'certificate_files': ['/home/u/.ssh/a-cert.pub', '/home/u/.ssh/b-cert.pub'],
        }
        entry = cm.format_ssh_config_entry(data)
        assert entry.count('IdentityFile') == 2, entry
        assert '/home/u/.ssh/id_rsa' in entry
        assert '/home/u/.ssh/id_ed25519' in entry
        assert entry.count('CertificateFile') == 2, entry

    def test_single_identityfile_write_unchanged(self, tmp_path):
        """A single IdentityFile must still be written exactly once (no regression)."""
        cm = make_cm()
        data = {
            'nickname': 'single',
            'hostname': 'single.example.com',
            'auth_method': 0,
            'key_select_mode': 2,
            'keyfile': '/home/u/.ssh/id_rsa',
        }
        entry = cm.format_ssh_config_entry(data)
        assert entry.count('IdentityFile') == 1, entry
        assert '/home/u/.ssh/id_rsa' in entry

    # -----------------------------------------------------------------------
    # ISSUE 5 – ForwardAgent socket path and $ENV_VAR
    # -----------------------------------------------------------------------

    def test_forwardagent_socket_path_truthy(self, tmp_path):
        """
        The man page states ForwardAgent accepts "an explicit path to an agent
        socket".  Such a value enables agent forwarding (truthy).
        """
        main = tmp_path / "config"
        main.write_text(
            "Host fa-path\n"
            "    HostName fa.example.com\n"
            "    ForwardAgent /tmp/ssh-agent.sock\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "fa-path")
        assert c is not None
        assert c.forward_agent is True, "ForwardAgent /path must be truthy"

    def test_forwardagent_env_var_truthy(self, tmp_path):
        """
        The man page states ForwardAgent accepts "the name of an environment
        variable (beginning with $)".  Such a value is truthy.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host fa-env\n"
            "    HostName fa.example.com\n"
            "    ForwardAgent $SSH_AUTH_SOCK\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "fa-env")
        assert c is not None
        assert c.forward_agent is True, "ForwardAgent $SSH_AUTH_SOCK must be truthy"

    def test_forwardagent_no_is_false(self, tmp_path):
        """ForwardAgent no must remain falsey."""
        main = tmp_path / "config"
        main.write_text(
            "Host fa-no\n    HostName fa.example.com\n    ForwardAgent no\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "fa-no")
        assert c is not None
        assert c.forward_agent is False

    # -----------------------------------------------------------------------
    # ISSUE 6 – RemoteForward single-argument (SOCKS proxy mode)
    # -----------------------------------------------------------------------

    def test_remoteforward_single_arg_parsed_as_socks(self, tmp_path):
        """
        The man page states that with no destination argument the remote
        forwarding "will be established as a SOCKS proxy".  The single-argument
        form must produce a remote forwarding rule.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host socks-remote\n"
            "    HostName socks.example.com\n"
            "    RemoteForward 9999\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "socks-remote")
        assert c is not None
        remote = [r for r in c.forwarding_rules if r["type"] == "remote"]
        assert len(remote) == 1, (
            "RemoteForward 9999 (SOCKS mode) should produce a forwarding rule"
        )
        assert remote[0]["listen_port"] == 9999
        assert remote[0].get("socks") is True

    def test_remoteforward_two_arg_form_works(self, tmp_path):
        """The standard two-argument RemoteForward form must continue to work."""
        main = tmp_path / "config"
        main.write_text(
            "Host rfwd\n"
            "    HostName rfwd.example.com\n"
            "    RemoteForward 2222 localhost:22\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "rfwd")
        assert c is not None
        remote = [r for r in c.forwarding_rules if r["type"] == "remote"]
        assert len(remote) == 1
        assert remote[0]["listen_port"] == 2222

    def test_forwardagent_yes_still_works(self, tmp_path):
        """ForwardAgent yes must continue to work correctly."""
        main = tmp_path / "config"
        main.write_text(
            "Host fa-yes\n    HostName fa.example.com\n    ForwardAgent yes\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "fa-yes")
        assert c is not None
        assert c.forward_agent is True

    # -----------------------------------------------------------------------
    # ISSUE 7 – ${VAR} environment variable expansion in option values
    # -----------------------------------------------------------------------

    def test_identityfile_curly_brace_env_var_expanded(self, tmp_path, monkeypatch):
        """
        The man page lists IdentityFile among the keywords that "support
        environment variables", so `${HOME}/.ssh/id_rsa` must be expanded.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        main = tmp_path / "config"
        main.write_text(
            "Host env-key\n"
            "    HostName env.example.com\n"
            "    IdentityFile ${HOME}/.ssh/id_rsa\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "env-key")
        assert c is not None
        assert "${HOME}" not in c.keyfile, "${HOME} must be expanded"
        assert c.keyfile == os.path.join(str(tmp_path), ".ssh", "id_rsa")

    # -----------------------------------------------------------------------
    # Non-bugs confirmed against spec
    # -----------------------------------------------------------------------

    def test_empty_host_block_is_silent_noop(self, tmp_path):
        """
        An empty Host block has no options to display; the app discarding it
        is correct UX behaviour (nothing to put in the connection list).
        The subsequent host with options must not be affected.
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
        assert conn_by_nickname(cm, "ghost") is None
        assert conn_by_nickname(cm, "real") is not None

    # -----------------------------------------------------------------------
    # Other syntax edge cases
    # -----------------------------------------------------------------------

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

    # -----------------------------------------------------------------------
    # ISSUE 8 – %d / % token expansion in IdentityFile
    # -----------------------------------------------------------------------

    def test_identityfile_percent_d_token_expanded(self, tmp_path, monkeypatch):
        """
        The ssh_config(5) TOKENS section lists IdentityFile among the
        directives accepting percent tokens.  The host-independent %d (local
        home directory) must be expanded at parse time.  (Runtime tokens such
        as %h/%r have no value without a connection and are left intact for
        ssh / `ssh -G` to resolve.)
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        main = tmp_path / "config"
        main.write_text(
            "Host pct-key\n"
            "    HostName pct.example.com\n"
            "    IdentityFile %d/.ssh/id_rsa\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "pct-key")
        assert c is not None
        assert "%d" not in c.keyfile, "%d must be expanded to the home directory"
        assert c.keyfile == os.path.join(str(tmp_path), ".ssh", "id_rsa")

    def test_identityfile_runtime_token_left_intact(self, tmp_path):
        """
        Connection-dependent tokens (e.g. %h) cannot be resolved at parse time;
        they must be preserved verbatim so ssh resolves them at connect time.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host rt-key\n"
            "    HostName rt.example.com\n"
            "    IdentityFile ~/.ssh/id_%h\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "rt-key")
        assert c is not None
        assert c.keyfile.endswith("/.ssh/id_%h")

    # -----------------------------------------------------------------------
    # ISSUE 9 – `IdentityFile none` handled as "no identity files"
    # -----------------------------------------------------------------------

    def test_identityfile_none_treated_as_suppressor(self, tmp_path):
        """
        ssh_config(5): "an argument of none may be used to indicate no identity
        files should be loaded."  `IdentityFile none` must be treated as a
        suppressor, not stored as a literal key path called 'none'.
        """
        main = tmp_path / "config"
        main.write_text(
            "Host no-key\n"
            "    HostName nokey.example.com\n"
            "    IdentityFile none\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()
        c = conn_by_nickname(cm, "no-key")
        assert c is not None
        assert c.keyfile != "none", "'none' must not be stored as a key path"
        assert c.keyfile == ""
        assert c.identity_files == []
        assert c.identity_file_none is True

    # -----------------------------------------------------------------------
    # ISSUE 10 – % token expansion in Include paths
    # -----------------------------------------------------------------------

    def test_include_percent_token_expanded(self, tmp_path, monkeypatch):
        """
        ssh_config(5): Include accepts tokens (including %d, the local home
        directory).  `Include %d/.ssh/included.conf` must expand %d so the
        included file is found and its hosts load.
        """
        # Point %d (home) at tmp_path so the included file is discoverable.
        monkeypatch.setenv("HOME", str(tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "included.conf").write_text(
            "Host pct-host\n    HostName pct.example.com\n"
        )

        main = tmp_path / "config"
        main.write_text(
            "Include %d/.ssh/included.conf\n"
            "Host anchor\n"
            "    HostName anchor.example.com\n"
        )
        cm = make_cm()
        cm.ssh_config_path = str(main)
        cm.load_ssh_config()

        assert conn_by_nickname(cm, "anchor") is not None
        assert conn_by_nickname(cm, "pct-host") is not None, (
            "Include %d/... must expand %d to the home directory so the "
            "included hosts load"
        )
