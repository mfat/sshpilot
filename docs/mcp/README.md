# SSH Pilot MCP quickstart

SSH Pilot ships two local MCP servers with deliberately separate trust
boundaries:

| Server | Use it for | Default access |
| --- | --- | --- |
| `sshpilot-mcp-dev` | Repository, API, architecture, Git, tests, and lint | Checkout-confined; read-only except allowlisted checks |
| `sshpilot-mcp-runtime` | Live daemon, session, SFTP, transfer, forward, operation, and interaction access | READ enabled; OPERATE and MUTATE disabled |

Most contributors should configure only `sshpilot-mcp-dev`. Add the runtime
server only when a task must exercise a running `sshpilotd`.

## Five-minute contributor setup

From the repository root, use the existing development virtual environment:

```bash
source .venv/bin/activate
python -m pip install "mcp>=2.0.0"
SSHPILOT_MCP_ROOT="$PWD" ./sshpilot-mcp-dev
```

The final command speaks MCP over stdio, so it waits silently for an MCP
client. Stop it with `Ctrl+C` when testing by hand.

For an editable package installation, after installing the system dependencies
from [running from source](../running-from-source.md), use:

```bash
python -m pip install -e '.[mcp]'
```

Installed releases support `python -m pip install 'sshpilot[mcp]'`.

## Connect a coding agent

Replace `/absolute/path/to/sshpilot` in these examples. Absolute paths keep the
server pinned to the intended checkout regardless of the client's working
directory.

### Codex

```bash
codex mcp add sshpilot-dev \
  --env SSHPILOT_MCP_ROOT=/absolute/path/to/sshpilot \
  -- /absolute/path/to/sshpilot/.venv/bin/python \
     /absolute/path/to/sshpilot/sshpilot-mcp-dev

codex mcp get sshpilot-dev
```

### Clients using `mcpServers`

Use this in the client's project MCP configuration (for example `.mcp.json`):

```json
{
  "mcpServers": {
    "sshpilot-dev": {
      "command": "/absolute/path/to/sshpilot/.venv/bin/python",
      "args": ["/absolute/path/to/sshpilot/sshpilot-mcp-dev"],
      "cwd": "/absolute/path/to/sshpilot",
      "env": {
        "SSHPILOT_MCP_ROOT": "/absolute/path/to/sshpilot"
      }
    }
  }
}
```

For clients with a different configuration shape, register the same command,
arguments, working directory, and environment variable as a local stdio MCP
server.

## Verify the developer server

Ask the connected agent to perform these calls in order:

1. Call `repo_info`. The returned `root` must be the checkout you configured.
2. Call `inspect_api`. It should report the protocol/API versions and method counts.
3. Call `check_api_drift`. A clean checkout should report `clean: true`.
4. Call `recommend_tests` with `paths=["src/sshpilot/api/daemon_client.py"]`.

If the tools do not appear, check that the configured Python can run:

```bash
PYTHONPATH=/absolute/path/to/sshpilot/src \
  /absolute/path/to/sshpilot/.venv/bin/python -c 'import mcp, sshpilot.mcp.dev'
```

## Agent workflow

SSH Pilot MCP complements general code search and knowledge graphs. Use a graph
or ordinary code-search tool to locate implementations and call relationships;
use these tools for authoritative SSH Pilot contracts and validation.

Before changing an existing API method:

1. Call `plan_api_change(method="...")`.
2. Inspect the returned client method, wire method, capability, and daemon handler.
3. Make the smallest headless API/daemon change before connecting GTK.
4. Call `recommend_tests(paths=[...])` for the edited paths.
5. Call `validate_change()`.
6. Run the recommended focused tests with `run_tests`.

Useful developer tools are grouped below:

| Purpose | Tools |
| --- | --- |
| Checkout discovery | `repo_info`, `read_text_file`, `list_directory` |
| Search | `search_source`, `find_symbol`, `find_tests` |
| Read-only Git | `git_status`, `git_log`, `git_diff` |
| API intelligence | `inspect_api`, `list_api_methods`, `trace_api_method`, `check_api_drift`, `plan_api_change` |
| Architecture | `check_frontend_neutrality`, `review_public_api`, `trace_interaction_scope`, `review_commit` |
| Change workflow | `recommend_tests`, `validate_change` |
| Controlled validation | `run_tests`, `run_lint`, `validate_api_artifacts` |

