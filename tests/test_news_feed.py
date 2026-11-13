"""news.json parsing, targeting and fetch behaviour (no network)."""

import json
from datetime import date
from pathlib import Path

import pytest

from sshpilot import news_feed as nf

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 20)


def _parse(items, **kwargs):
    env = dict(version="5.1.0", platform="linux", package="flatpak", today=TODAY)
    env.update(kwargs)
    return nf.parse_feed({"version": 1, "items": items}, **env)


def _item(**overrides):
    item = {"id": "a", "date": "2026-11-19", "title": {"en": "Hello"}}
    item.update(overrides)
    return item


def test_shipped_feed_is_valid_and_empty():
    data = json.loads((ROOT / "data" / "news.json").read_text())
    assert nf.parse_feed(data) == []


def test_minimal_item_parses():
    [item] = _parse([_item(body={"en": "Body", "fa": "متن"},
                           link={"url": "https://example.org", "label": {"en": "More"}})])
    assert item.id == "a"
    assert item.severity == "info"
    assert item.title == {"en": "Hello"}
    assert item.body == {"en": "Body", "fa": "متن"}
    assert item.link_url == "https://example.org"
    assert item.date_iso.startswith("2026-11-19")


@pytest.mark.parametrize("bad", [
    {"id": ""},
    {"date": "yesterday"},
    {"severity": "loud"},
    {"title": {"fa": "no english"}},
    {"title": "plain string"},
    {"link": {"url": "http://insecure.example"}},
    {"link": "https://example.org"},
    {"min_version": "five"},
    {"platforms": "linux"},
    {"expires": "soon"},
])
def test_invalid_items_are_skipped_not_the_feed(bad):
    items = _parse([_item(**bad), _item(id="good")])
    assert [i.id for i in items] == ["good"]


def test_version_range_is_inclusive():
    items = _parse([
        _item(id="min-ok", min_version="5.1"),
        _item(id="min-no", min_version="5.1.1"),
        _item(id="max-ok", max_version="5.1.0"),
        _item(id="max-no", max_version="5.0.9"),
    ])
    assert [i.id for i in items] == ["min-ok", "max-ok"]


def test_platform_and_package_targeting():
    items = _parse([
        _item(id="linux", platforms=["linux"]),
        _item(id="mac", platforms=["macos"]),
        _item(id="flatpak", packages=["flatpak"]),
        _item(id="appimage", packages=["appimage"]),
        _item(id="everyone"),
    ])
    assert [i.id for i in items] == ["linux", "flatpak", "everyone"]


def test_expiry_is_the_last_day_shown():
    items = _parse([_item(id="today", expires="2026-11-20"),
                    _item(id="gone", expires="2026-11-19")])
    assert [i.id for i in items] == ["today"]


def test_duplicate_ids_keep_the_first():
    items = _parse([_item(title={"en": "First"}), _item(title={"en": "Second"})])
    assert [i.title["en"] for i in items] == ["First"]


@pytest.mark.parametrize("feed", [None, [], {"version": 2, "items": []}, {"version": 1}])
def test_unusable_feed(feed):
    assert nf.parse_feed(feed) is None


def test_pick_text_language_fallback():
    texts = {"en": "Hello", "pt": "Olá", "fa": "سلام"}
    assert nf.pick_text(texts, ["pt_BR", "pt"]) == "Olá"
    assert nf.pick_text(texts, ["fa"]) == "سلام"
    assert nf.pick_text(texts, ["de"]) == "Hello"
    assert nf.pick_text({}, ["de"]) == ""


def test_fetch_failure_is_silent():
    def broken():
        raise OSError("offline")

    assert nf.fetch_news(broken) is None


def test_fetch_parses_bytes():
    payload = json.dumps({"version": 1, "items": [_item(id="x")]}).encode()
    items = nf.fetch_news(lambda: payload)
    assert [i.id for i in items] == ["x"]


def test_fetch_rejects_garbage():
    assert nf.fetch_news(lambda: b"\xff\xfe not json") is None


def test_package_detection(monkeypatch):
    import sshpilot.platform_utils as pu

    monkeypatch.setattr(pu, "is_flatpak", lambda: False)
    monkeypatch.setenv("APPIMAGE", "/tmp/x.AppImage")
    assert nf.current_package() == "appimage"
    monkeypatch.setattr(pu, "is_flatpak", lambda: True)
    assert nf.current_package() == "flatpak"


def test_news_off_means_no_fetch(monkeypatch):
    pytest.importorskip("gi")
    import types

    from sshpilot import window as window_module

    calls = []
    monkeypatch.setattr(nf, "fetch_news_async", lambda *a, **k: calls.append(a))
    app = types.SimpleNamespace(notification_store=types.SimpleNamespace(set_news=None))
    config = types.SimpleNamespace(get_setting=lambda key, default=None: False)
    stub = types.SimpleNamespace(get_application=lambda: app, config=config)
    window_module.MainWindow._start_news_fetch(stub)
    assert calls == []

    config.get_setting = lambda key, default=None: True
    window_module.MainWindow._start_news_fetch(stub)
    window_module.MainWindow._start_news_fetch(stub)  # once per app run
    assert len(calls) == 1
