"""``set_connection_layout``: replace the whole arrangement in one commit.

The sidebar saves a sorted view as the manual order with it (issue #1312) and
undoes that by sending the previous arrangement back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.repository import ConnectionRepository  # noqa: E402
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402
from sshpilot.api.models.connection_store import (  # noqa: E402
    GroupLayout,
    SetConnectionLayoutRequest,
)


def _repo(tmp_path):
    root = tmp_path / "ssh_config"
    state = tmp_path / "connections.json"
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    return repo, root, state


def _reload(tmp_path, root, state):
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )


def _seed(repo, *names):
    for name in names:
        repo.create_connection(
            {"nickname": name, "hostname": f"{name}.test", "protocol": "ssh"}
        )


def _layout(root, groups, generation=None):
    return SetConnectionLayoutRequest(
        root_connection_ids=tuple(root),
        groups=tuple(
            GroupLayout(group_id=gid, parent_id=parent, connection_ids=tuple(members))
            for gid, parent, members in groups
        ),
        expected_generation=generation,
    )


def _arrangement(snapshot):
    groups = sorted(snapshot.groups, key=lambda g: (g.parent_id or "", g.order))
    return (
        snapshot.root_connection_ids,
        [(g.id, g.parent_id, g.connection_ids) for g in groups],
    )


def test_layout_reorders_everything_in_one_change_and_persists(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed(repo, "zulu", "mike", "alpha", "kilo", "echo")
    web = repo.create_group("Web")
    db = repo.create_group("DB")
    sub = repo.create_group("Sub", parent_id=web.id)
    repo.assign_connection_to_group("kilo", web.id)
    repo.assign_connection_to_group("echo", web.id)
    repo.assign_connection_to_group("mike", sub.id)
    events = []
    repo.add_listener(events.append)
    generation = repo.snapshot().generation

    result = repo.set_connection_layout(_layout(
        ["alpha", "zulu"],
        [(db.id, None, []), (web.id, None, ["echo", "kilo"]), (sub.id, web.id, ["mike"])],
        generation,
    ))

    assert len(events) == 1
    snapshot = repo.snapshot()
    assert result == snapshot.generation == generation + 1
    expected = (
        ("alpha", "zulu"),
        [(db.id, None, ()), (web.id, None, ("echo", "kilo")), (sub.id, web.id, ("mike",))],
    )
    assert _arrangement(snapshot) == expected
    assert _arrangement(_reload(tmp_path, root, state).snapshot()) == expected


def test_layout_restores_membership_and_nesting(tmp_path):
    # Undo after a drag that moved a connection between groups and nested a
    # group: the earlier arrangement comes back exactly.
    repo, root, state = _repo(tmp_path)
    _seed(repo, "a", "b")
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.assign_connection_to_group("a", g1.id)
    before = _arrangement(repo.snapshot())

    repo.assign_connection_to_group("a", g2.id)
    repo.place_group(g2.id, g1.id, 0)
    undo = _layout(
        before[0],
        [(gid, parent, members) for gid, parent, members in before[1]],
    )
    repo.set_connection_layout(undo)

    assert _arrangement(repo.snapshot()) == before
    a = next(c for c in repo.snapshot().connections if c.id == "a")
    assert [ref.id for ref in a.groups] == [g1.id]


def test_layout_keeps_a_connection_in_several_groups(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed(repo, "a")
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.copy_connection_to_group("a", g1.id)
    repo.copy_connection_to_group("a", g2.id)

    repo.set_connection_layout(_layout([], [(g2.id, None, ["a"]), (g1.id, None, ["a"])]))

    a = next(c for c in repo.snapshot().connections if c.id == "a")
    assert {ref.id for ref in a.groups} == {g1.id, g2.id}


def test_unchanged_layout_does_not_bump_the_generation(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed(repo, "a", "b")
    generation = repo.snapshot().generation

    assert repo.set_connection_layout(_layout(["a", "b"], [], generation)) == generation


@pytest.mark.parametrize(
    "root, extra_group",
    [
        (["a"], False),  # drops "b"
        (["a", "b", "ghost"], False),  # a connection that does not exist
        (["a", "b"], True),  # a group that does not exist
    ],
)
def test_layout_from_a_different_set_of_items_is_stale(tmp_path, root, extra_group):
    repo, _root, _state = _repo(tmp_path)
    _seed(repo, "a", "b")
    groups = [("nope", None, [])] if extra_group else []
    before = _arrangement(repo.snapshot())

    with pytest.raises(CoreError) as error:
        repo.set_connection_layout(_layout(root, groups))

    assert error.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert _arrangement(repo.snapshot()) == before


def test_layout_with_an_old_generation_is_stale(tmp_path):
    repo, _root, _state = _repo(tmp_path)
    _seed(repo, "a", "b")
    generation = repo.snapshot().generation
    repo.reorder_connection("b", "a", None, "above")

    with pytest.raises(CoreError) as error:
        repo.set_connection_layout(_layout(["a", "b"], [], generation))

    assert error.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert repo.snapshot().root_connection_ids == ("b", "a")


@pytest.mark.parametrize(
    "case",
    ["root_twice", "root_and_group", "group_twice", "cycle", "unknown_parent"],
)
def test_malformed_layout_is_refused_without_changes(tmp_path, case):
    repo, _root, state = _repo(tmp_path)
    _seed(repo, "a", "b")
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    layouts = {
        "root_twice": (["a", "b", "a"], [(g1.id, None, []), (g2.id, None, [])]),
        "root_and_group": (["a", "b"], [(g1.id, None, ["a"]), (g2.id, None, [])]),
        "group_twice": (["a", "b"], [(g1.id, None, []), (g1.id, None, []), (g2.id, None, [])]),
        "cycle": (["a", "b"], [(g1.id, g2.id, []), (g2.id, g1.id, [])]),
        "unknown_parent": (["a", "b"], [(g1.id, "nope", []), (g2.id, None, [])]),
    }
    before = _arrangement(repo.snapshot())
    on_disk = state.read_bytes()

    with pytest.raises(CoreError) as error:
        repo.set_connection_layout(_layout(*layouts[case]))

    assert error.value.code is ErrorCode.VALIDATION_ERROR
    assert _arrangement(repo.snapshot()) == before
    assert state.read_bytes() == on_disk
