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
forward:<UUIDv4>
```

## Lifecycle

States: `created` → `starting` → `active` → `closing` → `closed`, or `failed`.

Open acknowledges `starting` immediately. Activation uses
`ExitOnForwardFailure=yes` plus local bind checks for local/dynamic forwards.
Process exit before activation → `failed`; after activation → `closed` /
`failed` by reason.

## Types

- Local: `bind_host:bind_port` → `destination_host:destination_port`
- Remote: remote bind → local destination
- Dynamic: local SOCKS bind; no destination fields

## Ownership

Originating client owns close. Forwards persist after GTK disconnect.
Daemon shutdown terminates all. Daemon restart loses all forwards.

## Retention

Up to 100 closed forward summaries.
