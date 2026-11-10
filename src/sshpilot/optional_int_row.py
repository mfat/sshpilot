"""Settings rows for options that may be left unset (not injected).

Unset is always the first dropdown choice ("Don't set"), never an off
switch or a bare ``0``. That keeps "not injected" from reading as
"disabled" or as the current SSH config value.

Stored values match the preferences/daemon surface:

* int rows: ``0`` while unset, otherwise the number shown
* bool rows: ``False`` while unset, ``True`` while forced on
* choice rows: ``""`` while unset, otherwise the selected keyword

No text of its own: the caller passes title and dropdown labels so every
user-visible string stays in a file the catalogues already extract.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # noqa: E402


class OptionalIntRow(Adw.ActionRow):
    """Don't-set dropdown + number field; ``value`` is 0 while unset."""

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
        dont_set_label: str,
        custom_label: str,
    ) -> None:
        super().__init__(title=title)
        lower = max(1, int(lower))
        self._syncing = False

        self._spin = Gtk.SpinButton.new_with_range(lower, int(upper), int(step))
        self._spin.set_numeric(True)
        self._spin.set_valign(Gtk.Align.CENTER)
        # The value a freshly enabled option starts from.
        self._spin.set_value(min(max(int(initial), lower), int(upper)))
        self._spin.update_property([Gtk.AccessibleProperty.LABEL], [title])

        model = Gtk.StringList.new([dont_set_label, custom_label])
        self._dropdown = Gtk.DropDown(model=model)
        self._dropdown.set_valign(Gtk.Align.CENTER)
        self._dropdown.set_selected(0)

        self.add_suffix(self._spin)
        self.add_suffix(self._dropdown)
        self.set_activatable_widget(self._dropdown)

        self._spin.connect("value-changed", self._on_changed)
        self._dropdown.connect("notify::selected", self._on_changed)
        self._refresh()

    def get_value(self) -> int:
        return int(self.value)

    def set_value(self, value) -> None:
        """Show *value*; zero or less leaves the option unset."""
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        self._syncing = True
        try:
            if number > 0:
                self._spin.set_value(number)
            self._dropdown.set_selected(1 if number > 0 else 0)
        finally:
            self._syncing = False
        self._refresh()

    def get_enabled(self) -> bool:
        return int(self._dropdown.get_selected()) != 0

    def set_enabled(self, enabled: bool) -> None:
        self._syncing = True
        try:
            self._dropdown.set_selected(1 if enabled else 0)
        finally:
            self._syncing = False
        self._refresh()

    def number_field_visible(self) -> bool:
        return bool(self._spin.get_visible())

    def _on_changed(self, *_args) -> None:
        if not self._syncing:
            self._refresh()

    def _refresh(self) -> None:
        enabled = int(self._dropdown.get_selected()) != 0
        self._spin.set_visible(enabled)
        new_value = int(self._spin.get_value()) if enabled else 0
        if new_value != self.value:
            self.value = new_value


class OptionalBoolRow(Adw.ComboRow):
    """Don't-set / On dropdown; ``active`` is False while unset."""

    __gtype_name__ = "SshPilotOptionalBoolRow"

    active = GObject.Property(type=bool, default=False)

    def __init__(
        self,
        *,
        title: str,
        dont_set_label: str,
        on_label: str,
    ) -> None:
        super().__init__(title=title)
        self._syncing = False
        self.set_model(Gtk.StringList.new([dont_set_label, on_label]))
        self.set_selected(0)
        self.connect("notify::selected", self._on_selected)

    def get_active(self) -> bool:
        return bool(self.active)

    def set_active(self, active) -> None:
        self._syncing = True
        try:
            self.set_selected(1 if active else 0)
        finally:
            self._syncing = False
        self._refresh()

    def _on_selected(self, *_args) -> None:
        if not self._syncing:
            self._refresh()

    def _refresh(self) -> None:
        new_active = int(self.get_selected()) == 1
        if new_active != self.active:
            self.active = new_active


class OptionalChoiceRow(Adw.ComboRow):
    """Don't-set dropdown of keywords; ``value`` is \"\" while unset."""

    __gtype_name__ = "SshPilotOptionalChoiceRow"

    value = GObject.Property(type=str, default="")

    def __init__(
        self,
        *,
        title: str,
        choices: tuple[str, ...] | list[str],
        dont_set_label: str,
    ) -> None:
        super().__init__(title=title)
        self._choices = tuple(choices)
        if not self._choices:
            raise ValueError("OptionalChoiceRow requires at least one choice")
        self._syncing = False
        model = Gtk.StringList.new([dont_set_label, *self._choices])
        self.set_model(model)
        self.set_selected(0)
        self.connect("notify::selected", self._on_selected)

    def get_value(self) -> str:
        return str(self.value or "")

    def set_value(self, value) -> None:
        """Show *value*; empty or unknown leaves the option unset."""
        text = "" if value is None else str(value)
        self._syncing = True
        try:
            if text in self._choices:
                self.set_selected(self._choices.index(text) + 1)
            else:
                self.set_selected(0)
        finally:
            self._syncing = False
        self._refresh()

    def _on_selected(self, *_args) -> None:
        if not self._syncing:
            self._refresh()

    def _refresh(self) -> None:
        idx = int(self.get_selected())
        if idx <= 0 or idx > len(self._choices):
            new_value = ""
        else:
            new_value = self._choices[idx - 1]
        if new_value != self.value:
            self.value = new_value
