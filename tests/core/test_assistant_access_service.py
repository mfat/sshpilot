"""Headless tests for the assistant-access settings service."""

from pathlib import Path

import pytest

from sshpilot.api.errors import SshPilotError
from sshpilot.api.models.settings import UpdateAssistantAccessRequest
from sshpilot.core.assistant_access_service import AssistantAccessService
from sshpilot.core.settings import get_default_config, save_settings


def _service(tmp_path: Path, *, known=None) -> AssistantAccessService:
    path = tmp_path / "config.json"
    save_settings(path, get_default_config())
    return AssistantAccessService(
        path,
        known_connection_ids=(lambda: known) if known is not None else None,
    )


def test_defaults_are_disabled(tmp_path: Path):
    service = _service(tmp_path)
    snapshot = service.get()
    assert snapshot.enabled is False
    assert snapshot.allowed_connection_ids == ()
    assert snapshot.operate is True
    assert snapshot.mutate is False
    assert snapshot.content is False
    assert snapshot.http_listen is False
    assert snapshot.http_port == 0
    assert snapshot.http_token  # minted on first read
    assert snapshot.http_listening is False


def test_update_persists_allowlist_and_http(tmp_path: Path):
    service = _service(tmp_path, known=["a", "b", "c"])
    updated = service.update(
        UpdateAssistantAccessRequest(
            enabled=True,
            allowed_connection_ids=("a", "c"),
            operate=True,
            mutate=True,
            content=True,
            http_listen=True,
            http_port=9555,
        )
    )
    assert updated.enabled is True
    assert updated.allowed_connection_ids == ("a", "c")
    assert updated.mutate is True
    assert updated.content is True
    assert updated.http_listen is True
    assert updated.http_port == 9555
    assert service.get().http_port == 9555


def test_regenerate_token(tmp_path: Path):
    service = _service(tmp_path, known=["a"])
    first = service.get().http_token
    updated = service.update(
        UpdateAssistantAccessRequest(
            enabled=True,
            allowed_connection_ids=("a",),
            regenerate_token=True,
            expected_revision=service.get().revision,
        )
    )
    assert updated.http_token
    assert updated.http_token != first


def test_unknown_ids_are_dropped(tmp_path: Path):
    service = _service(tmp_path, known=["a"])
    updated = service.update(
        UpdateAssistantAccessRequest(
            enabled=True,
            allowed_connection_ids=("a", "missing"),
        )
    )
    assert updated.allowed_connection_ids == ("a",)


def test_revision_conflict(tmp_path: Path):
    service = _service(tmp_path, known=["a"])
    current = service.get()
    with pytest.raises(SshPilotError):
        service.update(
            UpdateAssistantAccessRequest(
                enabled=True,
                allowed_connection_ids=("a",),
                expected_revision="deadbeef0000",
            )
        )
    service.update(
        UpdateAssistantAccessRequest(
            enabled=True,
            allowed_connection_ids=("a",),
            expected_revision=current.revision,
        )
    )
