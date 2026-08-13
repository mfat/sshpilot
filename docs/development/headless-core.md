# Headless core development

## Import rule

```bash
PYTHONPATH=src python3 -c "from sshpilot.core import ErrorCode; print(ErrorCode.VALIDATION_ERROR)"
```

Headless imports are enforced in a **fresh subprocess** with `python -I` and
`DISPLAY`/`WAYLAND_DISPLAY` unset (`tests/core/test_headless_imports.py`).
Plain `import gi` must raise `ImportError` under the blocker.

## Proof CLI

```bash
./sshpilot-core inspect-config
./sshpilot-core validate-connection --nickname Demo --host example.com --user alice
./sshpilot-core list-keys
./sshpilot-core inspect-connections --path /tmp/connections.json --json
./sshpilot-core validate-import /tmp/export.json --json
./sshpilot-core plan-import /tmp/export.json --strategy merge --json
./sshpilot-core build-ssh-command demo --user alice --port 2222 --json
```

Validation failures exit nonzero. Prefer `--json` for structured output.
Commands do not mutate state unless a write path is explicitly provided
(connection store autosave only when `--path` points at a writable store used
by create APIs — the CLI inspect/validate/plan paths are read-only).

## Tests

```bash
pytest -q tests/core
```

Phase 13.1 acceptance also requires the unfiltered full suite, combined-auth
repetitions, temporary OpenSSH integration tests, and the GUI production smoke
documented in `docs/testing/`.
