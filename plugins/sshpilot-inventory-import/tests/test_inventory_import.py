"""Tests for Inventory Import. Pure parsing/diff logic is tested directly."""

import importlib.util
import os
import sys

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "inventory_import_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Conn:
    def __init__(self, nickname, host, username="", port=22):
        self.nickname = nickname
        self.host = host
        self.username = username
        self.port = port


def test_parse_plain_hosts_basic():
    mod = _load()
    text = "web.example.com\n192.168.1.5:2222\n10.0.0.1 db.internal"
    rows = mod.parse_plain_hosts(text)
    assert len(rows) == 3
    assert rows[0].host == "web.example.com"
    assert rows[1].port == 2222
    assert rows[2].host == "db.internal"


def test_parse_csv_with_header():
    mod = _load()
    text = "nickname,host,user,port\nweb,10.0.0.1,deploy,22\n"
    rows = mod.parse_csv_hosts(text)
    assert len(rows) == 1
    assert rows[0].nickname == "web"
    assert rows[0].host == "10.0.0.1"
    assert rows[0].username == "deploy"


def test_parse_ansible_ini():
    mod = _load()
    text = """
[webservers]
web1 ansible_host=10.0.0.1 ansible_user=deploy
web2 ansible_host=10.0.0.2
"""
    rows = mod.parse_ansible_ini(text)
    by_nick = {r.nickname: r for r in rows}
    assert by_nick["web1"].host == "10.0.0.1"
    assert by_nick["web1"].username == "deploy"
    assert by_nick["web2"].group == "webservers"


def test_parse_ansible_yaml_simple():
    mod = _load()
    text = """
webservers:
  hosts:
    web1:
      ansible_host: 10.0.0.1
      ansible_user: root
"""
    rows = mod.parse_ansible_yaml_simple(text)
    assert len(rows) == 1
    assert rows[0].host == "10.0.0.1"
    assert rows[0].username == "root"


def test_diff_inventory_new_and_existing():
    mod = _load()
    rows = [
        mod.HostRow("newhost", "10.0.0.9"),
        mod.HostRow("web", "10.0.0.1"),
    ]
    existing = [_Conn("web", "10.0.0.1")]
    diff = mod.diff_inventory(rows, existing)
    assert diff[0].status == mod.STATUS_NEW
    assert diff[1].status == mod.STATUS_EXISTS


def test_diff_inventory_changed_nickname():
    mod = _load()
    rows = [mod.HostRow("web", "10.0.0.99")]
    existing = [_Conn("web", "10.0.0.1")]
    diff = mod.diff_inventory(rows, existing)
    assert diff[0].status == mod.STATUS_CHANGED


def test_plan_import_buckets_by_group_and_status():
    mod = _load()
    diff = [
        mod.DiffRow(row=mod.HostRow("a", "10.0.0.1", group="prod"),
                    status=mod.STATUS_NEW),
        mod.DiffRow(row=mod.HostRow("b", "10.0.0.2"),  # no row group
                    status=mod.STATUS_NEW),
        mod.DiffRow(row=mod.HostRow("c", "10.0.0.3", group="prod"),
                    status=mod.STATUS_NEW),
        mod.DiffRow(row=mod.HostRow("d", "10.0.0.4"),
                    status=mod.STATUS_CHANGED, existing_nickname="d"),
        mod.DiffRow(row=mod.HostRow("e", "10.0.0.5"),
                    status=mod.STATUS_EXISTS, existing_nickname="e"),
    ]
    # Select everything; EXISTS must still be skipped.
    plan = mod.plan_import(diff, range(len(diff)),
                           default_user="root", default_group="fallback")

    # a + c go to their own row group "prod"; b falls back to "fallback".
    assert set(plan.new_by_group) == {"prod", "fallback"}
    assert [d["nickname"] for d in plan.new_by_group["prod"]] == ["a", "c"]
    assert [d["nickname"] for d in plan.new_by_group["fallback"]] == ["b"]
    assert plan.new_no_group == []
    # d is CHANGED → update bucket, with the fallback group.
    assert plan.changed == [("d", plan.changed[0][1], "fallback")]
    assert plan.changed[0][1]["host"] == "10.0.0.4"
    # default_user is applied to payloads.
    assert plan.new_by_group["prod"][0]["username"] == "root"


def test_plan_import_no_default_group_goes_to_root():
    mod = _load()
    diff = [mod.DiffRow(row=mod.HostRow("a", "10.0.0.1"), status=mod.STATUS_NEW)]
    plan = mod.plan_import(diff, [0])
    assert plan.new_by_group == {}
    assert [d["nickname"] for d in plan.new_no_group] == ["a"]


def test_plan_import_ignores_unselected():
    mod = _load()
    diff = [
        mod.DiffRow(row=mod.HostRow("a", "10.0.0.1"), status=mod.STATUS_NEW),
        mod.DiffRow(row=mod.HostRow("b", "10.0.0.2"), status=mod.STATUS_NEW),
    ]
    plan = mod.plan_import(diff, [1])
    assert [d["nickname"] for d in plan.new_no_group] == ["b"]


def test_activate_registers_page():
    mod = _load()

    class _Ctx:
        settings = type("S", (), {"get": staticmethod(lambda k, d=None: d),
                                   "set": staticmethod(lambda k, v: None)})()
        ui = type("U", (), {"register_page": staticmethod(
            lambda *a, **k: None)})()

    mod.Plugin().activate(_Ctx())
