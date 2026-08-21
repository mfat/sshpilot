"""Shared policy for container-backed integration prerequisites."""

from __future__ import annotations

import os

import pytest


CANONICAL_ENV = "SSHPILOT_CANONICAL_TEST_ENV"


def is_canonical_environment() -> bool:
    return os.environ.get(CANONICAL_ENV) == "1"


def skip_or_fail(message: str) -> None:
    """Skip optional local integration, but fail the canonical integration gate."""

    if is_canonical_environment():
        pytest.fail(message)
    pytest.skip(message)
