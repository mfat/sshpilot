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


def test_conflicts_show_a_banner_naming_the_host_count(gui):
    """Whoever has a conflict sees it, upgrade or not."""
    conflicts = [_conflict("web"), _conflict("web", key="port", own="22", value="2222"),
                 _conflict("db")]
    notice, container, window, state, _opened = _notice(gui, conflicts)

    notice.check()
    _pump(lambda: notice._banner is not None)

    assert container.get_visible()
    assert notice._banner.get_revealed()
    assert "2 saved hosts" in notice._banner.get_title()
    assert window.config.values == {}


def _rows(notice):
    from gi.repository import Adw

    rows = []
    stack = list(notice._review_groups)
    while stack:
        widget = stack.pop()
        if isinstance(widget, Adw.ActionRow):
            rows.append(widget)
            continue
        item = widget.get_first_child()
        while item is not None:
            stack.append(item)
            item = item.get_next_sibling()
    return rows


def test_review_lists_each_host_and_opens_its_editor(gui):
    notice, _container, _window, _state, opened = _notice(
        gui, [_conflict("web"), _conflict("web", key="port", own="22", value="2222", line=4)]
    )
    notice.check()
    _pump(lambda: notice._banner is not None)

    notice._on_review()
    rows = _rows(notice)
    assert [row.get_title() for row in rows] == ["web"]
    subtitle = rows[0].get_subtitle()
    assert "User: uses “admin”" in subtitle and "“deploy”" in subtitle
    assert "Port: uses “2222”" in subtitle and "line 4" in subtitle

    rows[0].emit("activated")
    assert opened == ["web"]


def _check(notice, state, conflicts):
    asked = state["asked"]
    state["conflicts"] = tuple(conflicts)
    notice.check()
    _pump(lambda: state["asked"] > asked and not notice._checking)


def test_dismissed_conflicts_stay_hidden_and_a_new_one_brings_the_banner_back(gui):
    web = _conflict("web")
    notice, container, window, state, _opened = _notice(gui, [web])
    notice.check()
    _pump(lambda: notice._banner is not None)

    notice._on_dismiss()
    assert not notice._banner.get_revealed() and not container.get_visible()
    assert len(window.config.values["ui.config_conflicts_dismissed"]) == 1

    # The same conflict, even after unrelated lines moved it: still dismissed.
    _check(notice, state, [_conflict("web", line=9)])
    assert not notice._banner.get_revealed()

    # A new one, edited in by hand between launches: the banner returns,
    # counting only what is new.
    _check(notice, state, [web, _conflict("db")])
    assert notice._banner.get_revealed()
    assert "1 saved host " in notice._banner.get_title()


def test_a_fixed_conflict_is_forgotten_so_its_return_is_news(gui):
    web = _conflict("web")
    notice, _container, window, state, _opened = _notice(gui, [web])
    notice.check()
    _pump(lambda: notice._banner is not None)
    notice._on_dismiss()

    _check(notice, state, [])
    assert window.config.values["ui.config_conflicts_dismissed"] == []
    assert not notice._banner.get_revealed()

    _check(notice, state, [web])
    assert notice._banner.get_revealed()


def test_a_dismissal_from_an_earlier_launch_is_honoured(gui):
    from sshpilot.gtk.config_precedence_notice import conflict_fingerprint

    web = _conflict("web")
    config = _Config({"ui.config_conflicts_dismissed": [conflict_fingerprint(web)]})
    notice, container, _window, state, _opened = _notice(gui, [web], config=config)

    notice.check()
    _pump(lambda: state["asked"] and not notice._checking)

    assert not container.get_visible()
    assert notice._banner is None or not notice._banner.get_revealed()


def test_nothing_dismissed_by_default():
    from sshpilot.core.settings.defaults import get_default_config

    assert get_default_config()["ui"]["config_conflicts_dismissed"] == []
