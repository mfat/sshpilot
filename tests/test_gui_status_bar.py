"""The status bar widget, its controller and its place in the main window."""

import threading
import types

import pytest

from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui

from gi.repository import Gtk  # noqa: E402

from sshpilot import status_bar as sb  # noqa: E402
from sshpilot.gtk import notification_messages as nm  # noqa: E402
from sshpilot.gtk import status_bar_messages as sm  # noqa: E402


@pytest.fixture
def spec(monkeypatch):
    monkeypatch.setitem(
        sm.STATUS_MESSAGES,
        "test.hello",
        sm.StatusMessageSpec("Hello {name}", level=sm.StatusLevel.WARNING, timeout_s=None),
    )


def test_badge_text():
    assert sb.badge_text(0) == ""
    assert sb.badge_text(-3) == ""
    assert sb.badge_text(7) == "7"
    assert sb.badge_text(99) == "99"
    assert sb.badge_text(100) == "99+"


def test_bar_starts_empty_with_both_buttons():
    bar = sb.StatusBar()
    assert bar.message_text() == ""
    assert not bar.message_icon.get_visible()
    assert bar.logs_button.get_action_name() == "win.view-logs"
    assert bar.notifications_button.get_action_name() == "win.show-notifications"
    assert not bar.badge_label.get_visible()


def test_badge_follows_unread_count():
    bar = sb.StatusBar()
    bar.set_unread_count(3)
    assert bar.badge_label.get_visible()
    assert bar.badge_label.get_text() == "3"
    bar.set_unread_count(250)
    assert bar.badge_label.get_text() == "99+"
    bar.set_unread_count(0)
    assert not bar.badge_label.get_visible()


def test_post_unknown_code_shows_nothing():
    controller = sb.StatusBarController(sb.StatusBar())
    controller.post("not.curated", name="x")
    assert controller.bar.message_text() == ""


def test_post_shows_curated_message(spec):
    controller = sb.StatusBarController(sb.StatusBar())
    controller.post("test.hello", name="web")
    bar = controller.bar
    assert bar.message_text() == "Hello web"
    assert bar.message_icon.get_visible()
    assert bar.message_icon.has_css_class("warning")


def test_level_class_is_replaced_not_accumulated(spec, monkeypatch):
    monkeypatch.setitem(
        sm.STATUS_MESSAGES, "test.ok",
        sm.StatusMessageSpec("Ok", level=sm.StatusLevel.SUCCESS, timeout_s=None),
    )
    controller = sb.StatusBarController(sb.StatusBar())
    controller.post("test.hello", name="a")
    controller.post("test.ok")
    icon = controller.bar.message_icon
    assert icon.has_css_class("success")
    assert not icon.has_css_class("warning")


def test_timeout_clears_the_message(monkeypatch):
    sources = {}

    def fake_timeout(seconds, callback):
        sources["cb"] = callback
        sources["seconds"] = seconds
        return 4242

    monkeypatch.setattr(sb.GLib, "timeout_add_seconds", fake_timeout)
    monkeypatch.setattr(sb.GLib, "source_remove", lambda _id: True)
    monkeypatch.setitem(sm.STATUS_MESSAGES, "test.brief", sm.StatusMessageSpec("Brief", timeout_s=3))
    controller = sb.StatusBarController(sb.StatusBar())
    controller.post("test.brief")
    assert controller.bar.message_text() == "Brief"
    assert sources["seconds"] == 3
    sources["cb"]()
    assert controller.bar.message_text() == ""


def test_post_from_worker_thread_is_marshalled(spec):
    queued = []
    controller = sb.StatusBarController(sb.StatusBar(), dispatch=queued.append)
    worker = threading.Thread(target=lambda: controller.post("test.hello", name="bg"))
    worker.start()
    worker.join()
    assert controller.bar.message_text() == ""
    assert len(queued) == 1
    queued[0]()
    assert controller.bar.message_text() == "Hello bg"


def test_plugin_text_and_bad_level():
    controller = sb.StatusBarController(sb.StatusBar())
    controller.show_text("Deployed", level="nonsense", timeout_s=None)
    assert controller.bar.message_text() == "Deployed"
    assert controller.bar.message_icon.has_css_class("dim-label")


class _Subscription:
    def __init__(self):
        self.active = True

    def unsubscribe(self):
        self.active = False


