"""Unit tests for Host Info well-known web UI classification and open URLs."""

from sshpilot.api.models.host_info import ListeningPort
from sshpilot.host_info_web_uis import (
    browser_url,
    classify_listening_port,
    needs_local_forward,
)
from sshpilot.port_utils import allocate_ephemeral_local_port


def test_classify_by_well_known_port():
    match = classify_listening_port(ListeningPort(port=9090, process="", address="0.0.0.0"))
    assert match is not None
    assert match.scheme == "https"
    assert match.label == "Cockpit"


def test_classify_by_process_name():
    match = classify_listening_port(
        ListeningPort(port=12345, process="cockpit-ws", address="127.0.0.1")
    )
    assert match is not None
    assert match.scheme == "https"
    assert match.label == "Cockpit"


def test_unknown_listener_has_no_match():
    assert (
        classify_listening_port(ListeningPort(port=22, process="sshd", address="0.0.0.0"))
        is None
    )


def test_non_web_process_on_well_known_http_port_has_no_match():
    """sshd on 8080 must not be labeled HTTP or offered a browser open."""

    assert (
        classify_listening_port(ListeningPort(port=8080, process="sshd", address="0.0.0.0"))
        is None
    )
    assert (
        classify_listening_port(
            ListeningPort(port=443, process="/usr/sbin/sshd", address="::")
        )
        is None
    )


def test_needs_forward_for_loopback_and_unspecified():
    assert needs_local_forward("") is True
    assert needs_local_forward("0.0.0.0") is True
    assert needs_local_forward("::") is True
    assert needs_local_forward("127.0.0.1") is True
    assert needs_local_forward("::1") is True
    assert needs_local_forward("fe80::1") is True
    assert needs_local_forward("192.168.1.10") is False
    assert needs_local_forward("10.0.0.5") is False


def test_browser_url_direct_and_forwarded():
    assert (
        browser_url(scheme="https", address="192.168.1.10", port=9090)
        == "https://192.168.1.10:9090/"
    )
    assert (
        browser_url(scheme="http", address="0.0.0.0", port=8080, local_port=18080)
        == "http://127.0.0.1:18080/"
    )
    assert (
        browser_url(scheme="http", address="2001:db8::1", port=80)
        == "http://[2001:db8::1]:80/"
    )


def test_allocate_ephemeral_local_port_returns_usable_high_port():
    port = allocate_ephemeral_local_port("127.0.0.1")
    assert port is not None
    assert 1024 <= port <= 65535
    # A second allocation should usually differ (not required, but both valid).
    other = allocate_ephemeral_local_port("127.0.0.1")
    assert other is not None
    assert 1024 <= other <= 65535
