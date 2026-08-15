# Daemon-Owned Port Forwarding (Phase 10)

Local, remote, and dynamic (SOCKS) forwards are daemon-owned dedicated
`ssh -N` processes, independent of terminal tabs.

## Ownership

```text
GTK / plugin
    → typed daemon forward API
    → daemon-owned `ssh -N -L|-R|-D …`
    → forward remains active after GTK detaches
```

Config-static `LocalForward` / `RemoteForward` / `DynamicForward` directives
written into `~/.ssh/config` remain terminal-bound when an interactive session
loads that Host. Managed forwarding UI / plugin `ensure_local_forward` use
daemon forward services. Do not launch both for the same rule.

## Identity

```text
forward-<n>
```

## Lifecycle

States: `created` → `starting` → `active` → `closing` → `closed`, or `failed`.

Open acknowledges `starting` immediately. Activation uses
`ExitOnForwardFailure=yes`. **Local and dynamic** forwards become `ACTIVE`
only after the local bind accepts a TCP connect (default wait up to 30s so
host-key/password prompts can complete). **Remote** forwards become `ACTIVE`
after a short process-alive window (~0.5s); the remote bind is not visible to
the client, so ACTIVE is honest about process health with
`ExitOnForwardFailure`, not about an independently probed remote listener.

Do **not** use OpenSSH `ClearAllForwardings=yes` on the same argv as
`-L`/`-R`/`-D`: on current OpenSSH that option clears *all* forwards including
the ad-hoc flags. Daemon forward launch instead uses a temporary ssh config
that strips config-static `LocalForward` / `RemoteForward` / `DynamicForward`
lines so only the daemon-owned rule applies.

Process exit before activation → `failed`; after activation → `closed` /
`failed` by reason.

## Types

- Local: `bind_host:bind_port` → `destination_host:destination_port` (destination is relative to the SSH server)
- Remote: remote bind → local destination; requires server `AllowTcpForwarding` and typically `GatewayPorts clientspecified` / `PermitListen` for non-loopback remote binds. Remote port `0` is rejected when unsupported rather than returning an unknown port.
- Dynamic: local SOCKS5 bind (no-auth CONNECT exercised in integration tests); no destination fields

## Ownership

Originating client owns close. Forwards persist after GTK disconnect.
Observers may list/get but cannot close. Daemon shutdown terminates all.
Daemon restart loses all forwards.

## Availability

Plugin `ensure_local_forward` uses the daemon forward API. A missing daemon or
capability is reported as unavailable; no ControlMaster/`ssh -N` child or
frontend-owned forwarding process is selected.

## Phase 10.1 validation (exercised)

Real OpenSSH Alpine fixture + ephemeral daemon:

- local forward ACTIVE + HTTP payload round-trip through container echo
- dynamic SOCKS5 CONNECT + payload
- remote forward ACTIVE + container `nc` probe (skipped if networking blocks)
- bind conflict → `FORWARD_BIND_FAILED`
- client detach + owner-only close; daemon shutdown leaves no `ssh -N` orphan
- ACTIVE/close races (5×)

Config-static Host forwards remain session-owned (documented, not migrated).
