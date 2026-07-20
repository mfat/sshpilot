"""Field-input helpers for ConnectionDialog (autocomplete & picker popovers).

Extracted verbatim from connection_dialog.py as a mixin to shrink that
god-object. ConnectionDialog inherits this mixin, so ``self`` stays identical and
the move is a pure cut-and-paste with no logic changes. These build the inline
comma-completion and the jump-host / tag picker popovers, plus the
Wake-on-LAN MAC auto-detect button; none of them touch the SSH connection/auth
path.
"""

import logging
import threading

try:
    from gi.repository import Gtk, Gdk, GLib
except (ImportError, AttributeError):  # pragma: no cover - used in tests without GTK
    class _DummyGIMeta(type):
        def __getattr__(cls, name):
            value = _DummyGIMeta(name, (object,), {})
            setattr(cls, name, value)
            return value

        def __call__(cls, *args, **kwargs):
            return object()

    class Gtk(metaclass=_DummyGIMeta):
        pass

    class Gdk(metaclass=_DummyGIMeta):
        pass

    class GLib(metaclass=_DummyGIMeta):
        @staticmethod
        def idle_add(*args, **kwargs):
            return None

from . import wol

# Initialize gettext
try:
    from . import gettext as _
except ImportError:
    # Fallback for when gettext is not available
    _ = lambda s: s

logger = logging.getLogger(__name__)


