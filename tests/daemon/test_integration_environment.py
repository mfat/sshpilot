"""Tests for canonical container-integration failure policy."""

from __future__ import annotations

import pytest

from tests.daemon.integration_environment import skip_or_fail


def test_container_failure_skips_on_arbitrary_developer_machine(monkeypatch):
    monkeypatch.delenv("SSHPILOT_CANONICAL_TEST_ENV", raising=False)
    with pytest.raises(pytest.skip.Exception):
        skip_or_fail("fixture unavailable")


def test_container_failure_fails_in_canonical_environment(monkeypatch):
    monkeypatch.setenv("SSHPILOT_CANONICAL_TEST_ENV", "1")
    with pytest.raises(pytest.fail.Exception):
        skip_or_fail("fixture unavailable")
