"""The two dialogs: New workspace, and a workspace's details.

Widgets only, like page.py; the plugin does the fetching. Logos come through
the page's cache (``page.logo_chip`` and ``page.set_logo``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from . import connections, model  # noqa: E402


def _spinner():
    spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
    spinner.set_valign(Gtk.Align.CENTER)
    return spinner


def _suffix_button(label, callback, *classes):
    btn = Gtk.Button(label=label)
    btn.set_valign(Gtk.Align.CENTER)
    for c in classes:
        btn.add_css_class(c)
    btn.connect("clicked", lambda _b: callback())
    return btn


# --- New workspace -----------------------------------------------------------

def _logo_strip(page, logos, size=18, limit=4):
    box = Gtk.Box(spacing=3)
    box.set_valign(Gtk.Align.CENTER)
    for title, url in logos[:limit]:
        box.append(page.logo_chip(url, title, size))
    if len(logos) > limit:
        more = Gtk.Label(label=f"+{len(logos) - limit}")
        more.add_css_class("ee-more")
        more.set_valign(Gtk.Align.CENTER)
        box.append(more)
    return box


class StackPicker:
    """The popover behind "Add stack": a searchable list of what fits."""

    def __init__(self, editor, button):
        self.editor = editor
        self.popover = Gtk.Popover()
        self.popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        self.search = Gtk.SearchEntry(placeholder_text="Search stacks")
        box.append(self.search)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(320)
        scroller.set_max_content_height(420)
        scroller.set_min_content_width(380)
        # A popover sizes to its child's natural width, which the ellipsized
        # descriptions keep small; ask for the width outright.
        scroller.set_size_request(400, -1)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.add_css_class("navigation-sidebar")
        self.list.set_filter_func(self._filter)
        self.list.set_header_func(self._header)
        self.list.connect("row-activated", self._picked)
        scroller.set_child(self.list)
        box.append(scroller)
        self.empty = Gtk.Label(label="Nothing to add for this recipe.")
        self.empty.add_css_class("dim-label")
        box.append(self.empty)
        self.popover.set_child(box)
        self.search.connect("search-changed", lambda _e: self.list.invalidate_filter())
        self.search.connect("activate", lambda _e: self._pick_first())
        button.set_popover(self.popover)
        button.connect("notify::active", lambda b, _p: b.get_active() and self._fill())

    def _fill(self):
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        self.search.set_text("")
        offered = self.editor.addable()
        for s in offered:
            row = Gtk.ListBoxRow()
            row.stack = s
            line = Gtk.Box(spacing=10)
            line.set_margin_top(4)
            line.set_margin_bottom(4)
            chip = self.editor.page.logo_chip(s["logo"], s["title"], 20)
            chip.add_css_class("ee-stack")
            line.append(chip)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            title = Gtk.Label(label=s["title"], xalign=0)
            title.add_css_class("heading")
            text.append(title)
            desc = Gtk.Label(label=s["description"], xalign=0)
            desc.add_css_class("dim-label")
            desc.add_css_class("caption")
            desc.set_ellipsize(Pango.EllipsizeMode.END)
            desc.set_max_width_chars(48)
            desc.set_hexpand(True)
            text.append(desc)
            line.append(text)
            row.set_child(line)
            self.list.append(row)
        self.empty.set_visible(not offered)
        self.search.grab_focus()

    def _filter(self, row):
        query = self.search.get_text().strip().lower()
        s = row.stack
        return not query or query in f"{s['title']} {s['name']} {s['description']}".lower()

    @staticmethod
    def _header(row, before):
        tag = row.stack["tag"]
        if before is not None and before.stack["tag"] == tag:
            row.set_header(None)
            return
        label = Gtk.Label(label=dict(model.STACK_GROUPS).get(tag, tag.title()), xalign=0)
        label.add_css_class("ee-stack-group")
        label.set_margin_top(8)
        label.set_margin_start(6)
        row.set_header(label)

    def _pick_first(self):
        i = 0
        while (row := self.list.get_row_at_index(i)) is not None:
            if row.get_child_visible() and self._filter(row):
                self._picked(self.list, row)
                return
            i += 1

    def _picked(self, _list, row):
        self.popover.popdown()
        self.editor.add_stack(row.stack)


class MachineEditor:
    """One machine in the New workspace dialog: recipe, size, stacks."""

    def __init__(self, dialog, recipe_index, preset_stacks=()):
        self.dialog = dialog
        self.page = dialog.page
        self.group = Adw.PreferencesGroup()
        self.remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self.remove.add_css_class("flat")
        self.remove.set_tooltip_text("Remove this machine")
        self.remove.connect("clicked", lambda _b: dialog.remove_machine(self))
        self.group.set_header_suffix(self.remove)
        self._rows = []
        # name -> {title, logo, version, vars, stack (catalog item or None)}
        self.chosen = {}

        self.recipe_row = Adw.ComboRow(title="Recipe")
        self.recipe_row.set_factory(dialog.recipe_factory)
        self.recipe_row.set_enable_search(True)
        self.recipe_row.set_expression(
            Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        if hasattr(self.recipe_row, "set_search_match_mode"):
            self.recipe_row.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
        self.recipe_row.set_model(Gtk.StringList.new(dialog.recipe_labels))
        self.recipe_row.set_selected(recipe_index)

        # Size: always in sight, since it is one of the three things a machine is.
        self.size_row = Adw.ActionRow(title="Size")
        self.size_row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
        self.size_button = Gtk.ToggleButton(label="Change")
        self.size_button.set_valign(Gtk.Align.CENTER)
        self.size_button.add_css_class("flat")
        self.size_button.connect("toggled", lambda _b: self._layout())
        self.size_row.add_suffix(self.size_button)
        # On a plan that fixes sizes, as on the dashboard: Change is there but
        # off, and the way out is next to it.
        self.size_locked = Gtk.Box(spacing=6)
        self.size_locked.set_valign(Gtk.Align.CENTER)
        locked_change = Gtk.Button(label="Change")
        locked_change.add_css_class("flat")
        locked_change.set_sensitive(False)
        self.size_locked.append(locked_change)
        upgrade = Gtk.Button(label="Upgrade account")
        upgrade.add_css_class("suggested-action")
        upgrade.set_tooltip_text("Machine sizes are fixed on this account's plan")
        upgrade.connect("clicked", lambda _b: self.page.open_upgrade())
        self.size_locked.append(upgrade)
        self.size_row.add_suffix(self.size_locked)
        self.cpu = Adw.SpinRow.new_with_range(1, 64, 1)
        self.cpu.set_title("vCPU")
        self.ram = Adw.SpinRow.new_with_range(1, 512, 1)
        self.ram.set_title("RAM (GB)")
        self.disk = Adw.SpinRow.new_with_range(1, 2000, 1)
        self.disk.set_title("Disk (GB)")
        for row in (self.cpu, self.ram, self.disk):
            row.connect("notify::value", lambda *_a: self._size_changed())

        self.add_row = Adw.ActionRow(title="Stacks")
        self.add_row.add_prefix(Gtk.Image.new_from_icon_name("view-grid-symbolic"))
        add = Gtk.MenuButton()
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add stack"))
        add.set_valign(Gtk.Align.CENTER)
        self.add_button = add
        self.add_row.add_suffix(add)
        self.add_row.set_activatable_widget(add)
        self.picker = StackPicker(self, add)

        self.recipe_row.connect("notify::selected", lambda *_a: self._recipe_changed())
        self._reset_size()
        by_name = {s["name"]: s for s in dialog.stacks}
        for name, title, vars_ in preset_stacks:
            self._choose(by_name.get(name), name, title, vars_)
        self._layout()
        self._check_recipe()

    # --- layout ---

    def _layout(self):
        for row in self._rows:
            self.group.remove(row)
        rows = [self.recipe_row, self.size_row]
        if self.size_button.get_active() and self.size_button.get_visible():
            rows += [self.cpu, self.ram, self.disk]
        self.size_button.set_label("Done" if self.size_button.get_active() else "Change")
        # The heading row with "Add stack" first, then what has been added.
        rows.append(self.add_row)
        rows += [self._stack_row(name, c) for name, c in self.chosen.items()]
        for row in rows:
            self.group.add(row)
        self._rows = rows
        self.add_button.set_sensitive(bool(self.addable()))
        if not self.dialog.stacks:
            self.add_row.set_subtitle("No stack catalog on this account")
        elif self.chosen:
            count = len(self.chosen)
            self.add_row.set_subtitle(f"{count} added" if count > 1 else "1 added")
        else:
            self.add_row.set_subtitle("Nothing extra yet. Add languages and tools.")

    def _stack_row(self, name, c):
        row = Adw.ActionRow(title=c["title"])
        chip = self.page.logo_chip(c["logo"], c["title"], 22)
        chip.add_css_class("ee-stack")
        row.add_prefix(chip)
        stack = c["stack"]
        if stack and stack["version"]:
            options = stack["version"]["options"]
            version = Gtk.DropDown.new_from_strings(options)
            version.set_valign(Gtk.Align.CENTER)
            version.set_tooltip_text("Version")
            if c["version"] in options:
                version.set_selected(options.index(c["version"]))

            def picked(dd, _pspec, c=c, options=options):
                c["version"] = options[dd.get_selected()]

            version.connect("notify::selected", picked)
            row.add_suffix(version)
        elif stack is None:
            row.set_subtitle("From the template")
        remove = Gtk.Button.new_from_icon_name("window-close-symbolic")
        remove.add_css_class("flat")
        remove.set_valign(Gtk.Align.CENTER)
        remove.set_tooltip_text(f"Remove {c['title']}")
        remove.connect("clicked", lambda _b, name=name: self.remove_stack(name))
        row.add_suffix(remove)
        return row

    # --- recipe ---

    def recipe(self):
        item = self.recipe_row.get_selected_item()
        return self.dialog.recipes_by_label.get(item.get_string()) if item is not None else None

    def _check_recipe(self):
        r = self.recipe()
        if r is not None and not r["available"]:
            self.recipe_row.set_subtitle(r["reason"] or "Not available on this account")
        else:
            self.recipe_row.set_subtitle("")

    def _recipe_changed(self):
        r = self.recipe()
        # Keep the stacks that still fit; a Helm stack does not go on Debian.
        self.chosen = {n: c for n, c in self.chosen.items()
                       if c["stack"] is not None and model.stack_fits(c["stack"], r)}
        self._check_recipe()
        self._reset_size()
        self._layout()
        self.dialog.changed()

    # --- stacks ---

    def addable(self):
        r = self.recipe()
        return [s for s in self.dialog.stacks
                if s["name"] not in self.chosen and (r is None or model.stack_fits(s, r))]

    def _choose(self, stack, name, title, vars_=None):
        vars_ = dict(vars_ or {})
        version = ""
        if stack and stack["version"]:
            version = str(vars_.get(stack["version"]["key"]) or stack["version"]["default"])
        self.chosen[name] = {
            "title": stack["title"] if stack else title,
            "logo": stack["logo"] if stack else model.stack_logo(name, self.dialog.web),
            "version": version, "vars": vars_, "stack": stack,
        }

    def add_stack(self, stack):
        self._choose(stack, stack["name"], stack["title"])
        self._layout()
        self.dialog.changed()

    def remove_stack(self, name):
        self.chosen.pop(name, None)
        self._layout()
        self.dialog.changed()

    # --- size ---

    def _reset_size(self):
        recipe = self.recipe() or {}
        size = recipe.get("size") or model.recipe_size(None)
        limits = self.dialog.limits
        ram_min = max(1, -(-size["ram_mb"] // 1024))
        caps = ((self.cpu, size["cpu"], limits["cpu"]),
                (self.ram, ram_min, limits["ram_mb"] // 1024),
                (self.disk, size["storage_gb"], limits["storage_gb"]))
        # The backend's rule: a recipe that needs more than the cap keeps what
        # it needs and goes no further.
        growable = limits["custom"] and any(high > low for _r, low, high in caps)
        for row, low, high in caps:
            adj = row.get_adjustment()
            adj.set_lower(low)
            adj.set_upper(max(low, high) if limits["custom"] else low)
            row.set_value(low)
        self.size_button.set_visible(growable)
        self.size_locked.set_visible(not limits["custom"])
        if not growable:
            self.size_button.set_active(False)
        self._size_changed()

    def size(self):
        return {"cpu": int(self.cpu.get_value()),
                "ram_mb": int(self.ram.get_value()) * 1024,
                "storage_gb": int(self.disk.get_value())}

    def _default_size(self):
        default = (self.recipe() or {}).get("size") or model.recipe_size(None)
        return {"cpu": default["cpu"],
                "ram_mb": max(1, -(-default["ram_mb"] // 1024)) * 1024,
                "storage_gb": default["storage_gb"]}

    def _size_changed(self):
        size = self.size()
        same = size == self._default_size()
        if not self.dialog.limits["custom"]:
            self.size_row.set_subtitle(model.size_text(size) + "  (fixed on this plan)")
            return
        self.size_row.set_subtitle(model.size_text(size) + ("  (recipe default)" if same else ""))

    def spec(self):
        r = self.recipe()
        size = self.size()
        if size == self._default_size():
            size = None       # unchanged: let the recipe decide, and the plan not be asked
        stacks = []
        for name, c in self.chosen.items():
            vars_ = dict(c["vars"])
            if c["stack"] and c["stack"]["version"]:
                vars_[c["stack"]["version"]["key"]] = c["version"]
            stacks.append((name, vars_))
        return {"recipe": r, "stacks": stacks, "size": size}

    def ok(self):
        r = self.recipe()
        return r is not None and r["available"]


class TemplateGallery:
    """Step one: "Start from", the dashboard's template gallery as tiles.

    It has the dialog to itself, so the grid is as tall as the dialog and
    scrolls with it. A tile click moves on to the machines.
    """

    def __init__(self, dialog):
        self.dialog = dialog
        self.page = dialog.page
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.box.set_margin_top(18)
        self.box.set_margin_bottom(24)
        self.box.set_margin_start(18)
        self.box.set_margin_end(18)

        self.search = Gtk.SearchEntry(placeholder_text="Search templates, recipes and stacks")
        self.search.set_sensitive(False)
        self.search.connect("search-changed", lambda _e: self._filter())
        self.box.append(self.search)

        self.strip = Gtk.FlowBox()
        self.strip.set_selection_mode(Gtk.SelectionMode.NONE)
        self.strip.set_homogeneous(True)
        self.strip.set_min_children_per_line(3)
        self.strip.set_max_children_per_line(3)
        self.strip.set_column_spacing(10)
        self.strip.set_row_spacing(10)
        self.strip.set_valign(Gtk.Align.START)
        self.strip.set_filter_func(self._visible)
        self.box.append(self.strip)
        loading = Gtk.Box(spacing=8)
        loading.append(_spinner())
        loading.append(Gtk.Label(label="Loading templates"))
        self.strip.append(loading)
        self.tiles = []            # (button, template or None, haystack)
        self.empty = Gtk.Label(label="No template matches.")
        self.empty.add_css_class("dim-label")
        self.empty.set_visible(False)
        self.box.append(self.empty)

    def fill(self, templates):
        self.strip.remove_all()
        first = self._tile(None, None)
        for t in templates:
            self._tile(t, first)
        self.search.set_sensitive(bool(templates))
        self.search.grab_focus()

    def _tile(self, template, group):
        button = Gtk.ToggleButton()
        button.add_css_class("ee-tile")
        if group is not None:
            button.set_group(group)
        button.set_size_request(140, 100)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        if template is None:
            icon = Gtk.Image.new_from_icon_name("document-new-symbolic")
            icon.set_pixel_size(22)
            icon.set_halign(Gtk.Align.START)
            body.append(icon)
            title, sub, haystack = "Blank", "Your own machines", ""
        else:
            body.append(_logo_strip(self.page, template["logos"], 20, 4))
            count = len(template["machines"])
            title = template["title"]
            sub = f"{count} machine" + ("" if count == 1 else "s")
            if template["owned"]:
                sub += "  ·  yours"
            haystack = " ".join([template["title"], template["description"]]
                                + [t for t, _u in template["logos"]]).lower()
            button.set_tooltip_text(template["description"] or template["title"])
        name = Gtk.Label(label=title, xalign=0)
        name.add_css_class("heading")
        name.set_wrap(True)
        name.set_lines(2)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(13)
        name.set_width_chars(10)
        body.append(name)
        caption = Gtk.Label(label=sub, xalign=0)
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")
        body.append(caption)
        button.set_child(body)
        # "clicked", not "toggled": picking the tile that is already chosen
        # (after going back) must still move on.
        button.connect("clicked", self._clicked, template)
        self.strip.append(button)
        button.get_parent().set_focusable(False)
        button.get_parent().tile = (template, haystack)
        self.tiles.append((button, template, haystack))
        return button

    def _clicked(self, button, template):
        button.set_active(True)
        self.dialog.template_picked(template)

    def _query(self):
        return self.search.get_text().strip().lower()

    def _visible(self, child):
        template, haystack = getattr(child, "tile", (None, ""))
        query = self._query()
        return template is None or not query or query in haystack

    def _filter(self):
        self.strip.invalidate_filter()
        query = self._query()
        shown = sum(1 for _b, t, h in self.tiles if t is not None and (not query or query in h))
        self.empty.set_visible(bool(query) and not shown)


class NewWorkspaceDialog:
    """Two steps: pick a start (a template, or blank), then the machines.

    One long page with the gallery on top pushed the machines below the
    fold; now the gallery gets the whole dialog, and once something is
    picked it folds into one row above the machines.
    """

    def __init__(self, page):
        self.page = page
        state = page.state or {}
        account = state.get("account") or {}
        self.limits = account.get("limits") or dict(model.FALLBACK_LIMITS)
        self.max_machines = min(self.limits.get("max_machines") or model.MAX_MACHINES,
                                model.MAX_MACHINES)
        self.web = page.plugin.web_url()
        self.recipes_by_label = {}
        self.recipe_labels = []
        self.default_index = 0
        self.stacks = []
        self.templates = []
        self.template = None
        self.machines = []
        self._applying = False
        self._ready = False

        self.dialog = Adw.Dialog(title="New workspace", content_width=620, content_height=780)
        self.nav = Adw.NavigationView()
        self.dialog.set_child(self.nav)

        # --- step one: start from ---
        start = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.dialog.close())
        header.pack_start(cancel)
        start.add_top_bar(header)
        self.gallery = TemplateGallery(self)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.gallery.box)
        start.set_content(scroller)
        self.start_page = Adw.NavigationPage(title="Start from", tag="start")
        self.start_page.set_child(start)
        self.nav.add(self.start_page)

        # --- step two: machines ---
        form = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        self.create = Gtk.Button(label="Create and start")
        self.create.add_css_class("suggested-action")
        self.create.set_sensitive(False)
        self.create.connect("clicked", lambda _b: self._create())
        header.pack_end(self.create)
        form.add_top_bar(header)

        self.prefs = Adw.PreferencesPage()
        chosen = Adw.PreferencesGroup()
        self.chosen_row = Adw.ActionRow(title="Blank")
        self.chosen_logos = Gtk.Box()
        self.chosen_logos.set_valign(Gtk.Align.CENTER)
        self.chosen_row.add_prefix(self.chosen_logos)
        change = Gtk.Button(label="Change")
        change.add_css_class("flat")
        change.set_valign(Gtk.Align.CENTER)
        change.connect("clicked", lambda _b: self.nav.pop())
        self.chosen_row.add_suffix(change)
        self.chosen_row.set_activatable_widget(change)
        chosen.add(self.chosen_row)
        self.prefs.add(chosen)

        about = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.connect("changed", lambda _r: self.changed())
        self.duration_row = Adw.ComboRow(title="Stops after")
        self.duration_row.set_model(Gtk.StringList.new([d[0] for d in model.DURATIONS]))
        about.add(self.name_row)
        about.add(self.duration_row)
        self.about = about
        self.prefs.add(about)

        self.add_group = Adw.PreferencesGroup()
        self.add_button = Gtk.Button()
        self.add_button.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                                    label="Add another machine"))
        self.add_button.add_css_class("pill")
        self.add_button.set_halign(Gtk.Align.CENTER)
        self.add_button.connect("clicked", lambda _b: self.add_machine())
        self.add_group.add(self.add_button)
        self.prefs.add(self.add_group)

        form.set_content(self.prefs)
        self.form_page = Adw.NavigationPage(title="Machines", tag="machines")
        self.form_page.set_child(form)

        self.recipe_factory = Gtk.SignalListItemFactory()
        self.recipe_factory.connect("setup", self._setup_item)
        self.recipe_factory.connect("bind", self._bind_recipe)
        self._note_text()

    def present(self):
        self.dialog.present(self.page.root)
        self.page.plugin.load_catalog(self._loaded)

    def _note_text(self):
        credit = ((self.page.state or {}).get("account") or {}).get("credit") or {}
        text = ("Machines are reached through EasyEnv's SSH gateway with your profile's "
                "SSH key. The workspace stops by itself when its time is up.")
        if credit.get("state") == "unlimited":
            text += " This account has unlimited hours."
        elif credit.get("state") == "empty":
            text += " This account has no hours left, so the workspace will not start."
        elif credit.get("text"):
            text += f" This account has {credit['text']}."
        self.about.set_description(text)

    # --- list items ---

    @staticmethod
    def _setup_item(_f, item):
        box = Gtk.Box(spacing=8)
        image = Gtk.Image()
        chip = Gtk.Box()
        chip.add_css_class("ee-logo")
        chip.append(image)
        box.append(chip)
        text = Gtk.Label(xalign=0)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(text)
        box.image, box.text = image, text
        item.set_child(box)

    def _bind_recipe(self, _f, item):
        box = item.get_child()
        label = item.get_item().get_string()
        r = self.recipes_by_label.get(label) or {}
        box.text.set_text(label)
        self.page.set_logo(box.image, r.get("logo", ""), 18)

    # --- loading ---

    def _loaded(self, recipes, stacks, templates, error):
        if error:
            self._stop(f"Could not load recipes: {error}")
            return
        ordered = model.recipe_choices(recipes or [])
        known = {r["uuid"] for r in ordered}
        # A template may use a recipe the plain list leaves out.
        for t in templates or []:
            for m in t["machines"]:
                if m["recipe"]["uuid"] not in known:
                    ordered.append(m["recipe"])
                    known.add(m["recipe"]["uuid"])
        self._label_of = {}
        for r in ordered:
            label = r["title"] if r["available"] else f"{r['title']} (unavailable)"
            if label in self.recipes_by_label:
                label = f"{label} ({r['uuid']})"
            self.recipes_by_label[label] = r
            self.recipe_labels.append(label)
            self._label_of[r["uuid"]] = len(self.recipe_labels) - 1
        self.default_index = model.default_choice(ordered)
        self.stacks = stacks or []
        self.templates = templates or []
        if not self.recipe_labels:
            self._stop("No recipes on this account yet. Add one on the dashboard first.")
            return
        self._ready = True
        self.gallery.fill(self.templates)

    def _stop(self, text):
        self.gallery.strip.remove_all()
        label = Gtk.Label(label=text, wrap=True)
        label.add_css_class("dim-label")
        self.gallery.strip.append(label)

    # --- templates ---

    def template_picked(self, t):
        """A tile was clicked: lay out its machines and move to step two.

        Picking again always starts over from the template (or from one
        blank machine); anything edited before going back is dropped, as the
        choice says it should be.
        """
        if not self._ready:
            return
        previous = self.template
        self.template = t
        name = self.name_row.get_text().strip()
        if t is not None and (not name or (previous and name == previous["title"])):
            self.name_row.set_text(t["title"])
        elif t is None and previous and name == previous["title"]:
            self.name_row.set_text("")
        self._applying = True
        for m in list(self.machines):
            self.remove_machine(m)
        if t is None:
            self.add_machine(self.default_index)
        else:
            for m in t["machines"]:
                self.add_machine(self._label_of.get(m["recipe"]["uuid"], self.default_index),
                                 m["stacks"])
        self._applying = False
        self._show_choice()
        self.changed()
        self.nav.push(self.form_page)

    def _show_choice(self):
        child = self.chosen_logos.get_first_child()
        if child is not None:
            self.chosen_logos.remove(child)
        t = self.template
        if t is None:
            icon = Gtk.Image.new_from_icon_name("document-new-symbolic")
            self.chosen_logos.append(icon)
            self.chosen_row.set_title("Blank workspace")
            self.chosen_row.set_subtitle("Machines of your choosing")
            self.form_page.set_title("New workspace")
            return
        self.chosen_logos.append(_logo_strip(self.page, t["logos"], 20, 4))
        self.chosen_row.set_title(GLib.markup_escape_text(t["title"]))
        self.form_page.set_title(t["title"])

    def _template_still_applies(self):
        """The machines are still the template's recipes, in its order; the
        dashboard drops the template link once they are edited, and so does
        this."""
        t = self.template
        if t is None:
            return False
        picked = [(m.recipe() or {}).get("uuid") for m in self.machines]
        return picked == [m["recipe"]["uuid"] for m in t["machines"]]

    # --- machines ---

    def add_machine(self, index=None, stacks=()):
        if index is None:
            index = (self.machines[-1].recipe_row.get_selected() if self.machines
                     else self.default_index)
        editor = MachineEditor(self, index, stacks)
        self.machines.append(editor)
        # Keep "Add another machine" last.
        self.prefs.remove(self.add_group)
        self.prefs.add(editor.group)
        self.prefs.add(self.add_group)
        self._renumber()
        self.changed()

    def remove_machine(self, editor):
        self.prefs.remove(editor.group)
        self.machines.remove(editor)
        self._renumber()
        self.changed()

    def _renumber(self):
        for i, m in enumerate(self.machines):
            m.group.set_title(f"Machine {i + 1}")
            m.remove.set_visible(len(self.machines) > 1)
        self.add_button.set_sensitive(len(self.machines) < self.max_machines)

    def changed(self):
        if self._applying or not hasattr(self, "create"):
            return
        too_many = len(self.machines) > self.max_machines
        if too_many:
            self.add_group.set_description(
                f"This account's plan allows {self.max_machines} machines per workspace.")
        else:
            self.add_group.set_description(None)
        ok = (bool(self.name_row.get_text().strip()) and bool(self.machines)
              and not too_many and all(m.ok() for m in self.machines))
        self.create.set_sensitive(ok)
        if self.template is not None:
            self.chosen_row.set_subtitle(GLib.markup_escape_text(
                model.template_summary(self.template) if self._template_still_applies()
                else "Changed, so it is created as a plain workspace"))

    def _create(self):
        specs = [m.spec() for m in self.machines]
        d = model.DURATIONS[max(0, self.duration_row.get_selected())]
        template = self.template["uuid"] if self._template_still_applies() else None
        self.dialog.close()
        self.page.plugin.create(self.name_row.get_text().strip(), specs, d[1], d[2], template)


# --- Details -----------------------------------------------------------------

class DetailsDialog:
    """What a workspace is made of: each machine's recipe, size and stacks."""

    def __init__(self, page, view):
        self.page = page
        self.view = view
        self.dialog = Adw.Dialog(title=view["title"], content_width=560, content_height=640)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.prefs = Adw.PreferencesPage()
        toolbar.set_content(self.prefs)
        self.dialog.set_child(toolbar)

        summary = Adw.PreferencesGroup()
        now = datetime.now(timezone.utc)
        parts = [view["label"], model.time_text(view, now), model.subtitle(view, now)]
        summary.set_description("  ·  ".join(p for p in parts if p))
        self.prefs.add(summary)

        self.loading = Adw.PreferencesGroup()
        row = Adw.ActionRow(title="Loading machines")
        row.add_suffix(_spinner())
        self.loading.add(row)
        self.prefs.add(self.loading)

    def present(self):
        self.dialog.present(self.page.root)
        self.page.plugin.details(self.view, self._loaded)

    def _loaded(self, fresh, error):
        self.prefs.remove(self.loading)
        if error:
            group = Adw.PreferencesGroup()
            group.set_description(f"Could not load the machines: {error}")
            self.prefs.add(group)
            return
        running = fresh["state"] == model.RUNNING
        for m in fresh["machines"]:
            self.prefs.add(self._machine(fresh, m, running))

    def _machine(self, fresh, m, running):
        group = Adw.PreferencesGroup(title=m["title"])
        if running and m["state"] == model.RUNNING:
            buttons = Gtk.Box(spacing=6)
            copy = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
            copy.add_css_class("flat")
            copy.set_tooltip_text("Copy ssh command")
            copy.set_valign(Gtk.Align.CENTER)
            copy.connect("clicked", lambda _b: self.page.copy_command(m))
            buttons.append(copy)

            def ssh():
                self.dialog.close()
                self.page.plugin.ssh(fresh, m)

            buttons.append(_suffix_button("SSH", ssh, "suggested-action"))
            group.set_header_suffix(buttons)
        else:
            group.set_description(model.LABELS[m["state"]])

        recipe = Adw.ActionRow(title=m["recipe"] or "Recipe")
        recipe.set_subtitle(model.size_text(m["size"]) or "Size not reported")
        recipe.add_prefix(self.page.logo_chip(m["logo"], m["recipe"], 24))
        group.add(recipe)

        host = Adw.ActionRow(title=f"{m['user']}@{connections.host(m['uuid'])}")
        host.set_subtitle("Through ssh.easyenv.io with your profile's SSH key")
        host.add_css_class("property")
        host.add_prefix(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        group.add(host)

        if m["stacks"]:
            for s in m["stacks"]:
                row = Adw.ActionRow(title=s["title"])
                if s["version"]:
                    row.set_subtitle(f"Version {s['version']}")
                chip = self.page.logo_chip(s["logo"], s["title"], 20)
                chip.add_css_class("ee-stack")
                row.add_prefix(chip)
                group.add(row)
        else:
            row = Adw.ActionRow(title="No stacks", subtitle="Only what the recipe brings")
            row.add_prefix(Gtk.Image.new_from_icon_name("view-grid-symbolic"))
            group.add(row)
        return group
