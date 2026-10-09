# Threat model

## What this project does and where untrusted input enters

SSH Pilot is a desktop SSH connection manager and terminal (Python, GTK 4). A
per-user daemon, `sshpilotd` (`src/sshpilot/daemon/`), owns state, secrets and
every OpenSSH/SCP/SFTP child process; the GTK app and the optional MCP server
are clients of it over a Unix socket. OpenSSH is the SSH engine: SSH Pilot
writes `~/.ssh/config` and launches `ssh`, it does not implement the protocol.

Treat these as untrusted:

- **Remote hosts.** Anything a server sends: terminal output (escape
  sequences), SFTP directory listings and file names, host-info command output,
  banners, and data on forwarded ports.
- **Imported files.** SSH config files and their `Include`s, Ásbrú/other
  importer formats, KeePass `.kdbx` files, backup archives, `authorized_keys`
  and `known_hosts` content, and custom terminal themes.
- **Text the user pastes or types into connection fields.** It is written
  back to `~/.ssh/config`, so a value must never inject an extra directive
  (newlines, quoting, `Match`/`ProxyCommand` smuggling).
- **Network fetches.** The plugin registry (`plugins/registry_client.py`) and
  the update checker.
- **Other local users.** The daemon socket, askpass sockets and runtime
  directory must be reachable only by their owner.

## Components that matter most / least

Most important:
- `daemon/` — RPC dispatch, the socket and runtime-directory permissions,
  askpass/secret transfer, the process registry, PTY handling, SFTP/transfers,
  port forwarding, and `privileged_file_service.py`.
- ssh_config parsing and writing (`ssh_config_utils`, `config.py`, `core/`):
  config injection that leads to command execution is critical.
- Secret handling: secrets must never reach argv, environment values, logs,
  diagnostics, temporary files or ordinary RPC JSON.
- Path handling in the file manager and transfers: traversal or symlink
  following from remote names into local paths.
- The runtime MCP server (`mcp/runtime`): READ/OPERATE/MUTATE permission
  gating and redaction.
- Built-in protocol plugins (`plugins/builtin/`): argument injection into
  `mosh`, `telnet`, `kubectl`, `docker`, `xfreerdp`, `picocom`.

Less important, still in scope: GTK presentation code, preferences, i18n.

Out of scope: `sshpilot-mcp-dev` (a developer tool for this repository),
`plugins/examples/`, `tests/`, packaging (`flatpak/`, `debian/`, `packaging/`)
and vulnerabilities in OpenSSH, GTK, VTE or WebKit themselves.

## How to exercise it

- Unit tests: `python3 -m pytest -ra -n auto` (the default markers skip slow,
  GUI and e2e suites). `tests/daemon/` drives a real daemon headless.
- Daemon: `python3 -m sshpilot.daemon` with `XDG_RUNTIME_DIR`,
  `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `SSHPILOT_SSH_DIR` pointed at
  temporary directories, so nothing touches the real `~/.ssh`.
- App: `xvfb-run -a dbus-run-session python3 run.py` with the same variables
  plus `SSHPILOT_APP_ID=io.github.mfat.sshpilot.scan`.
- A local target: `ssh-keygen -A && /usr/sbin/sshd`, then connect to
  `localhost`.

## How you rate severity

- Critical: remote code execution from a malicious server, or command
  execution from an imported file or pasted connection value.
- High: secret disclosure (passwords, passphrases, private keys) to another
  local user, a log, argv or a remote host; another local user driving the
  daemon; writing outside the intended directory from a remote file name.
- Medium: local denial of service of the daemon, permission-gate bypass in
  the runtime MCP server, terminal escape sequences with effects beyond the
  terminal.
- Low: crashes on malformed input without further impact.

## Anything to leave alone

- Running a command the user explicitly configured (`ProxyCommand`,
  `LocalCommand`, pre-connection commands, port knocking) is intended.
- A user with write access to their own config or plugin directory running
  code as themselves is not a vulnerability.
- Plugins installed by the user run with the user's privileges by design.

Preferred patch format: a unified diff against `main`, with a regression test
under `tests/`.
