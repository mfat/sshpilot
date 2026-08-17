"""Preferences ↔ daemon SSH overrides ownership regression tests.

Proves the legacy ``Config`` cache can no longer clobber daemon-owned SSH
override state, that the local mutation fallback is gone, and that daemon
mutation failures are not swallowed.
"""

import json

import pytest

import sshpilot.config as config_mod
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.settings import UpdateGlobalSshOverridesRequest
from sshpilot.core.settings import CONFIG_VERSION, load_settings_strict
from sshpilot.core.ssh_overrides_service import SshOverridesService
from sshpilot.gtk.ssh_overrides_controller import SshOverridesController
from sshpilot.preferences import PreferencesWindow


# ---------------------------------------------------------------------------
# Fake row widgets (headless) + a fake client backed by the real service
# ---------------------------------------------------------------------------


class _Spin:
    def __init__(self, value=0):
        self._value = value
        self._sensitive = True

    def get_value(self):
        return self._value

    def set_value(self, value):
        self._value = value

    def set_sensitive(self, sensitive):
        self._sensitive = sensitive


class _Switch:
    def __init__(self, active=False):
        self._active = active
        self._sensitive = True

    def get_active(self):
        return self._active

    def set_active(self, active):
        self._active = active

    def set_sensitive(self, sensitive):
        self._sensitive = sensitive


class _Combo:
    def __init__(self, selected=0):
        self._selected = selected
        self._sensitive = True

    def get_selected(self):
        return self._selected

    def set_selected(self, index):
        self._selected = index

    def set_sensitive(self, sensitive):
        self._sensitive = sensitive


class _ServiceClient:
    """Minimal ``SshPilotClient`` standing in for the daemon transport."""

    def __init__(self, service):
        self._service = service

    def get_global_ssh_overrides(self):
        return self._service.get()

    def update_global_ssh_overrides(self, request):
        result = self._service.update(request)
        # Mirror the daemon's injected post-persistence runtime hook.  The
        # GTK controller must not call this helper itself.
        from sshpilot.ssh_multiplex import expire_all_masters
        expire_all_masters()
        return result

    def reset_global_ssh_overrides(self, expected_revision=None):
        result = self._service.reset(expected_revision=expected_revision)
        from sshpilot.ssh_multiplex import expire_all_masters
        expire_all_masters()
        return result


class _FailingClient(_ServiceClient):
    """A client whose mutation always fails with an unsupported-capability."""

    def update_global_ssh_overrides(self, request):
        raise SshPilotError(
            ErrorCode.UNSUPPORTED_CAPABILITY, "SSH overrides are unavailable"
        )


class _ClosedClient:
    """A daemon client whose transport has already been closed."""

    def get_global_ssh_overrides(self):
        raise SshPilotError(ErrorCode.TRANSPORT_CLOSED, "The client is closed")


def _make_config(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(config_mod, "get_config_dir", lambda: str(tmp_path))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(dict(payload, config_version=CONFIG_VERSION)),
        encoding="utf-8",
    )
    return config_mod.Config()


def _make_prefs(config, controller):
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.config = config
    prefs.ssh_overrides_controller = controller
    prefs.parent_window = None
    prefs._ssh_overrides_unavailable = False
    prefs._suppress_advanced_ssh_autosave = False
    prefs._advanced_ssh_last_good_snapshot = None

    prefs.apply_default_keepalive_row = _Switch(active=True)
    prefs.controlmaster_row = _Switch(active=False)
    prefs.connect_timeout_row = _Spin(value=0)
    prefs.connection_attempts_row = _Spin(value=0)
    prefs.keepalive_interval_row = _Spin(value=0)
    prefs.keepalive_count_row = _Spin(value=0)
    prefs.strict_host_row = _Combo(selected=0)
    prefs.batch_mode_row = _Switch(active=False)
    prefs.compression_row = _Switch(active=False)
    prefs.verbosity_row = _Spin(value=0)
    prefs.debug_enabled_row = _Switch(active=False)
    prefs.open_file_manager_externally_row = _Switch(active=False)
    prefs.sftp_keepalive_interval_row = _Spin(value=0)
    prefs.sftp_keepalive_count_row = _Spin(value=0)
    prefs.sftp_connect_timeout_row = _Spin(value=0)
    return prefs


def _expiry_record(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "sshpilot.ssh_multiplex.expire_all_masters",
        lambda **kwargs: calls.append(True),
    )
    return calls