`run_tests`, `run_lint`, and `validate_api_artifacts` use fixed argv allowlists.
There is no arbitrary shell or Git mutation tool. Subprocess results always
include `success`, `returncode`, and `timed_out`; check `success` rather than
inferring success from the MCP transport result.

## Runtime server

The runtime server connects only through `DaemonClient`; it never starts a
parallel SSH/SFTP implementation. Start or use an existing daemon, then
register `sshpilot.mcp.runtime` as a second stdio server.

For a source checkout using an explicit socket:

```bash
./sshpilot-daemon --socket /run/user/1000/sshpilot-mcp.sock
```

Generic client configuration:

```json
{
  "mcpServers": {
    "sshpilot-runtime": {
      "command": "/absolute/path/to/sshpilot/.venv/bin/python",
      "args": ["-m", "sshpilot.mcp.runtime"],
      "cwd": "/absolute/path/to/sshpilot",
      "env": {
        "PYTHONPATH": "/absolute/path/to/sshpilot/src",
        "SSHPILOT_MCP_SOCKET": "/run/user/1000/sshpilot-mcp.sock",
        "SSHPILOT_MCP_READ": "1"
      }
    }
  }
}
```

Use the actual runtime directory for your account; `/run/user/1000` is only an
example. Omit `SSHPILOT_MCP_SOCKET` to use SSH Pilot's default daemon socket.

### Runtime policy

| Variable | Default | Effect |
| --- | --- | --- |
| `SSHPILOT_MCP_READ` | enabled | Inspect daemon and resource state |
| `SSHPILOT_MCP_OPERATE` | disabled | Start/stop sessions and services; manage operations/interactions |
| `SSHPILOT_MCP_MUTATE` | disabled | Mutate remote SFTP state or cancel a transfer |
| `SSHPILOT_MCP_CONTENT` | disabled | Reveal daemon-declared content fields instead of `<redacted>` |
| `SSHPILOT_MCP_SOCKET` | daemon default | Override the daemon Unix socket |

Permissions are cumulative: OPERATE requires READ, and MUTATE requires READ +
OPERATE + MUTATE. Every MUTATE tool also requires `confirm=true` on that call.
Daemon capabilities control whether a tool is available; they do not grant
authorization.

Keep content disabled unless the task explicitly requires remote file contents
or another daemon-declared sensitive field in model context. Passwords,
passphrases, private keys, and raw secret frames are not ordinary MCP inputs;
authentication continues through SSH Pilot's protected interaction path and a
trusted frontend.

## Troubleshooting

- **No tools / import error:** install `mcp>=2.0.0` in the exact Python used by the client.
- **Wrong checkout:** set an absolute `SSHPILOT_MCP_ROOT` and confirm it with `repo_info`.
- **Runtime cannot connect:** start `sshpilotd`, verify the socket, and call the daemon's status command.
- **A runtime tool is missing:** call `capabilities`; unsupported daemon capabilities are hidden at startup.
- **Permission refused:** enable the required cumulative policy levels in the client configuration and restart the MCP server.
- **Content is `<redacted>`:** this is expected; opt in with `SSHPILOT_MCP_CONTENT=1` only when appropriate.
- **A controlled check returned normally but failed:** inspect its `success`, `returncode`, `stdout`, `stderr`, and `timed_out` fields.

## Developing the MCP servers

Run the smallest maintained checks first:

```bash
pytest -q tests/mcp
pytest -q tests/architecture/test_mcp_boundary.py
python scripts/generate_api_artifacts.py --check
ruff check src/sshpilot/mcp tests/mcp tests/architecture/test_mcp_boundary.py
```

The real-daemon, stdio, OpenSSH/SFTP, host-key, and FIDO scenarios are marked
`integration` and require their documented platform fixtures:

```bash
pytest -q -m integration tests/mcp
```

Architectural decisions are in [decisions.md](decisions.md), current
implementation status is in [status.md](status.md), and contributor rules are
in [AGENTS.md](AGENTS.md).
