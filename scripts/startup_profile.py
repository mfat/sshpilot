#!/usr/bin/env python3
"""Measure sshPilot startup.

Two reports:

``imports`` (default)
    Import time and dataclass count per module for the GUI entry
    (``sshpilot.main`` + ``sshpilot.window``, after GTK/Adwaita) and for the
    daemon (``sshpilot.daemon.server``). Each target is imported in a fresh
    interpreter run with ``-X importtime``; ``dataclasses._process_class`` is
    wrapped to count and time dataclass creation per defining module.

``launch``
    Starts the real app in a throwaway profile (its own APPDATA/LOCALAPPDATA,
    XDG dirs, ``~/.ssh`` and application id, so a running copy is never
    touched), waits for the populated sidebar to be painted, and prints the
    startup milestones (see ``sshpilot.startup_timing``). ``--cold`` stops the
    sandbox daemon before every run; ``--warm`` leaves one running.

Examples::

    python scripts/startup_profile.py imports --top 25
    python scripts/startup_profile.py launch --cold --warm --runs 5
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

TARGETS = {
    "gui": (
        "import gi\n"
        "gi.require_version('Gtk', '4.0')\n"
        "gi.require_version('Adw', '1')\n"
        "from gi.repository import Gtk, Adw\n"
        "_mark('gtk')\n"
        "import sshpilot.main\n"
        "import sshpilot.window\n"
    ),
    "daemon": "_mark('gtk')\nimport sshpilot.daemon.server\n",
}

# Runs in the child interpreter: count/time dataclasses per module, then
# import the target and dump the counts as JSON on stdout.
CHILD = r"""
import dataclasses, json, sys, time
sys.path.insert(0, {src!r})
_counts = {{}}
_orig = dataclasses._process_class
def _process_class(cls, *args, **kwargs):
    start = time.perf_counter()
    try:
        return _orig(cls, *args, **kwargs)
    finally:
        entry = _counts.setdefault(cls.__module__, [0, 0.0])
        entry[0] += 1
        entry[1] += time.perf_counter() - start
dataclasses._process_class = _process_class
_t0 = time.perf_counter()
_marks = {{}}
def _mark(name):
    _marks[name] = time.perf_counter()
{body}
_end = time.perf_counter()
print(json.dumps({{
    "dataclasses": _counts,
    "total_s": _end - _t0,
    "target_s": _end - _marks.get("gtk", _t0),
}}))
"""


def _child_env() -> dict:
    env = dict(os.environ)
    gtk_bin = ROOT / "vendor" / "gtk" / "bin"
    if sys.platform == "win32" and gtk_bin.is_dir():
        env["PATH"] = f"{gtk_bin}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(SRC), env.get("PYTHONPATH", "")])
    )
    return env


def _parse_importtime(stderr: str) -> dict[str, tuple[int, int]]:
    """``{module: (self_us, cumulative_us)}`` from ``-X importtime`` output."""
    result: dict[str, tuple[int, int]] = {}
    for line in stderr.splitlines():
        if not line.startswith("import time:") or "self [us]" in line:
            continue
        try:
            self_us, cumulative_us, name = line.split("|", 2)
            self_us = int(self_us.split(":")[-1].strip())
            cumulative_us = int(cumulative_us.strip())
        except ValueError:
            continue
        name = name.strip()
        # A module can appear more than once (re-import attempt); keep the max.
        previous = result.get(name, (0, 0))
        result[name] = (max(previous[0], self_us), max(previous[1], cumulative_us))
    return result


def _profile_target(name: str) -> dict:
    code = CHILD.format(src=str(SRC), body=TARGETS[name])
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", code],
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit(f"{name}: import failed (exit {proc.returncode})")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    data["imports"] = _parse_importtime(proc.stderr)
    return data


def _best_of(name: str, repeat: int) -> dict:
    runs = [_profile_target(name) for _ in range(repeat)]
    return min(runs, key=lambda run: run["target_s"])


def report_imports(args) -> None:
    results = {}
    for name in args.targets:
        data = _best_of(name, args.repeat)
        results[name] = data
        imports = data["imports"]
        sshpilot_mods = {
            mod: times for mod, times in imports.items()
            if mod == "sshpilot" or mod.startswith("sshpilot.")
        }
        dataclass_counts = data["dataclasses"]
        total_dc = sum(count for count, _secs in dataclass_counts.values())
        total_dc_ms = sum(secs for _count, secs in dataclass_counts.values()) * 1000
        print(f"\n=== {name}: target import {data['target_s'] * 1000:.0f} ms "
              f"(process {data['total_s'] * 1000:.0f} ms), "
              f"{len(sshpilot_mods)} sshpilot modules, "
              f"{total_dc} dataclasses ({total_dc_ms:.0f} ms) ===")
        print(f"{'cumulative ms':>13} {'self ms':>8} {'dataclasses':>11}  module")
        ranked = sorted(sshpilot_mods.items(), key=lambda item: -item[1][1])
        for mod, (self_us, cumulative_us) in ranked[: args.top]:
            count, secs = dataclass_counts.get(mod, (0, 0.0))
            dc = f"{count} ({secs * 1000:.0f}ms)" if count else ""
            print(f"{cumulative_us / 1000:13.1f} {self_us / 1000:8.1f} {dc:>11}  {mod}")
        print("\nmost dataclasses:")
        for mod, (count, secs) in sorted(
            dataclass_counts.items(), key=lambda item: -item[1][0]
        )[: min(args.top, 15)]:
            print(f"  {count:4d} {secs * 1000:7.1f} ms  {mod}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# launch benchmark

SAMPLE_HOSTS = 60


def _sandbox(root: Path) -> dict:
    """Environment for an app instance isolated from the user's own copy."""
    for sub in ("Roaming", "Local", "config", "data", "state", "ssh"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    ssh_config = root / "ssh" / "config"
    if not ssh_config.exists():
        lines = []
        for index in range(SAMPLE_HOSTS):
            lines += [
                f"Host bench-{index:03d}",
                f"    HostName 192.0.2.{index % 250 + 1}",
                f"    User user{index % 7}",
                f"    Port {22 + index % 3}",
                "",
            ]
        ssh_config.write_text("\n".join(lines), encoding="utf-8")
    env = _child_env()
    env.update({
        "APPDATA": str(root / "Roaming"),
        "LOCALAPPDATA": str(root / "Local"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "SSHPILOT_SSH_DIR": str(root / "ssh"),
        "SSHPILOT_APP_ID": "io.github.mfat.sshpilot.StartupProfile",
    })
    if sys.platform != "win32":
        runtime = root / "run"
        runtime.mkdir(mode=0o700, exist_ok=True)
        env["XDG_RUNTIME_DIR"] = str(runtime)
    return env


def _stop_daemon(env: dict) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--daemon", "--force", "stop"],
        env=env, cwd=str(ROOT), capture_output=True, timeout=30,
    )
    # Wait until a fresh connect would have to spawn.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "--daemon", "status"],
            env=env, cwd=str(ROOT), capture_output=True, timeout=30,
        )
        if status.returncode == 2:
            return
        time.sleep(0.2)


