#!/usr/bin/env python3
"""Smoke-test the daemon entrypoint inside a built macOS app bundle."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sshpilot.api.daemon_client import DaemonClient  # noqa: E402
from sshpilot.api.errors import SshPilotError  # noqa: E402


def _bundle_executable(bundle: Path) -> Path:
    executable = bundle / "Contents" / "MacOS" / "SSHPilot"
    if not executable.is_file():
        raise SystemExit(f"bundle executable not found: {executable}")
    return executable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()

    bundle = args.bundle.resolve()
    executable = _bundle_executable(bundle)

    with tempfile.TemporaryDirectory(prefix="sshpilot-bundle-smoke-") as temp_dir:
        temp = Path(temp_dir)
        socket_path = temp / "runtime" / "sshpilotd.sock"
        for name in (
            "HOME",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            path = temp / name.lower()
            path.mkdir(parents=True, exist_ok=True)
            if name == "XDG_RUNTIME_DIR":
                path.chmod(0o700)

        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(temp / "home"),
                "XDG_RUNTIME_DIR": str(temp / "xdg_runtime_dir"),
                "XDG_CONFIG_HOME": str(temp / "xdg_config_home"),
                "XDG_DATA_HOME": str(temp / "xdg_data_home"),
                "XDG_STATE_HOME": str(temp / "xdg_state_home"),
                "XDG_CACHE_HOME": str(temp / "xdg_cache_home"),
                "SSHPILOT_PACKAGED": "1",
            }
        )
        log_path = temp / "daemon.log"
        with log_path.open("w+") as log_file:
            process = subprocess.Popen(
                [str(executable), "--daemon", "--socket", str(socket_path)],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=environment,
            )
            client = None
            deadline = time.monotonic() + 15.0
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log_file.flush()
                        raise RuntimeError(
                            "bundled daemon exited before handshake "
                            f"(returncode={process.returncode})\n"
                            f"{log_path.read_text()}"
                        )
                    try:
                        client = DaemonClient(
                            socket_path=socket_path,
                            timeout=2.0,
                            connect_timeout=0.25,
                            client_name="macos-bundle-smoke",
                            frontend_type="smoke",
                        )
                        client.get_capabilities()
                        return 0
                    except SshPilotError:
                        if client is not None:
                            client.close()
                            client = None
                        time.sleep(0.1)
            finally:
                if client is not None:
                    client.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5.0)

            log_file.flush()
            raise RuntimeError(
                "timed out waiting for bundled daemon handshake\n"
                f"{log_path.read_text()}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
