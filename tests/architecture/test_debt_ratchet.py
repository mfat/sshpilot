"""Identity-based architecture-debt ratchet tests."""

from __future__ import annotations

import pytest

from tests.architecture.approved_debt import (
    APPROVED_BACKEND_OPS,
    APPROVED_CORE_DEBT,
    APPROVED_DAEMON_DEBT,
    APPROVED_PENDING,
    assert_debt_does_not_grow,
    debt_identity,
)
from tests.architecture.test_core_boundary import BACKEND_OPS, PENDING
from tests.core.test_dependency_boundary import CORE_DEBT, DAEMON_DEBT


def _current_debt():
    return {
        "PENDING": debt_identity(PENDING),
        "BACKEND_OPS": debt_identity(BACKEND_OPS, include_frontend=False),
        "DAEMON_DEBT": debt_identity(DAEMON_DEBT),
        "CORE_DEBT": debt_identity(CORE_DEBT),
    }


def _approved_debt():
    return {
        "PENDING": APPROVED_PENDING,
        "BACKEND_OPS": APPROVED_BACKEND_OPS,
        "DAEMON_DEBT": APPROVED_DAEMON_DEBT,
        "CORE_DEBT": APPROVED_CORE_DEBT,
    }


def test_current_debt_is_a_subset_of_the_reviewed_identity_baseline():
    for name, current in _current_debt().items():
        assert_debt_does_not_grow(current, _approved_debt()[name], name)


def test_removing_approved_debt_is_allowed():
    for name, approved in _approved_debt().items():
        removed = set(approved)
        if removed:
            removed.pop()
        assert_debt_does_not_grow(removed, approved, name)


@pytest.mark.parametrize(
    ("name", "entry"),
    [
        (
            "PENDING",
            (("new_frontend.py", "new_service", "new_symbol"), "M7"),
        ),
        (
            "BACKEND_OPS",
            (("new_frontend.py", "subprocess"), "M7"),
        ),
        (
            "DAEMON_DEBT",
            (("daemon/new_service.py", "sshpilot.legacy"), "M7"),
        ),
    ],
)
def test_new_migration_debt_fails(name, entry):
    current = set(_approved_debt()[name])
    current.add(entry)

    with pytest.raises(AssertionError, match="unapproved"):
        assert_debt_does_not_grow(current, _approved_debt()[name], name)


def test_replacing_removed_debt_with_different_debt_fails():
    approved = _approved_debt()["DAEMON_DEBT"]
    replacement = set(approved)
    replacement.pop()
    replacement.add((("daemon/replacement.py", "sshpilot.new_legacy"), "M7"))

    with pytest.raises(AssertionError, match="unapproved"):
        assert_debt_does_not_grow(replacement, approved, "DAEMON_DEBT")
