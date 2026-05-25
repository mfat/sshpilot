"""Compatibility shim: map removed Adw.MessageDialog to Adw.AlertDialog.

Adw.MessageDialog was deprecated in libadwaita 1.5 and removed in 1.9
(GNOME Platform 50).  Adw.AlertDialog is the supported replacement.

The main API differences are:
  - AlertDialog is not a Gtk.Window; it floats over its parent widget.
  - present(parent) takes the parent as an argument instead of using
    set_transient_for() / set_modal().

This module installs a drop-in replacement class as ``Adw.MessageDialog``
so that existing call-sites work without any changes.  Import this module
once, early in the application's startup sequence, before any UI modules
that reference ``Adw.MessageDialog`` are imported.
"""

from gi.repository import Adw


class _MessageDialogCompat(Adw.AlertDialog):
    """Adw.AlertDialog wrapper that accepts the Adw.MessageDialog constructor API."""

    def __init__(
        self,
        transient_for=None,
        modal=None,
        heading=None,
        body=None,
        title=None,          # Gtk.Window property sometimes passed as heading
        default_response=None,  # was not a real constructor arg; set via method
        **_ignored,
    ):
        effective_heading = heading or title or ""
        super().__init__(heading=effective_heading, body=body or "")
        self._compat_parent = transient_for

    @classmethod
    def new(cls, parent, heading="", body=""):
        return cls(transient_for=parent, heading=heading, body=body)

    # --- MessageDialog surface API that AlertDialog dropped ---

    def set_transient_for(self, parent):
        self._compat_parent = parent

    def get_transient_for(self):
        return getattr(self, "_compat_parent", None)

    def set_modal(self, modal):
        pass  # AlertDialog is always effectively modal

    def set_title(self, title):
        # Adw.MessageDialog exposed set_title() via Gtk.Window; map to set_heading()
        self.set_heading(title)

    # Override present so callers that didn't pass a parent still work
    def present(self, parent=None):
        actual = parent if parent is not None else getattr(self, "_compat_parent", None)
        super().present(actual)


# Patch the live Adw module object so every reference to Adw.MessageDialog
# (attribute lookup on the module) returns our compat class.  This covers
# both inline usage and class-level inheritance such as
# ``class Foo(Adw.MessageDialog):``.
if not hasattr(Adw, "MessageDialog") or not issubclass(
    Adw.MessageDialog, Adw.AlertDialog
):
    Adw.MessageDialog = _MessageDialogCompat
