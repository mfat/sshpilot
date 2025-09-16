import os
import subprocess
from sshpilot import askpass_utils


def test_askpass_uses_runtime_dir(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    env = os.environ.copy()
    env["SSHPILOT_ASKPASS_LOG_DIR"] = str(runtime_dir)
    script = askpass_utils.force_regenerate_askpass_script()
    subprocess.run([script], env=env, check=False)
    assert (runtime_dir / "sshpilot-askpass.log").exists()
