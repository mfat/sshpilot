"""Characterization of a KNOWN inconsistency — see docs/host-hostname-divergence.md.

Connection.host / Connection.hostname get different values for the SAME input
data depending on whether the object was freshly constructed (Connection())
or updated in place (update_data()). These tests pin the current divergent
behavior so it is visible and so a future unification (host = alias, always)
knows exactly which assertions to flip. They do NOT bless the behavior.
"""

import asyncio

from sshpilot.connection_manager import Connection

asyncio.set_event_loop(asyncio.new_event_loop())


# `Host myserver` + `HostName 203.0.113.7` as parse_host_config emits it.
WITH_HOSTNAME = {
    "nickname": "myserver",
    "host": "myserver",
    "hostname": "203.0.113.7",
    "username": "u",
}

# `Host jumpbox` with no HostName line; parse emits hostname='' but a raw
# caller-supplied dict may omit the key entirely — that's the divergent case.
NO_HOSTNAME_KEY = {
    "nickname": "jumpbox",
    "host": "jumpbox",
    "username": "u",
}


def test_fresh_object_keeps_alias_and_address_separate():
    conn = Connection(dict(WITH_HOSTNAME))
    assert conn.host == "myserver"          # the Host alias
    assert conn.hostname == "203.0.113.7"   # the real address


def test_update_path_overwrites_alias_with_hostname():
    conn = Connection(dict(WITH_HOSTNAME))
    conn.update_data(dict(WITH_HOSTNAME))   # same data, applied via update
    # DIVERGENCE: identical input, but host is now the address, not the alias.
    assert conn.host == "203.0.113.7"
    assert conn.hostname == "203.0.113.7"


def test_fresh_object_leaves_hostname_empty_when_key_missing():
    conn = Connection(dict(NO_HOSTNAME_KEY))
    assert conn.host == "jumpbox"
    assert conn.hostname == ""              # no HostName -> correctly empty


def test_update_path_mirrors_host_into_missing_hostname():
    conn = Connection(dict(NO_HOSTNAME_KEY))
    conn.update_data(dict(NO_HOSTNAME_KEY))
    # DIVERGENCE: hostname now claims a value that exists nowhere in the input.
    assert conn.host == "jumpbox"
    assert conn.hostname == "jumpbox"


def test_safe_accessor_is_stable_across_both_paths():
    """resolve_host_identifier() reads data['host'] first, so the ssh target
    stays the alias regardless of which path last touched the object."""
    fresh = Connection(dict(WITH_HOSTNAME))
    updated = Connection(dict(WITH_HOSTNAME))
    updated.update_data(dict(WITH_HOSTNAME))
    assert fresh.resolve_host_identifier() == "myserver"
    assert updated.resolve_host_identifier() == "myserver"
