"""Tests for Hetzner Cloud browser. Response parsing / dedup are pure Python and
tested with a sample API payload. The network client is not exercised here."""

import importlib.util
import os
import sys

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "hetzner_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


SERVERS = [
    {
        "id": 1,
        "name": "web1",
        "status": "running",
        "public_net": {"ipv4": {"ip": "1.2.3.4"}},
        "labels": {"env": "prod"},
    },
    {
        "id": 2,
        "name": "private-only",
        "status": "running",
        "public_net": {"ipv4": None},  # no public IPv4
        "labels": {},
    },
]


class _Conn:
    def __init__(self, nickname, host):
        self.nickname = nickname
        self.host = host


def test_servers_from_response_flags_missing_ip():
    mod = _load()
    rows = mod.servers_from_response(SERVERS)
    by_name = {r["name"]: r for r in rows}
    assert by_name["web1"]["ip"] == "1.2.3.4"
    assert by_name["web1"]["has_ip"] is True
    assert by_name["private-only"]["has_ip"] is False


def test_servers_from_response_garbage():
    mod = _load()
    assert mod.servers_from_response(None) == []
    assert mod.servers_from_response(["nope", 5]) == []


def test_server_connection_data_defaults_root():
    mod = _load()
    row = {"name": "web1", "ip": "1.2.3.4", "has_ip": True}
    data = mod.server_connection_data(row, "")
    assert data["username"] == "root"
    assert data["host"] == "1.2.3.4"
    assert data["protocol"] == "ssh" and data["port"] == 22
    assert mod.server_connection_data(row, "deploy")["username"] == "deploy"


def test_dedup_new_skips_existing_and_no_ip():
    mod = _load()
    rows = mod.servers_from_response(SERVERS)
    existing = [_Conn("web1", "9.9.9.9")]          # web1 already saved by name
    new = mod.dedup_new(rows, existing)
    assert new == []                                # web1 dup, private-only no IP

    new2 = mod.dedup_new(rows, [])
    assert [r["name"] for r in new2] == ["web1"]    # private-only excluded (no IP)


def test_activate_registers_page():
    mod = _load()

    class _Secrets:
        def get(self, k): return None
        def set(self, k, v): pass
        def delete(self, k): return False

    class _Settings:
        def get(self, k, d=None): return d
        def set(self, k, v): pass

    class _Ctx:
        secrets = _Secrets()
        settings = _Settings()
        pages = []
        ui = type("U", (), {"register_page": staticmethod(
            lambda *a: _Ctx.pages.append(a[0]))})()

    mod.Plugin().activate(_Ctx())
    assert "hetzner" in _Ctx.pages
