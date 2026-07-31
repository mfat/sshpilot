"""Integration package fixtures for Phase 10.1 (ephemeral daemon only)."""

# Intentionally minimal: Phase 10 helpers live in tests.daemon.phase10_helpers
# so daemon unit tests and integration tests share one stack builder. Tests
# never touch the production sshpilotd socket.
#
# Import daemon fixtures directly (pytest 8+ forbids non-top-level pytest_plugins).
from tests.daemon.conftest import daemon_factory  # noqa: F401
