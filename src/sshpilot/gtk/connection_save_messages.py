"""Frontend-owned wording for a connection save the daemon refused.

The daemon answers with a stable code and no sentence: an ``ErrorCode``, and
for ``MUTATION_AMBIGUOUS`` a ``MutationRefusal`` in ``details["refusal"]``
when it refused before writing anything. The connection editor words those
here, so a refusal the user can do something about says what, in their
language, and is not folded into "could not be saved".
"""
from __future__ import annotations

from gettext import gettext as _

from ..api.errors import ErrorCode, MutationRefusal, ValidationReason
from ..i18n import N_

_REFUSALS = {
    MutationRefusal.IDENTITY_UNRESOLVED: N_(
        "This connection can't be edited here for now. Hosts were removed "
        "from the SSH config while others with the same address and user "
        "were added, so SSH Pilot can't tell which of them this connection "
        "is. You can still connect to it, and change it in the SSH config "
        "editor or in Advanced Configuration."
    ),
    MutationRefusal.SHARED_DECLARATION: N_(
        "This host now shares its Host block with other hosts. Close this "
        "dialog and open the connection again to choose how to edit it."
    ),
    MutationRefusal.MULTIPLE_DECLARATIONS: N_(
        "More than one Host block in the SSH config names this host, so SSH "
        "Pilot can't tell which one to change. Change it in the SSH config "
        "editor or in Advanced Configuration."
    ),
    MutationRefusal.DECLARATION_CHANGED: N_(
        "This host's block in the SSH config changed after this dialog was "
        "opened. Close the dialog and open the connection again."
    ),
}

_CODES = {
    ErrorCode.STALE_EDITOR: N_(
        "This connection was changed after this dialog was opened. Close the "
        "dialog and open the connection again."
    ),
    ErrorCode.CONNECTION_ALREADY_EXISTS: N_(
        "A connection with this name already exists. Choose another name."
    ),
}


_REASONS = {
    ValidationReason.HOST_PATTERN: N_(
        "A pattern is not a connection: it sets options for every host it "
        "matches. Create it as a block in Advanced Configuration."
    ),
}


def save_refusal(error: object):
    """The ``MutationRefusal`` a failed save carries, or None."""
    if getattr(error, "code", None) is not ErrorCode.MUTATION_AMBIGUOUS:
        return None
    details = getattr(error, "details", None) or {}
    try:
        return MutationRefusal(details.get("refusal"))
    except (ValueError, AttributeError):
        return None


def save_failure_text(error: object) -> str:
    """What to tell the user about a save the daemon refused, or ``""`` when
    there is nothing more specific to say than that it failed."""
    refusal = save_refusal(error)
    if refusal is not None:
        return _(_REFUSALS[refusal])
    if getattr(error, "code", None) is ErrorCode.VALIDATION_FAILED:
        details = getattr(error, "details", None) or {}
        try:
            return _(_REASONS[ValidationReason(details.get("reason"))])
        except (ValueError, KeyError, AttributeError):
            return ""
    text = _CODES.get(getattr(error, "code", None))
    return _(text) if text else ""
