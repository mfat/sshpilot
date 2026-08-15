"""Tests for the headless atomic SSH config mutation store.

These reproduce the Task 0 golden mutation scenarios against
``SshConfigStore``: create/update/rename/delete/duplicate/split, included
fragments, multi-host blocks, comment/formatting preservation, CRLF,
symlink refusal, revision conflicts, write-failure rollback, backup
behavior, parent ``fsync``, client path mismatch, and path-free public
errors.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".."))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.ssh_config_store import (  # noqa: E402
    SshConfigStore,
)
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402


def _write(root: Path, text: str) -> Path:
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text(text, encoding="utf-8")
    return root


def _store(root: Path) -> SshConfigStore:
    return SshConfigStore(root)


def _ids(config) -> list:
    return [c.id for c in config.connections]


# ---------------------------------------------------------------------------
# create / update / rename / delete / duplicate / split
# ---------------------------------------------------------------------------


def test_create_appends_managed_block(tmp_path):
    root = _write(tmp_path / "config", "")
    store = _store(root)
    result = store.create(
        {
            "nickname": "new",
            "hostname": "example.net",
            "username": "u",
            "port": 22,
            "protocol": "ssh",
        }
    )
    assert result.connection_id == "new"
    assert "Host new" in root.read_text()
    assert "HostName example.net" in root.read_text()
    assert any(c.id == "new" for c in result.config.connections)


def test_create_rejects_duplicate_nickname(tmp_path):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)
    with pytest.raises(CoreError) as exc:
        store.create({"nickname": "web", "hostname": "x"})
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS


@pytest.mark.parametrize("prepared", [False, True])
def test_create_rejects_empty_authored_alias(tmp_path, prepared):
    root = _write(
        tmp_path / "config",
        "Host reserved\n    # authored reservation\n\n",
    )
    store = _store(root)
    before = root.read_bytes()
    operation = store.prepare_create if prepared else store.create
    with pytest.raises(CoreError) as exc:
        operation({"nickname": "reserved", "hostname": "example.com"})
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert root.read_bytes() == before


def test_create_rejects_authored_alias_in_included_file(tmp_path):
    root = _write(tmp_path / "config", "Include fragments/reserved.conf\n")
    reserved = _write(
        tmp_path / "fragments" / "reserved.conf",
        "Host reserved\n    # authored reservation\n",
    )
    store = _store(root)
    before = (root.read_bytes(), reserved.read_bytes())
    with pytest.raises(CoreError) as exc:
        store.prepare_create({"nickname": "reserved", "hostname": "example.com"})
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert (root.read_bytes(), reserved.read_bytes()) == before


@pytest.mark.parametrize("prepared", [False, True])
def test_duplicate_skips_empty_authored_copy_alias(tmp_path, prepared):
    root = _write(
        tmp_path / "config",
        "Host old-Copy\n    # authored reservation\n\n"
        "Host old\n    HostName old.example\n",
    )
    store = _store(root)
    operation = store.prepare_duplicate if prepared else store.duplicate
    result = operation("old")
    assert result.connection_id == "old-Copy-2"
    if prepared:
        assert "Host old-Copy-2\n" in result.target_text
        assert root.read_text().count("Host old-Copy-2\n") == 0
    else:
        assert "Host old-Copy\n    # authored reservation\n" in root.read_text()
        assert root.read_text().count("Host old-Copy-2\n") == 1


def test_duplicate_considers_authored_alias_in_included_file(tmp_path):
    root = _write(tmp_path / "config", "Include fragments/reserved.conf\n")
    _write(
        tmp_path / "fragments" / "reserved.conf",
        "Host old-Copy\n    # authored reservation\n",
    )
    source = _write(
        tmp_path / "fragments" / "source.conf",
        "Host old\n    HostName old.example\n",
    )
    root.write_text(
        "Include fragments/reserved.conf\nInclude fragments/source.conf\n",
        encoding="utf-8",
    )
    result = _store(root).prepare_duplicate("old")
    assert result.connection_id == "old-Copy-2"
    assert "Host old\n" in source.read_text()


def test_pattern_alias_does_not_reserve_create_or_duplicate(tmp_path):
    pattern = "Host reserved *\n    User deploy\n\n"
    root = _write(
        tmp_path / "config",
        pattern + "Host old\n    HostName old.example\n",
    )
    store = _store(root)
    created = store.create({"nickname": "reserved", "hostname": "reserved.example"})
    assert created.connection_id == "reserved"
    assert pattern in root.read_text()

    duplicate = store.duplicate("old")
    assert duplicate.connection_id == "old-Copy"
    assert pattern in root.read_text()


def test_update_rewrites_only_the_target_block(tmp_path):
    root = _write(
        tmp_path / "config",
        "# header\n"
        "Host web\n"
        "    # pinned comment\n"
        "    HostName example.com\n"
        "    User alice\n\n"
        "Host other\n"
        "    HostName o.example.com\n",
    )
    store = _store(root)
    store.load()
    result = store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    assert result.connection_id == "web"
    text = root.read_text()
    assert "# header" in text
    assert "# pinned comment" in text
    assert "HostName example.org" in text
    assert "Host other" in text
    assert "HostName o.example.com" in text


def test_rename_rewrites_the_host_alias(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host web\n    HostName example.com\n    User alice\n",
    )
    store = _store(root)
    result = store.update(
        "web",
        {
            "nickname": "web2",
            "hostname": "example.com",
            "username": "alice",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    assert result.connection_id == "web2"
    text = root.read_text()
    assert "Host web2" in text
    assert "Host web\n" not in text
    assert _ids(result.config) == ["web2"]


@pytest.mark.parametrize("pattern_before", [True, False])
def test_rename_does_not_rewrite_pattern_containing_destination(tmp_path, pattern_before):
    pattern = "Host new *\n    User deploy\n    # preserve this rule\n"
    concrete = "Host old\n    HostName old.example\n"
    text = (pattern + "\n" + concrete) if pattern_before else (concrete + "\n" + pattern)
    root = _write(tmp_path / "config", text)
    store = _store(root)
    result = store.update(
        "old",
        {"nickname": "new", "hostname": "old.example", "protocol": "ssh"},
        expected_generation=0,
    )
    updated = root.read_text()
    assert result.connection_id == "new"
    assert pattern in updated
    assert "Host new\n    HostName old.example\n" in updated
    assert "Host old\n" not in updated
    assert _ids(result.config).count("new") == 1


def test_pattern_containing_old_alias_is_not_repeated_ownership(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host old *\n    ForwardAgent no\n    # preserve this rule\n\n"
        "Host old\n    HostName old.example\n",
    )
    store = _store(root)
    before_rule = "Host old *\n    ForwardAgent no\n    # preserve this rule\n"
    store.update(
        "old",
        {"nickname": "renamed", "hostname": "old.example", "protocol": "ssh"},
        expected_generation=0,
    )
    updated = root.read_text()
    assert before_rule in updated
    assert "Host renamed\n    HostName old.example\n" in updated


@pytest.mark.parametrize("destination_before", [True, False])
def test_empty_concrete_destination_alias_rejects_rename(tmp_path, destination_before):
    destination = "Host new\n    # reserved\n"
    source = "Host old\n    HostName old.example\n"
    root = _write(
        tmp_path / "config",
        (destination + "\n" + source)
        if destination_before
        else (source + "\n" + destination),
    )
    store = _store(root)
    before = root.read_bytes()
    with pytest.raises(CoreError) as exc:
        store.update(
            "old",
            {"nickname": "new", "hostname": "old.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert root.read_bytes() == before


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_repeated_host_declaration_rejects_managed_mutation(tmp_path, operation):
    root = _write(
        tmp_path / "config",
        "Host foo\n    HostName first.example\n    # first comment\n\n"
        "Host foo\n    User deploy\n    # second comment\n",
    )
    store = _store(root)
    before = root.read_bytes()
    with pytest.raises(CoreError) as exc:
        if operation == "update":
            store.update(
                "foo",
                {"nickname": "foo", "hostname": "changed.example"},
                expected_generation=0,
            )
        else:
            store.delete("foo")
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert root.read_bytes() == before


def test_repeated_host_declaration_rejects_prepared_update(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host foo\n    HostName first.example\n\n"
        "Host foo bar\n    User deploy\n",
    )
    store = _store(root)
    before = root.read_bytes()
    with pytest.raises(CoreError) as exc:
        store.prepare_update(
            "foo",
            {"nickname": "foo", "hostname": "changed.example"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert root.read_bytes() == before
    assert "Host foo bar" in root.read_text()


def test_repeated_host_declaration_rejects_split(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host foo\n    HostName first.example\n\n"
        "Host foo\n    User deploy\n",
    )
    store = _store(root)
    before = root.read_bytes()
    with pytest.raises(CoreError) as exc:
        store.split(
            "foo",
            "foo",
            {"nickname": "foo-copy", "hostname": "copy.example"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert root.read_bytes() == before


def test_repeated_hosts_in_includes_reject_managed_mutation(tmp_path):
    root = _write(tmp_path / "config", "Include fragments/*.conf\n")
    fragments = root.parent / "fragments"
    fragments.mkdir()
    first = _write(fragments / "one.conf", "Host foo\n    HostName one.example\n")
    second = _write(fragments / "two.conf", "Host foo\n    HostName two.example\n")
    store = _store(root)
    before = (root.read_bytes(), first.read_bytes(), second.read_bytes())
    with pytest.raises(CoreError) as exc:
        store.update(
            "foo", {"nickname": "foo", "hostname": "new.example"}, expected_generation=0
        )
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert (root.read_bytes(), first.read_bytes(), second.read_bytes()) == before


def test_duplicate_does_not_rewrite_repeated_host_declarations(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host foo\n    HostName first.example\n\n"
        "Host foo\n    User deploy\n",
    )
    store = _store(root)
    result = store.duplicate("foo")
    assert result.connection_id == "foo-Copy"
    text = root.read_text()
    assert text.count("Host foo\n") == 2
    assert "Host foo-Copy" in text


def test_duplicate_creates_a_new_block(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host web\n    HostName example.com\n    User alice\n",
    )
    store = _store(root)
    result = store.duplicate("web")
    assert result.connection_id == "web-Copy"
    assert result.connection_id != "web"
    text = root.read_text()
    assert "HostName example.com" in text
    assert _ids(result.config) == ["web", "web-Copy"]


def test_delete_removes_the_host_block(tmp_path):
    root = _write(
        tmp_path / "config",
        "# header\nHost web\n    HostName example.com\n    User alice\n\n"
        "Host other\n    HostName o.example.com\n",
    )
    store = _store(root)
    result = store.delete("web")
    assert result.connection_id == "web"
    text = root.read_text()
    assert "Host web" not in text
    assert "Host other" in text
    assert _ids(result.config) == ["other"]


def test_delete_of_one_alias_keeps_the_block_for_others(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host db jump\n    HostName=db.internal\n    User dbuser\n",
    )
    store = _store(root)
    result = store.delete("jump")
    assert result.connection_id == "jump"
    text = root.read_text()
    assert "Host db" in text
    assert "jump" not in text.splitlines()[0]
    assert _ids(result.config) == ["db"]


def test_delete_concrete_alias_preserves_matching_pattern_rule(tmp_path):
    rule = "Host old *\n    ForwardAgent no\n    # preserve\n"
    root = _write(
        tmp_path / "config",
        rule + "\nHost old\n    HostName old.example\n",
    )
    store = _store(root)
    store.delete("old")
    assert root.read_text() == rule + "\n"


def test_split_concrete_alias_preserves_matching_pattern_rule(tmp_path):
    rule = "Host old *\n    ForwardAgent no\n    # preserve\n"
    root = _write(
        tmp_path / "config",
        rule + "\nHost old\n    HostName old.example\n",
    )
    store = _store(root)
    store.split(
        "old",
        "old",
        {"nickname": "new", "hostname": "new.example", "protocol": "ssh"},
        expected_generation=0,
    )
    updated = root.read_text()
    assert rule in updated
    assert "Host new\n" in updated


@pytest.mark.parametrize("prepared", [False, True])
def test_split_rejects_empty_authored_destination(tmp_path, prepared):
    root = _write(
        tmp_path / "config",
        "Host reserved\n    # authored reservation\n\n"
        "Host old\n    HostName old.example\n",
    )
    store = _store(root)
    before = root.read_bytes()
    operation = store.prepare_split if prepared else store.split
    with pytest.raises(CoreError) as exc:
        operation(
            "old",
            "old",
            {"nickname": "reserved", "hostname": "reserved.example"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert root.read_bytes() == before


def test_split_rejects_destination_remaining_in_shared_source_block(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host old other\n    HostName shared.example\n",
    )
    store = _store(root)
    before = root.read_bytes()
    with pytest.raises(CoreError) as exc:
        store.prepare_split(
            "other",
            "other",
            {"nickname": "old", "hostname": "old.example"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert root.read_bytes() == before


@pytest.mark.parametrize("prepared", [False, True])
def test_split_allows_recreating_removed_source_token(tmp_path, prepared):
    root = _write(
        tmp_path / "config",
        "Host old\n    HostName old.example\n",
    )
    store = _store(root)
    operation = store.prepare_split if prepared else store.split
    result = operation(
        "old",
        "old",
        {"nickname": "old", "hostname": "new.example"},
        expected_generation=0,
    )
    if prepared:
        assert "Host old\n" in result.target_text
        assert "HostName new.example\n" in result.target_text
        assert root.read_text() == "Host old\n    HostName old.example\n"
    else:
        assert result.connection_id == "old"
        assert "Host old\n    HostName new.example\n" in root.read_text()


def test_same_name_split_advances_direct_generation_and_rejects_stale_update(tmp_path):
    root = _write(tmp_path / "config", "Host old\n    HostName old.example\n")
    store = _store(root)
    result = store.split(
        "old",
        "old",
        {"nickname": "old", "hostname": "new.example"},
        expected_generation=0,
    )
    assert result.connection_id == "old"
    assert result.config.connections[0].generation == 1
    assert store._generations["old"] == 1
    assert store.load().connections[0].generation == 1

    before = root.read_bytes()
    with pytest.raises(CoreError) as exc:
        store.update(
            "old",
            {"nickname": "old", "hostname": "stale.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert root.read_bytes() == before

    updated = store.update(
        "old",
        {"nickname": "old", "hostname": "current.example", "protocol": "ssh"},
        expected_generation=1,
    )
    assert updated.config.connections[0].generation == 2
    assert store._generations["old"] == 2
    assert store.load().connections[0].generation == 2


def test_same_name_prepared_split_matches_direct_generation(tmp_path):
    root = _write(tmp_path / "config", "Host old\n    HostName old.example\n")
    store = _store(root)
    prepared = store.prepare_split(
        "old",
        "old",
        {"nickname": "old", "hostname": "new.example"},
        expected_generation=0,
    )
    result = store.commit_prepared(prepared)
    assert result.connection_id == "old"
    assert result.config.connections[0].generation == 1
    assert store._generations["old"] == 1
    assert store.load().connections[0].generation == 1


def test_split_surviving_shared_alias_keeps_generation_separate_from_new_alias(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host original other\n    HostName shared.example\n",
    )
    store = _store(root)
    result = store.split(
        "original",
        "other",
        {"nickname": "new", "hostname": "new.example"},
        expected_generation=0,
    )
    assert result.connection_id == "new"
    assert store._generations["original"] == 0
    assert store._generations["new"] == 1
    loaded = store.load()
    assert {item.id: item.generation for item in loaded.connections} == {
        "original": 0,
        "new": 1,
    }

    with pytest.raises(CoreError) as exc:
        store.update(
            "new",
            {"nickname": "new", "hostname": "stale.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    original = store.update(
        "original",
        {"nickname": "original", "hostname": "original.example", "protocol": "ssh"},
        expected_generation=0,
    )
    assert original.config.connections[0].generation == 1
    new = store.update(
        "new",
        {"nickname": "new", "hostname": "newer.example", "protocol": "ssh"},
        expected_generation=1,
    )
    assert new.config.connections[-1].generation == 2


def test_split_pattern_destination_is_preserved_and_does_not_collide(tmp_path):
    pattern = "Host reserved *\n    User deploy\n\n"
    root = _write(
        tmp_path / "config",
        pattern + "Host old\n    HostName old.example\n",
    )
    store = _store(root)
    result = store.split(
        "old",
        "old",
        {"nickname": "reserved", "hostname": "reserved.example"},
        expected_generation=0,
    )
    assert result.connection_id == "reserved"
    assert pattern in root.read_text()
    assert "Host reserved\n    HostName reserved.example\n" in root.read_text()


def test_delete_unknown_connection_raises_not_found(tmp_path):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)
    with pytest.raises(CoreError) as exc:
        store.delete("nope")
    assert exc.value.code is ErrorCode.CONNECTION_NOT_FOUND


def test_split_one_alias_from_multi_host_block(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host db jump\n    HostName=db.internal\n    User dbuser\n",
    )
    store = _store(root)
    result = store.split(
        "jump",
        "jump",
        {
            "nickname": "jump2",
            "hostname": "jump.example",
            "username": "j",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    assert result.connection_id == "jump2"
    text = root.read_text()
    assert "Host db" in text
    assert "Host jump2" in text
    assert "HostName jump.example" in text
    assert _ids(result.config) == ["db", "jump2"]


def test_edit_of_included_fragment_writes_back_to_the_fragment(tmp_path):
    root = tmp_path / "golden_root.conf"
    frag_dir = tmp_path / "fragments"
    frag_dir.mkdir()
    alpha = frag_dir / "alpha.conf"
    alpha.write_text("Host alpha\n    HostName alpha.example.com\n    User root\n")
    root.write_text(
        "Host local\n    HostName localhost\n\n"
        f"Include {frag_dir.as_posix()}/alpha.conf\n"
    )
    store = _store(root)
    store.load()
    result = store.update(
        "alpha",
        {
            "nickname": "alpha",
            "hostname": "alpha2.example.com",
            "username": "root",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    assert result.connection_id == "alpha"
    assert "HostName alpha2.example.com" in alpha.read_text()
    # The root config itself was not touched.
    assert "alpha2.example.com" not in root.read_text()


def test_concrete_destination_collision_in_included_files_rejects_rename(tmp_path):
    root = _write(tmp_path / "config", "Include fragments/one.conf\nInclude fragments/two.conf\n")
    fragments = root.parent / "fragments"
    fragments.mkdir()
    source = _write(fragments / "one.conf", "Host old\n    HostName old.example\n")
    destination = _write(fragments / "two.conf", "Host new\n    # reserved\n")
    store = _store(root)
    before = (root.read_bytes(), source.read_bytes(), destination.read_bytes())
    with pytest.raises(CoreError) as exc:
        store.update(
            "old",
            {"nickname": "new", "hostname": "old.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert (root.read_bytes(), source.read_bytes(), destination.read_bytes()) == before


def test_prepared_preview_supports_absolute_include_outside_root_parent(tmp_path):
    root = _write(tmp_path / "ssh" / "config", "Include "
                  + (tmp_path / "outside" / "hosts.conf").as_posix() + "\n")
    outside = tmp_path / "outside" / "hosts.conf"
    outside.parent.mkdir()
    outside.write_text("Host external\n    HostName old.example\n")
    store = _store(root)
    prepared = store.prepare_update(
        "external",
        {"nickname": "external", "hostname": "new.example", "protocol": "ssh"},
        expected_generation=0,
    )
    assert [item.id for item in store.preview_prepared(prepared).connections] == [
        "external"
    ]
    result = store.commit_prepared(prepared)
    assert prepared.target_revision == result.config.root_revision
    assert "HostName new.example" in outside.read_text()


def test_raw_prepared_revision_uses_target_include_graph(tmp_path):
    root = _write(tmp_path / "config", "Host local\n    HostName local.example\n")
    extra = tmp_path / "extra.conf"
    extra.write_text("Host extra\n    HostName extra.example\n")
    store = _store(root)
    prepared = store.prepare_replace_text(
        f"Include {extra}\n\n" + root.read_text(),
        store.load().root_revision,
    )
    preview = store.preview_prepared(prepared)
    assert {item.id for item in preview.connections} == {"local", "extra"}
    result = store.commit_prepared(prepared)
    assert prepared.target_revision == result.config.root_revision


def test_new_include_dependency_race_rejects_prepared_commit(tmp_path):
    root = _write(tmp_path / "config", "Host local\n    HostName local.example\n")
    extra = tmp_path / "extra.conf"
    extra.write_text("Host extra\n    HostName extra.example\n")
    store = _store(root)
    prepared = store.prepare_replace_text(
        f"Include {extra}\n\n" + root.read_text(),
        store.load().root_revision,
    )
    extra.write_text("Host extra\n    HostName changed.example\n")
    with pytest.raises(CoreError) as exc:
        store.commit_prepared(prepared)
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert "Include" not in root.read_text()


def test_edit_preserves_match_and_wildcard_blocks(tmp_path):
    root = _write(
        tmp_path / "config",
        "Match host *.internal\n    User matchuser\n\n"
        "Host *.wild\n    User wilduser\n\n"
        "Host web\n    HostName example.com\n    User alice\n",
    )
    store = _store(root)
    result = store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    assert result.connection_id == "web"
    text = root.read_text()
    assert "Match host *.internal" in text
    assert "User matchuser" in text
    assert "Host *.wild" in text
    assert "User wilduser" in text


def test_edit_preserves_multi_value_directives(tmp_path):
    root = _write(
        tmp_path / "config",
        "Host web\n"
        "    HostName example.com\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    IdentityFile ~/.ssh/id_rsa\n",
    )
    store = _store(root)
    result = store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.com",
            "username": "alice",
            "protocol": "ssh",
            "keyfile": os.path.expanduser("~/.ssh/id_ed25519"),
            "key_select_mode": 1,
        },
        expected_generation=0,
    )
    assert result.connection_id == "web"
    text = root.read_text()
    assert text.count("IdentityFile") == 2
    assert ".ssh/id_rsa" in text or "~/.ssh/id_rsa" in text


# ---------------------------------------------------------------------------
# CRLF and byte preservation
# ---------------------------------------------------------------------------


def test_crlf_file_stays_crlf_outside_edited_block(tmp_path):
    text = (
        "# header\r\n"
        "Host web\r\n"
        "    HostName example.com\r\n"
        "    User alice\r\n\r\n"
        "Host other\r\n"
        "    HostName o.example.com\r\n"
    )
    root = _write(tmp_path / "config", text)
    store = _store(root)
    store.load()
    store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    raw = root.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\r\n") >= 5
    # No bare LF-only lines are introduced.
    for line in raw.split(b"\r\n"):
        assert not line.endswith(b"\n")


def test_comments_and_blank_lines_are_preserved_by_edits(tmp_path):
    root = _write(
        tmp_path / "config",
        "# top comment\n\n"
        "Host web\n"
        "    # inside comment\n"
        "    HostName example.com\n"
        "    User alice\n"
        "\n"
        "# trailing\n",
    )
    store = _store(root)
    store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    text = root.read_text()
    assert "# top comment" in text
    assert "# inside comment" in text
    assert "# trailing" in text


# ---------------------------------------------------------------------------
# Atomicity, symlinks, backups, modes
# ---------------------------------------------------------------------------


def test_write_failure_rollback_leaves_file_unchanged(tmp_path, monkeypatch):
    root = _write(
        tmp_path / "config",
        "Host web\n    HostName example.com\n    User alice\n",
    )
    store = _store(root)
    before = root.read_text()

    import sshpilot.core.connections.ssh_config_store as store_mod

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", _boom)
    with pytest.raises(CoreError):
        store.update(
            "web",
            {
                "nickname": "web2",
                "hostname": "example.org",
                "username": "bob",
                "protocol": "ssh",
            },
            expected_generation=0,
        )
    assert root.read_text() == before
    assert "web2" not in root.read_text()


def test_revision_conflict_raises_stale_error(tmp_path, monkeypatch):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)

    import sshpilot.core.connections.ssh_config_store as store_mod

    real_check = store_mod.SshConfigStore._revision_matches

    def _concurrent_modify(self, expected):
        # A concurrent writer lands between the store's load and the commit,
        # so the pre-commit revision check must fail.
        with open(root, "a", encoding="utf-8") as handle:
            handle.write("Host racer\n    HostName r.example.com\n")
        return real_check(self, expected)

    monkeypatch.setattr(
        store_mod.SshConfigStore, "_revision_matches", _concurrent_modify
    )
    with pytest.raises(CoreError) as exc:
        store.update(
            "web",
            {"nickname": "web", "hostname": "example.org", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE


def test_stale_editor_generation_rejected(tmp_path):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)
    store.load()
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.org", "protocol": "ssh"},
        expected_generation=0,
    )
    # A stale editor passes generation 0 but the current generation is 1.
    with pytest.raises(CoreError) as exc:
        store.update(
            "web",
            {"nickname": "web", "hostname": "example.net", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE


def test_symlink_target_refused(tmp_path):
    real = _write(tmp_path / "real", "Host web\n    HostName example.com\n")
    link = tmp_path / "config"
    link.symlink_to(real)
    store = _store(link)
    store.load()  # reads through the symlink fine
    with pytest.raises(CoreError) as exc:
        store.update(
            "web",
            {"nickname": "web", "hostname": "x", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR
    assert str(tmp_path) not in str(exc.value)


def test_symlink_swap_detected_before_replacement(tmp_path, monkeypatch):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)
    victim = tmp_path / "victim"
    victim.write_text("private\n")

    import sshpilot.core.connections.ssh_config_store as store_mod

    real_write = store_mod._atomic_write_text

    def _swap_then_write(path, text, **kwargs):
        # Simulate an attacker swapping the target into a symlink right
        # before the writer's own pre-replace lstat check.
        Path(path).unlink()
        Path(path).symlink_to(victim)
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(store_mod, "_atomic_write_text", _swap_then_write)
    with pytest.raises(CoreError):
        store.update(
            "web",
            {"nickname": "web", "hostname": "x", "protocol": "ssh"},
            expected_generation=0,
        )
    # The symlink target was not overwritten.
    assert victim.read_text() == "private\n"


def test_backup_created_once(tmp_path):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)
    store.update(
        "web",
        {"nickname": "web", "hostname": "a", "protocol": "ssh"},
        expected_generation=0,
    )
    backup = tmp_path / "config.bak"
    assert backup.exists()
    assert "HostName example.com" in backup.read_text()
    backup_before = backup.read_bytes()
    store.update(
        "web",
        {"nickname": "web", "hostname": "b", "protocol": "ssh"},
        expected_generation=1,
    )
    # One-shot: the backup is never overwritten.
    assert backup.read_bytes() == backup_before


def test_new_file_mode_0600_and_existing_mode_preserved(tmp_path):
    root = _write(tmp_path / "config", "")
    os.chmod(root, 0o600)
    store = _store(root)
    store.create({"nickname": "new", "hostname": "example.net", "protocol": "ssh"})
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o600

    other = _write(tmp_path / "custom", "Host a\n    HostName a.example.com\n")
    os.chmod(other, 0o640)
    other_store = _store(other)
    other_store.update(
        "a",
        {"nickname": "a", "hostname": "b", "protocol": "ssh"},
        expected_generation=0,
    )
    assert stat.S_IMODE(os.stat(other).st_mode) == 0o640


def test_parent_directory_fsync(tmp_path, monkeypatch):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)
    fsync_calls = []

    import sshpilot.core.connections.ssh_config_store as store_mod

    real_fsync = os.fsync

    def _spy_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(store_mod.os, "fsync", _spy_fsync)
    store.update(
        "web",
        {"nickname": "web", "hostname": "a", "protocol": "ssh"},
        expected_generation=0,
    )
    # At least one fsync for the temp file and one for the parent directory.
    assert len(fsync_calls) >= 2


def test_parent_fsync_failure_is_reported_after_replacement(tmp_path, monkeypatch):
    # Parent-directory fsync failure must surface as a CoreError even though
    # os.replace has already succeeded — callers cannot assume the write
    # failed, and must not retry blindly.
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)

    import sshpilot.core.connections.ssh_config_store as store_mod

    real_fsync = os.fsync
    calls = []

    def _flaky_fsync(fd):
        calls.append(fd)
        if len(calls) >= 2:  # the parent-directory fsync fails
            raise OSError("fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(store_mod.os, "fsync", _flaky_fsync)
    with pytest.raises(CoreError) as exc:
        store.update(
            "web",
            {"nickname": "web", "hostname": "a", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR
    # The replacement had already succeeded before the fsync failure.
    assert "HostName a" in root.read_text()


# ---------------------------------------------------------------------------
# Client path authority
# ---------------------------------------------------------------------------


def test_client_source_path_is_not_authority_for_update(tmp_path):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    rogue = _write(tmp_path / "rogue", "Host evil\n    HostName evil.example.com\n")
    store = _store(root)
    result = store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "protocol": "ssh",
            "source": str(rogue),  # must be ignored
        },
        expected_generation=0,
    )
    assert result.connection_id == "web"
    assert "HostName example.org" in root.read_text()
    assert "HostName example.org" not in rogue.read_text()


def test_split_client_path_mismatch_rejected(tmp_path):
    root = _write(tmp_path / "config", "Host db jump\n    HostName=db.internal\n")
    store = _store(root)
    with pytest.raises(CoreError) as exc:
        store.split(
            "jump",
            "jump",
            {
                "nickname": "jump2",
                "protocol": "ssh",
                "source_config_path": str(tmp_path / "nonexistent"),
            },
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR


def test_split_client_path_matching_accepted(tmp_path):
    root = _write(tmp_path / "config", "Host db jump\n    HostName=db.internal\n")
    store = _store(root)
    result = store.split(
        "jump",
        "jump",
        {
            "nickname": "jump2",
            "hostname": "jump.example",
            "protocol": "ssh",
            "source_config_path": str(root),  # matches the recorded source
        },
        expected_generation=0,
    )
    assert result.connection_id == "jump2"
    assert "Host jump2" in root.read_text()


def test_no_path_in_public_errors(tmp_path):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    link = tmp_path / "evil-link"
    link.symlink_to(root)
    linked_store = _store(link)
    try:
        linked_store.update(
            "web",
            {"nickname": "web", "hostname": "x", "protocol": "ssh"},
            expected_generation=0,
        )
    except CoreError as exc:
        message = str(exc)
        assert str(tmp_path) not in message
        assert str(root) not in message
        assert "config" not in message.split(":")[1][:60]
    else:
        pytest.fail("expected a CoreError")


def test_generation_increments_only_after_successful_writes(tmp_path, monkeypatch):
    root = _write(tmp_path / "config", "Host web\n    HostName example.com\n")
    store = _store(root)
    config = store.load()
    web = next(c for c in config.connections if c.id == "web")
    assert web.generation == 0

    import sshpilot.core.connections.ssh_config_store as store_mod

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", _boom)
    with pytest.raises(CoreError):
        store.update(
            "web",
            {"nickname": "web", "hostname": "x", "protocol": "ssh"},
            expected_generation=0,
        )
    config = store.load()
    assert next(c for c in config.connections if c.id == "web").generation == 0

    monkeypatch.undo()
    store.update(
        "web",
        {"nickname": "web", "hostname": "y", "protocol": "ssh"},
        expected_generation=0,
    )
    config = store.load()
    assert next(c for c in config.connections if c.id == "web").generation == 1