def _start_daemon(env: dict) -> None:
    """Leave a ready daemon running (quitting the app stops it again)."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "--daemon"],
        env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "--daemon", "status"],
            env=env, cwd=str(ROOT), capture_output=True, timeout=30,
        )
        if status.returncode == 0:
            return
        time.sleep(0.2)
    raise SystemExit("the sandbox daemon did not become ready")


def _launch_once(env: dict, out: Path, timeout: float) -> dict:
    out.unlink(missing_ok=True)
    run_env = dict(env)
    run_env["SSHPILOT_STARTUP_PROFILE"] = str(out)
    run_env["SSHPILOT_STARTUP_LAUNCH_TIME"] = repr(time.time())
    start = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py")],
        env=run_env, cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise SystemExit("launch timed out before the sidebar was populated")
    wall = (time.perf_counter() - start) * 1000
    if not out.exists():
        raise SystemExit(f"app exited ({proc.returncode}) without a profile")
    data = json.loads(out.read_text(encoding="utf-8"))
    data["process_exit_ms"] = wall
    return data


def _summarise(label: str, runs: list[dict]) -> dict:
    names: list[str] = []
    for run in runs:
        for item in run["milestones"]:
            if item["name"] not in names:
                names.append(item["name"])
    print(f"\n=== {label} start: median of {len(runs)} runs (ms since launch) ===")
    summary = {}
    for name in names:
        values = [
            item["ms"] for run in runs for item in run["milestones"]
            if item["name"] == name
        ]
        if not values:
            continue
        detail = next(
            (item["detail"] for item in runs[-1]["milestones"]
             if item["name"] == name and item["detail"]), "",
        )
        median = statistics.median(values)
        summary[name] = median
        spread = f"{min(values):7.0f}-{max(values):<7.0f}" if len(values) > 1 else ""
        print(f"  {median:7.0f}  {spread} {name}{f'  ({detail})' if detail else ''}")
    return summary


def report_launch(args) -> None:
    root = Path(args.sandbox) if args.sandbox else (
        Path(tempfile.gettempdir()) / "sshpilot-startup-profile"
    )
    if args.fresh_sandbox and root.exists():
        shutil.rmtree(root)
    env = _sandbox(root)
    out = root / "profile.json"
    modes = []
    if args.cold:
        modes.append("cold")
    if args.warm:
        modes.append("warm")
    if not modes:
        modes = ["cold", "warm"]
    # One untimed run: compiles bytecode and warms GTK caches in the sandbox.
    _launch_once(env, out, args.timeout)
    results = {}
    for mode in modes:
        runs = []
        for _ in range(args.runs):
            _stop_daemon(env)
            if mode == "warm":
                _start_daemon(env)
            runs.append(_launch_once(env, out, args.timeout))
        results[mode] = {"summary": _summarise(mode, runs), "runs": runs}
    _stop_daemon(env)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    imports = sub.add_parser("imports", help="per-module import time and dataclasses")
    imports.add_argument("--targets", nargs="+", choices=sorted(TARGETS),
                         default=["gui", "daemon"])
    imports.add_argument("--top", type=int, default=30)
    imports.add_argument("--repeat", type=int, default=3,
                         help="keep the fastest of N runs (default 3)")
    imports.add_argument("--json", help="also write raw results here")
    launch = sub.add_parser("launch", help="time a real launch to a populated sidebar")
    launch.add_argument("--cold", action="store_true", help="no daemon running")
    launch.add_argument("--warm", action="store_true", help="daemon already running")
    launch.add_argument("--runs", type=int, default=5)
    launch.add_argument("--timeout", type=float, default=90.0)
    launch.add_argument("--sandbox", help="profile directory (default: temp dir)")
    launch.add_argument("--fresh-sandbox", action="store_true")
    launch.add_argument("--json", help="also write raw results here")
    args = parser.parse_args(argv)
    if args.command == "launch":
        report_launch(args)
    else:
        if args.command is None:
            args = parser.parse_args(["imports", *(argv or [])])
        report_imports(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
