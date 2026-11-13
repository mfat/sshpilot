"""The one-time main-window notice about saved hosts' unused settings."""

from __future__ import annotations

import time
import types

import pytest

pytestmark = pytest.mark.gui


def _pump(until, seconds=3.0):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline and not until():
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


def _conflict(host, key="user", own="deploy", value="admin", line=3, block="Host *"):
    from sshpilot.api.models.connections import ConfigConflict, EarlierDirective

    return ConfigConflict(
        connection_id=host, host=host, key=key, own_value=own,
        earlier=EarlierDirective(
            key=key, value=value, block=block, file="/home/me/.ssh/config", line=line,
        ),
    )


def _notice(gui, conflicts, config=None):
    from gi.repository import Gtk
    from sshpilot.gtk.config_precedence_notice import ConfigPrecedenceNotice

    state = {"conflicts": tuple(conflicts), "asked": 0}

    def list_config_conflicts():
        state["asked"] += 1
        return state["conflicts"]

    opened = []
    window = Gtk.Window()
    window.config = config if config is not None else _Config()
    window.client = types.SimpleNamespace(list_config_conflicts=list_config_conflicts)
    window.connection_manager = types.SimpleNamespace(
        get_connection_by_id=lambda cid: types.SimpleNamespace(nickname=cid),
    )
    window.show_connection_dialog = lambda connection: opened.append(connection.nickname)
    container = Gtk.Box()
    container.set_visible(False)
    notice = ConfigPrecedenceNotice(window, container)
    return notice, container, window, state, opened


def test_upgrader_with_conflicts_sees_a_banner_naming_the_host_count(gui):
    conflicts = [_conflict("web"), _conflict("web", key="port", own="22", value="2222"),
                 _conflict("db")]
    notice, container, window, state, _opened = _notice(gui, conflicts)

    notice.check()
    _pump(lambda: notice._banner is not None)

    assert container.get_visible()
    assert notice._banner.get_revealed()
    assert "2 saved hosts" in notice._banner.get_title()
    assert window.config.values == {}  # still pending until dismissed


def test_review_lists_each_host_and_opens_its_editor(gui):
    from gi.repository import Adw

    notice, _container, window, _state, opened = _notice(
        gui, [_conflict("web"), _conflict("web", key="port", own="22", value="2222", line=4)]
    )
    notice.check()
    _pump(lambda: notice._banner is not None)

    notice._on_review()
    rows = []
    for group in notice._review_groups:
        child = group
        stack = [child]
        while stack:
            widget = stack.pop()
            if isinstance(widget, Adw.ActionRow):
                rows.append(widget)
                continue
            item = widget.get_first_child()
            while item is not None:
                stack.append(item)
                item = item.get_next_sibling()
    assert [row.get_title() for row in rows] == ["web"]
    subtitle = rows[0].get_subtitle()
    assert "“admin”" in subtitle and "“deploy”" in subtitle and "line 4" in subtitle

    rows[0].emit("activated")
    assert opened == ["web"]

    notice._on_dismiss()
    assert window.config.values == {"ui.config_precedence_notice": False}
    assert not notice._banner.get_revealed()
    assert notice._review is None or not notice._review.get_visible()


def test_no_conflicts_retires_the_notice_without_showing_anything(gui):
    notice, container, window, state, _opened = _notice(gui, [])

    notice.check()
    _pump(lambda: state["asked"] and not notice._checking)

    assert notice._banner is None and not container.get_visible()
    assert window.config.values == {"ui.config_precedence_notice": False}


def test_fresh_install_or_retired_notice_never_asks(gui):
    notice, _container, _window, state, _opened = _notice(
        gui, [_conflict("web")], config=_Config({"ui.config_precedence_notice": False})
    )
    notice.check()
    _pump(lambda: False, seconds=0.2)
    assert state["asked"] == 0 and notice._banner is None


def test_fixing_the_last_conflict_hides_the_banner_for_good(gui):
    notice, container, window, state, _opened = _notice(gui, [_conflict("web")])
    notice.check()
    _pump(lambda: notice._banner is not None)

    state["conflicts"] = ()
    notice.check()
    _pump(lambda: state["asked"] == 2 and not notice._checking)

    assert not notice._banner.get_revealed() and not container.get_visible()
    assert window.config.values == {"ui.config_precedence_notice": False}


def test_fresh_install_defaults_turn_the_notice_off():
    from sshpilot.core.settings.defaults import get_default_config

    assert get_default_config()["ui"]["config_precedence_notice"] is False
