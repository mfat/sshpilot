"""
Cross-validate the in-app SSH config parser against `ssh -G`.

`ssh -G <host>` is OpenSSH's own resolver — the ground truth for how a config is
interpreted. For every concrete (non-wildcard) host we parse, this module asserts
that our parsed values agree with `ssh -G` for the fields the app handles. That
makes the parser self-checking: regressions surface without hand-maintained
expected values, and the checks stay correct across OpenSSH versions.

Caveats baked into the comparisons (see module docstring of
test_complex_config_parsing.py for the underlying parser behaviour):

  * `ssh -G` does NOT expand ``~`` in IdentityFile/CertificateFile, so both sides
    are normalised with expanduser+realpath before comparing.
  * `ssh -G` reports the *resolved* config (defaults + ``Host *`` merged in). We
    therefore compare identity/certificate lists with ``<=`` (our authored entries
    must be a subset of what ssh resolves), and only assert scalars we set.
  * Runtime tokens (%h, %r, ...) are deliberately NOT used in these fixtures —
    ssh expands them but our parser leaves them literal by design.
  * Wildcard / negated / Match entries are not queryable via ``ssh -G`` and are
    skipped (they live in the loader rules, not connections).
"""

import os
import shutil

import pytest

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.ssh_config_utils import get_effective_ssh_config


pytestmark = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="ssh binary not available"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _listen_port(spec: str):
    """Extract the listen port from an ``ssh -G`` forward listen token.

    Tokens look like ``5432`` or ``[::1]:8080`` or ``[127.0.0.1]:1081``.
    """
    token = spec.strip()
    if ":" in token:
        token = token.rsplit(":", 1)[1]
    token = token.strip("[]")
    try:
        return int(token)
    except ValueError:
        return None


def _forward_listen_ports(eff: dict, key: str):
    """Multiset (sorted list) of listen ports ssh -G reports for a forward type."""
    ports = []
    for entry in _as_list(eff.get(key)):
        first = entry.split()[0] if entry.split() else ""
        port = _listen_port(first)
        if port is not None:
            ports.append(port)
    return sorted(ports)


def _scalar(conn, key, default=""):
    return conn.data.get(key, default) or default


def assert_connection_matches_ssh_g(conn, config_path):
    """Assert a parsed connection agrees with ``ssh -G`` for the handled fields."""
    eff = get_effective_ssh_config(conn.nickname, config_file=str(config_path))
    assert eff, f"ssh -G returned nothing for {conn.nickname!r}"

    data = conn.data
    # --- scalars we explicitly handle -------------------------------------
    assert int(data.get("port") or 22) == int(eff["port"]), (
        f"{conn.nickname}: port {data.get('port')} != ssh -G {eff['port']}"
    )
    assert data.get("username") == eff["user"], (
        f"{conn.nickname}: user {data.get('username')!r} != ssh -G {eff['user']!r}"
    )
    # When HostName is omitted the app keeps hostname empty and falls back to the
    # alias; ssh -G reports the alias.
    expected_hostname = data.get("hostname") or conn.nickname
    assert expected_hostname == eff["hostname"], (
        f"{conn.nickname}: hostname {expected_hostname!r} != ssh -G {eff['hostname']!r}"
    )

    # ForwardAgent: truthy unless ssh resolves it to "no".
    ssh_fa = str(eff.get("forwardagent", "no")).strip().lower()
    assert bool(data.get("forward_agent")) == (ssh_fa != "no"), (
        f"{conn.nickname}: forward_agent {data.get('forward_agent')} vs ssh -G {ssh_fa!r}"
    )

    # --- lists: our authored entries must be a subset of ssh's resolution --
    our_ids = {_norm(f) for f in _as_list(data.get("identity_files"))}
    ssh_ids = {_norm(f) for f in _as_list(eff.get("identityfile"))}
    assert our_ids <= ssh_ids, (
        f"{conn.nickname}: identity_files {our_ids} not a subset of ssh -G {ssh_ids}"
    )

    our_certs = {_norm(f) for f in _as_list(data.get("certificate_files"))}
    ssh_certs = {_norm(f) for f in _as_list(eff.get("certificatefile"))}
    assert our_certs <= ssh_certs, (
        f"{conn.nickname}: certificate_files {our_certs} not a subset of ssh -G {ssh_certs}"
    )

    # --- ProxyJump host tokens --------------------------------------------
    proxy_jump = _as_list(data.get("proxy_jump"))
    if proxy_jump:
        ssh_pj = str(eff.get("proxyjump", "")).strip()
        for hop in proxy_jump:
            assert hop in ssh_pj, (
                f"{conn.nickname}: ProxyJump hop {hop!r} missing from ssh -G {ssh_pj!r}"
            )

    # --- Port forwardings: listen ports per type --------------------------
    forwarding_rules = _as_list(data.get("forwarding_rules"))
    for our_type, ssh_key in (
        ("local", "localforward"),
        ("remote", "remoteforward"),
        ("dynamic", "dynamicforward"),
    ):
        our_ports = sorted(
            r["listen_port"] for r in forwarding_rules if r["type"] == our_type
        )
        assert our_ports == _forward_listen_ports(eff, ssh_key), (
            f"{conn.nickname}: {our_type} listen ports {our_ports} != "
            f"ssh -G {_forward_listen_ports(eff, ssh_key)}"
        )


