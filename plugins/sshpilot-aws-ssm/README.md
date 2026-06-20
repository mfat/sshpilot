# AWS SSM (sshPilot plugin)

Adds an **AWS SSM** protocol so AWS Session Manager shells open in a normal
sshPilot terminal tab — for environments where direct SSH is disabled.

Each connection runs:

```
aws ssm start-session --target <instance-id> [--profile …] [--region …] [--document-name …]
```

## Requirements

- **AWS CLI v2** plus the **Session Manager plugin** (`session-manager-plugin`)
  installed and on `PATH`.
- AWS credentials configured for the CLI (profiles, SSO, or environment).
  **This plugin stores no credentials** — auth is delegated entirely to the CLI.
- Under Flatpak the host's `aws` is used via `flatpak-spawn --host` when it isn't
  present inside the sandbox.

## Use

Enable the plugin (Preferences ▸ Plugins) and restart. In the connection dialog
pick **AWS SSM** as the protocol, then set the instance id (and optionally
profile / region / SSM document).

## Permissions

`process` (runs the `aws` CLI) — declared for transparency; sshPilot plugins run
unsandboxed with full app privileges. Only install plugins you trust.

## Develop / test

```sh
pip install pytest "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

The argv assembly (`build_ssm_argv`) is pure Python and unit-tested without the
AWS CLI or GTK.
