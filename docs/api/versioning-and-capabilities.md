# Versioning and capabilities

See [compatibility.md](compatibility.md), [capabilities.md](capabilities.md),
[protocol-v1.md](protocol-v1.md).

## Handshake

Daemon clients negotiate `PROTOCOL_VERSION` and capability sets during handshake.
Incompatible daemons are rejected by the client factory.

## Relevant capabilities

* Sessions write/read/events
* Interactions read/respond + host-key
* SFTP / transfers / forwards write+read+events
* Daemon status/stop/restart

Markers in capabilities.md are enforced by `tests/api/test_api_documentation.py`.
