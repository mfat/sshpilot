# Forwards API

Stability: **stable**.

## Methods

`open_forward`, `get_forward`, `list_forwards`, `close_forward` —
see [methods.md](methods.md).

## Types

`LOCAL`, `REMOTE`, `DYNAMIC` (SOCKS).

## State machine

`created` → `starting` → `active` → `closing` → `closed` (also `failed`).

### Activation proof

* Local/dynamic: TCP connect to bind address succeeds (process-alive alone is insufficient).
* Remote: short process-alive window after `ExitOnForwardFailure=yes`.

## Timeouts / stop / cleanup

Active timeout default 30s. Stop is idempotent. Unexpected process exit → `failed`.
GTK UI exit does not stop daemon-owned forwards (rediscovery after restart).

## Examples

```python
fwd = client.open_forward(OpenForwardRequest(
    connection_id=cid, type=ForwardType.LOCAL,
    bind_host="127.0.0.1", bind_port=0,
    destination_host="127.0.0.1", destination_port=80,
))
# wait until ACTIVE
client.close_forward(CloseForwardRequest(forward_id=fwd.id))
```
