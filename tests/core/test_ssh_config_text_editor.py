"""Focused tests for the daemon-backed SSH config text editor.

Covers the required contracts:

* the daemon resolves the correct active SSH config file in normal and
  isolated mode;
* GTK loads and saves exclusively through the daemon client (the GTK adapter
  never reads or writes the SSH config file directly);
* stale saves are rejected (revision checked daemon-side);
* a successful save reloads daemon connection state and fires the normal
  connection update events.
"""

from __future__ import annotations

import inspect
import logging
import os
import stat
from pathlib import Path

import pytest

from sshpilot.api.models.connection_store import (
    SshConfigUnavailableReason,
    thaw_safe_metadata,
)
from sshpilot.core.connections.state_file import (
    ConnectionFileState,
    GroupFileState,
    write_connection_state,
)

from sshpilot.api.models.connections import (
    SaveSshConfigTextRequest,
    SshConfigText,
)
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.errors import CoreError, ErrorCode


def _write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def _make_repository(root: Path, *, isolated: bool = False, state_dir: Path) -> ConnectionRepository:
    state_dir.mkdir(parents=True, exist_ok=True)
    return ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=isolated),
        state_path=state_dir / "connections.json",
        legacy_config_path=state_dir / "config.json",
        isolated=isolated,
    )


# ---------------------------------------------------------------------------
# Daemon-side root resolution (normal vs isolated mode)
# ---------------------------------------------------------------------------


def test_daemon_resolves_normal_config_root(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(home / ".ssh"))

    from sshpilot.daemon.cli import _resolve_ssh_root

    root = _resolve_ssh_root(isolated=False)
    assert root == home / ".ssh" / "config"

    _write_config(root, "Host normal\n    HostName normal.example.com\n")
    store = SshConfigStore(root, isolated=False)
    text = store.get_text()
    assert text.text == "Host normal\n    HostName normal.example.com\n"
    assert text.display_name == "~/.ssh/config"
    assert text.writable is True
    assert text.revision


