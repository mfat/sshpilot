# Command Converter

`sshpilot/command_converter.py` turns a complete `ssh ...` command line — or a
bare `user@host` — into a sshPilot **connection-data dict**. It was extracted
from the former *Quick Connect* feature when that UI was removed; the parsing
logic was worth keeping because it lets a user paste a real SSH command and get a
fully populated connection.

The module has **no UI of its own yet**. It is documented here so it can be
wired into the new-connection flow (or a paste/import action) later.

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

- **No verbatim execution.** Unlike Quick Connect, the converter does *not*
  keep the raw command around to run as-is. Everything is decomposed into
  structured fields that get written to `~/.ssh/config` through the normal
  connection path, keeping the SSH config as the single source of truth (see
  `docs/architecture.md` → *SSH Connection & Authentication Architecture*).
- **Parsing only.** The module does not connect, write config, or touch the
  keyring. It is a pure function returning data; callers decide what to do with
  it (construct a `Connection`, pre-fill `ConnectionDialog`, etc.).
- **IPv6-aware forwardings.** `-L`/`-R`/`-D` specs accept bracketed IPv6 bind
  addresses (e.g. `[::1]:8080:localhost:80`).

## Wiring it into a UI later

A reasonable re-introduction would be a "Paste SSH command" entry in the
new-connection dialog:

```python
from sshpilot.command_converter import parse_ssh_command
from sshpilot.connection_dialog import ConnectionDialog

data = parse_ssh_command(text)
if data is None:
    ...  # show "could not parse" feedback
elif "error" in data:
    ...  # show data["error"] inline
else:
    dialog = ConnectionDialog(window, connection=Connection(data),
                              connection_manager=window.connection_manager)
    dialog.is_editing = False
    dialog.present()
```

Tests live in `tests/test_command_converter.py`.
