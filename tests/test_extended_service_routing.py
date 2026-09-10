from types import SimpleNamespace

from sshpilot.extended_service_policy import (
    ExtendedServiceRoute,
    format_forward_failure_detail,
    prefer_daemon_extended_services,
    resolve_file_manager_route,
)

def test_extended_services_are_always_daemon_owned():
    assert prefer_daemon_extended_services()
    assert resolve_file_manager_route() is ExtendedServiceRoute.DAEMON


def test_format_forward_failure_includes_daemon_message():
    summary = SimpleNamespace(
        state=SimpleNamespace(value="failed"),
        failure=SimpleNamespace(
            code="forward_startup_failed",
            message="The forward did not become active",
        ),
    )
    detail = format_forward_failure_detail(summary)
    assert "The forward did not become active" in detail
    assert "forward_startup_failed" in detail
    assert "failed" in detail


def test_format_forward_failure_without_payload():
    summary = SimpleNamespace(state=SimpleNamespace(value="closed"), failure=None)
    assert format_forward_failure_detail(summary) == "forward ended in state closed"
