"""Unit tests for the plugin discovery registry client (no network)."""

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import registry_client as rc
from sshpilot.plugins.api import API_VERSION


def _index(api=None):
    if api is None:
        api = API_VERSION[0]
    return {
        "schemaVersion": 1,
        "checksumAlgorithm": "sha256",
        "plugins": [
            {
                "id": "easyenv-workspaces", "name": "EasyEnv Workspaces",
                "description": "Provision easyenv.io workspaces.",
                "author": "mfat", "homepage": "https://example/ee",
                "latestVersion": "1.0.0",
                "versions": [{
                    "version": "1.0.0", "api_version": api,
                    "permissions": ["network", "keyring"],
                    "package": {"downloadUrl": "https://x/ee.zip",
                                "checksumUrl": "https://x/ee.zip.sha256"},
                }],
            },
            {
                "id": "future", "name": "Future Plugin", "latestVersion": "2.0.0",
                "versions": [{"version": "2.0.0", "api_version": API_VERSION[0] + 1,
                              "package": {"downloadUrl": "https://x/f.zip",
                                          "checksumUrl": "https://x/f.zip.sha256"}}],
            },
        ],
    }


def test_fetch_index_validates_schema(monkeypatch):
    monkeypatch.setattr(rc, "_read_url", lambda url, timeout: json.dumps(_index()).encode())
    idx = rc.fetch_index("https://x/plugins.json")
    assert idx["schemaVersion"] == 1

    monkeypatch.setattr(rc, "_read_url", lambda url, timeout: b'{"schemaVersion": 99}')
    with pytest.raises(rc.RegistryError):
        rc.fetch_index("https://x/plugins.json")

    monkeypatch.setattr(rc, "_read_url", lambda url, timeout: b'not json')
    with pytest.raises(rc.RegistryError):
        rc.fetch_index("https://x/plugins.json")


def test_list_entries_normalizes_and_flags_compatibility():
    entries = {e["id"]: e for e in rc.list_entries(_index())}
    ee = entries["easyenv-workspaces"]
    assert ee["name"] == "EasyEnv Workspaces"
    assert ee["version"] == "1.0.0"
    assert ee["permissions"] == ["network", "keyring"]
    assert ee["download_url"].endswith("ee.zip")
    assert ee["compatible"] is True
    assert entries["future"]["compatible"] is False   # api_version major+1


def test_download_package_verifies_checksum(monkeypatch, tmp_path):
    payload = b"PK\x03\x04 fake zip bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def fake_read(url, timeout):
        if url.endswith(".sha256"):
            return (digest + "  ee.zip\n").encode()
        return payload
    monkeypatch.setattr(rc, "_read_url", fake_read)

    dest = tmp_path / "ee.zip"
    got = rc.download_package("https://x/ee.zip", "https://x/ee.zip.sha256", str(dest))
    assert got == digest
    assert dest.read_bytes() == payload


def test_download_package_rejects_mismatch(monkeypatch, tmp_path):
    def fake_read(url, timeout):
        if url.endswith(".sha256"):
            return (("0" * 64) + "  ee.zip").encode()
        return b"some other bytes"
    monkeypatch.setattr(rc, "_read_url", fake_read)
    dest = tmp_path / "ee.zip"
    with pytest.raises(rc.RegistryError, match="mismatch"):
        rc.download_package("https://x/ee.zip", "https://x/ee.zip.sha256", str(dest))
    assert not dest.exists()
