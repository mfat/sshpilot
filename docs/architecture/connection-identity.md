# Connection Identity

Saved SSH Pilot identity is an app-owned UUID; the concrete SSH `Host` alias
is the launch selector.

```ssh
Host production
    HostName 10.0.0.5
    User root
```

is represented as:

```python
ConnectionSummary(
    id="550e8400-e29b-41d4-a716-446655440000",
    display_name="Production",
    ssh_alias="production",
    nickname="production",
    host="production",
    hostname="10.0.0.5",
    username="root",
)
```

`~/.ssh/config` is the source of truth. The app does not write or require
UUID metadata comments.

## Rules

- Saved SSH connection ID = immutable UUID; `ssh_alias` is the OpenSSH Host token
- Wildcard (`*`, `?`) and negated (`!…`) Host tokens are rules, not connections
- Multi-token concrete Host lines expose each token as its own connection ID
- A safe alias rename preserves UUID and app-owned metadata
- Ambiguous external edits never guess ownership

## Groups

Group IDs are stable application-defined slugs derived from the display name
(`production`, `production-2`, …). SSH membership is keyed by UUID.

## Runtime resources

Daemon-scoped counters allocate opaque IDs such as:

- `session-12`
- `terminal-19`
- `sftp-4`
- `transfer-31`
- `forward-7`
- `interaction-15`
- `request-3`
- `client-1`
- `attachment-2`

These are not UUIDs and are unique within the owning daemon process (or client
connection for request IDs).

## Secrets

Connection passwords are keyed by SSH host identity candidates
(`hostname` → `host` → `nickname`), not by a separate UUID. Private-key
passphrases remain keyed by identity-file path.
