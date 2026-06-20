# Hetzner Cloud (sshPilot plugin)

Browse the servers in your Hetzner Cloud project and add them as SSH connections
— no copying IPs from the console.

## What it does (and doesn't)

- Lists existing servers (name, status, public IPv4, labels) via the Hetzner
  Cloud API and adds the ones you pick as SSH connections (host = public IPv4,
  username your default, port 22), optionally into a group.
- **Browser only**: it does not create, resize, or destroy servers, and it does
  not store server passwords (the API doesn't expose them). Host authentication
  is whatever you've configured — typically an SSH key.

## Requirements

- A **Hetzner Cloud API token** (Project ▸ Security ▸ API tokens; read access is
  enough). It's stored in your OS keyring via `ctx.secrets`.
- sshPilot with plugin **API ≥ 1.4** (`ctx.list_connections()` for de-duping).
- Network access to `api.hetzner.cloud`.

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/hetzner/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/hetzner/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Permissions

`connections`, `keyring` (stores the API token), `network` (Hetzner API), `ui`,
`settings` — declared for transparency; sshPilot plugins run unsandboxed with
full app privileges. Only install plugins you trust.

## Develop / test

```sh
pip install pytest "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

Response parsing and de-dup are pure Python and unit-tested without the network;
`gi` is imported lazily inside the page factory. Live API verification needs a
real Hetzner token.
