# Temporary OpenSSH fixture (Phase 13)

## Purpose

Provide a disposable, localhost-only OpenSSH server for Phase 13 acceptance
and GUI smoke without touching the developer’s real `~/.ssh` configuration.

## Location

| Path | Role |
| --- | --- |
| `tests/fixtures/temporary_openssh.py` | Fixture implementation |
| `tests/integration/test_temporary_openssh_fixture.py` | Automated proofs |
| `scripts/phase13-openssh-fixture.sh` | Manual start / JSON print / destroy |

## Capabilities

* Password authentication
* Encrypted private key + passphrase
* Unencrypted public-key authentication
* SFTP subsystem
* Local / remote / dynamic (SOCKS) forwarding
* First-use host-key confirmation (empty `known_hosts`)
* Rejected authentication
* Deterministic cleanup via `destroy()`

## Setup

Requirements: `podman` or `docker`, `ssh`, `ssh-keygen`, `ssh-keyscan`.

```bash
cd /path/to/sshpilot
PYTHONPATH=src:. python3 - <<'PY'
from pathlib import Path
import tempfile, json
from tests.fixtures.temporary_openssh import start_temporary_openssh
root = Path(tempfile.mkdtemp(prefix='p13-sshd-'))
env = start_temporary_openssh(root)
print(json.dumps(env.to_json(), indent=2))
# … use env.port / env.password / env.plain_key_path …
env.destroy()
PY
```

Manual helper:

```bash
./scripts/phase13-openssh-fixture.sh
# prints JSON; press ENTER to destroy
```

## Isolation guarantees

* Binds `127.0.0.1:<ephemeral-port>` only
* Host keys generated inside the container
* Client keys / known_hosts / ssh_config written only under `tmp_path`
* Never reads or writes the user’s `~/.ssh/config` or `~/.ssh/known_hosts`

## Cleanup

```python
env.destroy()  # podman/docker rm -f <container> (retries + verifies)
```

`start_temporary_openssh()` registers process-exit auto-cleanup by default
(atexit + weakref). Callers that **hand off** the container to a later step
(Flatpak E2E, the manual helper) must disable that:

```python
env = start_temporary_openssh(root, auto_cleanup=False)
# or: env.detach()
meta = env.to_json()
# … later …
from tests.fixtures.temporary_openssh import destroy_temporary_openssh_meta
destroy_temporary_openssh_meta(meta)
```

Pytest also sweeps leftover `sshpilot-p13-*` containers at session start and
session finish (`cleanup_orphaned_temporary_openssh`). That path never touches
the production sshPilot daemon.

Automated:

```bash
PYTHONPATH=src:. pytest tests/integration/test_temporary_openssh_fixture.py -vv
```

Orphan containers (if a smoke run is killed with SIGKILL):

```bash
podman ps -a --filter name=sshpilot-p13
podman rm -f $(podman ps -aq --filter name=sshpilot-p13)
# Kill leftover catatonit/conmon holding overlay mounts if needed, then:
podman unshare rm -rf /tmp/sshpilot-phase13-smoke-*
```

Rootless Podman may leave UID-mapped overlay trees that ordinary `rm -rf`
cannot delete (`Permission denied` / `Device or resource busy`). Prefer
`podman unshare rm -rf` after `podman rm -f`, and confirm with:

```bash
pgrep -af 'phase13-fixture|sshpilot-p13' || echo none
ls -d /tmp/sshpilot-phase13-smoke-* 2>/dev/null || echo 'smoke dirs gone'
```

## Phase 13.2

The production smoke and daemon integration tests use this fixture for password, key, encrypted-key, SFTP, transfers, and forwarding proofs without touching the developer `~/.ssh`.
