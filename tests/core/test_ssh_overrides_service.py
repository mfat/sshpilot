"""Core ``SshOverridesService``: strict load, normalization, revision, reset."""

import json

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.settings import (
    REVISION_CONFLICT,
    SETTINGS_MALFORMED,
    UpdateGlobalSshOverridesRequest,
)
from sshpilot.core.settings import CONFIG_VERSION, get_default_config
from sshpilot.core.settings.ssh_overrides import (
    DEFAULT_SSH_OVERRIDES,
    FIELD_TO_CONFIG_KEY,
    compute_ssh_overrides_revision,
    normalize_ssh_overrides,
)
from sshpilot.core.ssh_overrides_service import SshOverridesService

_RAW_KEY = {field: key.split(".", 1)[1] for field, key in FIELD_TO_CONFIG_KEY.items()}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data) if isinstance(data, dict) else data
    if isinstance(payload, dict):
        payload.setdefault("config_version", CONFIG_VERSION)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _service(path):
    return SshOverridesService(path)


def _semantic(config):
    return normalize_ssh_overrides(config.get("ssh", {}))


def test_successful_mutations_notify_daemon_runtime_hook(tmp_path):
    calls = []
    service = SshOverridesService(
        tmp_path / "config.json",
        on_mutation=lambda: calls.append("runtime"),
    )

    service.update(UpdateGlobalSshOverridesRequest({"connect_timeout": 30}))
    service.reset()

    assert calls == ["runtime", "runtime"]


# ---------------------------------------------------------------------------
# Strict load behavior
# ---------------------------------------------------------------------------


def test_missing_file_returns_canonical_defaults(tmp_path):
    service = _service(tmp_path / "config.json")
    snapshot = service.get()

    assert snapshot.revision == compute_ssh_overrides_revision(DEFAULT_SSH_OVERRIDES)
    assert snapshot.connect_timeout == 0
    assert snapshot.connection_attempts == 0
    assert snapshot.server_alive_interval == 0
    assert snapshot.server_alive_count_max == 0
    assert snapshot.strict_host_key_checking == "accept-new"
    assert snapshot.batch_mode is False
    assert snapshot.compression is False
    assert snapshot.verbosity == 0
    assert snapshot.debug_enabled is False
    # The missing file is never materialized by a read.
    assert not (tmp_path / "config.json").exists()


def test_missing_strict_host_key_equates_to_explicit_accept_new(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {"ssh": {"connection_timeout": 5}},
    )
    service = _service(path)
    snapshot = service.get()
    assert snapshot.strict_host_key_checking == "accept-new"

    explicit = _write(
        tmp_path / "config.json",
        {
            "ssh": {"connection_timeout": 5, "strict_host_key_checking": "accept-new"},
        },
    )
    other = _service(explicit).get()
    assert other.revision == snapshot.revision
    assert other.connect_timeout == snapshot.connect_timeout == 5


def test_invalid_json_raises_stable_malformed_error(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    service = _service(path)

    with pytest.raises(SshPilotError) as excinfo:
        service.get()
    assert excinfo.value.code is ErrorCode.PERSISTENCE_FAILED
    assert excinfo.value.details.get("code") == SETTINGS_MALFORMED
    # Sanitized: no raw JSON, no path, no exception text in the public error.
    message = str(excinfo.value)
    assert "config.json" not in message
    assert "{" not in message


def test_non_object_root_raises_malformed(tmp_path):
    _write(tmp_path / "config.json", ["not", "an", "object"])
    with pytest.raises(SshPilotError) as excinfo:
        _service(tmp_path / "config.json").get()
    assert excinfo.value.details.get("code") == SETTINGS_MALFORMED


@pytest.mark.parametrize("raw", ["", "\n", "   \t  \n"])
def test_blank_or_whitespace_file_raises_malformed_unchanged(tmp_path, raw):
    path = tmp_path / "config.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(SshPilotError) as excinfo:
        _service(path).get()
    assert excinfo.value.code is ErrorCode.PERSISTENCE_FAILED
    assert excinfo.value.details.get("code") == SETTINGS_MALFORMED
    # Malformed input stays byte-for-byte unchanged and is never replaced.
    assert path.read_text(encoding="utf-8") == raw
    assert not (tmp_path / "config.json.bak").exists()


@pytest.mark.parametrize("version", ["abc", "3.5", True, False, [], {}, "3.0", ""])
def test_invalid_config_version_raises_malformed_unchanged(tmp_path, version):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"config_version": version, "ssh": {"batch_mode": True}}),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    with pytest.raises(SshPilotError) as excinfo:
        _service(path).get()
    assert excinfo.value.code is ErrorCode.PERSISTENCE_FAILED
    assert excinfo.value.details.get("code") == SETTINGS_MALFORMED
    # Malformed input stays byte-for-byte unchanged and is never backed up.
    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "config.json.bak").exists()


