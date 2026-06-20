# SSH Key Audit (sshPilot plugin)

A security-hygiene dashboard for your SSH keys: lists the public keys and
certificates in `~/.ssh` with their **age**, type/size, fingerprint, comment,
and (for certificates) **expiry**. Answers "which keys are older than a year?"
and "when does this cert expire?" at a glance — a companion to the Host Health
Dashboard.

## What it reads

Only `*.pub` / `*-cert.pub` files and `ssh-keygen` metadata — **never private
key contents**. "Age" is the public-key file's modification time (a proxy for
when the key was generated). Set the warning threshold (default 365 days) on the
page; with the default `warn_on_start` setting it shows a one-time toast at
startup if any key exceeds it.

## Requirements

- `ssh-keygen` available (OpenSSH). Under Flatpak the host's `ssh-keygen` is used
  via `flatpak-spawn --host` if it isn't in the sandbox.

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/key-audit/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/key-audit/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Permissions

`filesystem` (reads `~/.ssh`), `process` (runs `ssh-keygen`), `ui`, `settings` —
declared for transparency; sshPilot plugins run unsandboxed with full app
privileges. Only install plugins you trust.

## Develop / test

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

The `ssh-keygen` output parsing and age/expiry logic are pure Python and
unit-tested without `ssh-keygen` or GTK.
