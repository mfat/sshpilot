"""Tests for non-UUID runtime service identity helpers."""


from sshpilot.api.forward_identity import new_forward_id
from sshpilot.api.sftp_identity import new_sftp_id
from sshpilot.api.transfer_identity import new_transfer_id


def test_service_ids_are_sequential():
    sftp1 = new_sftp_id()
    sftp2 = new_sftp_id()
    assert sftp1.startswith("sftp-")
    assert sftp2.startswith("sftp-")
    assert sftp1 != sftp2

    t1 = new_transfer_id()
    t2 = new_transfer_id()
    assert t1.startswith("transfer-")
    assert t2.startswith("transfer-")
    assert t1 != t2

    f1 = new_forward_id()
    f2 = new_forward_id()
    assert f1.startswith("forward-")
    assert f2.startswith("forward-")
    assert f1 != f2
