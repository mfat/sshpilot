"""Regression guard: Connection.host / Connection.hostname are unified.

``host`` is ALWAYS the Host alias (falling back to the nickname);
``hostname`` is the HostName value or ''. Construction and in-place update
follow the same rule. These tests used to characterize the opposite — the
update path overwrote host with hostname and invented a hostname when the
key was absent; see docs/host-hostname-divergence.md for that history.
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

# `Host jumpbox` with no HostName line and no hostname key at all.
NO_HOSTNAME_KEY = {
    "nickname": "jumpbox",
    "host": "jumpbox",
    "username": "u",
}


def test_fresh_object_keeps_alias_and_address_separate():
    conn = Connection(dict(WITH_HOSTNAME))
    assert conn.host == "myserver"          # the Host alias
    assert conn.hostname == "203.0.113.7"   # the real address


def test_update_path_keeps_alias_and_address_separate():
    conn = Connection(dict(WITH_HOSTNAME))
    conn.update_data(dict(WITH_HOSTNAME))
    # Same rule as construction: the alias is never overwritten by the address.
    assert conn.host == "myserver"
    assert conn.hostname == "203.0.113.7"


def test_fresh_object_leaves_hostname_empty_when_key_missing():
    conn = Connection(dict(NO_HOSTNAME_KEY))
    assert conn.host == "jumpbox"
    assert conn.hostname == ""


def test_update_path_leaves_hostname_empty_when_key_missing():
    conn = Connection(dict(NO_HOSTNAME_KEY))
    conn.update_data(dict(NO_HOSTNAME_KEY))
    # No mirrored/invented hostname: absent stays empty.
    assert conn.host == "jumpbox"
    assert conn.hostname == ""


def test_safe_accessor_is_stable_across_both_paths():
    fresh = Connection(dict(WITH_HOSTNAME))
    updated = Connection(dict(WITH_HOSTNAME))
    updated.update_data(dict(WITH_HOSTNAME))
    assert fresh.resolve_host_identifier() == "myserver"
    assert updated.resolve_host_identifier() == "myserver"
