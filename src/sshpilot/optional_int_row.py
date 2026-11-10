"""A settings row for a number that may be left unset.

A bare ``0`` in a spin row reads as "zero seconds" when it means "this option
is not sent at all". This row keeps the two apart: a switch says whether the
option is configured, and the number field exists only while it is.

The row's ``value`` is what callers store: ``0`` while the switch is off,
otherwise the number shown. It mirrors the ``Adw.SpinRow`` surface the
preferences code already uses (``get_value``/``set_value``/``notify::value``),
so an unset option needs no separate handling there.

No text of its own: the caller passes the title and the "unset" subtitle, so
every user-visible string stays in a file the catalogues already extract.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # noqa: E402


class OptionalIntRow(Adw.ActionRow):
    """Switch + number field; ``value`` is 0 while the switch is off."""

    __gtype_name__ = "SshPilotOptionalIntRow"

    value = GObject.Property(type=int, default=0, minimum=0)

    def __init__(
        self,
        *,
        title: str,
        lower: int,
        upper: int,
        step: int = 1,
        initial: int,
        unset_subtitle: str,
    ) -> None:
        super().__init__(title=title)
        lower = max(1, int(lower))
        self._unset_subtitle = unset_subtitle
        self._syncing = False

        self._spin = Gtk.SpinButton.new_with_range(lower, int(upper), int(step))
        self._spin.set_numeric(True)
        self._spin.set_valign(Gtk.Align.CENTER)
        # The value a freshly enabled option starts from.
        self._spin.set_value(min(max(int(initial), lower), int(upper)))
        self._spin.update_property([Gtk.AccessibleProperty.LABEL], [title])

        self._switch = Gtk.Switch()
        self._switch.set_valign(Gtk.Align.CENTER)

        self.add_suffix(self._spin)
        self.add_suffix(self._switch)
        self.set_activatable_widget(self._switch)

        self._spin.connect("value-changed", self._on_changed)
        self._switch.connect("notify::active", self._on_changed)
        self._refresh()

    # -- the Adw.SpinRow-compatible surface ---------------------------------

    def get_value(self) -> int:
        return int(self.value)

    def set_value(self, value) -> None:
        """Show *value*; zero or less switches the option off."""
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        self._syncing = True
        try:
            if number > 0:
                self._spin.set_value(number)
            self._switch.set_active(number > 0)
        finally:
            self._syncing = False
        self._refresh()

    # -- introspection for tests ---------------------------------------------

    def get_enabled(self) -> bool:
        return bool(self._switch.get_active())

    def set_enabled(self, enabled: bool) -> None:
        self._switch.set_active(bool(enabled))

    def number_field_visible(self) -> bool:
        return bool(self._spin.get_visible())

    # -- internals ------------------------------------------------------------

    def _on_changed(self, *_args) -> None:
        if not self._syncing:
            self._refresh()

    def _refresh(self) -> None:
        enabled = self._switch.get_active()
        self._spin.set_visible(enabled)
        self.set_subtitle("" if enabled else self._unset_subtitle)
        new_value = int(self._spin.get_value()) if enabled else 0
        if new_value != self.value:
            self.value = new_value