# ---------------------------------------------------------------------------
# Task 1: legacy Config must not clobber daemon state
# ---------------------------------------------------------------------------


def test_config_owned_writes_cannot_clobber_daemon_overrides(
    tmp_path, monkeypatch
):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5, "batch_mode": False}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    prefs = _make_prefs(config, SshOverridesController(_ServiceClient(service)))
    prefs.connect_timeout_row = _Spin(value=30)
    prefs.batch_mode_row = _Switch(active=True)
    prefs.open_file_manager_externally_row = _Switch(active=True)
    expires = _expiry_record(monkeypatch)

    assert prefs.save_advanced_ssh_settings() is True

    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    # Daemon-owned values persisted through the controller survive.
    assert on_disk["ssh"]["connection_timeout"] == 30
    assert on_disk["ssh"]["batch_mode"] is True
    # The derived list reflects the daemon-composed state.
    assert "ConnectTimeout=30" in on_disk["ssh"]["ssh_overrides"]
    assert "BatchMode=yes" in on_disk["ssh"]["ssh_overrides"]
    # Config-owned rows were persisted too.
    assert on_disk["file_manager"]["open_externally"] is True
    assert expires == [True]


def test_legacy_cache_refreshed_after_daemon_save(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    prefs = _make_prefs(config, SshOverridesController(_ServiceClient(service)))
    prefs.connect_timeout_row = _Spin(value=30)

    assert prefs.save_advanced_ssh_settings() is True

    # The in-memory legacy cache is refreshed from the authoritative file.
    assert config.get_setting("ssh.connection_timeout", None) == 30

    # A later Config-owned write (whole-tree save) keeps the daemon value.
    config.set_setting("file_manager.open_externally", True)
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["ssh"]["connection_timeout"] == 30
    assert on_disk["file_manager"]["open_externally"] is True


def test_reload_json_cache_strict_reads_without_writing(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    externally_written = (
        json.dumps(
            {
                "config_version": CONFIG_VERSION,
                "ssh": {"connection_timeout": 42},
            }
        )
        + "\n"
    )
    (tmp_path / "config.json").write_text(externally_written, encoding="utf-8")
    after_write = (tmp_path / "config.json").read_bytes()

    result = config.reload_json_cache_strict()

    assert config.get_setting("ssh.connection_timeout", None) == 42
    assert result["ssh"]["connection_timeout"] == 42
    # No save happened: the file still holds the externally written bytes.
    assert (tmp_path / "config.json").read_bytes() == after_write


def test_reload_json_cache_strict_raises_on_malformed(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(Exception):
        config.reload_json_cache_strict()
    # Malformed input is untouched.
    assert path.read_text(encoding="utf-8") == "{ not json"


# ---------------------------------------------------------------------------
# Task 2: no local mutation fallback
# ---------------------------------------------------------------------------


def test_save_without_controller_does_not_persist_daemon_fields(
    tmp_path, monkeypatch
):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    prefs = _make_prefs(config, None)
    prefs.connect_timeout_row = _Spin(value=99)
    prefs.batch_mode_row = _Switch(active=True)
    expires = _expiry_record(monkeypatch)

    assert prefs.save_advanced_ssh_settings() is True

    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    # The nine daemon-owned fields were NOT mutated through the local config.
    assert on_disk["ssh"].get("connection_timeout") == 5
    assert on_disk["ssh"].get("batch_mode") is not True
    assert "ssh_overrides" not in on_disk["ssh"]
    # Config-owned rows still persist through Config.
    assert on_disk["ssh"]["apply_default_keepalive"] is True
    # No daemon mutation occurred, so there is no daemon runtime hook to run.
    assert expires == []


def test_late_controller_attachment_enables_existing_ssh_override_rows(
    tmp_path, monkeypatch
):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 42}},
    )
    prefs = _make_prefs(config, None)
    rows = (
        prefs.connect_timeout_row,
        prefs.connection_attempts_row,
        prefs.keepalive_interval_row,
        prefs.keepalive_count_row,
        prefs.strict_host_row,
        prefs.batch_mode_row,
        prefs.compression_row,
        prefs.verbosity_row,
        prefs.debug_enabled_row,
    )
    for row in rows:
        row.set_sensitive(False)

    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))

    prefs.set_ssh_overrides_controller(controller)

    assert prefs.ssh_overrides_controller is controller
    assert all(row._sensitive is True for row in rows)
    assert prefs.connect_timeout_row.get_value() == 42


