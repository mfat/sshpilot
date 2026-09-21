"""The SCP transfer dialog's pre-connection command status line.

The dialog has one status row, written by two things: transfer summaries from
the daemon, and -- while a launch is still being prepared -- the pre-connection
command line that explains a multi-second pause before anything has started.

The summary is the authority. The status line exists only to fill the gap
before the first one arrives, so it must never clear or overwrite a real
update, and it must never leave its own text behind once the command is done.
The first version of this got the second half wrong: it checked the label
against the idle text rather than against what it had itself written, so
"Running pre-connection command…" stayed on screen for the life of the
transfer.
"""

from __future__ import annotations

import pytest

from sshpilot.scp_window import make_pre_command_status_setter


IDLE = "Starting SCP transfer…"
RUNNING = "Running pre-connect command…"


class FakeLabel:
    def __init__(self, text=""):
        self._text = text

    def get_text(self):
        return self._text

    def set_text(self, text):
        self._text = text


def test_the_running_line_is_shown():
    label = FakeLabel(IDLE)
    setter = make_pre_command_status_setter(label, IDLE)

    setter(RUNNING)

    assert label.get_text() == RUNNING


def test_finishing_restores_the_idle_text():
    """The bug: the running line used to stay up for the whole transfer."""

    label = FakeLabel(IDLE)
    setter = make_pre_command_status_setter(label, IDLE)

    setter(RUNNING)
    setter(None)

    assert label.get_text() == IDLE


def test_a_transfer_update_that_lands_first_is_not_clobbered():
    label = FakeLabel(IDLE)
    setter = make_pre_command_status_setter(label, IDLE)

    setter(RUNNING)
    label.set_text("Transferring 3 of 7…")  # a summary arrives
    setter(None)

    assert label.get_text() == "Transferring 3 of 7…"


def test_clearing_without_ever_showing_anything_does_nothing():
    label = FakeLabel("Transferring 3 of 7…")
    setter = make_pre_command_status_setter(label, IDLE)

    setter(None)

    assert label.get_text() == "Transferring 3 of 7…"


def test_a_second_run_shows_and_restores_again():
    """A retried transfer re-runs the command; the line has to come back."""

    label = FakeLabel(IDLE)
    setter = make_pre_command_status_setter(label, IDLE)

    setter(RUNNING)
    setter(None)
    setter(RUNNING)
    setter(None)

    assert label.get_text() == IDLE


@pytest.mark.parametrize("value", ["", "   ", None, 0, object()])
def test_anything_that_is_not_text_reads_as_a_clear(value):
    label = FakeLabel(IDLE)
    setter = make_pre_command_status_setter(label, IDLE)

    setter(RUNNING)
    setter(value)

    assert label.get_text() == IDLE
