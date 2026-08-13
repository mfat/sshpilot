# SFTP API

Stability: **stable**.

## Methods

`open_sftp`, `get_sftp_service`, `list_sftp_services`, `close_sftp`,
`attach_sftp`, `sftp_list_directory`, `sftp_mkdir`, `sftp_copy`,
`sftp_rename`, `sftp_remove`, `sftp_rmdir`, `sftp_stat`, `sftp_readlink`,
`sftp_read_file`, `sftp_replace_file`, `sftp_chmod`, `sftp_symlink` — see
[methods.md](methods.md).

## State machine

`created` → `starting` → `ready` → `closing` → `closed` (also `failed`).

`READY` means the SFTP protocol handshake completed and the service is usable.
Auth/host-key go through the same InteractionBroker as sessions (service id as scope).

## Timeouts / cancellation / cleanup

Startup failures record stderr classification via `ServiceFailure`.
Close is idempotent. Retained closed records are bounded.

## Ownership

Owner client + attached clients may use READY services.

## Examples

```python
svc = client.open_sftp(OpenSftpRequest(connection_id=cid))
# wait until READY
listing = client.sftp_list_directory(ListDirectoryRequest(
    connection_id=cid, service_id=svc.id, path=".",
))
```
