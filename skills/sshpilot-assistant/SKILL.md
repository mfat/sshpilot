---
name: sshpilot-assistant
description: Use SSH Pilot's runtime MCP to reach allowlisted servers for commands, SFTP, host monitoring, sessions (SSH/RDP/mosh/serial), and forwards. Use whenever the user asks to operate on hosts managed in SSH Pilot.
---

# SSH Pilot assistant skill

Use **only** the `sshpilot-runtime` MCP server for SSH Pilot hosts. Do not open a parallel `ssh` process or read `~/.ssh` private keys.

## First call

1. Call `assistant_status`.
2. If `enabled` is false, tell the user to turn on **Preferences ▸ AI Assistants**, allowlist servers, and enable **Listen on localhost** if the HTTP URL is missing.
3. Call `list_connections` and work only with returned hosts.

## Preferred tools

| Goal | Tool |
| --- | --- |
| Run a command | `run_command(connection_id, command)` |
| CPU / memory / disk | `host_info(connection_id)` |
| Browse / read files | `open_sftp` then `sftp_list_directory` / `sftp_read_file` |
| Write / delete files | SFTP mutate tools with `confirm=true` after the user asked |
| Upload / download | `start_transfer` |
| Port forward | `open_forward` / `close_forward` |
| Interactive SSH / RDP / mosh / serial | `open_session` (desktop opens in SSH Pilot; you get session state) |
| Docker / ad-hoc ops | `run_command` on an allowlisted SSH host |

## Rules

- If a host is missing from `list_connections`, it is not allowlisted. Point the user at the SSH Pilot checkbox or **Allow AI assistants** on the connection.
- If `list_interactions` shows a pending password, passphrase, or host-key prompt, tell the user to approve it in the SSH Pilot window. Never ask them to paste a password into chat.
- Pass `confirm=true` on MUTATE tools only after the user explicitly asked for that change.
- Prefer `run_command` over typing into an interactive terminal for one-shot work.
- Never call secret reveal, unlock, or daemon stop/restart tools (they are not exposed).

## Install this skill

- Claude Code: copy to `~/.claude/skills/sshpilot-assistant/SKILL.md`
- Cursor: copy to `~/.cursor/skills/sshpilot-assistant/SKILL.md` or the project `.agents/skills/` tree
- Copilot: copy to `.github/skills/sshpilot-assistant/SKILL.md` or the user skills directory

## MCP client config

Enable **Listen on localhost** in SSH Pilot Preferences ▸ AI Assistants, then use the copied snippet (preferred):

```json
{
  "mcpServers": {
    "sshpilot-runtime": {
      "url": "http://127.0.0.1:PORT/mcp",
      "headers": { "Authorization": "Bearer TOKEN" }
    }
  }
}
```

Contributor stdio fallback (`python -m sshpilot.mcp.runtime`) still works when the optional `[mcp]` extra is installed. Env flags can only tighten the Preferences ceiling.
