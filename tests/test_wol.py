"""Tests for Wake-on-LAN module."""

import pytest

from sshpilot.wol import (
    normalize_mac,
    validate_mac,
    send_wol,
    is_wol_available,
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


def test_is_wol_available():
    # Just ensure it returns a bool (True if wakeonlan installed)
    assert isinstance(is_wol_available(), bool)
