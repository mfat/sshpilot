"""Tests for Phase 10 service identity helpers."""

import pytest

from sshpilot.api.forward_identity import (
    FORWARD_ID_PREFIX,
    forward_id_from_uuid,
    forward_uuid_from_id,
    new_forward_id,
)
from sshpilot.api.sftp_identity import (
    SFTP_ID_PREFIX,
    new_sftp_id,
    sftp_id_from_uuid,
    sftp_uuid_from_id,
)
from sshpilot.api.transfer_identity import (
    TRANSFER_ID_PREFIX,
    new_transfer_id,
    transfer_id_from_uuid,
    transfer_uuid_from_id,
)


@pytest.mark.parametrize(
    ("new_id", "from_uuid", "from_id", "prefix"),
    [
        (new_sftp_id, sftp_id_from_uuid, sftp_uuid_from_id, SFTP_ID_PREFIX),
        (new_transfer_id, transfer_id_from_uuid, transfer_uuid_from_id, TRANSFER_ID_PREFIX),
        (new_forward_id, forward_id_from_uuid, forward_uuid_from_id, FORWARD_ID_PREFIX),
    ],
)
def test_service_ids_are_prefixed_uuidv4(new_id, from_uuid, from_id, prefix):
    value = new_id()
    assert value.startswith(prefix)
    uuid_text = from_id(value)
    assert from_uuid(uuid_text) == value
    assert from_id(value) == uuid_text


@pytest.mark.parametrize(
    "from_id",
    [sftp_uuid_from_id, transfer_uuid_from_id, forward_uuid_from_id],
)
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "session:00000000-0000-0000-0000-000000000001",
        "sftp:",
        "sftp:not-a-uuid",
        "sftp:00000000-0000-0000-0000-000000000000",
        None,
        123,
    ],
)
def test_service_ids_reject_malformed_values(from_id, bad):
    with pytest.raises((ValueError, TypeError)):
        from_id(bad)


def test_nil_uuid_rejected_for_construction():
    with pytest.raises(ValueError):
        sftp_id_from_uuid("00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError):
        transfer_id_from_uuid("00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError):
        forward_id_from_uuid("00000000-0000-0000-0000-000000000000")
