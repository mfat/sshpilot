"""A detached RDP tab: the live session moved into a window of its own.

The tab's page is transferred between Adw.TabViews (``transfer_page``), so
the gtk4-frdp widget and its session move as they are -- nothing reconnects.
"Move Back to Tab" transfers it back into the main window. Closing this
window ends the session, as closing the tab would. The window itself is the
generic :class:`DetachedTabWindow`, shared with file manager tabs.
"""

from __future__ import annotations

from .detached_tab_window import DetachedTabWindow


class RdpWindow(DetachedTabWindow):
    """Holds one detached RDP tab."""
