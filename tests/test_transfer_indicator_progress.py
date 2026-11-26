"""The transfer button's ring shows the running transfers' combined progress."""

import pytest

from sshpilot.file_manager.transfer_indicator import combined_fraction


def _snap(fraction, total_bytes=0, state="running"):
    return {"state": state, "fraction": fraction, "total_bytes": total_bytes}


def test_no_running_transfer_has_no_progress():
    assert combined_fraction([]) is None
    assert combined_fraction([_snap(1.0, 10, state="done")]) is None


def test_unknown_size_has_no_progress():
    """Nothing to fill a ring with yet: the button keeps its spinner."""
    assert combined_fraction([_snap(None)]) is None


def test_one_transfer_is_its_own_progress():
    assert combined_fraction([_snap(0.4, 1000)]) == pytest.approx(0.4)


def test_progress_is_weighted_by_size():
    """A finished 5 KB file beside a 2 GB one at 10% is about 10%, not 55%."""
    snaps = [_snap(1.0, 5_000), _snap(0.1, 2_000_000_000)]
    assert combined_fraction(snaps) == pytest.approx(0.1, abs=0.001)


def test_transfer_without_a_size_counts_as_average():
    snaps = [_snap(0.5), _snap(1.0, 100), _snap(0.0, 300)]
    # sizes 200 (the average), 100 and 300: (100 + 100 + 0) / 600
    assert combined_fraction(snaps) == pytest.approx(1 / 3)


def test_transfers_without_any_size_average_evenly():
    assert combined_fraction([_snap(0.2), _snap(0.6)]) == pytest.approx(0.4)


def test_transfer_still_sizing_is_left_out():
    assert combined_fraction([_snap(None), _snap(0.7, 50)]) == pytest.approx(0.7)


def test_progress_stays_in_range():
    assert combined_fraction([_snap(1.7, 10)]) == 1.0
    assert combined_fraction([_snap(-0.3, 10)]) == 0.0
