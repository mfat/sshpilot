from types import SimpleNamespace

import pytest

from sshpilot.shortcut_utils import (
    DOUBLE_SHIFT_SHORTCUT,
    DoubleShiftDetector,
)


def _press_and_release(detector, pressed_at, released_at):
    assert detector.key_pressed(True, pressed_at) is False
    detector.key_released(True, released_at)


def test_double_shift_detector_accepts_two_clean_quick_presses():
    detector = DoubleShiftDetector(interval_seconds=0.5)

    _press_and_release(detector, 1.0, 1.05)

    assert detector.key_pressed(True, 1.3) is False
    assert detector.key_released(True, 1.35) is True


def test_double_shift_detector_rejects_slow_or_modified_sequences():
    detector = DoubleShiftDetector(interval_seconds=0.5)

    _press_and_release(detector, 1.0, 1.05)
    assert detector.key_pressed(True, 1.7) is False
    assert detector.key_released(True, 1.75) is False

    assert detector.key_pressed(True, 2.0) is False
    assert detector.key_released(True, 2.05) is True
    assert detector.key_pressed(False, 2.1) is False
    assert detector.key_pressed(True, 2.2) is False


def test_non_shift_key_while_shift_is_held_taints_the_sequence():
    detector = DoubleShiftDetector(interval_seconds=0.5)

    assert detector.key_pressed(True, 1.0) is False
    assert detector.key_pressed(False, 1.1) is False
    assert detector.key_released(True, 1.2) is False

    assert detector.key_pressed(True, 1.3) is False


def test_typing_with_shift_cancels_second_tap_before_activation():
    detector = DoubleShiftDetector(interval_seconds=0.5)

    _press_and_release(detector, 1.0, 1.05)
    assert detector.key_pressed(True, 1.2) is False
    assert detector.key_pressed(False, 1.25) is False

    assert detector.key_released(True, 1.3) is False


def test_shortcut_editor_has_separate_search_and_omnisearch_labels():
    try:
        from sshpilot.shortcut_editor import ACTION_LABELS
    except Exception as exc:  # pragma: no cover - depends on GTK test stubs
        pytest.skip(f"GTK stubs unavailable: {exc}")

    assert ACTION_LABELS['search'] == 'Search'
    assert ACTION_LABELS['omnisearch'] == 'Omnisearch'


def test_search_and_omnisearch_callbacks_use_separate_paths():
    try:
        from sshpilot.main import SshPilotApplication
    except Exception as exc:  # pragma: no cover - depends on GTK test stubs
        pytest.skip(f"GTK stubs unavailable: {exc}")

    calls = []
    window = SimpleNamespace(
        focus_search_entry=lambda: calls.append('search'),
        activate_omni_search=lambda: calls.append('omnisearch'),
    )
    app = SimpleNamespace(props=SimpleNamespace(active_window=window))

    SshPilotApplication.on_search(app, None, None)
    SshPilotApplication.on_omnisearch(app, None, None)

    assert calls == ['search', 'omnisearch']


def test_double_shift_shortcut_uses_stable_config_token():
    assert DOUBLE_SHIFT_SHORTCUT == '<double-shift>'
