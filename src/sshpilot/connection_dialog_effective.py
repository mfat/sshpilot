"""What OpenSSH would use for the connection being edited, edits included.

Every SSH option the editor shows depends on more than its own Host block:
an earlier block, a ``Match`` block, an included file, the system-wide file
or Settings ▸ SSH Options may decide it. After each edit this mixin asks
the daemon one question (``connections.evaluate_connection_edit``) with
what a save would send. The daemon writes the block with the save's own
code, writes nothing to disk, and evaluates the file as Advanced
Configuration evaluates a draft. The answer
(:class:`~sshpilot.connection_dialog_answer.EditorAnswer`) goes to

* the authentication section (:class:`AuthPolicy.set_draft`), so a checkbox
  and the "OpenSSH will try" line agree with OpenSSH before Save;
* the "Keys OpenSSH offers" list;
* the Routing page, the fields' marks and inherited values, and the Extra
  Options page (``connection_dialog_overrides``).

A lookup that fails is shown as unknown, never as a default.
"""

from __future__ import annotations

import logging
import threading
from gettext import gettext as _
from gettext import ngettext
from typing import Optional, Sequence

try:
    from gi.repository import Adw, GLib
except Exception:  # pragma: no cover - GTK-less import (headless tests)
    Adw = GLib = None

from .connection_dialog_draft import (
    _AUTH_KEYWORDS,
    EffectiveIdentity,
    read_answer,
)

logger = logging.getLogger(__name__)

#: Milliseconds an edit waits before OpenSSH is asked again.
_REFRESH_DELAY_MS = 250

# How the identity list was obtained.
PENDING = "pending"      # the draft: the dialog's lines counted
FAILED = "failed"        # OpenSSH was asked and did not answer
WAITING = "waiting"      # no answer yet


def identity_status_text(state: str, count: int = 0) -> str:
    """The caption under "Keys OpenSSH offers": how many, while known."""
    if state == FAILED:
        return _("Unknown: OpenSSH could not read the configuration.")
    if state == PENDING and count:
        return ngettext(
            "{count} key, tried in this order", "{count} keys, tried in this order", count
        ).format(count=count)
    if state == PENDING:
        return _("No keys")
    return _("Shown once OpenSSH has read the configuration.")


def identity_source_text(identity: EffectiveIdentity) -> str:
    """Where an effective identity file comes from; empty when not known."""
    if identity.origin == "host":
        return _("Set on this host")
    if identity.origin == "default":
        return _("OpenSSH default")
    if identity.origin == "block" and identity.block:
        # {block} is a block header of the SSH configuration, e.g. "Host *".
        return _("From {block}").format(block=identity.block)
    if identity.origin == "block":
        return _("From the top of the SSH configuration")
    return ""


