"""What OpenSSH would use for the connection being edited, edits included.

The dialog's authentication section and its identity-file list show the
lines of one ``Host`` block. Which methods OpenSSH tries and which identity
files it offers also depend on every other block that applies. This mixin
asks OpenSSH about the configuration *with the dialog's pending lines*
(see :mod:`sshpilot.connection_dialog_draft`) and hands the answer to

* the authentication section (:class:`AuthPolicy.set_draft`), so a checkbox
  and the "OpenSSH will try" line agree with OpenSSH before Save, and
* the "Identity files OpenSSH uses" list next to IdentitiesOnly.

A lookup that fails is shown as unknown, never as a default. A host whose
block is in an included file cannot be put into a draft; for it the saved
configuration is shown, and labelled as saved.
"""

from __future__ import annotations

import logging
import threading
from gettext import gettext as _
from typing import List, Optional, Sequence, Tuple

try:
    from gi.repository import Adw, GLib
except Exception:  # pragma: no cover - GTK-less import (headless tests)
    Adw = GLib = None

from .connection_dialog_draft import (
    DraftAnswer,
    EffectiveIdentity,
    build_drafts,
    read_answer,
)

logger = logging.getLogger(__name__)

#: Milliseconds an edit waits before OpenSSH is asked again.
_REFRESH_DELAY_MS = 250

# How the identity list was obtained.
PENDING = "pending"      # the draft: the dialog's lines counted
SAVED = "saved"          # the saved file only
FAILED = "failed"        # OpenSSH was asked and did not answer
WAITING = "waiting"      # no answer yet


def identity_status_text(state: str, dirty: bool) -> str:
    """The caption under "Identity files OpenSSH uses"."""
    if state == FAILED:
        return _("Unknown: OpenSSH could not read the configuration.")
    if state == PENDING and dirty:
        return _("Includes unsaved changes to authentication and keys")
    if state == SAVED and dirty:
        return _("As saved; unsaved changes are not counted")
    if state in (PENDING, SAVED):
        return _("As saved")
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


def identity_lines_for(
    files: Sequence[str], identities_only: str, none_first: bool = False
) -> List[str]:
    """The block's identity lines as a save writes them."""
    lines = []
    if none_first and not files:
        lines.append("IdentityFile none")
    for path in files:
        text = str(path)
        if any(ch.isspace() for ch in text):
            text = f'"{text}"'
        lines.append(f"IdentityFile {text}")
    if identities_only:
        lines.append(f"IdentitiesOnly {identities_only}")
    return lines