def test_closed_daemon_does_not_fall_back_to_local_ssh_overrides(
    tmp_path, monkeypatch
):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 99}},
    )
    prefs = _make_prefs(config, SshOverridesController(_ClosedClient()))

    def _local_config_is_not_authoritative(*_args, **_kwargs):
        pytest.fail("closed daemon must not read SSH overrides from Config")

    config.get_setting = _local_config_is_not_authoritative

    values = prefs._ssh_override_page_values()

    assert values == {
        "connect_timeout": 0,
        "connection_attempts": 0,
        "server_alive_interval": 0,
        "server_alive_count_max": 0,
        "strict_host_key_checking": "accept-new",
        "batch_mode": False,
        "compression": False,
        "verbosity": 0,
        "debug_enabled": False,
    }
    rows = (
        prefs.connect_timeout_row,
        prefs.connection_attempts_row,
        prefs.keepalive_interval_row,
        prefs.keepalive_count_row,
        prefs.strict_host_row,
        prefs.batch_mode_row,
        prefs.compression_row,
        prefs.verbosity_row,
        prefs.debug_enabled_row,
    )
    assert all(row._sensitive is False for row in rows)


def test_mark_daemon_unavailable_detaches_stale_ssh_overrides_controller(
    tmp_path, monkeypatch
):
    config = _make_config(tmp_path, monkeypatch, {"ssh": {}})
    controller = SshOverridesController(_ClosedClient())
    prefs = _make_prefs(config, controller)

    prefs.mark_daemon_unavailable()

    assert prefs.ssh_overrides_controller is None
    assert prefs.connect_timeout_row._sensitive is False


# ---------------------------------------------------------------------------
# Task 3: mutation failures are not swallowed
# ---------------------------------------------------------------------------


def test_save_reports_failure_and_preserves_snapshot_on_revision_conflict(
    tmp_path, monkeypatch
):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    loaded = controller.load()
    prefs = _make_prefs(config, controller)
    prefs.connect_timeout_row = _Spin(value=60)

    # Another daemon owner mutates the file after the page snapshot was taken,
    # bumping the revision beyond the controller's loaded snapshot.
    other = SshOverridesService(tmp_path / "config.json")
    other.update(UpdateGlobalSshOverridesRequest({"connect_timeout": 7}))
    # The legacy Config cache is refreshed to the daemon state, so its own
    # writes no longer re-clobber the file...
    config.reload_json_cache_strict()
    expires = _expiry_record(monkeypatch)

    # ...but the controller's snapshot predates the concurrent write.
    assert prefs.save_advanced_ssh_settings() is False
    # Failure is not silently swallowed: no master expiry, snapshot preserved.
    assert expires == []
    assert controller.snapshot().revision == loaded.revision
    assert controller.snapshot().connect_timeout == 5


def test_save_reports_failure_on_unsupported_capability(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_FailingClient(service))
    controller.load()
    prefs = _make_prefs(config, controller)
    prefs.connect_timeout_row = _Spin(value=60)
    expires = _expiry_record(monkeypatch)

    assert prefs.save_advanced_ssh_settings() is False
    assert expires == []


