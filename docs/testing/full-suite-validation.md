# Phase 13.1 full-suite validation

Recorded against acceptance work on `dev` after baseline `b9eca377`,
re-validated after the final smoke/fixture/docs changes on this HEAD.

## Commands and outcomes

### Combined authentication (mandatory repetitions)

```bash
pytest tests -k combined_auth -vv
```

| Run | Result |
| --- | --- |
| 1 | `20 passed, 14 skipped, 2796 deselected in 44.48s` |
| 2 | `20 passed, 14 skipped, 2796 deselected in 43.69s` |
| 3 | `20 passed, 14 skipped, 2796 deselected in 43.29s` |

No skips/xfails were added to force green. The “14 skipped” are environment/module
skips already present inside `tests/test_combined_auth.py` when optional paths
do not apply; all selected combined-auth cases passed.

### Unfiltered full pytest

```bash
pytest
```

Result:

```text
2785 passed, 45 skipped in 408.94s (0:06:48)
```

* Zero command-line deselected tests.
* Stale `#987` non-strict xfails removed from `tests/conftest.py` (they XPASS’d
  locally). Affected tests now `skipif` missing tools instead of xfail → no XPASS.

### Temporary OpenSSH fixture

```bash
PYTHONPATH=src:. pytest tests/integration/test_temporary_openssh_fixture.py -q
```

Result: included in the combined extra battery below (`20 passed` covering
fixture + isolation + CLI + headless imports).

### Production GUI smoke (40/40)

```bash
SSHPILOT_GUI_TESTS=1 DISPLAY=:1 PYTHONPATH=src:. \
  python3 tests/manual/phase13_production_smoke.py
```

Result: `Smoke finished: 40/40 passed` — table in
`docs/testing/phase13-production-smoke.md`
(HOME=`/tmp/sshpilot-phase13-smoke-4ltsntag` at generation time).

### Other gates (final battery after last change)

| Gate | Result |
| --- | --- |
| `git diff --check` | clean |
| `python3 -m compileall -q src` | OK |
| `ruff check` (acceptance Python paths) | All checks passed |
| `ty check src/sshpilot/core` | All checks passed |
| `pytest tests/core` + `tests/api` | 356 passed |
| `pytest tests/daemon` | 319 passed in 123.00s |
| `pytest tests -k combined_auth -vv` ×3 | 20 passed, 14 skipped each |
| `meson test -C builddir` | 2/2 OK |
| Race tests ×5 (`tests/core/test_races.py`) | 6 passed each run |
| GTK tests ×5 (`SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui`) | 56 passed, 1 skipped each run |
| Fixture + isolation + CLI + headless imports | 20 passed |
| `env -u DISPLAY -u WAYLAND_DISPLAY ./sshpilot-core validate-connection …` | exit 0 |
| API artifact snapshot (`tests/api -k artifact`) | 1 passed |
| Unfiltered `pytest` (final) | `2785 passed, 45 skipped` (0 deselected, 0 XPASS) |

### Race runs (individual)

| Run | Result |
| --- | --- |
| 1 | 6 passed in 0.12s |
| 2 | 6 passed in 0.25s |
| 3 | 6 passed in 0.14s |
| 4 | 6 passed in 0.29s |
| 5 | 6 passed in 0.18s |

### GTK runs (individual)

| Run | Result |
| --- | --- |
| 1 | 56 passed, 1 skipped in 55.28s |
| 2 | 56 passed, 1 skipped in 55.78s |
| 3 | 56 passed, 1 skipped in 55.55s |
| 4 | 56 passed, 1 skipped in 55.93s |
| 5 | 56 passed, 1 skipped in 56.52s |

## Cleanup verification

```bash
pgrep -af 'python3 -m sshpilot.daemon'
pgrep -af 'phase13-fixture|sshpilot-p13'
podman ps -a --filter name=sshpilot-p13
ls /tmp/.*.sshpilot-tmp-* 2>/dev/null
# rootless overlay leftovers under smoke HOME:
podman unshare rm -rf /tmp/sshpilot-phase13-smoke-*
```

Observed after acceptance:

* User daemon at `/run/user/1000/sshpilot/sshpilotd.sock` may remain (expected; not a test leak).
* No `sshpilot-p13` containers after fixture `destroy()` / `podman rm -f`.
* No sshpilot temporary transfer files in `/tmp`.
* Smoke isolated HOME trees removed via `podman unshare rm -rf` when overlay
  layers are root-mapped.
