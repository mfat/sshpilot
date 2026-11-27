"""The Directives view's GTK-free helpers: key files, jump hops, what an
answer of ssh -G comes to."""
from __future__ import annotations

import os

from sshpilot import advanced_config_fields as fields


def test_key_file_status(tmp_path):
    home = str(tmp_path)
    key = tmp_path / ".ssh" / "id"
    key.parent.mkdir()
    key.write_text("k")
    os.chmod(key, 0o644)
    status = fields.key_file_status("~/.ssh/id", home)
    if os.name == "nt":
        assert (status.state, status.path) == (fields.KEY_UNCHECKED, str(key))
    else:
        assert (status.state, status.path, status.mode) == (fields.KEY_OPEN, str(key), 0o644)
    key.chmod(0o600)
    assert fields.key_file_status('"~/.ssh/id"', home).state == (
        fields.KEY_UNCHECKED if os.name == "nt" else fields.KEY_OK
    )
    assert fields.key_file_status("~/.ssh/none", home).state == fields.KEY_MISSING
    for value in ("~/.ssh/id_%h", "${HOME}/.ssh/id", "~other/.ssh/id", ""):
        assert fields.key_file_status(value, home).state == fields.KEY_UNCHECKED


def test_paths_are_written_as_a_configuration_writes_them():
    assert fields.home_relative("/home/me/.ssh/id", "/home/me") == "~/.ssh/id"
    assert fields.home_relative("/home/me/my keys/id", "/home/me") == '"~/my keys/id"'
    assert fields.home_relative("/etc/ssh/key", "/home/me") == "/etc/ssh/key"


def test_jump_hops():
    assert fields.jump_hops("none") == [] and fields.jump_hops("") == []
    assert fields.jump_hops("a, admin@b:2222") == ["a", "admin@b:2222"]
    assert [fields.hop_host(h) for h in (
        "admin@bastion:2222", "ssh://u@h:22", "[::1]:22", "plain",
    )] == ["bastion", "h", "::1", "plain"]
    # ssh_config(5): a hop is [user@]host[:port] or an ssh URI, which may
    # end in a slash.
    assert [fields.hop_host(h) for h in (
        "ssh://gate/", "ssh://admin@gate:2222/", "ssh://admin@[::1]:22",
    )] == ["gate", "gate", "::1"]


def test_effective_values_flag_missing_and_readable_key_files(tmp_path):
    good = tmp_path / "id_good"
    good.write_text("k")
    good.chmod(0o600)
    open_key = tmp_path / "id_open"
    open_key.write_text("k")
    open_key.chmod(0o644)
    missing = tmp_path / "id_gone"
    values = [str(good), str(open_key), str(missing), "none", "~/%h_key"]

    found = fields.value_file_problems("identityfile", values, default=False,
                                       home=str(tmp_path))
    expected = [(fields.KEY_MISSING, str(missing))]
    if os.name != "nt":
        expected.insert(0, (fields.KEY_OPEN, str(open_key)))
    assert [(s.state, s.path) for s in found] == expected
    # A certificate is public: only a missing one is a problem.
    found = fields.value_file_problems("certificatefile", [str(open_key), str(missing)],
                                       default=False)
    assert [s.state for s in found] == [fields.KEY_MISSING]
    # OpenSSH's own default key list is tried only where the files exist.
    assert fields.value_file_problems("identityfile", [str(missing)], default=True) == []
    assert fields.value_file_problems("user", [str(missing)], default=False) == []


def test_connection_summary_reads_what_the_configuration_sets(tmp_path):
    home = str(tmp_path)
    values = {
        "hostname": ["10.20.4.15"], "user": ["postgres"], "port": ["5432"],
        "proxyjump": ["admin@bastion:2222,edge"], "proxycommand": ["none"],
        "identityfile": [f"{home}/.ssh/db_key", "~/.ssh/other key"],
        "identitiesonly": ["yes"],
    }
    summary = fields.connection_summary(values, defaults=set(), home=home)
    assert summary.hops == ["admin@bastion:2222", "edge"]
    assert summary.keys == ["~/.ssh/db_key", '"~/.ssh/other key"']
    assert summary.identities_only and not summary.port_default
    assert (summary.hostname, summary.user, summary.port) == ("10.20.4.15", "postgres", "5432")


def test_connection_summary_leaves_openssh_defaults_out():
    values = {
        "hostname": ["web1"], "user": ["me"], "port": ["22"], "proxyjump": ["none"],
        "proxycommand": ["nc -X 5 -x proxy:1080 %h %p"], "identityfile": ["~/.ssh/id_rsa"],
    }
    summary = fields.connection_summary(values, defaults={"port", "identityfile"})
    assert summary.port_default and summary.keys == [] and summary.hops == []
    assert summary.proxy_command.startswith("nc ")
