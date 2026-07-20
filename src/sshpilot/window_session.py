"""Session capture/restore for MainWindow.

Extracted verbatim from window.py as a mixin (matching WindowActions /
WindowBroadcastMixin) to shrink the window.py god-object. MainWindow inherits
this; methods keep their signatures and `self.` state access, so this is a pure
code move with no behavior change.

`TerminalWidget` is imported at module level so the isinstance() checks
below use the class object the session tests bind via
``window_session.TerminalWidget`` (window.py only imports it under
``TYPE_CHECKING``).
"""

import logging

from gettext import gettext as _

from .terminal import TerminalWidget

logger = logging.getLogger(__name__)


class WindowSessionMixin:
    """Capture the open tabs to a dict and restore them later."""

    def capture_session(self) -> dict:
        """Capture the current set of open tabs as a serializable session dict.

        Captures SSH terminal tabs (by connection nickname), local terminal
        tabs, and split-view tabs (layout + per-pane connection nicknames), in
        left-to-right tab order. File manager and other tabs are skipped.
        """
        from .split_view import SplitViewTab

        tabs = []
        try:
            n_pages = self.tab_view.get_n_pages()
        except Exception:
            n_pages = 0

        for i in range(n_pages):
            try:
                page = self.tab_view.get_nth_page(i)
            except Exception:
                page = None
            if page is None:
                continue
            if self._is_start_tab_page(page):
                continue
            child = page.get_child()
            if child is None:
                continue

            if isinstance(child, SplitViewTab):
                panes = []
                for pane in getattr(child, '_panes', []):
                    pane_conns = []
                    try:
                        terminals = pane.get_terminals()
                    except Exception:
                        terminals = []
                    for term in terminals:
                        conn = self.terminal_to_connection.get(term)
                        nickname = getattr(conn, 'nickname', None)
                        if nickname:
                            pane_conns.append({'nickname': nickname})
                    # Preserve empty panes only if some pane has terminals
                    panes.append(pane_conns)
                # Drop trailing empty panes so a freshly-restored split (which
                # always starts with two empty panes) round-trips cleanly.
                while panes and not panes[-1]:
                    panes.pop()
                if not any(panes):
                    continue
                tabs.append({
                    'type': 'split',
                    'layout': child.get_layout_mode(),
                    'custom_title': getattr(page, 'custom_tab_title', None),
                    'panes': panes,
                })
                continue

            if isinstance(child, TerminalWidget):
                is_local = False
                try:
                    is_local = bool(child._is_local_terminal())
                except Exception:
                    is_local = False
                if is_local:
                    tabs.append({'type': 'local'})
                    continue
                conn = self.terminal_to_connection.get(child)
                nickname = getattr(conn, 'nickname', None)
                if not nickname:
                    continue
                tabs.append({
                    'type': 'ssh',
                    'nickname': nickname,
                    'custom_title': getattr(page, 'custom_tab_title', None),
                })
                continue

            # File manager tabs / placeholders are intentionally skipped.

        return {'tabs': tabs}

    def _close_all_tabs(self):
        """Close every user tab; the pinned Start tab is kept."""
        self._suppress_close_confirmation = True
        try:
            for page in list(self.tab_view.get_pages()):
                if self._is_start_tab_page(page):
                    continue
                try:
                    self.tab_view.close_page(page)
                except Exception:
                    pass
        finally:
            self._suppress_close_confirmation = False
        self.show_start_tab()

    def _restore_split_tab(self, entry):
        """Recreate a split-view tab from a captured entry."""
        from .split_view import SplitViewTab
        from sshpilot import icon_utils

        panes = entry.get('panes') or []
        svt = SplitViewTab(self)
        page = self.tab_view.append(svt)
        page.set_title(_("Split View"))
        try:
            page.set_icon(icon_utils.new_gicon_from_icon_name('view-dual-symbolic'))
        except Exception:
            pass
        svt._tab_page = page
        layout = entry.get('layout')
        if layout in (SplitViewTab.HORIZONTAL, SplitViewTab.VERTICAL):
            svt.set_layout_mode(layout)

        for pane_index, pane_conns in enumerate(panes):
            if pane_index < len(svt._panes):
                pane = svt._panes[pane_index]
            else:
                pane = svt.add_pane()
            for conn_entry in pane_conns or []:
                nickname = conn_entry.get('nickname') if isinstance(conn_entry, dict) else None
                if not nickname:
                    continue
                connection = self.connection_manager.find_connection_by_nickname(nickname)
                if connection is None:
                    logger.warning(f"Session restore: split connection '{nickname}' not found; skipping")
                    continue
                try:
                    pane.add_connection(connection)
                except Exception as exc:
                    logger.error(f"Failed to restore split connection '{nickname}': {exc}")

        custom_title = entry.get('custom_title')
        if custom_title:
            self._apply_tab_title(page, custom_title)
        self.show_tab_view()
        self.tab_view.set_selected_page(page)

    def restore_session(self, data, replace: bool = True):
        """Recreate tabs from a captured session dict.

        When ``replace`` is True, all currently-open tabs are closed first.
        """
        if not isinstance(data, dict):
            logger.warning("Session restore called with invalid data")
            return
        tabs = data.get('tabs')
        if not isinstance(tabs, list):
            tabs = []

        if replace:
            self._close_all_tabs()

        for entry in tabs:
            if not isinstance(entry, dict):
                continue
            tab_type = entry.get('type')
            try:
                if tab_type == 'local':
                    self.terminal_manager.show_local_terminal()
                elif tab_type == 'split':
                    self._restore_split_tab(entry)
                elif tab_type == 'ssh':
                    nickname = entry.get('nickname')
                    if not nickname:
                        continue
                    connection = self.connection_manager.find_connection_by_nickname(nickname)
                    if connection is None:
                        logger.warning(f"Session restore: connection '{nickname}' not found; skipping")
                        continue
                    self.terminal_manager.connect_to_host(connection, force_new=True)
                    custom_title = entry.get('custom_title')
                    if custom_title:
                        page = self.tab_view.get_selected_page()
                        if page is not None and isinstance(page.get_child(), TerminalWidget):
                            self._apply_tab_title(page, custom_title)
            except Exception as exc:
                logger.error(f"Failed to restore tab {entry!r}: {exc}")
