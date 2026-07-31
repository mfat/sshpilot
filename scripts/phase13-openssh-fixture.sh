#!/usr/bin/env bash
# Start the Phase 13 temporary OpenSSH fixture for manual GUI smoke.
# Prints JSON credentials, waits for ENTER, then destroys the container.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
TMPDIR="${TMPDIR:-/tmp}/sshpilot-phase13-openssh-$$"
mkdir -p "$TMPDIR"
cleanup() {
  python3 - <<PY
from pathlib import Path
import json, sys
sys.path.insert(0, "${ROOT}")
sys.path.insert(0, "${ROOT}/src")
meta = Path("${TMPDIR}") / "meta.json"
if meta.exists():
    from tests.fixtures.temporary_openssh import destroy_temporary_openssh_meta
    destroy_temporary_openssh_meta(json.loads(meta.read_text()))
print("fixture destroyed", file=sys.stderr)
PY
  rm -rf "$TMPDIR"
}
trap cleanup EXIT
python3 - <<PY
from pathlib import Path
import json, sys
sys.path.insert(0, "${ROOT}")
sys.path.insert(0, "${ROOT}/src")
from tests.fixtures.temporary_openssh import start_temporary_openssh
# Persist after this starter process exits; bash trap destroys on EXIT.
env = start_temporary_openssh(Path("${TMPDIR}"), auto_cleanup=False)
meta = Path("${TMPDIR}") / "meta.json"
meta.write_text(json.dumps(env.to_json(), indent=2))
print(json.dumps(env.to_json(), indent=2))
print("Press ENTER to destroy the fixture…", file=sys.stderr)
PY
read -r _