def load_and_cross_check(tmp_path, config_text):
    main = tmp_path / "config"
    main.write_text(config_text)
    loaded = load_ssh_configuration(main, isolated=False)
    for conn in loaded.connections:
        assert_connection_matches_ssh_g(conn, main)
    return loaded, main


# ---------------------------------------------------------------------------
# A rich config exercising every fixed parser issue, cross-checked end-to-end
# ---------------------------------------------------------------------------

RICH_CONFIG = """\
Host plain
    HostName plain.example.com
    User alice
    Port 2222

# tab-separated (ISSUE 1)
Host tabhost
\tHostName\ttab.example.com
\tPort\t2200

# = separator (ISSUE 2)
Host eqhost
    HostName=eq.example.com
    Port=2201

# spaced = (ISSUE 2b)
Host spacedeq
    HostName spacedeq.example.com
    Port = 2202

# multiple IdentityFile (ISSUE 3)
Host multikey
    HostName multikey.example.com
    IdentityFile ~/.ssh/id_rsa
    IdentityFile ~/.ssh/id_ed25519

# ForwardAgent path (ISSUE 5a) and env (ISSUE 5b)
Host fapath
    HostName fapath.example.com
    ForwardAgent /tmp/agent.sock

Host faenv
    HostName faenv.example.com
    ForwardAgent $SSH_AUTH_SOCK

Host fano
    HostName fano.example.com
    ForwardAgent no

# RemoteForward SOCKS single-arg (ISSUE 6)
Host socksr
    HostName socksr.example.com
    RemoteForward 9999

# forwardings of every kind
Host fwd
    HostName fwd.example.com
    LocalForward 5432 localhost:5432
    DynamicForward 1080
    RemoteForward 2222 localhost:22

# ProxyJump multi-hop
Host viajump
    HostName viajump.example.com
    ProxyJump jumpa,jumpb

# wildcard / global defaults — must be skipped (a rule, not a connection)
Host *
    ServerAliveInterval 60
"""


class TestParserMatchesSshG:
    def test_rich_config_all_hosts_match(self, tmp_path):
        loaded, _ = load_and_cross_check(tmp_path, RICH_CONFIG)
        # Sanity: the wildcard host is a rule, not a cross-checked connection.
        assert all(c.id != "*" for c in loaded.connections)
        assert len(loaded.connections) >= 11

    def test_multikey_subset_is_exact(self, tmp_path):
        """For an explicitly-keyed host, ssh -G returns exactly our entries
        (no defaults appended), so the subset is in fact an equality."""
        main = tmp_path / "config"
        main.write_text(
            "Host mk\n"
            "    HostName mk.example.com\n"
            "    IdentityFile ~/.ssh/id_rsa\n"
            "    IdentityFile ~/.ssh/id_ed25519\n"
        )
        conn = next(
            c for c in load_ssh_configuration(main, isolated=False).connections
            if c.id == "mk"
        )
        eff = get_effective_ssh_config("mk", config_file=str(main))
        our = {_norm(f) for f in _as_list(conn.data.get("identity_files"))}
        ssh = {_norm(f) for f in _as_list(eff.get("identityfile"))}
        assert our == ssh, "explicit IdentityFile set should match ssh -G exactly"

    def test_repeated_host_block_merge_matches_ssh_g(self, tmp_path):
        """Repeated same-name Host blocks must merge to exactly what ssh -G resolves."""
        main = tmp_path / "config"
        main.write_text(
            "Host repeated\n"
            "    HostName first.example.com\n"
            "    Port 2001\n"
            "Host repeated\n"
            "    User seconduser\n"
            "    Port 2002\n"
        )
        loaded = load_ssh_configuration(main, isolated=False)
        reps = [c for c in loaded.connections if c.id == "repeated"]
        assert len(reps) == 1
        assert_connection_matches_ssh_g(reps[0], main)

    def test_forwardagent_path_agrees_with_ssh_g(self, tmp_path):
        main = tmp_path / "config"
        main.write_text(
            "Host fa\n    HostName fa.example.com\n    ForwardAgent /tmp/a.sock\n"
        )
        conn = next(
            c for c in load_ssh_configuration(main, isolated=False).connections
            if c.id == "fa"
        )
        eff = get_effective_ssh_config("fa", config_file=str(main))
        # ssh -G keeps the literal path; we map any non-"no" value to truthy.
        assert str(eff.get("forwardagent")).lower() != "no"
        assert conn.data.get("forward_agent") is True