def test_daemon_resolves_isolated_config_root(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    from sshpilot.daemon.cli import _resolve_ssh_root

    root = _resolve_ssh_root(isolated=True)
    assert root == home / ".config" / "sshpilot" / "ssh_config"

    _write_config(root, "Host isolated\n    HostName isolated.example.net\n")
    store = SshConfigStore(root, isolated=True)
    text = store.get_text()
    assert text.text == "Host isolated\n    HostName isolated.example.net\n"
    assert text.display_name.endswith("ssh_config")
    assert text.writable is True


# ---------------------------------------------------------------------------
# Daemon-side raw text read/write (exact text, revision, backup, safety)
# ---------------------------------------------------------------------------


def test_store_save_writes_exact_text_and_updates_revision(tmp_path):
    root = tmp_path / ".ssh" / "config"
    original = "Host web\n    HostName web.example.com\n"
    _write_config(root, original)

    store = SshConfigStore(root)
    loaded = store.get_text()
    assert loaded.text == original

    edited = "# user edit\nHost web\n    HostName web2.example.com\n"
    saved = store.replace_text(edited, loaded.revision)
    assert saved.text == edited
    assert saved.revision != loaded.revision
    # Exact text as entered — no reformatting.
    assert root.read_text(encoding="utf-8") == edited
    # No .bak copy is left beside the config.
    assert not (tmp_path / ".ssh" / "config.bak").exists()
    # Permissions preserved (0600).
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o600


def test_store_rejects_stale_save(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host a\n")
    store = SshConfigStore(root)
    loaded = store.get_text()

    # External change after the editor loaded the file.
    _write_config(root, "Host b\n")
    with pytest.raises(CoreError) as exc_info:
        store.replace_text("Host c\n", loaded.revision)
    assert exc_info.value.code is ErrorCode.STALE_CONNECTION_STATE
    # Previous file bytes untouched.
    assert root.read_text(encoding="utf-8") == "Host b\n"


def test_store_rejects_save_when_included_file_changed(tmp_path):
    root = tmp_path / ".ssh" / "config"
    included = tmp_path / ".ssh" / "conf.d" / "extra.conf"
    included.parent.mkdir(parents=True)
    included.write_text("Host extra\n", encoding="utf-8")
    _write_config(root, f"Include {included}\nHost a\n")

    store = SshConfigStore(root)
    loaded = store.get_text()

    included.write_text("Host extra2\n", encoding="utf-8")
    with pytest.raises(CoreError) as exc_info:
        store.replace_text("Host c\n", loaded.revision)
    assert exc_info.value.code is ErrorCode.STALE_CONNECTION_STATE


def test_store_saves_linked_root_in_place(tmp_path):
    target = tmp_path / "dotfiles" / "config"
    _write_config(target, "Host a\n")
    link = tmp_path / "config-link"
    link.symlink_to(target)

    store = SshConfigStore(link)
    text = store.get_text()
    assert text.writable is True
    store.replace_text("Host b\n", text.revision)
    assert target.read_text(encoding="utf-8") == "Host b\n"
    assert link.is_symlink()


def test_repository_edits_linked_root_through_daemon_transaction(tmp_path):
    # The full daemon path: capture, write, post-write record. A linked
    # ~/.ssh/config (Home Manager, Stow, chezmoi) must save like a file.
    target = tmp_path / "dotfiles" / "config"
    _write_config(target, "Host one\n    HostName one.example.com\n")
    root = tmp_path / ".ssh" / "config"
    root.parent.mkdir()
    root.symlink_to(target)
    repo = _make_repository(root, state_dir=tmp_path / "state")
    assert [c.id for c in repo.snapshot().connections] == ["one"]

    repo.create_connection(
        {"nickname": "two", "hostname": "two.example.com", "protocol": "ssh"}
    )
    repo.update_connection(
        "one",
        {"nickname": "one", "hostname": "uno.example.com", "protocol": "ssh"},
    )
    loaded = repo.get_ssh_config_text()
    repo.save_ssh_config_text(
        SaveSshConfigTextRequest(
            text=loaded.text + "# raw\n", expected_revision=loaded.revision
        )
    )

    assert root.is_symlink() and os.readlink(root) == str(target)
    body = target.read_text(encoding="utf-8")
    assert "Host two" in body and "uno.example.com" in body and "# raw" in body
    assert {c.id for c in repo.snapshot().connections} == {"one", "two"}


# ---------------------------------------------------------------------------
# Repository: save reloads connection state and publishes normal events
# ---------------------------------------------------------------------------


def test_repository_save_reloads_connection_state_and_fires_events(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host one\n    HostName one.example.com\n")
    repo = _make_repository(root, state_dir=tmp_path / "state")
    assert [c.id for c in repo.snapshot().connections] == ["one"]

    changes = []
    repo.add_listener(changes.append)

    loaded = repo.get_ssh_config_text()
    assert loaded.text == "Host one\n    HostName one.example.com\n"

    edited = (
        "Host one\n    HostName one.example.com\n"
        "\n"
        "Host two\n    HostName two.example.com\n"
    )
    saved = repo.save_ssh_config_text(
        SaveSshConfigTextRequest(text=edited, expected_revision=loaded.revision)
    )
    assert saved.revision != loaded.revision

    # Connection state reloaded immediately by the daemon (no watcher wait).
    connection_ids = {c.id for c in repo.snapshot().connections}
    assert {"one", "two"} <= connection_ids

    # The normal repository change (→ connection.created / store.changed
    # events) was published.
    assert changes, "expected a RepositoryChange after a successful save"
    after_ids = {c.id for c in changes[-1].after.connections}
    assert "two" in after_ids


def test_repository_save_rejects_stale_revision(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host a\n")
    repo = _make_repository(root, state_dir=tmp_path / "state")

    loaded = repo.get_ssh_config_text()
    _write_config(root, "Host b\n")

    with pytest.raises(CoreError) as exc_info:
        repo.save_ssh_config_text(
            SaveSshConfigTextRequest(
                text="Host c\n",
                expected_revision=loaded.revision,
            )
        )
    assert exc_info.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert root.read_text(encoding="utf-8") == "Host b\n"


def _write_sidecar_state(path: Path, *, connection_id: str) -> None:
    write_connection_state(
        path,
        ConnectionFileState(
            groups=(
                GroupFileState(
                    id="group",
                    name="Group",
                    connection_ids=(connection_id,),
                ),
            ),
            metadata={
                connection_id: {
                    "tags": ["ops"],
                    "wol_mac": "00:11:22:33:44:55",
                },
            },
        ),
    )


def test_raw_host_rename_reconciles_group_and_metadata_sidecar(tmp_path):
    root = tmp_path / ".ssh" / "config"
    state_dir = tmp_path / "state"
    _write_config(root, "Host old\n    HostName old.example.com\n")
    _write_sidecar_state(state_dir / "connections.json", connection_id="old")
    repo = _make_repository(root, state_dir=state_dir)
    loaded = repo.get_ssh_config_text()

    repo.save_ssh_config_text(
        SaveSshConfigTextRequest(
            text="Host new\n    HostName old.example.com\n",
            expected_revision=loaded.revision,
        )
    )

    snapshot = repo.snapshot()
    assert snapshot.groups[0].connection_ids == ("new",)
    assert [item.connection_id for item in snapshot.metadata] == ["new"]
    assert thaw_safe_metadata(snapshot.metadata[0].values)["tags"] == ["ops"]


def test_raw_host_delete_removes_group_and_metadata_sidecar(tmp_path):
    root = tmp_path / ".ssh" / "config"
    state_dir = tmp_path / "state"
    _write_config(root, "Host old\n    HostName old.example.com\n")
    _write_sidecar_state(state_dir / "connections.json", connection_id="old")
    repo = _make_repository(root, state_dir=state_dir)
    loaded = repo.get_ssh_config_text()

    repo.save_ssh_config_text(
        SaveSshConfigTextRequest(text="# deleted\n", expected_revision=loaded.revision)
    )

    snapshot = repo.snapshot()
    assert snapshot.groups[0].connection_ids == ()
    assert snapshot.metadata == ()


def test_repository_save_rolls_back_when_written_config_does_not_parse(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host ok\n    HostName ok.example.com\n")
    repo = _make_repository(root, state_dir=tmp_path / "state")

    loaded = repo.get_ssh_config_text()
    # A Host header with a dangling quote makes the app's strict parser fail.
    bad = "Host \"broken\n    HostName broken.example.com\n"
    with pytest.raises(CoreError):
        repo.save_ssh_config_text(
            SaveSshConfigTextRequest(text=bad, expected_revision=loaded.revision)
        )
    # The failed write rolled the file back to its previous bytes.
    assert root.read_text(encoding="utf-8") == "Host ok\n    HostName ok.example.com\n"


def test_application_service_reports_raw_config_validation_failure(tmp_path, caplog):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host ok\n    HostName ok.example.com\n")
    repository = _make_repository(root, state_dir=tmp_path / "state")
    service = ConnectionApplicationService(repository)
    loaded = service.get_ssh_config_text()

    with caplog.at_level(logging.WARNING, logger="sshpilot.core.connection_application_service"):
        with pytest.raises(Exception) as exc_info:
            service.save_ssh_config_text(
                SaveSshConfigTextRequest(
                    text="Host \"broken\n    HostName broken.example.com\n",
                    expected_revision=loaded.revision,
                )
            )

    assert getattr(exc_info.value, "code", None).value == "validation_failed"
    assert "invalid Host header" in str(exc_info.value)
    assert "CONFIG_PARSE_ERROR" in caplog.text
    assert "SSH configuration contains an invalid Host header" in caplog.text
    assert root.read_text(encoding="utf-8") == "Host ok\n    HostName ok.example.com\n"


# ---------------------------------------------------------------------------
# Full typed API round trip over the daemon socket
# ---------------------------------------------------------------------------


def test_daemon_client_round_trip_and_stale_rejection_over_wire(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host wire\n    HostName wire.example.com\n")
    repository = _make_repository(root, state_dir=tmp_path / "state")
    service = ConnectionApplicationService(repository)

    from sshpilot.api import DaemonClient, ErrorCode as ApiErrorCode
    from sshpilot.daemon import DaemonServer

    socket_path = tmp_path / "sshpilotd.sock"
    server = DaemonServer(lambda: service, socket_path=socket_path)
    server.start_in_thread()
    client = DaemonClient(socket_path=socket_path)
    try:
        loaded = client.get_ssh_config_text()
        assert loaded.text == "Host wire\n    HostName wire.example.com\n"
        assert loaded.writable is True

        edited = "Host wire\n    HostName wire2.example.com\n"
        saved = client.save_ssh_config_text(
            SaveSshConfigTextRequest(text=edited, expected_revision=loaded.revision)
        )
        assert saved.revision != loaded.revision
        assert root.read_text(encoding="utf-8") == edited

        with pytest.raises(Exception) as exc_info:
            client.save_ssh_config_text(
                SaveSshConfigTextRequest(
                    text="Host x\n",
                    expected_revision=loaded.revision,
                )
            )
        assert getattr(exc_info.value, "code", None) is ApiErrorCode.STALE_EDITOR
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped()


# ---------------------------------------------------------------------------
# GTK side: loads and saves through the daemon client, never the file
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Fake daemon client: owns the file like the real daemon does."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.load_calls = 0
        self.save_calls = 0
        self.last_text = None
        self.last_expected_revision = None

    def get_ssh_config_text(self) -> SshConfigText:
        self.load_calls += 1
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return SshConfigText(
            text=text,
            revision=f"rev-load-{self.load_calls}",
            display_name="~/.ssh/config",
            writable=True,
        )

    def save_ssh_config_text(self, request: SaveSshConfigTextRequest) -> SshConfigText:
        self.save_calls += 1
        self.last_text = request.text
        self.last_expected_revision = request.expected_revision
        # The daemon writes the file; the GTK service must never do this.
        self.path.write_text(request.text, encoding="utf-8")
        return SshConfigText(
            text=request.text,
            revision=f"rev-save-{self.save_calls}",
            display_name="~/.ssh/config",
            writable=True,
        )


def test_gtk_service_loads_and_saves_through_daemon_client(tmp_path):
    from sshpilot.ssh_config_editor_service import DaemonSshConfigTextService

    config = tmp_path / ".ssh" / "config"
    config.parent.mkdir(parents=True)
    original = "Host gtk\n    HostName gtk.example.com\n"
    config.write_text(original, encoding="utf-8")
    mtime_before = config.stat().st_mtime_ns

    client = _RecordingClient(config)
    service = DaemonSshConfigTextService(client)
    try:
        loaded = service.load().result(timeout=10)
        assert loaded.text == original
        assert client.load_calls == 1
        assert client.save_calls == 0
        # The GTK adapter never touched the file while loading.
        assert config.stat().st_mtime_ns == mtime_before

        edited = "# edited\nHost gtk\n    HostName new.example.com\n"
        saved = service.save_text(edited).result(timeout=10)
        assert client.save_calls == 1
        assert client.last_text == edited
        assert client.last_expected_revision == loaded.revision
        assert saved.text == edited
        assert saved.revision != loaded.revision
        # The file changed only because the (simulated) daemon wrote it.
        assert config.read_text(encoding="utf-8") == edited
    finally:
        service.close()


def test_gtk_editor_service_source_never_touches_the_config_file():
    """The GTK adapter contains no direct file-write primitives at all."""
    import sshpilot.ssh_config_editor_service as module

    source = inspect.getsource(module)
    for banned in ("atomic_write_text", "os.replace", "shutil.", "write_text", "os.open"):
        assert banned not in source, f"GTK SSH config service must not use {banned!r}"


def test_snapshot_names_a_linked_root_that_cannot_be_seen(tmp_path):
    # #1373: inside the Flatpak a Home Manager link into /nix/store dangles;
    # the snapshot names the target so the window can say why it is empty.
    root = tmp_path / ".ssh" / "config"
    root.parent.mkdir()
    root.symlink_to("/nix/store/abc-home-manager-files/.ssh/config")
    snapshot = _make_repository(root, state_dir=tmp_path / "state").snapshot()
    assert snapshot.connections == ()
    assert snapshot.ssh_config_read_only is True
    assert snapshot.ssh_config_unavailable is SshConfigUnavailableReason.DANGLING_LINK
    assert (
        snapshot.ssh_config_unavailable_path
        == "/nix/store/abc-home-manager-files/.ssh/config"
    )


def test_snapshot_names_no_reason_for_a_missing_or_readable_config(tmp_path):
    root = tmp_path / ".ssh" / "config"
    repo = _make_repository(root, state_dir=tmp_path / "state")
    assert repo.snapshot().ssh_config_unavailable is None
    target = tmp_path / "dotfiles" / "config"
    _write_config(target, "Host a\n")
    root.parent.mkdir(exist_ok=True)
    root.symlink_to(target)
    repo = _make_repository(root, state_dir=tmp_path / "state2")
    assert repo.snapshot().ssh_config_unavailable is None
    assert repo.snapshot().ssh_config_unavailable_path == ""


needs_permissions = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores file modes",
)


@needs_permissions
def test_a_locked_ssh_folder_starts_and_names_the_folder(tmp_path):
    # Before: the load raised, the daemon never started, and the window
    # only said its background service timed out.
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host a\n  HostName 192.0.2.1\n")
    os.chmod(root.parent, 0)
    try:
        repo = _make_repository(root, state_dir=tmp_path / "state")
        snapshot = repo.snapshot()
    finally:
        os.chmod(root.parent, 0o700)
    assert snapshot.connections == ()
    assert snapshot.ssh_config_read_only is True
    assert snapshot.ssh_config_unavailable is SshConfigUnavailableReason.PERMISSION_DENIED
    assert snapshot.ssh_config_unavailable_path == str(root.parent)


@needs_permissions
def test_an_unreadable_config_file_starts_and_names_the_file(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host a\n  HostName 192.0.2.1\n")
    os.chmod(root, 0)
    try:
        snapshot = _make_repository(root, state_dir=tmp_path / "state").snapshot()
    finally:
        os.chmod(root, 0o600)
    assert snapshot.ssh_config_unavailable is SshConfigUnavailableReason.PERMISSION_DENIED
    assert snapshot.ssh_config_unavailable_path == str(root)


@needs_permissions
def test_an_unreadable_include_starts_and_names_the_include(tmp_path):
    root = tmp_path / ".ssh" / "config"
    extra = tmp_path / ".ssh" / "extra"
    _write_config(root, f"Include {extra}\nHost a\n  HostName 192.0.2.1\n")
    _write_config(extra, "Host b\n  HostName 192.0.2.2\n")
    os.chmod(extra, 0)
    try:
        snapshot = _make_repository(root, state_dir=tmp_path / "state").snapshot()
    finally:
        os.chmod(extra, 0o600)
    assert snapshot.connections == ()
    assert snapshot.ssh_config_unavailable is SshConfigUnavailableReason.UNREADABLE
    assert snapshot.ssh_config_unavailable_path == str(extra)


def test_an_invalid_config_starts_instead_of_stopping_the_daemon(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host -oProxyCommand=evil\n  HostName 192.0.2.1\n")
    snapshot = _make_repository(root, state_dir=tmp_path / "state").snapshot()
    assert snapshot.ssh_config_unavailable is SshConfigUnavailableReason.INVALID
    assert snapshot.ssh_config_unavailable_path == str(root)


def test_a_flatpak_without_ssh_folder_access_says_so(tmp_path, monkeypatch):
    # Inside the sandbox an unshared ~/.ssh looks like a missing one; the
    # sandbox's own grant list tells the two apart.
    from sshpilot.core.connections import ssh_config_store

    root = tmp_path / ".ssh" / "config"
    monkeypatch.setattr(ssh_config_store, "_flatpak_shared_paths", lambda: [Path("/nix/store")])
    snapshot = _make_repository(root, state_dir=tmp_path / "state").snapshot()
    assert snapshot.ssh_config_unavailable is SshConfigUnavailableReason.SANDBOX_HIDDEN
    assert snapshot.ssh_config_unavailable_path == str(root.parent)

    monkeypatch.setattr(ssh_config_store, "_flatpak_shared_paths", lambda: [root.parent])
    assert _make_repository(root, state_dir=tmp_path / "s2").snapshot().ssh_config_unavailable is None


def test_flatpak_grant_list_is_parsed_like_flatpak_writes_it(tmp_path, monkeypatch):
    from sshpilot.core.connections import ssh_config_store

    info = tmp_path / "flatpak-info"
    info.write_text("[Context]\nfilesystems=~/.ssh:create;/nix/store:ro;\n")
    real_exists = os.path.exists
    monkeypatch.setattr(
        ssh_config_store.os.path, "exists",
        lambda p: True if p == "/.flatpak-info" else real_exists(p),
    )
    import configparser
    real_read = configparser.ConfigParser.read
    monkeypatch.setattr(
        configparser.ConfigParser, "read",
        lambda self, _p, encoding=None: real_read(self, info, encoding=encoding),
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not ssh_config_store.flatpak_hides(tmp_path / ".ssh")
    assert not ssh_config_store.flatpak_hides(Path("/nix/store/x"))
    assert ssh_config_store.flatpak_hides(tmp_path / "dotfiles")
    info.write_text("[Context]\nfilesystems=home:ro;\n")
    assert not ssh_config_store.flatpak_hides(tmp_path / ".ssh")
    info.write_text("[Context]\nfilesystems=host;\n")
    assert not ssh_config_store.flatpak_hides(Path("/etc/ssh"))
    # The isolated-mode config lives in the app's own data, which the grant
    # never lists but the sandbox always has.
    info.write_text("[Context]\nfilesystems=/nix/store:ro;\n")
    app_config = tmp_path / ".var" / "app" / "io.github.mfat.sshpilot" / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(app_config))
    monkeypatch.setenv("FLATPAK_ID", "io.github.mfat.sshpilot")
    assert not ssh_config_store.flatpak_hides(app_config / "sshpilot")
    assert not ssh_config_store.flatpak_hides(app_config.parent / "data")
    assert ssh_config_store.flatpak_hides(tmp_path / ".ssh")


def _seed_group(root, state_dir):
    repo = _make_repository(root, state_dir=state_dir)
    group = repo.create_group("Prod")
    repo.assign_connection_to_group("web", group.id)
    return group.id


def _assert_group_survives(root, state_dir, group_id):
    snapshot = _make_repository(root, state_dir=state_dir).snapshot()
    assert snapshot.ssh_config_unavailable is None
    [group] = [g for g in snapshot.groups if g.id == group_id]
    assert group.connection_ids == ("web",)


def test_an_unseen_linked_config_keeps_groups_and_identities(tmp_path):
    # Before: a dangling link loaded as an empty config, and reconciling the
    # sidecar against it tombstoned every SSH identity and emptied groups.
    target = tmp_path / "store" / "hm-ssh-config"
    _write_config(target, "Host web\n  HostName 192.0.2.1\n")
    root = tmp_path / ".ssh" / "config"
    root.parent.mkdir()
    root.symlink_to(target)
    state_dir = tmp_path / "state"
    group_id = _seed_group(root, state_dir)
    before = (state_dir / "connections.json").read_bytes()

    hidden = tmp_path / "store-hidden"
    target.parent.rename(hidden)
    repo = _make_repository(root, state_dir=state_dir)
    snapshot = repo.snapshot()
    assert snapshot.ssh_config_unavailable is SshConfigUnavailableReason.DANGLING_LINK
    [group] = [g for g in snapshot.groups if g.id == group_id]
    assert group.connection_ids == ()
    with pytest.raises(CoreError) as excinfo:
        repo.create_group("Other")
    assert excinfo.value.code is ErrorCode.SSH_CONFIG_READ_ONLY
    assert (state_dir / "connections.json").read_bytes() == before

    hidden.rename(target.parent)
    _assert_group_survives(root, state_dir, group_id)


@needs_permissions
def test_a_locked_ssh_folder_keeps_groups_and_identities(tmp_path):
    root = tmp_path / ".ssh" / "config"
    _write_config(root, "Host web\n  HostName 192.0.2.1\n")
    state_dir = tmp_path / "state"
    group_id = _seed_group(root, state_dir)
    before = (state_dir / "connections.json").read_bytes()
    os.chmod(root.parent, 0)
    try:
        repo = _make_repository(root, state_dir=state_dir)
        assert repo.snapshot().connections == ()
        repo.reload()
    finally:
        os.chmod(root.parent, 0o700)
    assert (state_dir / "connections.json").read_bytes() == before
    repo.reload()
    assert repo.snapshot().ssh_config_unavailable is None
    assert [c.id for c in repo.snapshot().connections] == ["web"]
    _assert_group_survives(root, state_dir, group_id)
