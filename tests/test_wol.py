"""Tests for Wake-on-LAN module."""

import pytest
from unittest.mock import patch, MagicMock
import socket

from sshpilot.wol import (
    normalize_mac,
    validate_mac,
    send_wol,
    is_wol_available,
    get_subnet_broadcast,
)


def test_normalize_mac():
    assert normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("  aa:bb:cc:dd:ee:ff  ") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("") == ""


def test_validate_mac():
    assert validate_mac("aa:bb:cc:dd:ee:ff") is True
    assert validate_mac("AA-BB-CC-DD-EE-FF") is True
    assert validate_mac("aabbccddeeff") is True
    assert validate_mac("aa:bb:cc:dd:ee:ff:00") is False
    assert validate_mac("gg:bb:cc:dd:ee:ff") is False
    assert validate_mac("") is False
    assert validate_mac("1.2.3.4") is False


@pytest.mark.skipif(not is_wol_available(),
                    reason="wakeonlan not installed; send_wol short-circuits before MAC validation")
def test_send_wol_invalid_mac():
    ok, msg = send_wol("invalid")
    assert ok is False
    assert "Invalid" in msg or "invalid" in msg.lower()


def test_send_wol_empty_mac():
    ok, msg = send_wol("")
    assert ok is False


def test_is_wol_available():
    # Just ensure it returns a bool (True if wakeonlan installed)
    assert isinstance(is_wol_available(), bool)


def test_get_subnet_broadcast_uses_interface_mask():
    """get_subnet_broadcast should use the real interface netmask, not hardcode /24."""
    import psutil
    snic = MagicMock()
    snic.family = socket.AF_INET
    snic.address = "10.0.0.5"
    snic.netmask = "255.255.0.0"  # /16

    with patch("psutil.net_if_addrs", return_value={"eth0": [snic]}):
        result = get_subnet_broadcast("10.0.1.200")
    # With /16 mask the broadcast is 10.0.255.255, not 10.0.1.255
    assert result == "10.0.255.255"


def test_get_subnet_broadcast_slash24():
    """/24 network still works correctly."""
    import psutil
    snic = MagicMock()
    snic.family = socket.AF_INET
    snic.address = "192.168.1.10"
    snic.netmask = "255.255.255.0"

    with patch("psutil.net_if_addrs", return_value={"eth0": [snic]}):
        result = get_subnet_broadcast("192.168.1.50")
    assert result == "192.168.1.255"


def test_get_subnet_broadcast_no_match_returns_none():
    """Returns None when no local interface is on the same subnet as target."""
    import psutil
    snic = MagicMock()
    snic.family = socket.AF_INET
    snic.address = "10.0.0.5"
    snic.netmask = "255.255.255.0"

    with patch("psutil.net_if_addrs", return_value={"eth0": [snic]}):
        result = get_subnet_broadcast("192.168.1.50")
    assert result is None


def test_get_subnet_broadcast_invalid_ip():
    assert get_subnet_broadcast("not-an-ip") is None
    assert get_subnet_broadcast("") is None