def test_string_integer_config_version_is_accepted(tmp_path):
    path = _write(tmp_path / "config.json", {"config_version": str(CONFIG_VERSION)})
    snapshot = _service(path).get()
    assert snapshot.revision == compute_ssh_overrides_revision(DEFAULT_SSH_OVERRIDES)


def test_blank_whitespace_file_rejected_by_loader_directly(tmp_path):
    from sshpilot.core.settings import SettingsFileError, load_settings_strict

    path = tmp_path / "config.json"
    path.write_text("   \n  ", encoding="utf-8")
    with pytest.raises(SettingsFileError):
        load_settings_strict(path)
    assert path.read_text(encoding="utf-8") == "   \n  "


def test_bad_ssh_value_raises_malformed_without_overwrite(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {"ssh": {"verbosity": "loud"}},
    )
    before = path.read_text(encoding="utf-8")
    service = _service(path)

    with pytest.raises(SshPilotError) as excinfo:
        service.get()
    assert excinfo.value.details.get("code") == SETTINGS_MALFORMED
    # The malformed input is never overwritten by a read.
    assert path.read_text(encoding="utf-8") == before


def test_obsolete_version_replaced_with_defaults(tmp_path):
    path = _write(tmp_path / "config.json", {"config_version": 0})
    service = _service(path)
    snapshot = service.get()
    assert snapshot.revision == compute_ssh_overrides_revision(DEFAULT_SSH_OVERRIDES)
    assert (tmp_path / "config.json.bak").exists()


# ---------------------------------------------------------------------------
# Normalization / revision
# ---------------------------------------------------------------------------


def test_numeric_strings_rejected():
    with pytest.raises(TypeError):
        normalize_ssh_overrides({"connection_timeout": "10"})
    with pytest.raises(TypeError):
        normalize_ssh_overrides({"batch_mode": "true"})
    with pytest.raises(TypeError):
        normalize_ssh_overrides({"verbosity": True})


def test_revision_changes_with_semantic_values():
    base = dict(DEFAULT_SSH_OVERRIDES)
    changed = dict(base, connect_timeout=30)
    assert compute_ssh_overrides_revision(base) != compute_ssh_overrides_revision(
        changed
    )
    assert compute_ssh_overrides_revision(base) == compute_ssh_overrides_revision(
        dict(base)
    )


# ---------------------------------------------------------------------------
# Mutation / reset
# ---------------------------------------------------------------------------


def test_update_persists_patch_and_derived_overrides(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {"ssh": {"batch_mode": False}},
    )
    service = _service(path)
    before = service.get()
    assert before.batch_mode is False

    updated = service.update(
        UpdateGlobalSshOverridesRequest(
            patch={"batch_mode": True, "connect_timeout": 12},
            expected_revision=before.revision,
        )
    )
    assert updated.batch_mode is True
    assert updated.connect_timeout == 12
    assert updated.revision != before.revision

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["ssh"]["batch_mode"] is True
    assert on_disk["ssh"]["connection_timeout"] == 12
    assert "-o" in on_disk["ssh"]["ssh_overrides"]
    assert "BatchMode=yes" in on_disk["ssh"]["ssh_overrides"]
    assert "ConnectTimeout=12" in on_disk["ssh"]["ssh_overrides"]


def test_noop_update_keeps_revision_stable(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {"ssh": {"connection_timeout": 5}},
    )
    service = _service(path)
    first = service.get()

    noop = service.update(
        UpdateGlobalSshOverridesRequest(
            patch={"connect_timeout": 5},
            expected_revision=first.revision,
        )
    )
    assert noop.revision == first.revision


