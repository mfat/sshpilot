"""Notification center model: activity, news, unread counts and persistence."""

import json
from datetime import date

import pytest

from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui

from sshpilot import notification_store as ns  # noqa: E402
from sshpilot.gtk import notification_messages as nm  # noqa: E402
from sshpilot.news_feed import NewsItem  # noqa: E402


def _no_timer(_delay, _callback):
    return 0


@pytest.fixture
def spec(monkeypatch):
    monkeypatch.setitem(
        nm.NOTIFICATIONS,
        "test.failed",
        nm.NotificationSpec(
            title="Copy to “{name}” failed",
            details="{error}",
            severity=nm.NotificationSeverity.ERROR,
        ),
    )


def _store(tmp_path, **kwargs):
    return ns.NotificationStore(str(tmp_path / "n.json"), schedule=_no_timer, **kwargs)


def _news(item_id, **overrides):
    values = dict(
        id=item_id,
        date_iso="2026-11-10T00:00:00+00:00",
        severity="info",
        title={"en": f"Title {item_id}"},
        body={"en": "Body"},
    )
    values.update(overrides)
    return NewsItem(**values)


def test_unknown_code_records_nothing(tmp_path):
    store = _store(tmp_path)
    assert store.notify("not.in.catalogue") is None
    assert store.entries(ns.TAB_ACTIVITY) == []
    assert store.unread_count == 0


def test_notify_records_code_params_and_fallback(tmp_path, spec):
    store = _store(tmp_path)
    entry_id = store.notify("test.failed", name="web", error="denied")
    entry = store.get(entry_id)
    assert entry.code == "test.failed"
    assert entry.params == {"name": "web", "error": "denied"}
    assert entry.fallback_title == "Copy to “web” failed"
    assert entry.severity == "error"
    assert store.unread_count == 1
    assert store.unread_activity == 1


def test_params_are_redacted_before_storage(tmp_path, spec):
    store = _store(tmp_path)
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    entry = store.get(store.notify("test.failed", name="web", error=secret))
    assert "abc" not in entry.params["error"]
    assert "REDACTED" in entry.params["error"]


def test_mark_read_dismiss_and_clear(tmp_path, spec):
    store = _store(tmp_path)
    first = store.notify("test.failed", name="a", error="x")
    store.notify("test.failed", name="b", error="y")
    store.set_news([_news("n1")])
    assert (store.unread_activity, store.unread_news, store.unread_count) == (2, 1, 3)

    store.mark_read(ns.TAB_ACTIVITY)
    assert (store.unread_activity, store.unread_news, store.unread_count) == (0, 1, 1)

    store.dismiss(first)
    assert len(store.entries(ns.TAB_ACTIVITY)) == 1

    store.clear(ns.TAB_ACTIVITY)
    assert store.entries(ns.TAB_ACTIVITY) == []
    assert len(store.entries(ns.TAB_NEWS)) == 1


def test_activity_is_capped(tmp_path, spec, monkeypatch):
    monkeypatch.setattr(ns, "ACTIVITY_LIMIT", 5)
    store = _store(tmp_path)
    ids = [store.notify("test.failed", name=str(i), error="e") for i in range(8)]
    kept = {e.id for e in store.entries(ns.TAB_ACTIVITY)}
    assert len(kept) == 5
    # Same-second timestamps tie; what matters is the cap, not which survive.
    assert kept <= set(ids)


def test_dismissed_news_never_returns(tmp_path):
    store = _store(tmp_path)
    store.set_news([_news("n1"), _news("n2")])
    n1 = next(e for e in store.entries(ns.TAB_NEWS) if e.news_id == "n1")
    store.dismiss(n1.id)
    store.set_news([_news("n1"), _news("n2")])
    assert [e.news_id for e in store.entries(ns.TAB_NEWS)] == ["n2"]
    assert "n1" in store.dismissed_news_ids


def test_clearing_news_remembers_every_item(tmp_path):
    store = _store(tmp_path)
    store.set_news([_news("n1"), _news("n2")])
    store.clear(ns.TAB_NEWS)
    store.set_news([_news("n1"), _news("n2"), _news("n3")])
    assert [e.news_id for e in store.entries(ns.TAB_NEWS)] == ["n3"]


def test_news_keeps_read_state_and_drops_retracted_items(tmp_path):
    store = _store(tmp_path)
    store.set_news([_news("n1"), _news("n2")])
    store.mark_read(ns.TAB_NEWS)
    store.set_news([_news("n1", title={"en": "Edited"})])
    entries = store.entries(ns.TAB_NEWS)
    assert [e.news_id for e in entries] == ["n1"]
    assert entries[0].read is True
    assert entries[0].title_i18n == {"en": "Edited"}
    assert store.unread_news == 0


def test_expired_news_is_dropped(tmp_path):
    store = _store(tmp_path, today=lambda: date(2026, 12, 1))
    store.set_news([_news("old", expires="2026-11-30"), _news("live", expires="2026-12-01")])
    assert [e.news_id for e in store.entries(ns.TAB_NEWS)] == ["live"]


