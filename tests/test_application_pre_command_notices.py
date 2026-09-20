"""How the application routes pre-connection command notices.

There is one subscription for these notices and many possible surfaces, so the
application keeps a scope-id registry and dispatches to it. The guarantee being
protected here is that the *alert* does not depend on that registry: a surface
that never registered, or one that blew up, must not be able to turn a failed
pre-connection command back into the silent failure this feature replaced.

The toast itself needs a real window, so it is exercised by the live run; this
pins the routing around it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandPhase,
    PreCommandReason,
    PreConnectionCommandNotice,
)


@pytest.fixture
def application():
    """An application stub carrying only what the notice handler touches."""

    from sshpilot.main import SshPilotApplication

    app = SimpleNamespace(
        _pre_command_status_targets={},
        _pre_command_active={},
        # No overlay: the handler then logs instead of building a toast, which
        # keeps this test free of a display while still running every branch
        # that decides *whether* to alert.
        window=SimpleNamespace(_is_quitting=False, toast_overlay=None),
    )
    for name in (
        "register_pre_command_status",
        "unregister_pre_command_status",
        "_set_pre_command_status",
        "_deliver_pre_command_status",
        "_handle_pre_command_event",
        "_connection_display_name",
    ):
        setattr(app, name, getattr(SshPilotApplication, name).__get__(app))
    return app


def _notice(phase, reason=PreCommandReason.OK, *, scope_id="scope-1", exit_code=None):
    return PreConnectionCommandNotice(
        connection_id="conn-1",
        scope_id=scope_id,
        kind=PreCommandLaunchKind.TERMINAL,
        phase=phase,
        reason=reason,
        exit_code=exit_code,
    )


def test_a_running_notice_reaches_the_surface_that_owns_the_scope(application):
    seen = []
    application.register_pre_command_status("scope-1", seen.append)

    application._handle_pre_command_event(_notice(PreCommandPhase.RUNNING))

    assert seen and seen[0]


def test_a_finished_notice_clears_the_surface_however_it_finished(application):
    seen = []
    application.register_pre_command_status("scope-1", seen.append)

    application._handle_pre_command_event(
        _notice(PreCommandPhase.FINISHED, PreCommandReason.NONZERO_EXIT, exit_code=1)
    )
    application._handle_pre_command_event(
        _notice(PreCommandPhase.FINISHED, PreCommandReason.COALESCED)
    )

    assert seen == [None, None]


def test_another_scopes_notice_is_not_delivered(application):
    seen = []
    application.register_pre_command_status("scope-1", seen.append)

    application._handle_pre_command_event(
        _notice(PreCommandPhase.RUNNING, scope_id="scope-2")
    )

    assert seen == []


def test_an_unregistered_scope_is_handled_without_a_surface(application):
    """The common case early in a connect, and it must not raise."""

    application._handle_pre_command_event(
        _notice(PreCommandPhase.FINISHED, PreCommandReason.TIMED_OUT)
    )


def test_a_broken_surface_is_dropped_rather_than_retried(application):
    def _explode(_text):
        raise RuntimeError("widget is gone")

    application.register_pre_command_status("scope-1", _explode)

    application._handle_pre_command_event(_notice(PreCommandPhase.RUNNING))

    assert application._pre_command_status_targets == {}


def test_unregistering_stops_delivery(application):
    seen = []
    application.register_pre_command_status("scope-1", seen.append)
    application.unregister_pre_command_status("scope-1")

    application._handle_pre_command_event(_notice(PreCommandPhase.RUNNING))

    assert seen == []


def test_nothing_is_delivered_while_the_window_is_quitting(application):
    seen = []
    application.register_pre_command_status("scope-1", seen.append)
    application.window._is_quitting = True

    application._handle_pre_command_event(_notice(PreCommandPhase.RUNNING))

    assert seen == []


def test_a_surface_that_binds_late_still_gets_the_running_line(application):
    """The dominant case for a terminal tab.

    The daemon starts the command on a worker as soon as the open is accepted,
    routinely before the frontend has processed the response and learned its
    session id. Without replay the line would almost never appear on the tab
    it exists for -- which is how this was caught, on a live run.
    """

    application._handle_pre_command_event(_notice(PreCommandPhase.RUNNING))

    seen = []
    application.register_pre_command_status("scope-1", seen.append)

    assert seen and seen[0]


def test_a_finished_command_is_not_replayed_to_a_later_surface(application):
    application._handle_pre_command_event(_notice(PreCommandPhase.RUNNING))
    application._handle_pre_command_event(
        _notice(PreCommandPhase.FINISHED, PreCommandReason.OK)
    )

    seen = []
    application.register_pre_command_status("scope-1", seen.append)

    assert seen == []


def test_replay_does_not_leak_between_scopes(application):
    application._handle_pre_command_event(
        _notice(PreCommandPhase.RUNNING, scope_id="scope-9")
    )

    seen = []
    application.register_pre_command_status("scope-1", seen.append)

    assert seen == []