class _Client:
    def __init__(self, instance="i1"):
        self.server_instance_id = instance
        self.callbacks = []
        self.subscriptions = []

    def subscribe_events(self, callback):
        self.callbacks.append(callback)
        sub = _Subscription()
        self.subscriptions.append(sub)
        return sub


def test_no_routes_means_no_subscription():
    controller = sb.StatusBarController(sb.StatusBar())
    client = _Client()
    controller.attach_client(client)
    assert client.callbacks == []
    assert not controller.subscribed


def test_routed_events_reach_the_bar_and_center(spec, monkeypatch):
    monkeypatch.setitem(
        sm.EVENT_ROUTES, "evt",
        lambda event, lookup: ("test.hello", {"name": lookup(event.connection_id)}),
    )
    notified = []
    monkeypatch.setitem(
        nm.NOTIFY_EVENT_ROUTES, "evt",
        lambda event, lookup: ("test.code", {"id": event.connection_id}),
    )
    from sshpilot import notification_store

    monkeypatch.setattr(notification_store, "notify", lambda code, **p: notified.append((code, p)))
    queued = []
    controller = sb.StatusBarController(
        sb.StatusBar(), dispatch=queued.append, name_lookup=lambda cid: f"name-{cid}"
    )
    client = _Client()
    controller.attach_client(client)
    assert controller.subscribed
    client.callbacks[0](types.SimpleNamespace(type="evt", connection_id="c1"))
    for callback in list(queued):
        callback()
    assert controller.bar.message_text() == "Hello name-c1"
    assert notified == [("test.code", {"id": "c1"})]


def test_stale_client_events_are_dropped(spec, monkeypatch):
    monkeypatch.setitem(sm.EVENT_ROUTES, "evt", lambda event, lookup: ("test.hello", {"name": "x"}))
    queued = []
    controller = sb.StatusBarController(sb.StatusBar(), dispatch=queued.append)
    old = _Client("old")
    controller.attach_client(old)
    new = _Client("new")
    controller.attach_client(new)
    assert not old.subscriptions[0].active
    old.callbacks[0](types.SimpleNamespace(type="evt", connection_id=None))
    assert queued == []
    controller.close()
    assert not new.subscriptions[0].active
    new.callbacks[0](types.SimpleNamespace(type="evt", connection_id=None))
    assert queued == []


def test_controller_binds_store_badge(tmp_path):
    from sshpilot.notification_store import NotificationStore

    store = NotificationStore(str(tmp_path / "n.json"), schedule=lambda *_a: 0)
    controller = sb.StatusBarController(sb.StatusBar(), notification_store=store)
    store.add_text_entry("one")
    store.add_text_entry("two")
    assert controller.bar.badge_label.get_text() == "2"
    controller.shutdown()
    store.add_text_entry("three")
    assert controller.bar.badge_label.get_text() == "2"


def _visibility_stub(setting, terminal_fullscreen=False):
    bar = Gtk.Box()
    separator = Gtk.Separator()
    config = types.SimpleNamespace(get_setting=lambda key, default=None: setting)
    fullscreen = types.SimpleNamespace(terminal_presentation_active=terminal_fullscreen)
    return types.SimpleNamespace(
        status_bar=bar,
        status_bar_separator=separator,
        config=config,
        fullscreen_controller=fullscreen,
        _status_bar_action=None,
    )


@pytest.mark.parametrize("setting,fullscreen,visible", [
    (True, False, True),
    (False, False, False),
    (True, True, False),
])
def test_window_visibility_follows_setting_and_fullscreen(setting, fullscreen, visible):
    from sshpilot.window import MainWindow

    stub = _visibility_stub(setting, fullscreen)
    MainWindow.update_status_bar_visibility(stub)
    assert stub.status_bar.get_visible() is visible
    assert stub.status_bar_separator.get_visible() is visible


def test_fullscreen_controller_resyncs_status_bar():
    from sshpilot.window_fullscreen import WindowFullscreenController

    calls = []
    window = types.SimpleNamespace(update_status_bar_visibility=lambda: calls.append(1))
    controller = WindowFullscreenController.__new__(WindowFullscreenController)
    controller.window = window
    controller._sync_status_bar()
    assert calls == [1]


def test_post_status_reaches_the_application(spec, monkeypatch):
    received = []
    app = types.SimpleNamespace(post_status=lambda code, **p: received.append((code, p)))
    monkeypatch.setattr(sb.Gio.Application, "get_default", staticmethod(lambda: app))
    sb.post_status("test.hello", name="x")
    assert received == [("test.hello", {"name": "x"})]
