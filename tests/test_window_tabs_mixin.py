"""Guards for the WindowTabsMixin extraction.

The tab/pane lifecycle methods were moved verbatim out of window.py into
sshpilot/window_tabs.py as a mixin. These checks ensure every method resolves
to the mixin module (so no stray copy in the window.py body silently shadows
it) and that the mixin is wired into the MRO. The hardened teardown behavior is
covered separately by tests/test_fm_tab_teardown.py.
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


# Every method moved into the mixin — the full set, not a sample, so a body
# copy shadowing any one of them via MRO is caught (the dead-duplicate hazard).
_TAB_METHODS = (
    "_on_tab_close_confirmed",
    "_close_tab",
    "_on_tab_bar_pressed",
    "_show_tab_rename_popover",
    "_apply_tab_title",
    "_build_tab_context_menus",
    "_on_tab_bar_secondary_press",
    "_on_tab_setup_menu",
    "_file_manager_embed_for_child",
    "_teardown_file_manager_embed",
    "_teardown_embed_controller",
    "_teardown_all_file_manager_tabs",
    "_enabled_tab_actions",
    "_tab_menu_target",
    "_on_tabmenu_duplicate",
    "_on_tabmenu_rename",
    "_rename_tab_page",
    "_on_tabmenu_reconnect",
    "_on_tabmenu_manage_files",
    "_on_tabmenu_open_system_terminal",
    "_on_tabmenu_new_local",
    "_on_tabmenu_close",
    "_on_tabmenu_close_others",
    "_on_tabmenu_close_right",
    "_apply_split_layout_to_target",
    "_on_tabmenu_layout_horizontal",
    "_on_tabmenu_layout_vertical",
    "_on_tabmenu_layout_default",
    "_on_tabmenu_layout_compact",
    "_on_tabmenu_fm_new_window",
    "_launch_external_file_manager",
    "_bulk_close_target_pages",
    "_count_sessions_in_pages",
    "_run_suppressed_close",
    "_on_bulk_close_response",
    "_confirm_then_bulk_close",
    "on_tab_close",
    "_on_tab_close_response",
    "_on_split_tab_close_response",
    "on_tab_attached",
    "_register_convert_to_split_drop",
    "_update_layout_toggle_state",
    "_apply_tab_layout_mode",
    "_convert_terminal_tab_to_split",
    "_update_tab_button_visibility",
    "on_tab_detached",
    "on_open_split_view_clicked",
    "on_local_terminal_button_clicked",
    "on_tab_button_clicked",
)


def test_tab_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _TAB_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_tabs", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_tabs_mixin_in_mro():
    wm = _window_module()
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowTabsMixin" in mro_names


def test_enabled_tab_actions_for_plugin_like_page():
    """Non-terminal tabs (Docker Console, WebTab, …) get rename/close actions.

    Returning an empty set used to hide the whole context menu because items
    use ``hidden-when=action-disabled``.
    """
    from sshpilot.window_tabs import WindowTabsMixin

    class Stub(WindowTabsMixin):
        def _file_manager_embed_for_child(self, _child):
            return None

    enabled = Stub()._enabled_tab_actions(object())
    assert enabled == {
        "tabmenu-rename",
        "tabmenu-close",
        "tabmenu-close-others",
        "tabmenu-close-right",
    }


class _FakeWidget:
    """Minimal stand-in for Gtk.Widget used by find_tab_page_at."""

    def __init__(self, css_name='', css_classes=(), parent=None, page=None):
        self._css_name = css_name
        self._css_classes = set(css_classes)
        self._parent = parent
        self._page = page

    def get_css_name(self):
        return self._css_name

    def has_css_class(self, name):
        return name in self._css_classes

    def get_parent(self):
        return self._parent

    def get_property(self, name):
        if name == 'page':
            return self._page
        raise AttributeError(name)


def test_find_tab_page_at_returns_page_when_click_on_tab():
    from sshpilot.window_tabs import find_tab_page_at

    page = object()
    tab_bar = types.SimpleNamespace()
    tab = _FakeWidget(css_name='tab', page=page)
    label = _FakeWidget(css_name='label', parent=tab)
    tab_bar.pick = lambda x, y, flags: label

    assert find_tab_page_at(tab_bar, 10, 5) is page


def test_find_tab_page_at_ignores_empty_bar_space():
    from sshpilot.window_tabs import find_tab_page_at

    tab_bar = types.SimpleNamespace()
    empty = _FakeWidget(css_name='tabbox', parent=tab_bar)
    tab_bar.pick = lambda x, y, flags: empty

    assert find_tab_page_at(tab_bar, 200, 5) is None


def test_find_tab_page_at_ignores_close_button():
    from sshpilot.window_tabs import find_tab_page_at

    page = object()
    tab_bar = types.SimpleNamespace()
    tab = _FakeWidget(css_name='tab', page=page, parent=tab_bar)
    close_btn = _FakeWidget(
        css_name='button', css_classes=('tab-close-button',), parent=tab
    )
    icon = _FakeWidget(css_name='image', parent=close_btn)
    tab_bar.pick = lambda x, y, flags: icon

    assert find_tab_page_at(tab_bar, 40, 5) is None


def test_find_tab_page_at_ignores_end_action_widget():
    from sshpilot.window_tabs import find_tab_page_at

    tab_bar = types.SimpleNamespace()
    end_action = _FakeWidget(
        css_name='widget', css_classes=('end-action',), parent=tab_bar
    )
    button = _FakeWidget(css_name='button', parent=end_action)
    tab_bar.pick = lambda x, y, flags: button

    assert find_tab_page_at(tab_bar, 500, 5) is None
