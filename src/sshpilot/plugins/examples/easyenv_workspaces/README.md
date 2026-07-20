# EasyEnv Workspaces — sshPilot plugin example

Provisions [easyenv.io](https://easyenv.io) dev workspaces over their **REST
API** and connects to them as **standard SSH connections** — no `easyenv` CLI
binary and no NetBird mesh. It demonstrates the provider-plugin archetype:
sign in, list/create workspaces, and turn them into sshPilot connections (one
node → one connection; several nodes → a sidebar group of per-node
connections).

Workspaces are built from **recipes** (e.g. Ubuntu 24.04, Python Dev Env), not
the pre-baked multi-VM *templates*: only recipe-built boxes can be assigned a
public IP over REST. Template workspaces come back on the NetBird mesh with
unroutable `box-…` host names, so they can't be reached by plain SSH — boxes
without a routable address are skipped with a warning rather than turned into
dead connections.

The plugin imports only the public SDK (`sshpilot.plugins.api`) plus the Python
standard library (`urllib`/`json`/`threading`) and GTK/libadwaita — no
third-party deps.

## The page

A native GTK4/libadwaita dashboard (follows the app's light/dark theme):

- **Sign-in hero** — branded card with a service-token field, a keyring note,
  and a "Get a token" link.
- **Account header** — initials avatar, `email · plan`, a plan badge, and the
  remaining compute-hours pill (from the account's `remaining_time_seconds`).
- **Workspace cards** — a responsive grid. Each card shows a theme-aware status
  pill (Running / Provisioning / Terminated / Failed), a live countdown timer,
  the `ssh user@ip:port` line with a copy button, the recipe (coloured avatar),
  and actions: **Open** (or **Recreate** when terminal), **Stop**, **Clone**,
  **Delete**, and an info button (full details dialog). Provisioning cards show a
  pulsing progress bar and auto-flip to Running.
- **Search / Filter / Sort** and a **New Workspace** dialog (name, recipe, node
  count, duration).

## How it works

- **Auth:** REST headers `X-Service-Token: <token>` + `Account-ID: <uuid>`. You
  paste a service token from
  https://dashboard.easyenv.io/auth/login?redirect=/dashboard/profile ; the
  plugin stores it in the OS keyring (`ctx.secrets`) and the active account uuid
  in `ctx.settings`.
- **Provision:** `POST /v1/workspaces/` with a `boxes` array (one entry per
  node, each referencing a recipe uuid from `GET /v1/recipes/`) +
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
3. Tools ▸ **EasyEnv Workspaces** → paste the token → Sign in → **New
   Workspace** → choose a recipe, node count and duration → Create & provision.

## Notes

- Connections are written to `~/.ssh/config` like any other SSH host. Each
  provisioned host gets `StrictHostKeyChecking accept-new`,
  `UserKnownHostsFile /dev/null` (boxes are ephemeral and recycle IPs), and
  `PreferredAuthentications password` / `PubkeyAuthentication no` (so a loaded
  ssh-agent with many keys doesn't trip "Too many authentication failures"
  before the password is tried).
- **Public IP** is always requested (it's what makes direct SSH possible);
  depending on your easyenv plan this may have cost/availability implications.
- **Mesh-only boxes are skipped.** If a workspace's box has no routable address
  (a NetBird `box-…` mesh name, e.g. an older template-built workspace), the
  plugin warns and skips it instead of writing a connection that would fail with
  "Could not resolve hostname".
- **Stopped = terminal.** EasyEnv workspaces are ephemeral; once a workspace's
  duration ends it becomes `stopped`, which the API can't restart ("Stopped
  workspace cannot be started"). Such workspaces show as **Terminated** and offer
  **Recreate** (a fresh workspace from the same recipe[s]) instead of Open.
- The token lives only in the OS keyring — never written to the repo or logs.
