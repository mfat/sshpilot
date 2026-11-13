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
    # What the connection list holds and whether the SSH config was readable.
    state["loaded"] = ["web", "db"]
    state["unavailable"] = None
    class _Manager:
        def get_connection_by_id(self, cid):
            return types.SimpleNamespace(nickname=cid)

        def snapshot(self):
            return types.SimpleNamespace(ssh_config_unavailable=state["unavailable"])

        @property
        def connections(self):
            return [types.SimpleNamespace(nickname=n) for n in state["loaded"]]

    window.connection_manager = _Manager()
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
    assert len(window.config.values["ui.config_conflicts_dismissed"]["default"]) == 1

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
    assert window.config.values["ui.config_conflicts_dismissed"] == {"default": []}
    assert not notice._banner.get_revealed()

    _check(notice, state, [web])
    assert notice._banner.get_revealed()


def test_a_dismissal_from_an_earlier_launch_is_honoured(gui):
    from sshpilot.gtk.config_precedence_notice import conflict_fingerprint

    web = _conflict("web")
    config = _Config(
        {"ui.config_conflicts_dismissed": {"default": [conflict_fingerprint(web)]}}
    )
    notice, container, _window, state, _opened = _notice(gui, [web], config=config)

    notice.check()
    _pump(lambda: state["asked"] and not notice._checking)

    assert not container.get_visible()
    assert notice._banner is None or not notice._banner.get_revealed()


def test_nothing_dismissed_by_default():
    from sshpilot.core.settings.defaults import get_default_config

    assert get_default_config()["ui"]["config_conflicts_dismissed"] == {}


def test_a_mode_switch_keeps_what_the_other_mode_dismissed(gui):
    """Isolated mode reads another SSH config: its scan says nothing about
    the default one's conflicts, so their dismissal survives the round trip."""
    web = _conflict("web")
    notice, _container, window, state, _opened = _notice(gui, [web])
    notice.check()
    _pump(lambda: notice._banner is not None)
    notice._on_dismiss()

    window.config.values["ssh.use_isolated_config"] = True
    state["loaded"] = ["lab"]
    _check(notice, state, [])
    window.config.values["ssh.use_isolated_config"] = False
    state["loaded"] = ["web", "db"]
    _check(notice, state, [web])

    assert not notice._banner.get_revealed()
    assert window.config.values["ui.config_conflicts_dismissed"]["default"]


def test_dismissals_are_kept_per_mode(gui):
    web = _conflict("web")
    config = _Config({"ssh.use_isolated_config": True})
    notice, _container, window, state, _opened = _notice(gui, [web], config=config)
    notice.check()
    _pump(lambda: notice._banner is not None)
    notice._on_dismiss()
    assert set(window.config.values["ui.config_conflicts_dismissed"]) == {"isolated"}

    # The same conflict in default mode was never dismissed there.
    window.config.values["ssh.use_isolated_config"] = False
    _check(notice, state, [web])
    assert notice._banner.get_revealed()


def test_an_unreadable_config_neither_shows_nor_forgets(gui):
    web = _conflict("web")
    notice, _container, window, state, _opened = _notice(gui, [web])
    notice.check()
    _pump(lambda: notice._banner is not None)
    notice._on_dismiss()
    stored = dict(window.config.values["ui.config_conflicts_dismissed"])

    state["unavailable"] = "permission_denied"
    _check(notice, state, [])
    assert window.config.values["ui.config_conflicts_dismissed"] == stored

    state["unavailable"] = None
    _check(notice, state, [web])
    assert not notice._banner.get_revealed()


def test_a_dismissal_stored_as_a_plain_list_counts_for_default_mode(gui):
    from sshpilot.gtk.config_precedence_notice import conflict_fingerprint

    web = _conflict("web")
    config = _Config({"ui.config_conflicts_dismissed": [conflict_fingerprint(web)]})
    notice, container, _window, state, _opened = _notice(gui, [web], config=config)
    notice.check()
    _pump(lambda: state["asked"] and not notice._checking)
    assert not container.get_visible()


def test_a_scan_answered_after_a_mode_switch_is_discarded(gui):
    """The scan starts in default mode, the user switches to isolated mode,
    then the default-mode answer arrives. It must not be applied to isolated
    mode's dismissals, nor shown; a fresh scan runs instead."""
    import threading

    from sshpilot.gtk.config_precedence_notice import conflict_fingerprint

    lab = _conflict("lab")
    config = _Config({
        "ui.config_conflicts_dismissed": {"isolated": [conflict_fingerprint(lab)]},
    })
    notice, _container, window, state, _opened = _notice(gui, [], config=config)
    state["loaded"] = ["web", "lab"]  # "lab" exists in both modes
    release = threading.Event()
    answers = [(_conflict("web"),), (lab,)]  # default-mode answer, then isolated

    def slow_scan():
        state["asked"] += 1
        if state["asked"] == 1:
            release.wait(5)
        return answers[state["asked"] - 1]

    window.client.list_config_conflicts = slow_scan
    notice.check()                                   # starts in default mode
    window.config.values["ssh.use_isolated_config"] = True
    state["loaded"] = ["lab"]
    release.set()
    _pump(lambda: state["asked"] == 2 and not notice._checking)

    # The default-mode answer (web) was dropped and never touched isolated
    # dismissals; the fresh isolated scan found "lab", already dismissed.
    assert window.config.values["ui.config_conflicts_dismissed"]["isolated"] == [
        conflict_fingerprint(lab)
    ]
    assert notice._banner is None or not notice._banner.get_revealed()
    assert [item.host for item in notice._conflicts] == ["lab"]


def test_a_store_change_during_a_scan_triggers_a_fresh_one(gui):
    import threading

    notice, _container, window, state, _opened = _notice(gui, [])
    generation = {"value": 1}
    manager = window.connection_manager
    original = manager.snapshot
    manager.snapshot = lambda: types.SimpleNamespace(
        ssh_config_unavailable=None, generation=generation["value"]
    )
    release = threading.Event()

    def slow_scan():
        state["asked"] += 1
        if state["asked"] == 1:
            release.wait(5)
            return (_conflict("web"),)
        return ()

    window.client.list_config_conflicts = slow_scan
    notice.check()
    generation["value"] = 2
    release.set()
    _pump(lambda: state["asked"] == 2 and not notice._checking)

    assert notice._conflicts == ()
    assert notice._banner is None
    manager.snapshot = original