class ConnectionDialogEffectiveMixin:
    """Draft evaluation for the connection editor; see the module docstring."""

    # -- the identity list -------------------------------------------------

    def _build_effective_identity_row(self):
        """The read-only "Identity files OpenSSH uses" expander."""
        row = Adw.ExpanderRow(title=_("Identity files OpenSSH uses"))
        row.set_subtitle(identity_status_text(WAITING, False))
        self.effective_identity_row = row
        self._effective_identity_rows = []
        self._effective_identity_shown: Optional[tuple] = None
        return row

    def _show_effective_identities(
        self, state: str, identities: Sequence[EffectiveIdentity], dirty: bool
    ) -> None:
        row = getattr(self, "effective_identity_row", None)
        if row is None:
            return
        row.set_subtitle(identity_status_text(state, dirty))
        shown = tuple(identities) if state in (PENDING, SAVED) else ()
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

    # -- what is pending ---------------------------------------------------

    def _identity_state(self) -> Tuple[Tuple[str, ...], str]:
        try:
            files = tuple(str(p) for p in self._collect_identity_files())
        except Exception:
            files = ()
        try:
            only = self._selected_identities_only()
        except Exception:
            only = ""
        return files, only

    def _capture_effective_baseline(self) -> None:
        """Remember the loaded lines: what differs from them is unsaved."""
        section = getattr(self, "auth_policy", None)
        self._effective_baseline = (
            tuple(section.policy.to_lines()) if section is not None else (),
            self._identity_state(),
        )

    def _pending_identity_lines(self) -> Optional[List[str]]:
        """The identity lines to put in the draft; ``None`` when unchanged
        (the saved lines then stay exactly as written)."""
        baseline = getattr(self, "_effective_baseline", None)
        state = self._identity_state()
        if baseline is not None and baseline[1] == state and self.is_editing:
            return None
        files, only = state
        keeps_none = bool(
            getattr(getattr(self, "connection", None), "identity_file_none", False)
        )
        return identity_lines_for(files, only, none_first=keeps_none)

    # -- asking ------------------------------------------------------------

    def _schedule_effective_refresh(self, delay: int = _REFRESH_DELAY_MS) -> None:
        """Ask OpenSSH about the pending block, shortly after the last edit."""
        if GLib is None or getattr(self, "auth_policy", None) is None:
            return
        pending = getattr(self, "_effective_refresh_timer", 0)
        if pending:
            GLib.source_remove(pending)
        self._effective_refresh_timer = GLib.timeout_add(
            max(0, int(delay)), self._run_effective_refresh
        )

    def _cancel_effective_refresh(self) -> None:
        pending = getattr(self, "_effective_refresh_timer", 0)
        if pending and GLib is not None:
            GLib.source_remove(pending)
        self._effective_refresh_timer = 0
        # An answer still on its way is dropped.
        self._effective_serial = getattr(self, "_effective_serial", 0) + 1

    def _run_effective_refresh(self) -> bool:
        self._effective_refresh_timer = 0
        section = getattr(self, "auth_policy", None)
        parent = getattr(self, "parent_window", None)
        client = getattr(parent, "client", None)
        try:
            ssh = self._selected_protocol_id() == "ssh"
        except Exception:
            ssh = True
        if section is None or not ssh or not (
            callable(getattr(parent, "_daemon_ready", None))
            and parent._daemon_ready()
            and callable(getattr(client, "evaluate_ssh_config_draft", None))
            and callable(getattr(client, "get_ssh_config_text", None))
        ):
            return False
        connection = getattr(self, "connection", None)
        editing = bool(getattr(self, "is_editing", False)) and connection is not None
        if editing:
            host = str(getattr(connection, "nickname", "") or "").strip()
        else:
            # One host is asked about: the first of several names typed.
            typed = getattr(self, "_typed_primary_alias", None)
            if callable(typed):
                host = typed()
            else:
                row = getattr(self, "nickname_row", None)
                host = row.get_text().strip() if row is not None else ""
        if not host:
            self._show_effective_identities(WAITING, (), False)
            return False
        if getattr(self, "_effective_baseline", None) is None:
            self._capture_effective_baseline()
        auth_lines = list(section.policy.to_lines())
        identity_lines = self._pending_identity_lines()
        dirty = (not editing) or identity_lines is not None or (
            tuple(auth_lines) != self._effective_baseline[0]
        )
        self._effective_serial = serial = getattr(self, "_effective_serial", 0) + 1

        def current() -> bool:
            return serial == getattr(self, "_effective_serial", 0)

        def apply(state, effective, inherited, identities):
            if not current():
                return False
            if state == PENDING:
                section.policy.set_draft(
                    effective.first(), inherited.first(), auth_lines
                )
                self._effective_draft_answered = True
                section.sync()
            elif state == FAILED:
                section.policy.set_lookup_failed()
                self._effective_draft_answered = True
                section.sync()
            else:
                # Not a block of the root file: the section keeps what the
                # saved configuration says.
                self._effective_draft_answered = False
            self._show_effective_identities(
                FAILED if identities is None else state, identities or (), dirty
            )
            notes = getattr(self, "_apply_override_notes", None)
            if callable(notes) and state != SAVED:
                notes()
            return False

        def worker():
            state, effective, inherited, identities = FAILED, None, None, None
            try:
                root = client.get_ssh_config_text().text
                drafts = build_drafts(
                    root, host, auth_lines, identity_lines, new=not editing
                )
                if drafts is None:
                    state = SAVED
                    identities = self._saved_effective_identities(client, connection)
                else:
                    effective = self._ask_draft(client, drafts.effective, host, current)
                    if effective is not None and drafts.inherited != drafts.effective:
                        inherited = self._ask_draft(
                            client, drafts.inherited, host, current
                        )
                    else:
                        inherited = effective
                    if effective is not None and inherited is not None:
                        state = PENDING
                        identities = effective.identities
            except Exception:
                logger.debug("Draft evaluation for the editor failed", exc_info=True)
                state, identities = FAILED, None
            if current():
                GLib.idle_add(apply, state, effective, inherited, identities)

        threading.Thread(
            target=worker, daemon=True, name="sshpilot-editor-draft"
        ).start()
        return False

    @staticmethod
    def _ask_draft(client, text: str, host: str, current) -> Optional[DraftAnswer]:
        """One draft evaluation; ``None`` when OpenSSH gives no answer.

        The daemon lets a client's newest evaluation supersede its earlier
        ones, and Advanced Configuration asks through the same client: an
        evaluation it cut short is asked once more, unless this dialog has
        itself moved on to a newer one.
        """
        from .api.errors import ErrorCode, SshPilotError
        from .api.models.connections import EvaluateSshConfigDraftRequest

        request = EvaluateSshConfigDraftRequest(text=text, host=host)
        for _attempt in range(2):
            if not current():
                return None
            try:
                return read_answer(
                    client.evaluate_ssh_config_draft(request), text, host
                )
            except SshPilotError as exc:
                if exc.code != ErrorCode.OPERATION_CANCELLED:
                    raise
        return None

    @staticmethod
    def _saved_effective_identities(
        client, connection
    ) -> Optional[Tuple[EffectiveIdentity, ...]]:
        """``IdentityFile`` values of ``ssh -G`` for the saved host, in order."""
        if connection is None or not callable(
            getattr(client, "get_effective_config", None)
        ):
            return None
        from .api.connection_identity import connection_id_for

        result = client.get_effective_config(connection_id_for(connection))
        if not getattr(result, "available", False):
            return None
        found = []
        for line in getattr(result, "full", ()) or ():
            key, _sep, value = str(line).partition(" ")
            if key.strip().lower() == "identityfile" and value.strip():
                found.append(EffectiveIdentity(value.strip()))
        return tuple(found)
