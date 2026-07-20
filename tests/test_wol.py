"""Tests for Wake-on-LAN module."""

from unittest.mock import patch, MagicMock
import socket

from sshpilot.wol import (
    normalize_mac,
    validate_mac,
    send_wol,
    build_magic_packet,
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


def test_send_wol_invalid_mac():
    ok, msg = send_wol("invalid")
    assert ok is False
    assert "Invalid" in msg or "invalid" in msg.lower()


def test_send_wol_empty_mac():
    ok, msg = send_wol("")
    assert ok is False


def test_build_magic_packet():
    """6 sync bytes then the MAC 16 times -- 102 bytes, per the AMD magic packet spec."""
    packet = build_magic_packet("aa:bb:cc:dd:ee:ff")
    assert len(packet) == 6 + 6 * 16 == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16
    # Accepts any format normalize_mac accepts
    assert build_magic_packet("AA-BB-CC-DD-EE-FF") == packet
    assert build_magic_packet("aabbccddeeff") == packet


def test_send_wol_broadcasts_to_the_derived_address():
    """The packet goes out on a broadcast-enabled UDP socket, to the given target."""
    sock = MagicMock()
    with patch("socket.socket") as mk:
        mk.return_value.__enter__.return_value = sock
        ok, msg = send_wol("aa:bb:cc:dd:ee:ff", broadcast_ip="192.168.1.255", port=9)
    assert ok is True, msg
    sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload, addr = sock.sendto.call_args[0]
    assert payload == build_magic_packet("aa:bb:cc:dd:ee:ff")
    assert addr == ("192.168.1.255", 9)


def test_send_wol_rejects_a_non_literal_broadcast_address():
    """A hostname would make sendto() resolve and unicast the packet somewhere."""
    with patch("socket.socket") as mk:
        ok, msg = send_wol("aa:bb:cc:dd:ee:ff", broadcast_ip="attacker.example.com")
    assert ok is False
    assert "broadcast" in msg.lower()
    mk.assert_not_called()

    with patch("socket.socket") as mk:
        ok, _ = send_wol("aa:bb:cc:dd:ee:ff", broadcast_ip="192.168.1.999")
    assert ok is False
    mk.assert_not_called()


def test_get_subnet_broadcast_uses_interface_mask():
    """get_subnet_broadcast should use the real interface netmask, not hardcode /24."""
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
    snic = MagicMock()
    snic.family = socket.AF_INET
    snic.address = "192.168.1.10"
    snic.netmask = "255.255.255.0"

    with patch("psutil.net_if_addrs", return_value={"eth0": [snic]}):
        result = get_subnet_broadcast("192.168.1.50")
    assert result == "192.168.1.255"


def test_get_subnet_broadcast_no_match_returns_none():
    """Returns None when no local interface is on the same subnet as target."""
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
