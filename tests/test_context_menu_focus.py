"""Context-menu items must not take focus when clicked.

A clicked item that grabs focus hands it back to the row the popover hangs
off when the menu closes, and a ListBox row taking focus collapses a
multi-selection to that row: right-click → Delete on three selected
connections then showed only one highlighted behind the dialog.

Pure unit test: ``Gtk.Button`` is replaced with a recorder, so no display is
needed. The behaviour itself was measured against the real sidebar.
"""

from sshpilot import context_menu


class RecordingButton:
    instances = []

    def __init__(self):
        self.focus_on_click = True
        RecordingButton.instances.append(self)

    def set_focus_on_click(self, value):
        self.focus_on_click = value

    def add_css_class(self, _name):
        pass

    def set_child(self, _child):
        pass

    def connect(self, *_args):
        pass


class Stub:
    def __init__(self, *_args, **_kwargs):
        pass

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


def test_menu_items_do_not_take_focus_on_click(monkeypatch):
    RecordingButton.instances = []
    monkeypatch.setattr(context_menu.Gtk, 'Button', RecordingButton)
    monkeypatch.setattr(context_menu.Gtk, 'Box', Stub)
    monkeypatch.setattr(context_menu.Gtk, 'Label', Stub)
    monkeypatch.setattr(context_menu.Gtk.Image, 'new_from_icon_name', Stub)
    monkeypatch.setattr(context_menu.Gio.MenuItem, 'new', Stub)
    monkeypatch.setattr(context_menu.GLib, 'Variant', Stub)
    monkeypatch.setattr(context_menu.Pango.AttrList, 'new', Stub)

    menu = context_menu.IconContextMenu()
    menu.add_item('user-trash-symbolic', 'Delete', lambda: None)

    assert len(RecordingButton.instances) == 1
    assert RecordingButton.instances[0].focus_on_click is False
