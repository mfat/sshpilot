# Headless core development

## Import rule

```bash
PYTHONPATH=src python3 -c "from sshpilot.core import ErrorCode; print(ErrorCode.VALIDATION_ERROR)"
```

With `gi` blocked (see `tests/core/test_headless_imports.py`), importing
`sshpilot.core`, `sshpilot.api`, and `sshpilot.daemon` must succeed.

## Proof CLI

```bash
./sshpilot-core inspect-config
./sshpilot-core validate-connection --nickname Demo --host example.com --user alice
./sshpilot-core list-keys
```

This is intentionally tiny — it proves the boundary, not a product TUI.

## Tests

```bash
pytest -q tests/core
```
