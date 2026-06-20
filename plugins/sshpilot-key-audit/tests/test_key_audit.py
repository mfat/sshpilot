"""Tests for SSH Key Audit. ssh-keygen output parsing and age/classify logic
are pure Python and tested with fixture strings. No GTK or ssh-keygen needed."""

import importlib.util
import os
import sys
from datetime import datetime

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "key_audit_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_parse_fingerprint_basic():
    mod = _load()
    out = mod.parse_fingerprint(
        "256 SHA256:abc123 me@laptop (ED25519)")
    assert out == {"bits": 256, "fingerprint": "SHA256:abc123",
                   "comment": "me@laptop", "type": "ED25519"}


def test_parse_fingerprint_comment_with_spaces_and_empty():
    mod = _load()
    spaced = mod.parse_fingerprint(
        "3072 SHA256:zzz work key for prod (RSA)")
    assert spaced["bits"] == 3072
    assert spaced["comment"] == "work key for prod"
    assert spaced["type"] == "RSA"

    no_comment = mod.parse_fingerprint("256 SHA256:qq  (ED25519)")
    assert no_comment["comment"] == ""
    assert no_comment["type"] == "ED25519"


def test_parse_fingerprint_garbage_returns_none():
    mod = _load()
    assert mod.parse_fingerprint("not a fingerprint line") is None
    assert mod.parse_fingerprint("") is None


def test_parse_cert_validity_window_and_forever():
    mod = _load()
    text = (
        "        Type: ssh-ed25519-cert-v01@openssh.com user certificate\n"
        "        Valid: from 2024-01-01T00:00:00 to 2025-01-01T00:00:00\n")
    out = mod.parse_cert_validity(text)
    assert out == {"forever": False,
                   "valid_from": "2024-01-01T00:00:00",
                   "valid_to": "2025-01-01T00:00:00"}

    assert mod.parse_cert_validity("        Valid: forever\n")["forever"] is True
    assert mod.parse_cert_validity("no validity here") is None


def test_cert_expiry_days():
    mod = _load()
    now = datetime(2024, 1, 1)
    assert mod.cert_expiry_days("2024-01-11T00:00:00", now) == 10
    assert mod.cert_expiry_days("2023-12-22T00:00:00", now) == -10
    assert mod.cert_expiry_days("garbage", now) is None
    assert mod.cert_expiry_days(None, now) is None


def test_age_days_and_classify():
    mod = _load()
    now = 1_000_000_000
    assert mod.age_days(now - 5 * 86400, now) == 5
    assert mod.age_days(now + 999, now) == 0           # future clamps to 0
    assert mod.classify(400, 365) == "old"
    assert mod.classify(100, 365) == "ok"


def test_activate_registers_page():
    mod = _load()

    class _Settings:
        def __init__(self): self._d = {}
        def get(self, k, d=None): return self._d.get(k, d)
        def set(self, k, v): self._d[k] = v

    class _Ctx:
        def __init__(self):
            self.settings = _Settings()
            self.pages = []
            self.subscribed = {}
            self.ui = self
            self.events = self

        def register_page(self, page_id, *a): self.pages.append(page_id)
        def subscribe(self, event, cb): self.subscribed[event] = cb

    ctx = _Ctx()
    mod.Plugin().activate(ctx)
    assert "keyaudit" in ctx.pages
    assert mod.Events.APP_STARTED in ctx.subscribed  # warn_on_start default True
