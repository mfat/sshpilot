# EasyEnv Workspaces — sshPilot plugin example

Integrates the [easyenv.io](https://easyenv.io/cli) CLI (`easyenv`,
[github.com/donedeploy/easyenv-cli](https://github.com/donedeploy/easyenv-cli))
as a sshPilot plugin. It demonstrates the **CLI-driven, mesh-protocol**
integration archetype (contrast with `examples/mock_vps`, which is an
IP/SSH provider).

easyenv is mesh-based: `easyenv workspace ssh <id>` opens a shell over the
easyenv mesh and does **not** expose a host/port/user/key. So the plugin:

1. Registers a **protocol backend** `easyenv` whose `build_spawn` runs
   `easyenv workspace ssh <workspace-id>` in a sshPilot terminal tab.
2. Adds an **EasyEnv Workspaces** page (Tools menu) to sign in, list, create,
   and manage workspaces, then opens a terminal via the public
   `ctx.add_connection` + `ctx.open_connection`.

Only `sshpilot.plugins.api` is imported.

## Install (real easyenv)

1. Install the CLI and sign in (the account + service token are created on
   easyenv.io — the CLI "does everything but signup"):
   ```sh
   # see https://easyenv.io/cli for your platform
   easyenv auth login --token <paste-from-easyenv.io>
   ```
2. Copy this plugin into the user plugin dir and enable it:
   ```sh
   cp -r easyenv_workspaces ~/.local/share/sshpilot/plugins/easyenv-workspaces
   ```
   Open sshPilot ▸ Preferences ▸ Plugins, enable **EasyEnv Workspaces**, restart.
3. Tools ▸ **EasyEnv Workspaces** → create/list/connect.

## Try it with no account (bundled stub)

A self-contained `stub/easyenv` mirrors the real CLI's command surface (state
in `~/.local/state/easyenv-stub.json`, never touching `~/.config/easyenv/`).
Its `workspace ssh` drops into a real local shell, so the terminal tab is a
genuinely working session. Put the stub first on `PATH`:

```sh
mkdir -p ~/.local/bin
cp easyenv_workspaces/stub/easyenv ~/.local/bin/easyenv
chmod +x ~/.local/bin/easyenv          # if the executable bit was lost on copy
```

The same plugin then drives the stub. Swapping to the real binary is just
installing it ahead of the stub on `PATH` and running `easyenv auth login`.

## Flatpak

Inside the Flatpak sandbox the host `easyenv` isn't on the sandbox `PATH`, so
the plugin routes calls through `flatpak-spawn --host` (the sshPilot manifest
grants `--talk-name=org.freedesktop.Flatpak`). Both the page's CLI calls and
the terminal child use the prefix. This applies to any CLI-driven plugin.

## Notes / to confirm with the partner

- The exact `workspace ssh` subcommand (`workspace ssh <id>` vs `machine ssh -w`)
  — change one argv list in `EasyEnvBackend.build_spawn` if it differs.
- Per the EasyEnv API (OpenAPI `WorkspaceList`), a workspace's stable id is
  `uuid`, its display name is `title`, and `status` is one of
  active/not_started/stopped/in_progress/failed; list responses are paginated
  (`results: [...]`). The plugin parses these (and tolerates `id`/`name`/`state`
  in case the CLI reshapes the payload). Confirm the CLI's actual `--output
  json` shape with the partner.
- A workspace may auto-stop after its TTL; resume before connecting if needed.
- Mesh = no SFTP/port-forward/ssh-copy-id via sshPilot (capabilities are empty).
- The easyenv token is owned by the CLI (OS keychain); the plugin never stores
  it in sshPilot's keyring.
