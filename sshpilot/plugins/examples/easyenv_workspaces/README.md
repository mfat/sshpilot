# EasyEnv Workspaces — sshPilot plugin example

Provisions [easyenv.io](https://easyenv.io) dev workspaces over their **REST
API** and connects to them as **standard SSH connections** — no `easyenv` CLI
binary and no NetBird mesh. It demonstrates the provider-plugin archetype:
sign in, list/create workspaces, and turn them into sshPilot connections (a
single VM → one connection; a multi-VM template → a sidebar group of per-node
connections).

The plugin imports only the public SDK (`sshpilot.plugins.api`) plus the Python
standard library (`urllib`/`json`/`threading`) and GTK — no third-party deps.

## How it works

- **Auth:** REST headers `X-Service-Token: <token>` + `Account-ID: <uuid>`. You
  paste a service token from
  https://dashboard.easyenv.io/auth/login?redirect=/dashboard/profile ; the
  plugin stores it in the OS keyring (`ctx.secrets`) and the active account uuid
  in `ctx.settings`.
- **Provision:** `POST /v1/workspaces/` with `workspace_template` +
  `settings.public_ip_requested = true`, then `…/start/`, then poll
  `GET /v1/workspaces/{uuid}/` until `active`.
- **Connect:** each box comes back with a **public IP** (`host_address`) plus
  `ssh_username` / `ssh_port` / `vm_password`. The plugin creates ordinary
  sshPilot SSH connections from those (password stored in the keyring, fed via
  sshpass). Opening one is normal SSH — it works from anywhere, no VPN.

## Install

1. Get a service token: https://dashboard.easyenv.io/auth/login?redirect=/dashboard/profile
2. Copy this plugin into the user plugin dir and enable it:
   ```sh
   cp -r easyenv_workspaces ~/.local/share/sshpilot/plugins/easyenv-workspaces
   ```
   sshPilot ▸ Preferences ▸ Plugins → enable **EasyEnv Workspaces** → restart.
3. Tools ▸ **EasyEnv Workspaces** → paste the token → Sign in → pick a template → Create.

## Notes

- Connections are written to `~/.ssh/config` like any other SSH host. Each
  provisioned host gets `StrictHostKeyChecking accept-new`,
  `UserKnownHostsFile /dev/null` (boxes are ephemeral and recycle IPs), and
  `PreferredAuthentications password` / `PubkeyAuthentication no` (so a loaded
  ssh-agent with many keys doesn't trip "Too many authentication failures"
  before the password is tried).
- **Public IP** is always requested (it's what makes direct SSH possible);
  depending on your easyenv plan this may have cost/availability implications.
- Stopped/expired workspaces are restarted on **Open** (start + wait), and the
  connection's IP/password are refreshed if they changed.
- The token lives only in the OS keyring — never written to the repo or logs.
