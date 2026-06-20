"""Tests for Session Log. Pure logic is tested directly."""

import importlib.util
import os
import sys

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "session_log_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_record_open_and_close():
    mod = _load()
    store = mod.SessionLogStore()
    store.record_open(
        session_id="s1", nickname="web", host="10.0.0.1",
        opened_at="2026-01-01T10:00:00+00:00")
    assert store.record_close(
        session_id="s1",
        closed_at="2026-01-01T10:30:00+00:00")
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0]["duration_seconds"] == 1800


def test_ring_buffer_caps_entries():
    mod = _load()
    store = mod.SessionLogStore(max_entries=3)
    for index in range(5):
        sid = f"s{index}"
        store.record_open(session_id=sid, nickname=f"h{index}", host="1.1.1.1",
                          opened_at=f"2026-01-01T10:0{index}:00+00:00")
        store.record_close(session_id=sid,
                           closed_at=f"2026-01-01T10:0{index}:30+00:00")
    assert len(store.entries()) == 3


def test_totals_by_connection():
    mod = _load()
    store = mod.SessionLogStore({
        "entries": [
            {"nickname": "web", "host": "1.1.1.1", "duration_seconds": 60},
            {"nickname": "web", "host": "1.1.1.1", "duration_seconds": 120},
            {"nickname": "db", "host": "2.2.2.2", "duration_seconds": 30},
        ]
    })
    totals = store.totals_by_connection()
    assert totals[0] == ("web", 2, 180)
    assert totals[1] == ("db", 1, 30)


def test_export_csv():
    mod = _load()
    store = mod.SessionLogStore({
        "entries": [{
            "nickname": "web",
            "host": "1.1.1.1",
            "opened_at": "t1",
            "closed_at": "t2",
            "duration_seconds": 5,
        }]
    })
    csv_text = store.export_csv()
    assert "nickname,host" in csv_text
    assert "web,1.1.1.1" in csv_text


def test_filter_entries_matches_nickname_or_host():
    mod = _load()
    store = mod.SessionLogStore({
        "entries": [
            {"nickname": "prod-web", "host": "10.0.0.1"},
            {"nickname": "db", "host": "db.prod.internal"},
        ]
    })
    assert len(store.filter_entries(nickname="prod", host="prod")) == 2
    assert len(store.filter_entries(nickname="db", host="db")) == 1


def test_activate_subscribes_to_session_events():
    mod = _load()
    subscribed = {}

    class _Events:
        SESSION_OPENED = "session_opened"
        SESSION_CLOSED = "session_closed"

        @staticmethod
        def subscribe(event, callback):
            subscribed[event] = callback

    class _Ctx:
        settings = type("S", (), {"get": staticmethod(lambda k, d=None: d),
                                   "set": staticmethod(lambda k, v: None)})()
        ui = type("U", (), {"register_page": staticmethod(
            lambda *a, **k: None)})()
        events = _Events

    mod.Plugin().activate(_Ctx())
    assert mod.Events.SESSION_OPENED in subscribed
    assert mod.Events.SESSION_CLOSED in subscribed
