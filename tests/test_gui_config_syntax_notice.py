"""The main-window banner shown while OpenSSH rejects the SSH configuration."""

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


def _error(line=5, diagnostic="Bad configuration option: *"):
    from sshpilot.api.models.connections import ConfigSyntaxError

    return ConfigSyntaxError(
        file="/etc/sshpilot-test/config", line=line, diagnostic=diagnostic
    )


def _notice(gui, errors):
    from gi.repository import Gtk
    from sshpilot.gtk.config_syntax_notice import ConfigSyntaxNotice

    state = {"errors": tuple(errors), "asked": 0, "edited": 0}

    def list_config_syntax_errors():
        state["asked"] += 1
        return state["errors"]

    def open_editor():
        state["edited"] += 1

    window = Gtk.Window()
    window.client = types.SimpleNamespace(
        list_config_syntax_errors=list_config_syntax_errors
    )
    window._open_ssh_config_editor = open_editor
    container = Gtk.Box()
    container.set_visible(False)
    window.set_child(container)
    return ConfigSyntaxNotice(window, container), container, state


def _check(notice, state):
    asked = state["asked"]
    notice.check()
    _pump(lambda: state["asked"] > asked and not notice._checking)


def test_banner_names_the_line_and_goes_when_fixed(gui):
    notice, container, state = _notice(gui, [_error()])

    _check(notice, state)

    assert container.get_visible()
    banner = notice._banner
    assert banner.get_revealed()
    assert banner.has_css_class("error")
    title = banner.get_title()
    assert "line 5" in title
    assert "/etc/sshpilot-test/config" in title
    assert title.endswith("Bad configuration option: *")

    banner.emit("button-clicked")
    assert state["edited"] == 1

    state["errors"] = ()
    _check(notice, state)
    assert not container.get_visible()
    assert not banner.get_revealed()


def test_banner_counts_the_other_lines(gui):
    from sshpilot.gtk.config_syntax_notice import banner_title

    one_more = banner_title((_error(), _error(line=9)))
    assert "line 5" in one_more and "1 more line " in one_more
    assert "2 more lines" in banner_title((_error(), _error(9), _error(12)))
    # One line whatever OpenSSH said: a banner centres each line of its title.
    assert "\n" not in banner_title((_error(),))
    assert banner_title((_error(diagnostic=""),)).endswith("is not valid.")


def test_a_failed_check_leaves_the_banner_as_it_was(gui):
    notice, container, state = _notice(gui, [_error()])
    _check(notice, state)
    assert container.get_visible()

    def failing():
        state["asked"] += 1
        raise RuntimeError("daemon went away")

    notice._window.client.list_config_syntax_errors = failing
    _check(notice, state)
    assert container.get_visible()


def test_a_check_during_a_check_asks_again(gui):
    notice, container, state = _notice(gui, [_error()])
    notice.check()
    notice.check()
    _pump(lambda: state["asked"] >= 2 and not notice._checking)
    assert state["asked"] == 2
    assert container.get_visible()