class ConnectionDialogFieldHelpersMixin:
    def _on_wol_detect_mac_clicked(self, button):
        """Detect MAC from ARP in a background thread and update wol_mac_row."""
        host = (self.hostname_row.get_text() or '').strip()
        if not host:
            self._row_set_message(self.wol_mac_row, _("Enter hostname first"), is_error=True)
            return
        try:
            port_val = int((self.port_row.get_text() or '22').strip() or '22')
        except ValueError:
            port_val = 22
        button.set_sensitive(False)
        mac_row = self.wol_mac_row
        detect_btn = button

        def _detect():
            mac = wol.get_mac_from_arp(host, port=port_val, trigger_first=True)
            GLib.idle_add(_apply_result, mac)

        def _apply_result(mac):
            try:
                detect_btn.set_sensitive(True)
                if mac:
                    mac_row.set_text(mac)
                    self._row_set_message(mac_row, _("MAC detected"), is_error=False)
                else:
                    self._row_set_message(
                        mac_row,
                        _("Not found. Is the host on and on the same subnet?"),
                        is_error=True,
                    )
            except Exception as e:
                logger.debug("WoL detect callback: %s", e)
                detect_btn.set_sensitive(True)

        t = threading.Thread(target=_detect, daemon=True)
        t.start()

    def _show_host_picker_popover(self, button):
        """Show a popover to pick a jump host from the saved connection inventory."""
        if not self.connection_manager:
            return

        current_nickname = getattr(self.connection, 'nickname', '') if self.connection else ''
        candidates = [
            c for c in self.connection_manager.connections
            if c.nickname != current_nickname
        ]
        if not candidates:
            return

        popover = Gtk.Popover()
        popover.set_parent(button)
        popover.set_has_arrow(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_size_request(280, -1)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text(_("Filter hosts…"))
        outer.append(search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, min(300, len(candidates) * 56 + 8))

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class('boxed-list')

        def _make_row(conn):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            lbl_nick = Gtk.Label(label=conn.nickname)
            lbl_nick.set_halign(Gtk.Align.START)
            lbl_nick.add_css_class('heading')
            box.append(lbl_nick)
            host_str = getattr(conn, 'host', '') or getattr(conn, 'hostname', '')
            user_str = getattr(conn, 'username', '')
            subtitle = f"{user_str}@{host_str}" if user_str and host_str else host_str
            if subtitle:
                lbl_host = Gtk.Label(label=subtitle)
                lbl_host.set_halign(Gtk.Align.START)
                lbl_host.add_css_class('caption')
                lbl_host.add_css_class('dim-label')
                box.append(lbl_host)
            row.set_child(box)
            row._conn = conn
            return row

        for c in candidates:
            list_box.append(_make_row(c))

        def _filter_func(row):
            query = search_entry.get_text().lower().strip()
            if not query:
                return True
            conn = getattr(row, '_conn', None)
            if conn is None:
                return False
            host_str = getattr(conn, 'host', '') or getattr(conn, 'hostname', '')
            return query in conn.nickname.lower() or query in host_str.lower()

        list_box.set_filter_func(_filter_func)
        search_entry.connect('search-changed', lambda _e: list_box.invalidate_filter())

        def _on_row_activated(_lb, row):
            conn = getattr(row, '_conn', None)
            if conn is None:
                return
            jump_target = conn.nickname
            current = self.proxy_jump_row.get_text().strip()
            if current:
                self._set_text_without_completion(
                    self.proxy_jump_row, current.rstrip(',') + ',' + jump_target
                )
            else:
                self._set_text_without_completion(self.proxy_jump_row, jump_target)
            popover.popdown()

        list_box.connect('row-activated', _on_row_activated)
        scrolled.set_child(list_box)
        outer.append(scrolled)
        popover.set_child(outer)
        popover.popup()
        search_entry.grab_focus()

    def _tag_candidates(self):
        """Known tags for inline completion in the tags row."""
        cfg = getattr(self.parent_window, 'config', None)
        if cfg is None or not hasattr(cfg, 'get_all_tags'):
            return []
        return [t for t, _n in cfg.get_all_tags()]

    def _jump_host_candidates(self):
        """Saved connection nicknames for inline completion in the jump hosts row."""
        if not self.connection_manager:
            return []
        current = getattr(self.connection, 'nickname', '') if self.connection else ''
        return sorted(
            (c.nickname for c in self.connection_manager.connections
             if c.nickname and c.nickname != current),
            key=str.casefold,
        )

    def _setup_comma_autocomplete(self, row, get_candidates, separator=", "):
        """Install inline completion + Tab-accept on a comma-separated EntryRow.

        Completes the segment being typed at the end of the entry with the
        first candidate (case-insensitive prefix match, already-listed values
        skipped); the suggested suffix is selected so typing replaces it.
        Tab accepts the suggestion and appends *separator* for the next value.
        State lives on the row (row._ac_state); programmatic set_text should
        go through _set_text_without_completion.
        """
        state = {'busy': False, 'prev_len': 0, 'active': False}
        row._ac_state = state

        def on_changed(_editable):
            if state['busy']:
                return
            state['active'] = False
            text = row.get_text()
            prev_len = state['prev_len']
            state['prev_len'] = len(text)
            if len(text) <= prev_len:
                return  # deletion (e.g. backspacing a suggestion) — don't re-complete
            try:
                cursor = row.get_position()
            except Exception:
                return
            from .tag_groups import complete_tag_text
            try:
                result = complete_tag_text(text, cursor, get_candidates())
            except Exception:
                result = None
            if result is None:
                return
            completed, select_start = result
            state['busy'] = True
            try:
                row.set_text(completed)
                # Keep prev_len at the typed text's length so deleting the
                # selected suggestion doesn't immediately re-trigger completion.
                state['prev_len'] = select_start
                row.select_region(select_start, -1)
                state['active'] = True
            except Exception:
                pass
            finally:
                state['busy'] = False

        def on_key_pressed(_controller, keyval, _keycode, _modifier):
            if keyval not in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab):
                return False
            if not state['active']:
                return False
            bounds = row.get_selection_bounds()
            text = row.get_text()
            # Only act while the suggestion (selection reaching the end) is live.
            if not bounds or bounds[1] != len(text):
                state['active'] = False
                return False
            state['busy'] = True
            try:
                new_text = text + separator
                row.set_text(new_text)
                state['prev_len'] = len(new_text)
                row.set_position(len(new_text))
            except Exception:
                return False
            finally:
                state['busy'] = False
            state['active'] = False
            return True

        row.connect('changed', on_changed)
        # Capture phase so Tab wins over the focus chain only when handled.
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect('key-pressed', on_key_pressed)
        row.add_controller(key_ctrl)

    def _set_text_without_completion(self, row, text):
        """Set a row's text without triggering its inline completion."""
        state = getattr(row, '_ac_state', None)
        if state:
            state['busy'] = True
        try:
            row.set_text(text)
            if state:
                state['prev_len'] = len(text)
                state['active'] = False
        finally:
            if state:
                state['busy'] = False

    def _show_tag_picker_popover(self, button):
        """Show a popover to pick from the tags already used by other connections."""
        cfg = getattr(self.parent_window, 'config', None)
        if cfg is None or not hasattr(cfg, 'get_all_tags'):
            return
        try:
            candidates = cfg.get_all_tags()
        except Exception:
            candidates = []
        if not candidates:
            return

        popover = Gtk.Popover()
        popover.set_parent(button)
        popover.set_has_arrow(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_size_request(280, -1)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text(_("Filter tags…"))
        outer.append(search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, min(300, len(candidates) * 44 + 8))

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class('boxed-list')

        def _make_row(tag, count):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            icon = Gtk.Image.new_from_icon_name('tag-symbolic')
            icon.set_valign(Gtk.Align.CENTER)
            box.append(icon)
            lbl = Gtk.Label(label=tag)
            lbl.set_halign(Gtk.Align.START)
            lbl.set_hexpand(True)
            lbl.add_css_class('heading')
            box.append(lbl)
            lbl_count = Gtk.Label(label=_("{n} connections").format(n=count))
            lbl_count.add_css_class('caption')
            lbl_count.add_css_class('dim-label')
            box.append(lbl_count)
            row.set_child(box)
            row._tag = tag
            return row

        for tag, count in candidates:
            list_box.append(_make_row(tag, count))

        def _filter_func(row):
            query = search_entry.get_text().lower().strip()
            if not query:
                return True
            tag = getattr(row, '_tag', None)
            return bool(tag) and query in tag.lower()

        list_box.set_filter_func(_filter_func)
        search_entry.connect('search-changed', lambda _e: list_box.invalidate_filter())

        def _on_row_activated(_lb, row):
            tag = getattr(row, '_tag', None)
            if not tag:
                return
            from .tag_groups import add_tag_to_list
            current = [t.strip() for t in self.tags_row.get_text().split(',') if t.strip()]
            tags, changed = add_tag_to_list(current, tag)
            if changed:
                self._set_text_without_completion(self.tags_row, ', '.join(tags))
            popover.popdown()

        list_box.connect('row-activated', _on_row_activated)
        scrolled.set_child(list_box)
        outer.append(scrolled)
        popover.set_child(outer)
        popover.popup()
        search_entry.grab_focus()