def test_save_success_expires_masters_after_mutation(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    prefs = _make_prefs(config, SshOverridesController(_ServiceClient(service)))
    prefs.connect_timeout_row = _Spin(value=30)
    expires = _expiry_record(monkeypatch)

    assert prefs.save_advanced_ssh_settings() is True
    assert expires == [True]


def test_save_advanced_ssh_settings_returns_false_on_revision_conflict(tmp_path, monkeypatch):
    # Settings autosave per-field now (no Done-button flush), so what used to
    # be exercised through on_close_request() is exercised directly here.
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    controller.load()
    prefs = _make_prefs(config, controller)
    prefs.connect_timeout_row = _Spin(value=60)

    # Concurrent daemon mutation: refresh the Config cache but keep the stale
    # controller snapshot, producing a revision conflict on save.
    other = SshOverridesService(tmp_path / "config.json")
    other.update(UpdateGlobalSshOverridesRequest({"connect_timeout": 7}))
    config.reload_json_cache_strict()

    assert prefs.save_advanced_ssh_settings() is False


def test_save_advanced_ssh_settings_returns_true_on_success(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    prefs = _make_prefs(config, SshOverridesController(_ServiceClient(service)))
    prefs.connect_timeout_row = _Spin(value=30)

    assert prefs.save_advanced_ssh_settings() is True


# ---------------------------------------------------------------------------
# Cross-client clobber race: a stale legacy cache must not rewrite an older
# SSH-overrides revision back to disk before the daemon mutation runs.
# ---------------------------------------------------------------------------


def test_stale_snapshot_save_conflicts_without_manual_refresh(
    tmp_path, monkeypatch
):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    loaded = controller.load()
    prefs = _make_prefs(config, controller)
    prefs.connect_timeout_row = _Spin(value=60)
    prefs.batch_mode_row = _Switch(active=True)
    expires = _expiry_record(monkeypatch)

    # Another SshOverridesService changes connection_timeout to revision B
    # after the page snapshot (revision A) was taken.  No manual cache
    # refresh: the save path must protect itself.
    other = SshOverridesService(tmp_path / "config.json")
    other.update(UpdateGlobalSshOverridesRequest({"connect_timeout": 7}))

    # The stale controller snapshot is rejected.
    assert prefs.save_advanced_ssh_settings() is False
    # ControlMasters are not expired.
    assert expires == []
    # The B value remains on disk.
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["ssh"]["connection_timeout"] == 7
    # Config-owned settings were applied from the refreshed cache and were
    # not allowed to restore revision A.
    assert on_disk["ssh"].get("connection_timeout") != 5
    assert on_disk["ssh"]["apply_default_keepalive"] is True
    # The controller snapshot is preserved.
    assert controller.snapshot().revision == loaded.revision
    assert controller.snapshot().connect_timeout == 5


def test_stale_snapshot_reset_conflicts_without_manual_refresh(
    tmp_path, monkeypatch
):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    loaded = controller.load()
    prefs = _make_prefs(config, controller)
    expires = _expiry_record(monkeypatch)

    other = SshOverridesService(tmp_path / "config.json")
    other.update(UpdateGlobalSshOverridesRequest({"connect_timeout": 7}))

    assert prefs._apply_default_advanced_settings() is False
    # ControlMasters are not expired.
    assert expires == []
    # The B value remains on disk.
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["ssh"]["connection_timeout"] == 7
    # Config-owned settings were applied from the refreshed cache and were
    # not allowed to restore revision A.
    assert on_disk["ssh"].get("connection_timeout") != 5
    assert on_disk["ssh"]["apply_default_keepalive"] is True
    # The controller snapshot is preserved.
    assert controller.snapshot().revision == loaded.revision
    assert controller.snapshot().connect_timeout == 5


def test_malformed_file_aborts_save_before_any_write(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    controller.load()
    prefs = _make_prefs(config, controller)
    prefs.connect_timeout_row = _Spin(value=60)
    expires = _expiry_record(monkeypatch)

    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    before = path.read_bytes()

    assert prefs.save_advanced_ssh_settings() is False
    # No Config-owned writes, no daemon mutation, no master expiry.
    assert expires == []
    assert path.read_bytes() == before


def test_malformed_file_aborts_reset_before_any_write(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    controller.load()
    prefs = _make_prefs(config, controller)
    expires = _expiry_record(monkeypatch)

    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    before = path.read_bytes()

    assert prefs._apply_default_advanced_settings() is False
    assert expires == []
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# Shared daemon file sanity (used by the tasks above)
# ---------------------------------------------------------------------------


def test_config_and_daemon_read_the_same_file(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    loaded, _migrated = load_settings_strict(tmp_path / "config.json")
    assert loaded["ssh"]["connection_timeout"] == 5
    assert config.get_setting("ssh.connection_timeout", None) == 5


# ---------------------------------------------------------------------------
# Autosave wiring: every Advanced SSH row persists on change, none of it
# depends on the Done button / a close-time flush.
# ---------------------------------------------------------------------------


def test_field_changed_persists_and_advances_snapshot_on_success(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    controller.load()
    prefs = _make_prefs(config, controller)
    prefs._advanced_ssh_last_good_snapshot = prefs._snapshot_advanced_ssh_rows()

    prefs.compression_row.set_active(True)
    prefs._on_advanced_ssh_field_changed(prefs.compression_row, None)

    # Actually landed on the daemon, not just marked as if it did.
    assert service.get().compression is True
    assert prefs._advanced_ssh_last_good_snapshot['compression'] is True


def test_field_changed_reverts_all_rows_on_daemon_failure(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))
    controller.load()
    prefs = _make_prefs(config, controller)
    baseline = prefs._snapshot_advanced_ssh_rows()
    prefs._advanced_ssh_last_good_snapshot = baseline

    failures = []
    prefs._show_ssh_save_failure = lambda: failures.append(True)

    # User edits the field...
    prefs.connect_timeout_row.set_value(60)

    # ...but a concurrent daemon mutation makes the controller's cached
    # revision stale, so the save is rejected as a revision conflict.
    other = SshOverridesService(tmp_path / "config.json")
    other.update(UpdateGlobalSshOverridesRequest({"connect_timeout": 7}))
    config.reload_json_cache_strict()

    prefs._on_advanced_ssh_field_changed(prefs.connect_timeout_row, None)

    assert failures == [True]
    # The whole page rolls back to the last-saved snapshot, not just the
    # row that was edited: the daemon patch is atomic, so a rejected patch
    # means nothing in it landed.
    assert prefs._snapshot_advanced_ssh_rows() == baseline


def test_field_changed_is_noop_while_suppressed(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    prefs = _make_prefs(config, SshOverridesController(_ServiceClient(service)))
    prefs._suppress_advanced_ssh_autosave = True

    def _fail_if_called():
        raise AssertionError("save_advanced_ssh_settings must not run while suppressed")

    prefs.save_advanced_ssh_settings = _fail_if_called

    prefs.compression_row.set_active(True)
    prefs._on_advanced_ssh_field_changed(prefs.compression_row, None)  # must not raise


def test_snapshot_round_trip_restores_values_and_clears_suppress_flag(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    prefs = _make_prefs(config, SshOverridesController(_ServiceClient(service)))
    snapshot = prefs._snapshot_advanced_ssh_rows()

    prefs.connect_timeout_row.set_value(99)
    prefs.compression_row.set_active(True)
    prefs.strict_host_row.set_selected(2)

    prefs._restore_advanced_ssh_rows(snapshot)

    assert prefs._snapshot_advanced_ssh_rows() == snapshot
    assert prefs._suppress_advanced_ssh_autosave is False


def test_reset_refreshes_last_good_snapshot(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 5}},
    )
    service = SshOverridesService(tmp_path / "config.json")
    prefs = _make_prefs(config, SshOverridesController(_ServiceClient(service)))
    _expiry_record(monkeypatch)

    # A stale baseline from before the reset; if it's left untouched, a
    # later failed edit would roll back to the wrong (pre-reset) values.
    prefs._advanced_ssh_last_good_snapshot = {"stale": True}

    assert prefs._apply_default_advanced_settings() is True
    assert prefs._advanced_ssh_last_good_snapshot == prefs._snapshot_advanced_ssh_rows()
    assert prefs._advanced_ssh_last_good_snapshot != {"stale": True}


def test_controller_attach_seeds_last_good_snapshot(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        monkeypatch,
        {"ssh": {"connection_timeout": 42}},
    )
    prefs = _make_prefs(config, None)
    service = SshOverridesService(tmp_path / "config.json")
    controller = SshOverridesController(_ServiceClient(service))

    prefs.set_ssh_overrides_controller(controller)

    assert prefs._advanced_ssh_last_good_snapshot == prefs._snapshot_advanced_ssh_rows()


def test_sftp_keepalive_interval_changed_persists_immediately(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch, {})
    prefs = _make_prefs(config, None)
    prefs.sftp_keepalive_interval_row.set_value(45)

    prefs.on_sftp_keepalive_interval_changed(prefs.sftp_keepalive_interval_row)

    assert config.get_setting('file_manager.sftp_keepalive_interval', None) == 45


def test_sftp_keepalive_count_changed_persists_immediately(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch, {})
    prefs = _make_prefs(config, None)
    prefs.sftp_keepalive_count_row.set_value(3)

    prefs.on_sftp_keepalive_count_changed(prefs.sftp_keepalive_count_row)

    assert config.get_setting('file_manager.sftp_keepalive_count_max', None) == 3


def test_sftp_connect_timeout_changed_persists_immediately(tmp_path, monkeypatch):
    config = _make_config(tmp_path, monkeypatch, {})
    prefs = _make_prefs(config, None)
    prefs.sftp_connect_timeout_row.set_value(20)

    prefs.on_sftp_connect_timeout_changed(prefs.sftp_connect_timeout_row)

    assert config.get_setting('file_manager.sftp_connect_timeout', None) == 20
