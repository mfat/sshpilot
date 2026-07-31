"""Integration package fixtures for Phase 10.1 (ephemeral daemon only)."""

# Intentionally minimal: Phase 10 helpers live in tests.daemon.phase10_helpers
# so daemon unit tests and integration tests share one stack builder. Tests
# never touch the production sshpilotd socket.

# Session restore metadata tests need the ephemeral ``daemon_factory`` from
# the daemon package (isolated XDG + DaemonServer).
pytest_plugins = ("tests.daemon.conftest",)
