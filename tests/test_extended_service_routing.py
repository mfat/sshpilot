from sshpilot.extended_service_policy import (
    ExtendedServiceRoute, prefer_daemon_extended_services, resolve_file_manager_route,
)

def test_extended_services_are_always_daemon_owned():
    assert prefer_daemon_extended_services()
    assert resolve_file_manager_route() is ExtendedServiceRoute.DAEMON
