import threading

import pytest

from sshpilot.api import ErrorCode, SshPilotError


def test_closed_client_rejects_commands_and_new_subscriptions(
    fake_repo,
    client_factory,
):
    client = client_factory(fake_repo)
    capabilities = client.get_capabilities()
    client.close()

    with pytest.raises(SshPilotError) as command_error:
        client.list_connections()
    with pytest.raises(SshPilotError) as subscription_error:
        client.subscribe_events(lambda _event: None)

    assert command_error.value.code is ErrorCode.INVALID_REQUEST
    assert subscription_error.value.code is ErrorCode.INVALID_REQUEST
    assert client.get_capabilities() is capabilities


def test_core_service_commands_reject_the_wrong_thread(
    fake_repo,
    application_services_factory,
):
    client = application_services_factory(fake_repo)
    failures = []

    def invoke():
        try:
            client.list_connections()
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    worker.join()

    assert len(failures) == 1
    assert isinstance(failures[0], SshPilotError)
    assert failures[0].code is ErrorCode.INVALID_REQUEST
