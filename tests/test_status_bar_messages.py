"""The status bar and notification catalogues: empty by default, curated by hand."""

import pytest

from sshpilot.gtk import notification_messages as nm
from sshpilot.gtk import status_bar_messages as sm


def test_status_catalogue_and_routes_ship_empty():
    # Nothing reaches the status bar until a message is chosen explicitly.
    assert sm.STATUS_MESSAGES == {}
    assert sm.EVENT_ROUTES == {}


def test_notification_catalogue_and_routes_ship_empty():
    assert nm.NOTIFICATIONS == {}
    assert nm.NOTIFY_EVENT_ROUTES == {}


def test_unknown_status_code_renders_nothing():
    assert sm.format_status_message("no.such.code") is None


@pytest.fixture
def status_spec(monkeypatch):
    monkeypatch.setitem(
        sm.STATUS_MESSAGES,
        "test.transfer",
        sm.StatusMessageSpec(
            template="Transferred {count} item to “{name}”",
            plural="Transferred {count} items to “{name}”",
            level=sm.StatusLevel.SUCCESS,
            timeout_s=4,
        ),
    )


def test_status_spec_formats_with_plural(status_spec):
    one = sm.format_status_message("test.transfer", {"count": 1, "name": "web"})
    many = sm.format_status_message("test.transfer", {"count": 3, "name": "web"})
    assert one.text == "Transferred 1 item to “web”"
    assert many.text == "Transferred 3 items to “web”"
    assert many.level is sm.StatusLevel.SUCCESS
    assert many.timeout_s == 4


def test_disabled_status_spec_renders_nothing(monkeypatch):
    monkeypatch.setitem(
        sm.STATUS_MESSAGES, "test.off", sm.StatusMessageSpec("Hidden", enabled=False)
    )
    assert sm.format_status_message("test.off") is None


def test_status_spec_missing_parameter_renders_nothing(monkeypatch):
    monkeypatch.setitem(sm.STATUS_MESSAGES, "test.p", sm.StatusMessageSpec("Hi {name}"))
    assert sm.format_status_message("test.p", {}) is None


def test_every_level_has_an_icon_and_class():
    for level in sm.StatusLevel:
        assert sm.LEVEL_ICONS[level].endswith("-symbolic")
        assert sm.LEVEL_CSS_CLASSES[level]


def test_notification_spec_formats_title_details_and_action(monkeypatch):
    monkeypatch.setitem(
        nm.NOTIFICATIONS,
        "test.failed",
        nm.NotificationSpec(
            title="Transfer to “{name}” failed",
            details="{error}",
            severity=nm.NotificationSeverity.ERROR,
            action=nm.ActionSpec("Open Log", "win.view-logs", target_param="name"),
        ),
    )
    rendered = nm.format_notification("test.failed", {"name": "web", "error": "EPERM"})
    assert rendered.title == "Transfer to “web” failed"
    assert rendered.details == "EPERM"
    assert rendered.severity is nm.NotificationSeverity.ERROR
    assert rendered.action_label == "Open Log"
    assert rendered.action_name == "win.view-logs"
    assert rendered.action_target == "web"


def test_disabled_or_unknown_notification_renders_nothing(monkeypatch):
    monkeypatch.setitem(
        nm.NOTIFICATIONS, "test.off", nm.NotificationSpec("Off", enabled=False)
    )
    assert nm.format_notification("test.off") is None
    assert nm.format_notification("nope") is None
    assert not nm.is_enabled("test.off")


def test_every_severity_has_an_icon_and_class():
    for severity in nm.NotificationSeverity:
        assert nm.SEVERITY_ICONS[severity].endswith("-symbolic")
        assert nm.SEVERITY_CSS_CLASSES[severity]
