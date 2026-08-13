# Sessions API

Stability: **stable**.

## Methods

`open_session`, `get_session`, `list_sessions`, `close_session`, `attach_session`,
`detach_session`, terminal I/O helpers — see [methods.md](methods.md).

## Request / response

* `OpenSessionRequest(connection_id, dimensions?, remote_command?, force_tty?)` → `SessionSummary`
* Summary fields: `id`, `connection_id`, `state`, timestamps, `failure`, `exit_info`
* `remote_command` (optional): when set, the daemon runs `<ssh> <alias> <remote_command>`
  inside the connection instead of a plain interactive shell. This is how
  container shells (`docker exec -it <container> sh`) and container logs
  (`docker logs -f <container>`) are opened. Requires the `sessions.command`
  capability.
* `force_tty` (optional, default `False`): when `True` the daemon adds `-t` to
  the SSH argv so a remote PTY is allocated. Interactive remote commands such
  as `docker exec -it <container> sh` need this, otherwise OpenSSH runs the
  command with no remote terminal and docker rejects the TTY request.

## Events

`SESSION_CREATED`, `SESSION_STATE_CHANGED`, `SESSION_EXITED`, `SESSION_CLOSED`.

## State machine

`created` → `starting` → `running` → `closing` → `exited` → `closed`
(also `starting`/`running` → `failed` → `closed`).

### Semantic definition (Phase 13.2)

| State | Meaning |
| --- | --- |
| `starting` | Accepted; process/auth in progress — **not usable** |
| `running` | Authenticated; ControlMaster ready when broker path used |
| `failed` | Startup/auth/runtime failure (cancel → `OPERATION_CANCELLED`) |
| `closed` | Terminal; resources reaped |

There is no Protocol v1 `CANCELLED` session enum yet; cancellation is
`failed` + `OPERATION_CANCELLED`.

## Timeouts / cancellation

* Host-key interaction timeout default 180s; secret timeout 120s.
* Auth-gate wait default 60s after process spawn.
* Cancel interaction declines askpass; child terminated; session fails closed.

## Ownership

Originating client owns interactions. Attachments may interact when attached.
UI disconnect does not close daemon-owned sessions (detach policy).

## Retention / cleanup

Closed sessions retained within configured bounds; shutdown reaps children.

## Examples

```python
opened = client.open_session(OpenSessionRequest(connection_id=cid))
# poll until RUNNING / FAILED / CLOSED
client.close_session(CloseSessionRequest(session_id=opened.id))
```
