"""ConnectionService domain tests (GI-free)."""
from __future__ import annotations

import threading

import pytest

from sshpilot.core.connections import ConnectionService, MutationKind
from sshpilot.core.errors import CoreError


@pytest.fixture
def service(tmp_path):
    return ConnectionService(store_path=tmp_path / "connections.json", autosave=True)


def test_create_update_delete(service):
    events = []
    service.add_listener(lambda e: events.append(e.kind))
    created = service.create(
        {"nickname": "Demo", "hostname": "example.com", "username": "alice", "port": 22}
    )
    assert created.nickname == "Demo"
    assert service.get(created.id) is not None
    updated = service.update(created.id, {"port": 2222})
    assert updated.port == 2222
    service.delete(created.id)
    assert service.get(created.id) is None
    assert MutationKind.CREATED in events
    assert MutationKind.UPDATED in events
    assert MutationKind.DELETED in events


def test_duplicate_and_empty_identifier(service):
    base = service.create(
        {"nickname": "Base", "hostname": "h.example", "username": "u", "port": 22}
    )
    dup = service.duplicate(base.id)
    assert dup.nickname.startswith("Base-")
    assert dup.id != base.id
    # Spaced nicknames are allowed (plugins like EasyEnv create them); dialog UI
    # still enforces Host-safe tokens separately.
    spaced = service.create(
        {"nickname": "ws / Ubuntu 24.04", "hostname": "h.example", "username": "u"}
    )
    assert spaced.nickname == "ws / Ubuntu 24.04"
    with pytest.raises(CoreError):
        service.create({"nickname": "", "hostname": "h.example", "username": "u"})
    with pytest.raises(CoreError):
        service.create({"nickname": "Base", "hostname": "h.example", "username": "u"})


def test_group_assign_remove_ordering(service):
    g = service.create_group("Prod")
    a = service.create({"nickname": "A", "hostname": "a.test", "username": "u", "group_id": g.id})
    b = service.create({"nickname": "B", "hostname": "b.test", "username": "u"})
    assert a.group_id == g.id
    service.remove_from_group(a.id)
    assert service.get(a.id).group_id is None
    service.reorder([b.id, a.id])
    ordered = service.list_connections()
    assert [c.nickname for c in ordered] == ["B", "A"]
    service.delete_group(g.id)


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "store.json"
    s1 = ConnectionService(store_path=path, autosave=True)
    c = s1.create({"nickname": "Persist", "hostname": "p.test", "username": "u"})
    s1.create_group("G1")
    s2 = ConnectionService(store_path=path, autosave=False)
    s2.load(path)
    assert s2.find_by_nickname("Persist").id == c.id
    assert s2.list_groups()


def test_import_export_conversion(service):
    service.create({"nickname": "X", "hostname": "x.test", "username": "u"})
    payload = service.to_export_dict()
    other = ConnectionService(autosave=False)
    other.load_export_dict(payload, replace=True)
    assert other.find_by_nickname("X") is not None


def test_operation_without_gi():
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    script = (
        "import builtins, sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "real = builtins.__import__\n"
        "builtins.__import__ = lambda name, *a, **k: (_ for _ in ()).throw(ImportError('blocked')) "
        "if name == 'gi' or name.startswith('gi.') else real(name, *a, **k)\n"
        "from sshpilot.core.connections import ConnectionService\n"
        "svc = ConnectionService(autosave=False)\n"
        "svc.create({'nickname': 'NoGI', 'hostname': 'n.test', 'username': 'u'})\n"
        "assert svc.find_by_nickname('NoGI')\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_update_versus_delete_race(tmp_path):
    service = ConnectionService(store_path=tmp_path / "race.json", autosave=False)
    created = service.create({"nickname": "Race", "hostname": "r.test", "username": "u"})
    barrier = threading.Barrier(3)
    errors = []

    def updater():
        barrier.wait()
        try:
            service.update(created.id, {"port": 2222})
        except CoreError:
            pass
        except Exception as exc:
            errors.append(exc)

    def deleter():
        barrier.wait()
        try:
            service.delete(created.id)
        except CoreError:
            pass
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=updater), threading.Thread(target=deleter)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()
    assert not errors
