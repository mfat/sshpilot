"""Shared helpers for the agent experiment (snapshots, isolated test runs, grading).

See README.md in this directory. Work happens in $SSHPILOT_EXPERIMENT_DIR
(outside the checkout); the checkout itself is only read via ``git archive``.
"""
import io, os, subprocess, sys, tarfile, tempfile, xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PY = os.environ.get("SSHPILOT_EXPERIMENT_PYTHON") or (str(REPO / ".venv/bin/python") if (REPO / ".venv/bin/python").exists() else sys.executable)
try:
    EXP = Path(os.environ["SSHPILOT_EXPERIMENT_DIR"]).resolve()
except KeyError:
    raise SystemExit("set SSHPILOT_EXPERIMENT_DIR to a directory outside the checkout") from None
if EXP == REPO or REPO in EXP.parents:
    raise SystemExit("SSHPILOT_EXPERIMENT_DIR must be outside the checkout (agents must not see future commits)")
EXP.mkdir(parents=True, exist_ok=True)


def git(*args, binary=False):
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode()


def snapshot(rev: str, target: Path) -> Path:
    data = git("archive", "--format=tar", rev, binary=True)
    target.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(target, filter="data")
    return target


def fix_test_files(fix: str):
    names = git("show", "--name-only", "--format=", fix).split()
    return [n for n in names if n.startswith("tests/") and n.endswith(".py")]


def safe_env(home: Path, root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SSHPILOT", "WAYLAND", "XDG_", "PYTEST"))}
    for sub in ("config", "data", "cache", "state", "runtime"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(home / "runtime", 0o700)
    env.update(
        HOME=str(home), XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
        XDG_CACHE_HOME=str(home / "cache"), XDG_STATE_HOME=str(home / "state"), XDG_RUNTIME_DIR=str(home / "runtime"),
        SSHPILOT_SSH_DIR=str(home / "ssh"), SSHPILOT_APP_ID="io.github.mfat.sshpilot.experiment",
        GDK_BACKEND="x11", PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1",
    )
    env.pop("DISPLAY", None)
    return env


def run_tests(root: Path, tests, timeout=600) -> dict:
    """Run test files in *root* isolated; per-test outcomes from JUnit."""
    with tempfile.TemporaryDirectory(prefix="exp-run-") as tmp:
        tmp = Path(tmp)
        report = tmp / "junit.xml"
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1280x1024x24", PY, "-m", "pytest", "-q", "-n0",
               "-p", "no:cacheprovider", "-p", "no:hypothesispytest", f"--junitxml={report}", "-o", "junit_family=xunit2", "--", *tests]
        try:
            proc = subprocess.run(cmd, cwd=root, env=safe_env(tmp / "home", root), capture_output=True, text=True, timeout=timeout)
            rc, tail = proc.returncode, (proc.stdout + proc.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            rc, tail = None, "TIMEOUT"
        cases = {}
        if report.is_file():
            for case in ET.parse(report).getroot().iter("testcase"):
                tid = f"{case.get('classname')}::{case.get('name')}"
                state, msg = "passed", ""
                for child in case:
                    if child.tag in ("failure", "error", "skipped"):
                        state = {"failure": "failed", "error": "error", "skipped": "skipped"}[child.tag]
                        msg = (child.get("message") or "")[:200]
                cases[tid] = (state, msg)
    return {"rc": rc, "cases": cases, "tail": tail}