class ConnectionDialogEffectiveMixin:
    """Draft evaluation for the connection editor; see the module docstring."""

    # -- the identity list -------------------------------------------------

    def _build_effective_identity_row(self):
        """The read-only "Keys OpenSSH offers" expander."""
        row = Adw.ExpanderRow(title=_("Keys OpenSSH offers"))
        row.set_subtitle(identity_status_text(WAITING))
        self.effective_identity_row = row
        self._effective_identity_rows = []
        self._effective_identity_shown: Optional[tuple] = None
        return row

    def _show_effective_identities(
        self, state: str, identities: Sequence[EffectiveIdentity]
    ) -> None:
        row = getattr(self, "effective_identity_row", None)
        if row is None:
            return
        row.set_subtitle(identity_status_text(state, len(identities)))
        shown = tuple(identities) if state == PENDING else ()
        if shown == self._effective_identity_shown:
            return
        self._effective_identity_shown = shown
        for child in self._effective_identity_rows:
            row.remove(child)
        self._effective_identity_rows = []
        for identity in shown:
            # A path is not markup: say so before the title is set.
            child = Adw.ActionRow()
            child.set_use_markup(False)
            child.set_title(identity.path)
            child.set_activatable(False)
            child.set_title_selectable(True)
            child.set_subtitle(identity_source_text(identity))
            row.add_row(child)
            self._effective_identity_rows.append(child)
        row.set_enable_expansion(bool(shown))

    # -- asking ------------------------------------------------------------

    def _schedule_effective_refresh(self, delay: int = _REFRESH_DELAY_MS) -> None:
        """Ask OpenSSH about the edit, shortly after the last change."""
        if GLib is None or getattr(self, "_effective_closed", False):
            return
        pending = getattr(self, "_effective_refresh_timer", 0)
        if pending:
            GLib.source_remove(pending)
        self._effective_refresh_timer = GLib.timeout_add(
            max(0, int(delay)), self._run_effective_refresh
        )

    def effective_settled(self) -> bool:
        """No question to OpenSSH is waiting or on its way: every page shows
        the answer for the form as it is (what a user sees after a pause)."""
        if getattr(self, "_effective_refresh_timer", 0):
            return False
        return getattr(self, "_effective_answered", 0) == getattr(self, "_effective_serial", 0)

    def _cancel_effective_refresh(self) -> None:
        pending = getattr(self, "_effective_refresh_timer", 0)
        if pending and GLib is not None:
            GLib.source_remove(pending)
        self._effective_refresh_timer = 0
        self._effective_closed = True
        # An answer still on its way is dropped.
        self._effective_serial = getattr(self, "_effective_serial", 0) + 1

    def _edit_evaluation_request(self):
        """What the save would send, as a question to OpenSSH; ``None`` when
        the form cannot be saved as it stands (no name yet, a refused value):
        there is then nothing to ask about."""
        from . import connection_save_request as save_request
        from .api.connection_identity import connection_id_for
        from .api.models.connections import EvaluateConnectionEditRequest

        try:
            data = self._collect_connection_data()
        except Exception:
            logger.debug("Could not read the form for OpenSSH", exc_info=True)
            return None
        changed = data.pop("__changed_fields", None)
        for key in [key for key in data if key.startswith("__")]:
            data.pop(key)
        without = tuple(sorted(_AUTH_KEYWORDS))
        connection = getattr(self, "connection", None)
        try:
            if bool(getattr(self, "is_editing", False)) and connection is not None:
                delta = changed is not None
                update = None
                if not delta or changed:
                    update = save_request.update_request(
                        data, delta_available=delta, changed_fields=changed or (),
                        expected_generation=None,
                    )
                return EvaluateConnectionEditRequest(
                    connection_id=str(connection_id_for(connection)),
                    update=update, without_own=without,
                )
            if not str(data.get("nickname") or "").strip():
                return None
            return EvaluateConnectionEditRequest(
                create=save_request.create_request(data), without_own=without,
            )
        except (TypeError, ValueError):
            # A value the save would refuse too (validation marks the field).
            return None

    def _run_effective_refresh(self) -> bool:
        self._effective_refresh_timer = 0
        parent = getattr(self, "parent_window", None)
        client = getattr(parent, "client", None)
        try:
            ssh = self._selected_protocol_id() == "ssh"
        except Exception:
            ssh = True
        if not ssh or not (
            callable(getattr(parent, "_daemon_ready", None))
            and parent._daemon_ready()
            and callable(getattr(client, "evaluate_connection_edit", None))
        ):
            return False
        request = self._edit_evaluation_request()
        if request is None:
            self._show_effective_identities(WAITING, ())
            # No host to ask about yet (a new connection without a name):
            # the authentication section starts from OpenSSH's built-in
            # order, so it stays editable; an answer replaces it.
            fallback = getattr(self, "_auth_policy_set_fallback", None)
            if callable(fallback) and not getattr(self, "_effective_draft_answered", False):
                fallback({})
            return False
        section = getattr(self, "auth_policy", None)
        auth_lines = list(section.policy.to_lines()) if section is not None else []
        self._effective_serial = serial = getattr(self, "_effective_serial", 0) + 1

        def current() -> bool:
            return serial == getattr(self, "_effective_serial", 0)

        def apply(result):
            if current():
                self._take_editor_answer(result, auth_lines)
                self._effective_answered = serial
            return False

        def worker():
            result = None
            try:
                result = self._ask_edit(client, request, current)
            except Exception:
                logger.debug("Edit evaluation for the editor failed", exc_info=True)
            if current():
                GLib.idle_add(apply, result)

        threading.Thread(
            target=worker, daemon=True, name="sshpilot-editor-draft"
        ).start()
        return False

    @staticmethod
    def _ask_edit(client, request, current):
        """One edit evaluation; ``None`` when this dialog moved on.

        The daemon lets a client's newest question supersede the one before;
        a question cut short is asked once more, unless this dialog has
        itself moved on to a newer one.
        """
        from .api.errors import ErrorCode, SshPilotError

        for _attempt in range(2):
            if not current():
                return None
            try:
                return client.evaluate_connection_edit(request)
            except SshPilotError as exc:
                if exc.code != ErrorCode.OPERATION_CANCELLED:
                    raise
        return None

    def _take_editor_answer(self, result, auth_lines) -> None:
        """Put OpenSSH's answer on every page of the editor."""
        from .connection_dialog_answer import NO_EFFECT, EditorAnswer

        answer = None if result is None else EditorAnswer(result)
        self._editor_answer = answer
        available = answer is not None and answer.available
        # The Routing page's picture: which of ProxyJump and ProxyCommand
        # OpenSSH uses when a block sets both.
        self._effective_resolved = answer.first() if available else {}
        redraw = getattr(self, "_schedule_route_update", None)
        if callable(redraw):
            redraw()
        section = getattr(self, "auth_policy", None)
        if section is not None:
            policy = section.policy
            if available:
                policy.forced = dict(answer.forced)
                policy.earlier = {
                    keyword: answer.used(keyword)
                    for keyword in _AUTH_KEYWORDS
                    if answer.line_state(keyword)[0] == NO_EFFECT
                }
                policy.set_inherited(answer.first())
                policy.set_draft(answer.first(), answer.inherited_first(), auth_lines)
                self._effective_draft_answered = True
            elif answer is not None and answer.refusal is not None:
                # Not saveable as it stands: keep what was known.
                pass
            else:
                policy.set_lookup_failed()
                self._effective_draft_answered = True
            section.sync()
        identities = None
        if available:
            draft = read_answer(result.evaluation, result.text, result.host)
            identities = draft.identities if draft is not None else None
        self._show_effective_identities(
            PENDING if identities is not None else FAILED, identities or ()
        )
        if available:
            inherited = getattr(self, "_apply_inherited_rows", None)
            if callable(inherited):
                inherited(answer)
        notes = getattr(self, "_apply_override_notes", None)
        if callable(notes):
            notes()
