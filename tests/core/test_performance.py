"""Generous performance smoke checks (not microbenchmarks)."""
from __future__ import annotations

import time

from sshpilot.core.connections import ConnectionService
from sshpilot.core.import_export import plan_import
from sshpilot.core.keys import KeyService
from sshpilot.core.ssh import SSHLaunchRequest, build_ssh_process_spec


def test_import_core_is_fast():
    start = time.perf_counter()
    import importlib

    importlib.invalidate_caches()
    importlib.import_module("sshpilot.core")
    importlib.import_module("sshpilot.core.connections")
    importlib.import_module("sshpilot.core.ssh")
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"core import took {elapsed:.2f}s"


def test_large_connection_dataset(tmp_path):
    svc = ConnectionService(store_path=tmp_path / "big.json", autosave=False)
    start = time.perf_counter()
    for i in range(500):
        svc.create(
            {
                "nickname": f"Host{i}",
                "hostname": f"h{i}.example.test",
                "username": "u",
                "port": 22,
            }
        )
    elapsed = time.perf_counter() - start
    assert len(svc.list_connections()) == 500
    assert elapsed < 10.0, f"create 500 took {elapsed:.2f}s"


def test_validate_large_import_and_many_ssh_specs():
    conns = [
        {"nickname": f"N{i}", "hostname": f"h{i}.test", "username": "u"}
        for i in range(1000)
    ]
    start = time.perf_counter()
    plan = plan_import({"version": 1, "connections": conns})
    assert plan.ok
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0

    start = time.perf_counter()
    for i in range(500):
        build_ssh_process_spec(SSHLaunchRequest(destination=f"h{i}", port=22 + (i % 10)))
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0


def test_list_large_key_directory(tmp_path):
    for i in range(100):
        (tmp_path / f"id_test_{i}").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        (tmp_path / f"id_test_{i}.pub").write_text("ssh-ed25519 AAAA")
    start = time.perf_counter()
    keys = KeyService(tmp_path).discover_keys()
    elapsed = time.perf_counter() - start
    assert len(keys) >= 1
    assert elapsed < 5.0