def test_history_round_trips_and_rerenders(tmp_path, spec, monkeypatch):
    store = _store(tmp_path)
    store.notify("test.failed", name="web", error="denied")
    store.set_news([_news("n1")])
    store.dismiss(store.entries(ns.TAB_NEWS)[0].id)
    store.set_news([_news("n2")])
    store.flush()

    data = json.loads((tmp_path / "n.json").read_text())
    assert data["version"] == 1
    assert data["dismissed_news_ids"] == ["n1"]

    reloaded = _store(tmp_path)
    activity = reloaded.entries(ns.TAB_ACTIVITY)
    assert [e.params["name"] for e in activity] == ["web"]
    assert [e.news_id for e in reloaded.entries(ns.TAB_NEWS)] == ["n2"]
    assert reloaded.unread_count == 2

    # A code that later leaves the catalogue still shows its stored sentence.
    monkeypatch.delitem(nm.NOTIFICATIONS, "test.failed")
    from sshpilot.notification_center import entry_view

    assert entry_view(activity[0]).title == "Copy to “web” failed"


def test_corrupt_history_starts_empty(tmp_path):
    (tmp_path / "n.json").write_text("{not json")
    store = _store(tmp_path)
    assert store.entries(ns.TAB_ACTIVITY) == []


def test_history_file_is_private(tmp_path, spec):
    store = _store(tmp_path)
    store.notify("test.failed", name="web", error="x")
    store.flush()
    assert ((tmp_path / "n.json").stat().st_mode & 0o777) == 0o600


def test_plugin_text_entry(tmp_path):
    store = _store(tmp_path)
    entry = store.get(store.add_text_entry("Deploy done", details="ok",
                                           severity="success", source="deployer"))
    assert entry.fallback_title == "Deploy done"
    assert entry.severity == "success"
    assert entry.code is None
    assert store.add_text_entry("   ") is None
    odd = store.get(store.add_text_entry("x", severity="bogus"))
    assert odd.severity == "info"


def test_changed_signal_and_unread_notify(tmp_path, spec):
    store = _store(tmp_path)
    seen = []
    store.connect("changed", lambda *_a: seen.append("changed"))
    store.connect("notify::unread-count", lambda *_a: seen.append(store.unread_count))
    store.notify("test.failed", name="a", error="b")
    assert seen == [1, "changed"]


# -- the dialog ---------------------------------------------------------------

def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def _rows(dialog, tab):
    from gi.repository import Adw

    return [w for w in _walk(dialog._lists[tab]) if isinstance(w, Adw.ExpanderRow)]


def test_dialog_lists_entries_and_marks_the_visible_tab_read(tmp_path, spec):
    from sshpilot.notification_center import NotificationCenterDialog

    store = _store(tmp_path)
    store.notify("test.failed", name="web", error="denied")
    store.set_news([_news("n1", body={"en": "News body"})])
    dialog = NotificationCenterDialog(store, initial_tab=ns.TAB_ACTIVITY)

    [activity] = _rows(dialog, ns.TAB_ACTIVITY)
    [news] = _rows(dialog, ns.TAB_NEWS)
    assert activity.get_title() == "Copy to “web” failed"
    assert news.get_title() == "Title n1"
    # Opening on Activity reads it; News keeps its badge until viewed.
    assert store.unread_activity == 0
    assert store.unread_news == 1
    assert dialog._pages[ns.TAB_NEWS].get_badge_number() == 1

    dialog.stack.set_visible_child_name(ns.TAB_NEWS)
    assert store.unread_count == 0
    assert dialog._pages[ns.TAB_NEWS].get_badge_number() == 0
    # Read in the store, still marked new in this dialog.
    assert len(_rows(dialog, ns.TAB_NEWS)) == 1
    dialog.close()


def test_dialog_follows_dismiss_and_shows_empty_state(tmp_path, spec):
    from gi.repository import Adw, Gtk

    from sshpilot.notification_center import NotificationCenterDialog

    store = _store(tmp_path)
    store.notify("test.failed", name="web", error="denied")
    dialog = NotificationCenterDialog(store)
    [row] = _rows(dialog, ns.TAB_ACTIVITY)
    dismiss = next(
        w for w in _walk(row)
        if isinstance(w, Gtk.Button) and w.get_tooltip_text() == "Dismiss"
    )
    dismiss.emit("clicked")
    assert _rows(dialog, ns.TAB_ACTIVITY) == []
    assert any(isinstance(w, Adw.StatusPage) for w in _walk(dialog._lists[ns.TAB_ACTIVITY]))
    assert not dialog._clear_buttons[ns.TAB_ACTIVITY].get_sensitive()
    dialog.close()


def test_entry_action_button(tmp_path, monkeypatch):
    from gi.repository import Gtk

    from sshpilot.notification_center import NotificationCenterDialog

    monkeypatch.setitem(
        nm.NOTIFICATIONS,
        "test.action",
        nm.NotificationSpec(
            title="Check the log",
            action=nm.ActionSpec("Open Log", "win.view-logs"),
        ),
    )
    store = _store(tmp_path)
    store.notify("test.action")
    dialog = NotificationCenterDialog(store)
    ran = []
    monkeypatch.setattr(dialog, "_run_action", lambda name, target: ran.append((name, target)))
    [row] = _rows(dialog, ns.TAB_ACTIVITY)
    button = next(
        w for w in _walk(row) if isinstance(w, Gtk.Button) and w.get_label() == "Open Log"
    )
    button.emit("clicked")
    assert ran == [("win.view-logs", None)]
    dialog.close()
