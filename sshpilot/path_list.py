"""GTK-free helper for the connection dialog's multi-file editors.

Kept in its own module (no ``gi`` imports) so the add/remove/order/dedup logic
is unit-testable under the test suite's stubbed ``gi`` environment, independent
of any libadwaita widget.
"""


class PathList:
    """Ordered, de-duplicated list of file paths.

    Backs ``FileListEditor`` (IdentityFile/CertificateFile lists); the widget
    mirrors this model into Adwaita rows for display.
    """

    def __init__(self):
        self._items = []

    def set(self, paths):
        self._items = []
        for p in (paths or []):
            self.add(p)

    def add(self, path) -> bool:
        path = str(path or '').strip()
        if not path or path in self._items:
            return False
        self._items.append(path)
        return True

    def remove(self, path) -> bool:
        if path in self._items:
            self._items.remove(path)
            return True
        return False

    def get(self):
        return list(self._items)

    def __len__(self):
        return len(self._items)
