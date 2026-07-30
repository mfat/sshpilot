# Headless core (architecture pointer)

See `docs/development/headless-core.md` for CLI usage and import rules.

Architecture boundary: `docs/architecture/core-boundary.md`.
Dependency direction: `docs/architecture/dependency-direction.md`.
Phase 13 audit: `docs/architecture/phase13-dependency-audit.md`.
Phase 13 completion matrix: `docs/architecture/phase13-completion-matrix.md`.
Compatibility shims: `docs/architecture/core-compatibility-shims.md`.
Daemon test isolation: `docs/architecture/daemon-test-isolation.md`.
Production smoke: `docs/testing/phase13-production-smoke.md`.
Temporary OpenSSH fixture: `docs/testing/temporary-openssh-fixture.md`.

Phase 13 completed the reusable connection domain, SSH `ProcessSpec` builder,
askpass/secret interaction policy, import/export planning, and transfer
conflict policy under `sshpilot.core`. GTK remains an interaction adapter.
