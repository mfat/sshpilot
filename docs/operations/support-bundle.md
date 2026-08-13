# Support bundle contents

What **Help → Export Diagnostics…** includes and excludes.

## Included

| File | Description |
| --- | --- |
| `logs/*` | Rotating app logs and crash reports from the state directory |
| `system-info.txt` | Platform, runtime, and tool versions (`StartupInfo`) |
| `version.txt` | Application version and app id |
| `config.json` | Redacted copy of `config.json` (secret-ish keys replaced) |
| `daemon-diagnostics.json` | Sanitized daemon snapshot when reachable |

## daemon-diagnostics.json

When the local daemon answers `daemon.diagnostics`:

```json
{
  "available": true,
  "diagnostics": { "...": "wire-safe fields only" }
}
```

When unreachable:

```json
{
  "available": false,
  "reason": "unavailable",
  "message": "..."
}
```

The snapshot contains lifecycle state, resource counts, idle policy, uptime,
thread counts, and memory/descriptor metrics. It never includes:

- filesystem paths or socket paths
- environment variables or secrets
- terminal output or interaction payloads
- connection passwords or key material

## Excluded (by design)

- Saved connection lists and hostnames from `~/.ssh/config`
- Private keys, passphrases, or keyring contents
- Full session/transfer payloads

For interactive troubleshooting, developers may still ask for full log files via
**Help → View Logs → Open Log Folder**.

See [daemon-diagnostics.md](daemon-diagnostics.md).
