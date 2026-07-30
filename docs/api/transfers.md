# Transfers API

Stability: **stable**.

## Methods

`start_transfer`, `get_transfer`, `list_transfers`, `cancel_transfer` —
see [methods.md](methods.md).

## State machine

`created` → `queued`/`running` → `completed` | `failed` | `cancelled`.

## Behavior

* Uses an existing READY SFTP service.
* Progress is monotonic; conflict policy via `core.transfers.decide_conflict`.
* Cancel reaches `CANCELLED`; atomic temp files (`_TEMP_PREFIX`) cleaned.
* Final rename only after success.

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
