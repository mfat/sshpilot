"""Public SSH overrides model validation and revision contract tests."""

import pytest

from sshpilot.api.models.settings import (
    EDITABLE_FIELDS,
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
    _compute_revision,
)


def test_editable_fields_exactly_nine():
    assert EDITABLE_FIELDS == {
        "connect_timeout",
        "connection_attempts",
        "server_alive_interval",
        "server_alive_count_max",
        "strict_host_key_checking",
        "batch_mode",
        "compression",
        "verbosity",
        "debug_enabled",
    }


def test_default_snapshot_is_valid():
    snapshot = GlobalSshOverrides(
        revision="abc",
        connect_timeout=0,
        connection_attempts=0,
        server_alive_interval=0,
        server_alive_count_max=0,
        strict_host_key_checking="accept-new",
        batch_mode=False,
        compression=False,
        verbosity=0,
        debug_enabled=False,
    )
    assert snapshot.to_dict()["revision"] == "abc"
    assert snapshot.to_dict()["applies_immediately"] is True


def test_revision_ignores_applies_immediately():
    from sshpilot.core.settings.ssh_overrides import DEFAULT_SSH_OVERRIDES

    base = dict(DEFAULT_SSH_OVERRIDES)
    a = dict(base, connect_timeout=5)
    b = dict(a)
    assert _compute_revision(a) == _compute_revision(b)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(connect_timeout=-1),
        dict(connect_timeout=601),
        dict(connection_attempts=101),
        dict(server_alive_interval=86401),
        dict(server_alive_count_max=101),
        dict(verbosity=4),
        dict(verbosity=-1),
    ],
)
def test_out_of_range_integer_rejected(kwargs):
    base = dict(
        revision="r",
        connect_timeout=0,
        connection_attempts=0,
        server_alive_interval=0,
        server_alive_count_max=0,
        strict_host_key_checking="accept-new",
        batch_mode=False,
        compression=False,
        verbosity=0,
        debug_enabled=False,
    )
    base.update(kwargs)
    with pytest.raises(ValueError):
        GlobalSshOverrides(**base)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(connect_timeout=0.5),
        dict(connection_attempts="10"),
        dict(server_alive_interval=True),
        dict(batch_mode=1),
        dict(compression="yes"),
        dict(debug_enabled="true"),
    ],
)
def test_wrong_type_rejected(kwargs):
    base = dict(
        revision="r",
        connect_timeout=0,
        connection_attempts=0,
        server_alive_interval=0,
        server_alive_count_max=0,
        strict_host_key_checking="accept-new",
        batch_mode=False,
        compression=False,
        verbosity=0,
        debug_enabled=False,
    )
    base.update(kwargs)
    with pytest.raises(TypeError):
        GlobalSshOverrides(**base)


def test_unknown_strict_host_value_rejected():
    with pytest.raises(ValueError):
        GlobalSshOverrides(
            revision="r",
            connect_timeout=0,
            connection_attempts=0,
            server_alive_interval=0,
            server_alive_count_max=0,
            strict_host_key_checking="maybe",
            batch_mode=False,
            compression=False,
            verbosity=0,
            debug_enabled=False,
        )


def test_update_request_rejects_unknown_fields():
    with pytest.raises(ValueError):
        UpdateGlobalSshOverridesRequest(patch={"not_a_field": 1})


def test_update_request_rejects_wrong_typed_value():
    with pytest.raises(TypeError):
        UpdateGlobalSshOverridesRequest(patch={"batch_mode": "true"})


def test_update_request_accepts_partial_patch():
    request = UpdateGlobalSshOverridesRequest(
        patch={"connect_timeout": 30},
        expected_revision="abc",
    )
    assert request.patch == {"connect_timeout": 30}
    assert request.expected_revision == "abc"


def test_update_request_requires_mapping_patch():
    with pytest.raises(TypeError):
        UpdateGlobalSshOverridesRequest(patch=["connect_timeout"])


def test_missing_strict_host_key_and_explicit_default_share_revision():
    from sshpilot.core.settings.ssh_overrides import normalize_ssh_overrides

    without = normalize_ssh_overrides({})
    with_default = normalize_ssh_overrides(
        {"strict_host_key_checking": "accept-new"}
    )
    assert _compute_revision(without) == _compute_revision(with_default)


# ---------------------------------------------------------------------------
# Request immutability / expected_revision validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("revision", ["", "   ", 0, 12, True, [], {}])
def test_update_request_rejects_invalid_expected_revision(revision):
    with pytest.raises(ValueError):
        UpdateGlobalSshOverridesRequest(
            patch={"connect_timeout": 5}, expected_revision=revision
        )


def test_update_request_accepts_none_expected_revision():
    request = UpdateGlobalSshOverridesRequest(
        patch={"connect_timeout": 5}, expected_revision=None
    )
    assert request.expected_revision is None


def test_update_request_patch_is_immutable():
    request = UpdateGlobalSshOverridesRequest(patch={"connect_timeout": 5})
    with pytest.raises(TypeError):
        request.patch["connect_timeout"] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        del request.patch["connect_timeout"]  # type: ignore[misc]


def test_update_request_copies_caller_patch():
    caller_patch = {"connect_timeout": 5}
    request = UpdateGlobalSshOverridesRequest(patch=caller_patch)

    caller_patch["connect_timeout"] = 99
    caller_patch["batch_mode"] = True

    assert request.patch == {"connect_timeout": 5}
    assert set(request.patch) == {"connect_timeout"}


def test_snapshot_revision_must_be_non_empty_string():
    base = dict(
        revision="r",
        connect_timeout=0,
        connection_attempts=0,
        server_alive_interval=0,
        server_alive_count_max=0,
        strict_host_key_checking="accept-new",
        batch_mode=False,
        compression=False,
        verbosity=0,
        debug_enabled=False,
    )
    for bad in ("", "   ", 123, None, True, ["r"]):
        with pytest.raises((TypeError, ValueError)):
            GlobalSshOverrides(**{**base, "revision": bad})


def test_snapshot_applies_immediately_must_be_boolean():
    base = dict(
        revision="r",
        connect_timeout=0,
        connection_attempts=0,
        server_alive_interval=0,
        server_alive_count_max=0,
        strict_host_key_checking="accept-new",
        batch_mode=False,
        compression=False,
        verbosity=0,
        debug_enabled=False,
    )
    for bad in (1, 0, "yes", None):
        with pytest.raises(TypeError):
            GlobalSshOverrides(**{**base, "applies_immediately": bad})
