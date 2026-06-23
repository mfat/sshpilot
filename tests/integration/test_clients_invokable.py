"""Integration: confirm the mosh/kubectl client binaries the backends target are
present and runnable (a cheap reliability gate without spinning a cluster)."""

import shutil
import subprocess
import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not shutil.which("mosh"), reason="mosh not installed")
def test_mosh_client_invokable():
    out = subprocess.run(["mosh", "--version"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert "mosh" in (out.stdout + out.stderr).lower()


@pytest.mark.skipif(not shutil.which("kubectl"), reason="kubectl not installed")
def test_kubectl_client_invokable():
    out = subprocess.run(["kubectl", "version", "--client"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert "version" in (out.stdout + out.stderr).lower()
