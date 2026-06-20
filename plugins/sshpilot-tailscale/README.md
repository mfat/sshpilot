# Tailscale Sync (sshPilot plugin)

Lists your Tailscale tailnet peers (name, IP, online status, tags) and adds any
peer — or a filtered batch — as an SSH connection, so mesh machines show up in
sshPilot without copying IPs or MagicDNS names by hand.

## How it works

Runs `tailscale status --json` and parses the peer list. Connections use the
peer's MagicDNS name as the host (falling back to the Tailscale IP), with your
configured default SSH username. Peers already saved (by nickname or host) are
shown as *Added* and skipped by bulk-add.

## Requirements

- The **`tailscale`** CLI installed and logged in. Under Flatpak the host's
  `tailscale` is used via `flatpak-spawn --host`.
- sshPilot with plugin **API ≥ 1.4** (`ctx.list_connections()` for de-duping).

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/tailscale/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/tailscale/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Permissions

`connections`, `process` (runs `tailscale`), `ui`, `settings` — declared for
transparency; sshPilot plugins run unsandboxed with full app privileges. Only
install plugins you trust.

## Develop / test

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

Status parsing, filtering, and de-dup are pure Python and unit-tested against a
fixture without Tailscale or GTK; `gi` is imported lazily inside the page factory.
