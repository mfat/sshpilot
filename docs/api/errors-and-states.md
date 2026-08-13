# Errors and states

Canonical enumerations: [errors.md](errors.md), [state-machines.md](state-machines.md).

## Session auth failures (Phase 13.2)

| Situation | State | Error |
| --- | --- | --- |
| Wrong password / key | `failed`/`closed` | `session_startup_failed` |
| Prompt cancel | `failed`/`closed` | `operation_cancelled` |
| Host-key reject | `failed` | `session_startup_failed` |
| Premature RUNNING | **fixed** — RUNNING requires ControlMaster when broker path used | |

## Service states quick map

* Session: created/starting/running/closing/exited/failed/closed
* SFTP: created/starting/ready/closing/failed/closed
* Transfer: created/queued/running/completed/failed/cancelled
* Forward: created/starting/active/closing/failed/closed
* Interaction: pending/claimed/answered/cancelled/expired/failed
* Daemon: starting/ready/idle/draining/stopping/stopped
