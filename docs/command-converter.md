# Command Converter

`sshpilot/command_converter.py` turns a complete `ssh ...` command line — or a
bare `user@host` — into a sshPilot **connection-data dict**. It was extracted
from the former *Quick Connect* feature when that UI was removed.

## Where it is used

- **CLI connect** (`sshpilot.cli_connect`): `sshpilot -p 2222 user@host` (and
  similar) is parsed here so the live session argv and the optional “Save as
  new connection” dialog share the same fields. See `sshpilot.cli_connect` and
  `sshpilot.unsaved_host`.
- Still suitable for a future “Paste SSH command” entry in the new-connection
  dialog.

## Public API

```python
from sshpilot.command_converter import parse_ssh_command

data = parse_ssh_command("ssh -p 2222 -i ~/.ssh/id_ed25519 user@host")
```

`parse_ssh_command(command_text: str) -> dict | None`

- Returns a **connection-data dict** on success.
- Returns `{"error": "<message>"}` for input that is recognised but rejected
  (e.g. a command whose first token is not `ssh`, such as `scp ...`). The
  message is translated and suitable for showing inline in a dialog.
- Returns `None` when the input cannot be parsed into a connection at all
  (empty string, an `ssh` invocation with no host token, etc.).

Accepted input:

- A bare `user@host` (no spaces, no `ssh` prefix).
- A command whose first shell token is exactly `ssh`.

## Output shape

The returned dict matches what the connection dialog and
`sshpilot.connection_manager.Connection` consume:

| Key | Meaning |
| --- | --- |
| `nickname`, `host`, `hostname` | Target host (all set to the host token) |
| `username` | From `user@host`, `-l`, or `-o User=` |
| `port` | From `-p` or `-o Port=` (default `22`) |
| `keyfile`, `key_select_mode` | From `-i` / `-o IdentityFile=`; `key_select_mode` becomes `2` (specific key) |
| `x11_forwarding` | `True` if `-X` is present |
| `forward_agent` | `True` if `-A` or `-o ForwardAgent=yes` |
| `proxy_jump` | List of hops from `-J a,b` |
| `forwarding_rules` | Parsed `-L` / `-R` / `-D` specs (IPv6-aware) |
| `extra_ssh_config` | Any other `-o Key=Value` and `-C`/`-4`/`-6`, as SSH-config lines |
| `unparsed_args` | Tokens that were recognised as options but not specifically handled |
| `auth_method` | `0` (key-based) by default |

## Design notes

- **CLI live sessions** pass the original `ssh` argv through to OpenSSH in the
  VTE. The converter still fills structured fields for the save dialog /
  ssh_config persistence path.
- **Parsing only** in this module: it does not connect, write config, or touch
  the keyring. Callers decide what to do with the data.
- **IPv6-aware forwardings.** `-L`/`-R`/`-D` specs accept bracketed IPv6 bind
  addresses (e.g. `[::1]:8080:localhost:80`).

## Related modules

- `sshpilot.cli_connect` — CLI argv → resolve → open tab (or fail without
  starting a connection).
- `sshpilot.unsaved_host` — detect whether hostname/IP+user is already in ssh
  config (save-prompt helper; does not create connections).

Tests live in `tests/test_command_converter.py`, `tests/test_cli_connect.py`,
and `tests/test_unsaved_host.py`.
