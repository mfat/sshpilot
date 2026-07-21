"""Monospace font selection dialog.

Extracted verbatim from preferences.py into its own module to shrink that
god-object. This is a self-contained Adw.Window for picking a monospace terminal
font; it depends only on GTK/Pango (no preferences state), so it stands alone and
preferences.py imports it back.

The widget tree is defined declaratively in
``resources/ui/monospace_font_dialog.blp`` (compiled to ``.ui`` and loaded via
``Gtk.Template``); only the dynamic font model, selection wiring, and preview
CSS remain in Python.
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('PangoFT2', '1.0')
from gi.repository import Gtk, Adw, Pango, PangoFT2


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/monospace_font_dialog.ui")
class MonospaceFontDialog(Adw.Window):
    __gtype_name__ = "MonospaceFontDialog"

    search_entry = Gtk.Template.Child()
    font_view = Gtk.Template.Child()
    size_spin = Gtk.Template.Child()
    preview_label = Gtk.Template.Child()

    def __init__(self, parent=None, current_font="Monospace 12"):
        super().__init__()

        # Callers may pass a widget (Settings is a NavigationPage); a transient
        # parent must be the window it lives in.
        from .window_dialogs import parent_window
        self.set_transient_for(parent_window(parent))

        # Store callback
        self.callback = None

        # Parse current font
        self.current_font_desc = Pango.FontDescription.from_string(current_font)

        # Font list model (non-widget state — built imperatively)
        self.font_model = Gtk.ListStore(str, str, object)  # display_name, family, font_desc
        self.font_filter = Gtk.TreeModelFilter(child_model=self.font_model)
        self.font_filter.set_visible_func(self.filter_fonts)
        self.font_view.set_model(self.font_filter)

        name_renderer = Gtk.CellRendererText()
        name_column = Gtk.TreeViewColumn("Font", name_renderer, text=0)
        self.font_view.append_column(name_column)

        # The TreeSelection isn't a template object, so wire it here.
        selection = self.font_view.get_selection()
        selection.connect("changed", self.on_selection_changed)

        self.size_spin.set_value(self.current_font_desc.get_size() / Pango.SCALE)

        self.populate_fonts()

    def populate_fonts(self):
        # Get font map
        fontmap = PangoFT2.FontMap.new()
        families = fontmap.list_families()

        monospace_families = []
        for family in families:
            if family.is_monospace():
                family_name = family.get_name()

                # Get available faces for this family
                faces = family.list_faces()
                for face in faces:
                    face_name = face.get_face_name()

                    # Create font description
                    desc = Pango.FontDescription()
                    desc.set_family(family_name)
                    desc.set_size(12 * Pango.SCALE)

                    # Set style if not regular
                    if face_name.lower() != "regular":
                        if "bold" in face_name.lower():
                            desc.set_weight(Pango.Weight.BOLD)
                        if "italic" in face_name.lower() or "oblique" in face_name.lower():
                            desc.set_style(Pango.Style.ITALIC)

                    # Display name
                    if face_name.lower() == "regular":
                        display_name = family_name
                    else:
                        display_name = f"{family_name} {face_name}"

                    monospace_families.append((display_name, family_name, desc))

        # Sort by family name
        monospace_families.sort(key=lambda x: x[0].lower())

        # Add to model
        for display_name, family_name, desc in monospace_families:
            self.font_model.append([display_name, family_name, desc])

        # Select current font if possible
        self.select_current_font()

    def select_current_font(self):
        current_family = self.current_font_desc.get_family()
        if not current_family:
            return

        iter = self.font_model.get_iter_first()
        while iter:
            family = self.font_model.get_value(iter, 1)
            if family.lower() == current_family.lower():
                # Convert to filter iter
                filter_iter = self.font_filter.convert_child_iter_to_iter(iter)
                if filter_iter[1]:  # Check if conversion was successful
                    selection = self.font_view.get_selection()
                    selection.select_iter(filter_iter[1])

                    # Scroll to selection
                    path = self.font_filter.get_path(filter_iter[1])
                    self.font_view.scroll_to_cell(path, None, False, 0.0, 0.0)
                break
            iter = self.font_model.iter_next(iter)

    def filter_fonts(self, model, iter, data):
        search_text = self.search_entry.get_text().lower()
        if not search_text:
            return True

        font_name = model.get_value(iter, 0).lower()
        return search_text in font_name

    @Gtk.Template.Callback()
    def on_search_changed(self, entry):
        self.font_filter.refilter()

    def on_selection_changed(self, selection):
        model, iter = selection.get_selected()
        if iter:
            font_desc = model.get_value(iter, 2).copy()
            font_desc.set_size(int(self.size_spin.get_value()) * Pango.SCALE)
            self.update_preview(font_desc)

    @Gtk.Template.Callback()
    def on_size_changed(self, spin):
        selection = self.font_view.get_selection()
        model, iter = selection.get_selected()
        if iter:
            font_desc = model.get_value(iter, 2).copy()
            font_desc.set_size(int(spin.get_value()) * Pango.SCALE)
            self.update_preview(font_desc)

    def update_preview(self, font_desc):
        # Remove previous CSS provider if it exists
        if hasattr(self, '_css_provider'):
            context = self.preview_label.get_style_context()
            context.remove_provider(self._css_provider)

        # Create CSS for the font
        css = f"""
        .preview-font {{
            font-family: "{font_desc.get_family()}";
            font-size: {font_desc.get_size() / Pango.SCALE}pt;
            font-weight: normal;
            font-style: normal;
        """

        if font_desc.get_weight() == Pango.Weight.BOLD:
            css += "font-weight: bold;"
        if font_desc.get_style() == Pango.Style.ITALIC:
            css += "font-style: italic;"

        css += "}"

        # Apply CSS
        self._css_provider = Gtk.CssProvider()
        self._css_provider.load_from_data(css.encode())

        context = self.preview_label.get_style_context()
        context.add_provider(self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Ensure the class is added (but only once)
        if not context.has_class("preview-font"):
            context.add_class("preview-font")

    @Gtk.Template.Callback()
    def on_cancel(self, button):
        self.close()

    @Gtk.Template.Callback()
    def on_select(self, button):
        selection = self.font_view.get_selection()
        model, iter = selection.get_selected()
        if iter and self.callback:
            font_desc = model.get_value(iter, 2).copy()
            font_desc.set_size(int(self.size_spin.get_value()) * Pango.SCALE)
            font_string = font_desc.to_string()
            self.callback(font_string)
        self.close()

    def set_callback(self, callback):
        """Set callback function that receives the selected font string"""
        self.callback = callback
