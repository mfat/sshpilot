"""Tests for Tailscale Sync. Status parsing / filtering / dedup are pure Python
and tested with a realistic `tailscale status --json` fixture. No GTK or
Tailscale needed."""

import importlib.util
import os
import sys

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "tailscale_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


STATUS = {
    "Self": {
        "HostName": "laptop",
        "DNSName": "laptop.tail1234.ts.net.",
        "TailscaleIPs": ["100.64.0.1"],
        "Online": True,
        "OS": "linux",
    },
    "Peer": {
        "nodekeyA": {
            "HostName": "web1",
            "DNSName": "web1.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.2", "fd7a::2"],
            "Online": True,
            "OS": "linux",
            "Tags": ["tag:server", "tag:prod"],
        },
        "nodekeyB": {
            "HostName": "nas",
            "DNSName": "nas.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.3"],
            "Online": False,
            "OS": "linux",
            "Tags": ["tag:storage"],
        },
    },
}


class _Conn:
    def __init__(self, nickname, host):
        self.nickname = nickname
        self.host = host


def test_parse_excludes_self_by_default():
    mod = _load()
    rows = mod.parse_tailscale_status(STATUS)
    names = [r["name"] for r in rows]
    assert names == ["nas", "web1"]            # sorted, no self
    web = next(r for r in rows if r["name"] == "web1")
    assert web["ip"] == "100.64.0.2"           # IPv4 preferred over IPv6
    assert web["online"] is True
    assert "tag:prod" in web["tags"]


def test_parse_dedupes_colliding_short_names():
    mod = _load()
    status = {"Peer": {
        "a": {"DNSName": "web.prod.tail.ts.net.", "TailscaleIPs": ["100.64.0.10"]},
        "b": {"DNSName": "web.staging.tail.ts.net.", "TailscaleIPs": ["100.64.0.11"]},
    }}
    rows = mod.parse_tailscale_status(status)
    names = sorted(r["name"] for r in rows)
    assert names == ["web", "web-2"]              # no collision
    assert len({r["name"] for r in rows}) == 2


def test_parse_include_self():
    mod = _load()
    rows = mod.parse_tailscale_status(STATUS, include_self=True)
    assert "laptop" in [r["name"] for r in rows]


def test_parse_garbage_is_empty():
    mod = _load()
    assert mod.parse_tailscale_status(None) == []
    assert mod.parse_tailscale_status({"Peer": "nope"}) == []


def test_matches_filter_text_and_tag():
    mod = _load()
    rows = mod.parse_tailscale_status(STATUS)
    web = next(r for r in rows if r["name"] == "web1")
    assert mod.matches_filter(web, "web") is True
    assert mod.matches_filter(web, "100.64.0.2") is True
    assert mod.matches_filter(web, "tag:prod") is True
    assert mod.matches_filter(web, "tag:storage") is False
    assert mod.matches_filter(web, "") is True


def test_peer_connection_data_prefers_dns_name():
    mod = _load()
    rows = mod.parse_tailscale_status(STATUS)
    web = next(r for r in rows if r["name"] == "web1")
    data = mod.peer_connection_data(web, "deploy")
    assert data["host"] == "web1.tail1234.ts.net"
    assert data["nickname"] == "web1"
    assert data["username"] == "deploy"
    assert data["protocol"] == "ssh" and data["port"] == 22


def test_dedup_new_skips_existing():
    mod = _load()
    rows = mod.parse_tailscale_status(STATUS)
    existing = [_Conn("web1", "anything")]              # by nickname
    new = mod.dedup_new(rows, existing)
    assert [r["name"] for r in new] == ["nas"]


def test_status_argv_when_present(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.shutil, "which",
                        lambda n: "/usr/bin/tailscale" if n == "tailscale" else None)
    assert mod.tailscale_status_argv() == [
        "/usr/bin/tailscale", "status", "--json"]


def test_activate_registers_page():
    mod = _load()

    class _Settings:
        def get(self, k, d=None): return d
        def set(self, k, v): pass

    class _Ctx:
        settings = _Settings()
        pages = []
        ui = type("U", (), {"register_page": staticmethod(
            lambda *a: _Ctx.pages.append(a[0]))})()

    mod.Plugin().activate(_Ctx())
    assert "tailscale" in _Ctx.pages