def test_update_rejects_stale_revision(tmp_path):
    path = tmp_path / "config.json"
    _write(path, {"ssh": {}})
    service = _service(path)

    # A concurrent writer changes the file behind the service.
    _write(path, {"ssh": {"connection_timeout": 99}})

    with pytest.raises(SshPilotError) as excinfo:
        service.update(
            UpdateGlobalSshOverridesRequest(
                patch={"connect_timeout": 5},
                expected_revision="stale-revision",
            )
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details.get("code") == REVISION_CONFLICT


def test_reset_restores_defaults_and_preserves_other_keys(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {
            "ssh": {
                "connection_timeout": 12,
                "batch_mode": True,
                "some_other_key": "kept",
            },
            "app": {"theme": "dark"},
        },
    )
    service = _service(path)
    before = service.get()

    reset = service.reset(expected_revision=before.revision)
    assert reset.revision == compute_ssh_overrides_revision(DEFAULT_SSH_OVERRIDES)
    assert reset.connect_timeout == 0
    assert reset.batch_mode is False

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["ssh"]["some_other_key"] == "kept"
    assert on_disk["app"]["theme"] == "dark"


def test_reset_rejects_stale_revision(tmp_path):
    path = _write(tmp_path / "config.json", {"ssh": {}})
    service = _service(path)
    with pytest.raises(SshPilotError) as excinfo:
        service.reset(expected_revision="not-the-current-revision")
    assert excinfo.value.details.get("code") == REVISION_CONFLICT


def test_reset_writes_defaults_for_absent_keys(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {"ssh": {"connection_timeout": 12}},
    )
    service = _service(path)
    service.reset()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    for field, default in DEFAULT_SSH_OVERRIDES.items():
        raw_key = _RAW_KEY[field]
        assert on_disk["ssh"][raw_key] == default


# ---------------------------------------------------------------------------
# Settings view shims
# ---------------------------------------------------------------------------


def test_get_ssh_config_includes_derived_overrides(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {"ssh": {"batch_mode": True, "connection_timeout": 7}},
    )
    service = _service(path)
    ssh = service.get_ssh_config()
    assert ssh["batch_mode"] is True
    assert ssh["ssh_overrides"] == [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=7",
        "-o", "StrictHostKeyChecking=accept-new",
    ]


def test_get_setting_reads_namespaced_and_top_level(tmp_path):
    path = _write(
        tmp_path / "config.json",
        {"ssh": {"connection_timeout": 7}, "use-askpass": False},
    )
    service = _service(path)
    assert service.get_setting("ssh.connection_timeout", 0) == 7
    assert service.get_setting("use-askpass", True) is False
    assert service.get_setting("ssh.missing_key", "fallback") == "fallback"


def test_get_ssh_config_degrades_to_defaults_on_malformed(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    service = _service(path)
    ssh = service.get_ssh_config()
    assert ssh["batch_mode"] is False
    assert ssh["ssh_overrides"] == ["-o", "StrictHostKeyChecking=accept-new"]
    # The malformed file is untouched.
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_base_defaults_shape_matches_core_defaults(tmp_path):
    path = _write(tmp_path / "config.json", {"ssh": {}})
    service = _service(path)
    ssh = service.get_ssh_config()
    defaults = get_default_config()["ssh"]
    for key, value in defaults.items():
        if key == "ssh_overrides":
            # Derived by compose_ssh_overrides; never compared against the
            # stored default list.
            continue
        assert ssh.get(key) == value


# ---------------------------------------------------------------------------
# Live ControlMaster composition (task 6)
# ---------------------------------------------------------------------------

_MUX_ARGS = (
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=%C",
    "-o", "ControlPersist=60",
)


def _join(ssh):
    return " ".join(ssh["ssh_overrides"])


def test_controlmaster_false_yields_no_fragment(tmp_path):
    path = _write(tmp_path / "config.json", {"ssh": {"controlmaster": False}})
    service = SshOverridesService(path, controlmaster_extra=_MUX_ARGS)

    ssh = service.get_ssh_config()
    assert "ControlMaster=auto" not in _join(ssh)


def test_live_controlmaster_toggle_without_restart(tmp_path):
    path = _write(tmp_path / "config.json", {"ssh": {"controlmaster": False}})
    service = SshOverridesService(path, controlmaster_extra=_MUX_ARGS)

    assert "ControlMaster=auto" not in _join(service.get_ssh_config())

    # External/current config enables controlmaster on the same file; the same
    # long-lived service composes the fragment on the next read.
    _write(path, {"ssh": {"controlmaster": True}})
    assert "ControlMaster=auto" in _join(service.get_ssh_config())

    # And turning it back off removes the fragment again.
    _write(path, {"ssh": {"controlmaster": False}})
    assert "ControlMaster=auto" not in _join(service.get_ssh_config())


def test_controlmaster_fragment_only_injected_when_enabled(tmp_path):
    # The args are always injected, but the loaded value gates inclusion.
    path = _write(tmp_path / "config.json", {"ssh": {"controlmaster": False}})
    service = SshOverridesService(path, controlmaster_extra=_MUX_ARGS)

    ssh = service.get_ssh_config()
    assert ssh["ssh_overrides"] == ["-o", "StrictHostKeyChecking=accept-new"]

    _write(path, {"ssh": {"controlmaster": True}})
    ssh = service.get_ssh_config()
    assert "ControlMaster=auto" in _join(ssh)
    assert "-o" in ssh["ssh_overrides"][:2]


def test_persisted_mutation_composes_controlmaster_when_enabled(tmp_path):
    path = _write(tmp_path / "config.json", {"ssh": {"controlmaster": True}})
    service = SshOverridesService(path, controlmaster_extra=_MUX_ARGS)
    before = service.get()

    updated = service.update(
        UpdateGlobalSshOverridesRequest(
            patch={"connect_timeout": 12},
            expected_revision=before.revision,
        )
    )
    assert updated.connect_timeout == 12

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "ControlMaster=auto" in on_disk["ssh"]["ssh_overrides"]
