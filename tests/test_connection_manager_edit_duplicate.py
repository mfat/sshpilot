"""Characterization tests for the ConnectionManager edit/duplicate helpers.

apply_connection_update and duplicate_connection were added when connection
persistence logic moved out of window.py. They had no direct coverage; these
pin their current behavior (persistence gating, attribute sync, nickname/copy
rules, private-key stripping) before any refactor. generate_duplicate_nickname
is covered by test_duplicate_nickname.py.
"""

import types
import pytest

cm_mod = pytest.importorskip("sshpilot.connection_manager")
ConnectionManager = cm_mod.ConnectionManager
Connection = cm_mod.Connection


def _cm(**attrs):
    cm = ConnectionManager.__new__(ConnectionManager)
    for k, v in attrs.items():
        setattr(cm, k, v)
    return cm


def _full_data(**over):
    d = dict(nickname="n", hostname="h", username="u", port=22,
             keyfile="", password="", x11_forwarding=False, auth_method=0)
    d.update(over)
    return d


# --- apply_connection_update ---------------------------------------------


def test_apply_update_returns_false_and_skips_sync_when_write_fails():
    cm = _cm(update_connection=lambda c, d: False)
    old = types.SimpleNamespace()
    assert cm.apply_connection_update(old, _full_data(nickname="new")) is False
    # No attribute sync happened on a failed write.
    assert not hasattr(old, "nickname")


def _delegating_update(c, d):
    """Mimic the real update_connection contract: persist (stubbed out here),
    then sync the live object via update_data()."""
    c.update_data(d)
    return True


def test_apply_update_syncs_attributes_on_success():
    cm = _cm(update_connection=_delegating_update)
    old = Connection(_full_data())
    old.ssh_cmd = ["stale"]
    ok = cm.apply_connection_update(
        old, _full_data(nickname="new", hostname="hh", username="uu", port=2200))
    assert ok is True
    assert old.nickname == "new"
    assert old.hostname == "hh"
    assert old.host == "new"           # host = the alias (nickname), never the address
    assert old.username == "uu"
    assert old.port == 2200
    assert old.ssh_cmd == []           # prepared command invalidated


def test_apply_update_normalizes_proxy_jump_string_to_list():
    cm = _cm(update_connection=_delegating_update)
    old = Connection(_full_data())
    cm.apply_connection_update(old, _full_data(proxy_jump="a, b  c"))
    assert old.proxy_jump == ["a", "b", "c"]


def test_apply_update_coerces_bad_auth_method_to_zero():
    cm = _cm(update_connection=_delegating_update)
    old = Connection(_full_data())
    cm.apply_connection_update(old, _full_data(auth_method="not-an-int"))
    assert old.auth_method == 0


# --- duplicate_connection -------------------------------------------------


def _plugin_conn(data):
    return types.SimpleNamespace(data=data, protocol="telnet",
                                 nickname=data.get("nickname", ""))


def _group_mgr():
    return types.SimpleNamespace(get_connection_groups=lambda n: [], groups={})


def test_duplicate_strips_private_keys_and_appends_copy_suffix():
    saved = {}
    cm = _cm(get_connections=list,
             update_connection=lambda c, d: (saved.update(d) or True))
    conn = _plugin_conn({
        "nickname": "orig", "command": "echo", "foo": "bar",
        "__internal": 1, "aliases": ["a"], "password_changed": True,
    })
    dup = cm.duplicate_connection(conn, _group_mgr())
    assert dup.nickname == "orig-Copy"
    # Private/volatile keys are dropped from the persisted copy.
    for k in ("__internal", "aliases", "password_changed"):
        assert k not in saved
    assert saved["foo"] == "bar"       # ordinary data carried over
    assert saved["nickname"] == "orig-Copy"


def test_duplicate_raises_runtimeerror_when_save_fails():
    cm = _cm(get_connections=list, update_connection=lambda c, d: False)
    with pytest.raises(RuntimeError):
        cm.duplicate_connection(_plugin_conn({"nickname": "orig"}), _group_mgr())


def test_duplicate_mirrors_original_group_membership():
    moved = []
    cm = _cm(get_connections=list, update_connection=lambda c, d: True)
    gm = types.SimpleNamespace(
        get_connection_groups=lambda n: ["grp1"],
        groups={"grp1": {}},
        move_connection=lambda nick, gid: moved.append((nick, gid)),
    )
    cm.duplicate_connection(_plugin_conn({"nickname": "orig"}), gm)
    assert moved == [("orig-Copy", "grp1")]
