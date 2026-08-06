"""Controller revision-safety after a selection change.

``SecretBackendsController.update_selection`` refreshes the staged
``SecretConfiguration`` from the daemon after a successful selection (the
selection bumped its revision), so a following configuration update uses the
authoritative revision instead of conflicting.  A failed selection preserves
both staged snapshots.
"""

from __future__ import annotations

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.secrets import (
    REVISION_CONFLICT,
    SecretBackendState,
    SecretConfiguration,
)
from sshpilot.gtk.secret_backends_controller import SecretBackendsController


class FakeSecretsClient:
    """Daemon-shaped fake: selection/update bump the revision; stale revisions
    are rejected with ``REVISION_CONFLICT``."""

    def __init__(self) -> None:
        self._backend = "bitwarden"
        self._session_timeout = 30
        self._revision_counter = 1

    # -- internal daemon simulation -------------------------------------

    def _revision(self) -> str:
        return f"rev-{self._revision_counter}"

    def _bump(self) -> str:
        self._revision_counter += 1
        return self._revision()

    def _config(self) -> SecretConfiguration:
        return SecretConfiguration(
            revision=self._revision(),
            backend=self._backend,
            session_timeout=self._session_timeout,
            remember_in_keyring=False,
            bitwarden_profile="",
            bitwarden_server="",
            keepassxc_database="",
            keepassxc_keyfile="",
        )

    def _state(self) -> SecretBackendState:
        return SecretBackendState(
            selected_backend=self._backend,
            effective_backend=self._backend,
            locked=True,
            needs_unlock=True,
            login_required=False,
            session_timeout=self._session_timeout,
            remember_in_keyring=False,
            persists_secrets=True,
        )

    # -- client surface -------------------------------------------------

    def get_secret_configuration(self) -> SecretConfiguration:
        return self._config()

    def get_secret_state(self) -> SecretBackendState:
        return self._state()

    def update_secret_selection(
        self, backend: str, *, expected_revision: str | None = None
    ) -> SecretBackendState:
        if expected_revision is not None and expected_revision != self._revision():
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The secret configuration has been modified since last read",
                details={"code": REVISION_CONFLICT},
            )
        self._backend = backend
        self._bump()
        return self._state()

    def update_secret_configuration(self, request) -> SecretConfiguration:
        if (
            request.expected_revision is not None
            and request.expected_revision != self._revision()
        ):
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The secret configuration has been modified since last read",
                details={"code": REVISION_CONFLICT},
            )
        if "session_timeout" in (request.patch or {}):
            self._session_timeout = request.patch["session_timeout"]
        self._bump()
        return self._config()


@pytest.fixture
def controller():
    return SecretBackendsController(FakeSecretsClient())


def test_selection_then_configuration_update_uses_new_revision(controller):
    """load at revision A -> select a different backend (creates B) -> update
    session timeout using B -> update succeeds (no revision conflict)."""
    loaded = controller.load_configuration()
    assert loaded.revision == "rev-1"

    state = controller.update_selection("rbw")
    assert state.selected_backend == "rbw"
    # The controller refreshed its configuration to the post-selection revision.
    assert controller.configuration() is not None
    assert controller.configuration().revision == "rev-2"
    assert controller.configuration().backend == "rbw"
    assert controller.state() is state

    updated = controller.update_configuration({"session_timeout": 9})
    assert updated.session_timeout == 9
    assert updated.backend == "rbw"
    assert updated.revision == "rev-3"


def test_two_consecutive_selections(controller):
    """Each selection re-stages the authoritative configuration, so a second
    selection does not conflict on the first's new revision."""
    controller.load_configuration()
    first = controller.update_selection("rbw")
    assert first.selected_backend == "rbw"
    assert controller.configuration().revision == "rev-2"

    second = controller.update_selection("keepassxc")
    assert second.selected_backend == "keepassxc"
    assert controller.configuration().revision == "rev-3"
    assert controller.configuration().backend == "keepassxc"


def test_failed_selection_preserves_configuration_and_state(controller):
    """On a REVISION_CONFLICT the daemon rejects the selection and the
    controller keeps both staged snapshots untouched."""
    loaded = controller.load_configuration()
    state = controller.load_state()
    assert loaded.revision == "rev-1"

    with pytest.raises(SshPilotError) as excinfo:
        controller.update_selection("rbw", expected_revision="rev-stale")
    assert excinfo.value.details.get("code") == REVISION_CONFLICT

    assert controller.configuration() is loaded
    assert controller.configuration().revision == "rev-1"
    assert controller.state() is state
    assert controller.state().selected_backend == "bitwarden"


def test_failed_selection_keeps_controller_usable(controller):
    """After a failed selection the controller still stages fresh reads."""
    controller.load_configuration()
    with pytest.raises(SshPilotError):
        controller.update_selection("rbw", expected_revision="rev-stale")
    refreshed = controller.load_configuration()
    assert refreshed.backend == "bitwarden"
