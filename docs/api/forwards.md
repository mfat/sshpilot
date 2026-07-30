# Forwards API

Stability: **stable**.

## Methods

`open_forward`, `get_forward`, `list_forwards`, `claim_forward`, `close_forward` —
see [methods.md](methods.md).

## Claim Forward Operation (`claim_forward`)

- **Request model**: `ClaimForwardRequest(forward_id: ForwardId)`
- **Client API**: `DaemonClient.claim_forward(request)`
- **Wire method**: `forwards.claim`
- **Required capability**: `forwards.write`
- **Orphan Definition**: A port forward whose original creating client disconnected without closing the forward. The forward remains alive in the daemon runtime.
- **Owner Disconnect Behavior**: When a client disconnects, its active forwards become orphaned rather than closing, allowing other clients or a restarted app instance to claim ownership.
- **Successful Claim Response**: Returns updated `ForwardSummary` with `owner_id` set to the calling client's `ClientId`.
- **Already Owned By Self**: Returns `ForwardSummary` successfully without error.
- **Owned By Another Client Error**: Returns `SshPilotError(ErrorCode.FORWARD_OWNED_BY_ANOTHER)`.
- **Closed Forward Error**: Returns `SshPilotError(ErrorCode.FORWARD_NOT_FOUND)` or `ErrorCode.INVALID_STATE`.
- **Concurrent Claim Race**: First claim wins; subsequent concurrent claim receives `FORWARD_OWNED_BY_ANOTHER`.
- **Close After Claim**: The new owner can call `close_forward()` to close the forward.
- **Protocol Compatibility**: Compatible with Protocol v1.

## Types

`LOCAL`, `REMOTE`, `DYNAMIC` (SOCKS).

## State machine

`created` → `starting` → `active` → `closing` → `closed` (also `failed`).

### Activation proof

* Local/dynamic: TCP connect to bind address succeeds (process-alive alone is insufficient).
* Remote: short process-alive window after `ExitOnForwardFailure=yes`.

## Timeouts / stop / cleanup

Active timeout default 30s. Stop is idempotent. Unexpected process exit → `failed`.
GTK UI exit does not stop daemon-owned forwards (rediscovery and claim after restart).

## Examples

```python
# Claim an orphaned forward
claimed = client.claim_forward(ClaimForwardRequest(forward_id=fwd_id))
assert claimed.owner_id == client.client_id

# Close the forward
client.close_forward(CloseForwardRequest(forward_id=claimed.id))
```
