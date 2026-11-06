"""Exercise built-in RDP crypto against the real Windows WinPR runtime."""

from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="bundled Windows runtime")
def test_winpr_md4_and_rc4_known_answers():
    runtime = ROOT / "vendor/gtk/bin"
    if not (runtime / "winpr3.dll").exists():
        pytest.skip("requires the bundled Windows runtime")
    result = subprocess.run([sys.executable, str(ROOT / "packaging/windows/check-rdp-crypto.py"),
                             str(runtime), str(runtime)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checks passed without the OpenSSL legacy provider" in result.stdout
