"""Settings rows for options that may be left unset (not injected).

Unset is always the first dropdown choice ("Not set"), never an off
switch or a bare ``0``. That keeps "not injected" from reading as
"disabled" or as the current SSH config value.

Stored values match the preferences/daemon surface:

* int rows: ``0`` while unset, otherwise the number shown
* choice rows: ``""`` while unset, otherwise the selected keyword

No text of its own: the caller passes title and dropdown labels so every
user-visible string stays in a file the catalogues already extract.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # noqa: E402


class OptionalIntRow(Adw.ComboRow):
    """Not-set dropdown + number field; ``value`` is 0 while unset.

    A combo row, like :class:`OptionalChoiceRow`, so both kinds of row draw
    the same dropdown. The number field sits just before it.
    """

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

        self.set_model(Gtk.StringList.new([dont_set_label, custom_label]))
        self.set_selected(0)
        # The popup keeps both choices, but the row itself shows no label
        # once set: the number beside the arrow already says so.
        self._custom_label = custom_label
        self.set_list_factory(self.get_factory())
        shown = Gtk.SignalListItemFactory()
        shown.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=0)))
        shown.connect("bind", self._bind_shown_choice)
        self.set_factory(shown)

        self.add_suffix(self._spin)
        # add_suffix appends after the row's own dropdown; move the number
        # field ahead of it so every row's arrow lines up on the right.
        box = self._spin.get_parent()
        if isinstance(box, Gtk.Box):
            box.reorder_child_after(self._spin, None)

        self._spin.connect("value-changed", self._on_changed)
        self.connect("notify::selected", self._on_changed)
        self._refresh()

    def _bind_shown_choice(self, _factory, item) -> None:
        text = item.get_item().get_string()
        item.get_child().set_label("" if text == self._custom_label else text)

    def shown_choice_text(self) -> str:
        """The label the row displays for the current choice (for tests)."""
        return "" if self.get_enabled() else self.get_selected_item().get_string()

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
            self.set_selected(1 if number > 0 else 0)
        finally:
            self._syncing = False
        self._refresh()

    def get_enabled(self) -> bool:
        return int(self.get_selected()) != 0

    def set_enabled(self, enabled: bool) -> None:
        self._syncing = True
        try:
            self.set_selected(1 if enabled else 0)
        finally:
            self._syncing = False
        self._refresh()

    def number_field_visible(self) -> bool:
        return bool(self._spin.get_visible())

    def _on_changed(self, *_args) -> None:
        if not self._syncing:
            self._refresh()

    def _refresh(self) -> None:
        enabled = int(self.get_selected()) != 0
        self._spin.set_visible(enabled)
        new_value = int(self._spin.get_value()) if enabled else 0
        if new_value != self.value:
            self.value = new_value


class OptionalChoiceRow(Adw.ComboRow):
    """Not-set dropdown of keywords; ``value`` is \"\" while unset."""

    __gtype_name__ = "SshPilotOptionalChoiceRow"

    value = GObject.Property(type=str, default="")

    def __init__(
        self,
        *,
        title: str,
        choices: tuple[str, ...] | list[str],
        dont_set_label: str,
        labels: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        super().__init__(title=title)
        self._choices = tuple(choices)
        if not self._choices:
            raise ValueError("OptionalChoiceRow requires at least one choice")
        self._syncing = False
        # ``labels`` word the choices for people when the stored keyword alone
        # would not say much ("inet"); the value stays the keyword.
        shown = tuple(labels) if labels is not None else self._choices
        if len(shown) != len(self._choices):
            raise ValueError("OptionalChoiceRow needs one label per choice")
        model = Gtk.StringList.new([dont_set_label, *shown])
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


class SwitchExpanderRow(Adw.ExpanderRow):
    """A switch whose own settings fold out underneath it.

    ``active`` mirrors the expander's enable switch, so the row reads and
    notifies like an ``Adw.SwitchRow`` (``get_active``/``set_active``/
    ``notify::active``) while the nested rows are only reachable when on.
    """

    __gtype_name__ = "SshPilotSwitchExpanderRow"

    active = GObject.Property(type=bool, default=True)

    def __init__(self, *, title: str) -> None:
        super().__init__(title=title)
        self.set_show_enable_switch(True)
        self.bind_property(
            "active",
            self,
            "enable-expansion",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )

    def get_active(self) -> bool:
        return bool(self.active)

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
