# Transfers API

Stability: **stable**.

## Methods

`start_transfer`, `get_transfer`, `list_transfers`, `cancel_transfer` —
see [methods.md](methods.md).

## State machine

`created` → `queued`/`running` → `completed` | `failed` | `cancelled`.

## Behavior

* `start_transfer` uses an existing READY SFTP service; its generic conflict
  policies and atomic temporary-file behavior apply only to SFTP transfers.
* Native `start_scp_transfer` uses daemon-owned system `scp`, reports indeterminate
  progress when byte totals are unavailable, and is overwrite-only. `fail`,
  `skip`, and `rename` conflict policies are rejected for native SCP.
* Cancellation supervises the native process group and reports `CANCELLED` only
  after the child is reaped. Modern SCP retries once with `-O` only for strong
  SFTP-subsystem negotiation failures.
* SFTP final rename and temporary-file cleanup semantics do not imply portable
  atomicity for native SCP.

## Ownership / retention / concurrency

Owned by the requesting client through the transfer runtime; queue admission is
bounded; retained records within configured limits.

## Examples

```python
t = client.start_transfer(StartTransferRequest(
    connection_id=cid, sftp_service_id=sid,
    direction=TransferDirection.UPLOAD,
    remote_path="a.txt", local_path="/tmp/a.txt",
    conflict_policy=TransferConflictPolicy.OVERWRITE,
))
client.cancel_transfer(CancelTransferRequest(transfer_id=t.id))
```
