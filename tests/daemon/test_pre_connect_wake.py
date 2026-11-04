"""Wake-on-LAN as the first pre-connection step.

A connection with a MAC address wakes its host when it does not answer,
waits for it, and then knocks or runs its command. These tests pin:

* an awake host costs one probe and says nothing;
* a sleeping host is woken, waited for, and the launch then proceeds;
* a host that never answers times out without refusing the launch;
* a knock or command connection is woken but not waited for (its port stays
  closed until the knock), and the knock still runs after the wake;
* no wake through a jump host, or with the switch off.
"""

from __future__ import annotations

from types import SimpleNamespace

from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandMode,
    PreCommandPhase,
    PreCommandReason,
    PreCommandSettings,
    PreCommandStage,
)
from sshpilot.daemon import pre_connection_command as module
from sshpilot.daemon.pre_connection_command import (
    WAKE_TIMEOUT_SECONDS,
    PreConnectionCommandRunner,
)


class Clock:
    """A clock that only moves when the runner sleeps."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _wake_settings(**overrides):
    values = dict(
        hostname="thinkbook.lan",
        target_port=3389,
        wake_mac="aa:bb:cc:dd:ee:ff",
    )
    values.update(overrides)
    return PreCommandSettings(**values)


def _runner(settings, *, answers, sent=(True, "Magic packet sent.")):
    """*answers* is the probe's result per call, last one repeating."""
    clock = Clock()
    probes, wakes, notices = [], [], []
    answers = list(answers)

    def probe(host, port, timeout):
        probes.append((host, port))
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def send_wake(values):
        wakes.append(values.wake_mac)
        return sent

    instance = PreConnectionCommandRunner(
        settings=SimpleNamespace(
            pre_command_timeout_seconds=5,
            pre_command_coalesce_seconds=0,
        ),
        lookup=lambda connection_id: settings,
        clock=clock,
        probe=probe,
        send_wake=send_wake,
        sleep=clock.sleep,
    )
    instance.subscribe_events(lambda event: notices.append(event.payload))
    return instance, probes, wakes, notices, clock


def _run(instance):
    return instance.run(
        ConnectionId("thinkbook"), scope_id="tab-1", kind=PreCommandLaunchKind.TERMINAL
    )


def test_an_awake_host_costs_one_probe_and_says_nothing():
    instance, probes, wakes, notices, _ = _runner(_wake_settings(), answers=[True])

    assert _run(instance) is True
    assert probes == [("thinkbook.lan", 3389)]
    assert wakes == []
    assert notices == []


def test_a_sleeping_host_is_woken_and_waited_for():
    instance, probes, wakes, notices, clock = _runner(
        _wake_settings(), answers=[False, False, False, True]
    )

    assert _run(instance) is True
    assert wakes == ["aa:bb:cc:dd:ee:ff"]
    assert len(probes) == 4  # the first check, then until it answers
    assert [n.phase for n in notices] == [PreCommandPhase.RUNNING, PreCommandPhase.FINISHED]
    assert all(n.stage is PreCommandStage.WAKE for n in notices)
    assert notices[-1].reason is PreCommandReason.OK
    assert notices[-1].scope_id == "tab-1"


def test_a_host_that_never_answers_times_out_but_the_launch_goes_on():
    instance, _, wakes, notices, clock = _runner(_wake_settings(), answers=[False])
    started = clock.now

    assert _run(instance) is True
    assert wakes == ["aa:bb:cc:dd:ee:ff"]
    assert notices[-1].reason is PreCommandReason.TIMED_OUT
    assert notices[-1].stage is PreCommandStage.WAKE
    assert clock.now - started >= WAKE_TIMEOUT_SECONDS


def test_a_wake_packet_that_cannot_be_sent_is_reported_and_not_waited_for():
    instance, probes, _, notices, _ = _runner(
        _wake_settings(), answers=[False], sent=(False, "Invalid MAC address.")
    )

    assert _run(instance) is True
    assert len(probes) == 1
    assert notices[-1].reason is PreCommandReason.START_FAILED
    assert notices[-1].output == "Invalid MAC address."


def test_a_knocked_host_is_woken_without_waiting_then_knocked(monkeypatch):
    knocks = []
    order = []

    def fake_knock(host, steps):
        order.append("knock")
        knocks.append(host)
        return SimpleNamespace(
            succeeded=True, sent=len(steps), failed=(), duration_ms=1,
            address="192.0.2.5", log_summary="", failure_reason="",
        )

    monkeypatch.setattr(module, "send_knock", fake_knock)
    settings = _wake_settings(knock_sequence="7000,8000", mode=PreCommandMode.KNOCK)
    instance, probes, wakes, notices, _ = _runner(settings, answers=[False])
    instance._send_wake = lambda values: (order.append("wake"), (True, ""))[1]

    assert _run(instance) is True
    assert order == ["wake", "knock"]
    assert len(probes) == 1, "a port behind a knock stays closed; waiting would only time out"
    assert [n.stage for n in notices] == [
        PreCommandStage.WAKE, PreCommandStage.WAKE,
        PreCommandStage.KNOCK, PreCommandStage.KNOCK,
    ]


def test_no_wake_through_a_jump_host():
    instance, probes, wakes, notices, _ = _runner(
        _wake_settings(direct=False), answers=[False]
    )

    assert _run(instance) is True
    assert probes == [] and wakes == [] and notices == []


def test_no_wake_when_switched_off():
    instance, probes, wakes, _, _ = _runner(
        _wake_settings(wake_before_connect=False), answers=[False]
    )

    assert _run(instance) is True
    assert probes == [] and wakes == []


def test_settings_read_wake_on_lan_from_metadata():
    settings = PreCommandSettings.from_metadata(
        {"wol_mac": " 7e:22:b4:60:39:ba ", "wol_port": "7", "wol_broadcast_ip": "192.168.8.255"}
    )
    assert settings.wake_mac == "7e:22:b4:60:39:ba"
    assert settings.wake_port == 7
    assert settings.wake_broadcast_ip == "192.168.8.255"
    assert settings.wake_before_connect is True
    assert PreCommandSettings.from_metadata(
        {"wol_mac": "aa:bb:cc:dd:ee:ff", "wol_on_connect": False}
    ).wake_before_connect is False
