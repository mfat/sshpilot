# Connection Identity

Saved connection identity is the concrete SSH `Host` alias.

```ssh
Host production
    HostName 10.0.0.5
    User root
```

is represented as:

```python
ConnectionSummary(
    id="production",
    nickname="production",
    host="production",
    hostname="10.0.0.5",
    username="root",
)
```

`~/.ssh/config` is the source of truth. The app does not write or require
UUID metadata comments.

## Rules

- Saved connection ID = SSH Host alias (`connection.id == connection.nickname`)
- Wildcard (`*`, `?`) and negated (`!…`) Host tokens are rules, not connections
- Multi-token concrete Host lines expose each token as its own connection ID
- An alias rename is deletion of the old ID plus creation of the new ID
- There is no heuristic rename detection

## Groups

Group IDs are stable application-defined slugs derived from the display name
(`production`, `production-2`, …). Membership is keyed by connection alias.

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
