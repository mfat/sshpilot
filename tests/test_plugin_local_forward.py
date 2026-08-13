from sshpilot.extended_service_policy import allow_legacy_local_forward

def test_plugin_cannot_enable_frontend_forward_process():
    assert not allow_legacy_local_forward(prefer_daemon=False)
