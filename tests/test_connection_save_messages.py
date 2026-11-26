"""What the editor says about a save the daemon refused."""
from sshpilot.api.errors import ErrorCode, MutationRefusal, SshPilotError
from sshpilot.gtk.connection_save_messages import save_failure_text, save_refusal


def _refused(refusal):
    return SshPilotError(
        ErrorCode.MUTATION_AMBIGUOUS, "refused", details={"refusal": refusal}
    )


def test_each_named_refusal_says_what_it_is():
    texts = {refusal: save_failure_text(_refused(refusal.value)) for refusal in MutationRefusal}
    assert all(texts.values()) and len(set(texts.values())) == len(texts)
    assert "can't tell which of them" in texts[MutationRefusal.IDENTITY_UNRESOLVED]
    assert "Advanced Configuration" in texts[MutationRefusal.IDENTITY_UNRESOLVED]
    assert save_refusal(_refused("identity_unresolved")) is MutationRefusal.IDENTITY_UNRESOLVED


def test_an_outcome_nobody_knows_gets_no_made_up_reason():
    """MUTATION_AMBIGUOUS without a refusal is a write that may have landed:
    there is nothing truthful to add to "could not be saved"."""
    unknown = SshPilotError(ErrorCode.MUTATION_AMBIGUOUS, "may have completed")
    assert save_failure_text(unknown) == "" and save_refusal(unknown) is None
    assert save_failure_text(_refused("a code from a newer daemon")) == ""
    assert save_failure_text(OSError("disk")) == ""
    assert save_failure_text(SshPilotError(ErrorCode.PERSISTENCE_FAILED, "x")) == ""


def test_a_stale_editor_and_a_taken_name_are_said_too():
    assert "open the connection again" in save_failure_text(
        SshPilotError(ErrorCode.STALE_EDITOR, "stale")
    )
    assert "already exists" in save_failure_text(
        SshPilotError(ErrorCode.CONNECTION_ALREADY_EXISTS, "exists")
    )
