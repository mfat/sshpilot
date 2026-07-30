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
import json, os, sys
sys.path.insert(0, "${ROOT}")
sys.path.insert(0, "${ROOT}/src")
meta = Path("${TMPDIR}") / "meta.json"
if meta.exists():
    data = json.loads(meta.read_text())
    from tests.fixtures.temporary_openssh import TemporaryOpenSSH
    # Reconstruct minimal destroy via runtime
    import subprocess
    subprocess.run((data["runtime"], "rm", "-f", data["container_id"]), check=False)
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
env = start_temporary_openssh(Path("${TMPDIR}"))
meta = Path("${TMPDIR}") / "meta.json"
meta.write_text(json.dumps(env.to_json(), indent=2))
print(json.dumps(env.to_json(), indent=2))
print("Press ENTER to destroy the fixture…", file=sys.stderr)
PY
read -r _
