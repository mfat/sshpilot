"""Refreshing Recent must not blank the Start page.

Every daemon event refreshes this page (``Application._handle_api_client_event``
forwards all of them to ``schedule_connection_refresh``), and the read is async.
Clearing to a "Loading…" placeholder up front therefore made the visible Start
page flash *real rows -> placeholder -> real rows* on each event — including the
``CONNECTION_STORE_CHANGED`` a sidebar drag-and-drop reorder emits. The rows must
stay on screen until ``_render_recent_connections`` swaps them in one go.
"""

from types import SimpleNamespace

import pytest

from sshpilot.welcome_page import WelcomePage


class _Box:
    """Gtk.Box double tracking children and the order they were added."""

    def __init__(self, children=()):
        self.children = list(children)

    def get_first_child(self):
        return self.children[0] if self.children else None

    def append(self, child):
        self.children.append(child)

    def remove(self, child):
        self.children.remove(child)


class _Child:
    def __init__(self, label="row"):
        self.label = label
        self._next = None

    def get_next_sibling(self):
        return self._next


def _link(box):
    """Wire up sibling pointers the way _clear_box walks them."""
    for current, following in zip(box.children, box.children[1:] + [None]):
        current._next = following
    return box


class _Request:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Bridge:
    def __init__(self):
        self.submissions = []

    def submit(self, operation, on_success=None, on_error=None):
        self.submissions.append((operation, on_success, on_error))
        return _Request()


def _page(children, *, bridge=None):
    page = WelcomePage.__new__(WelcomePage)
    page._recent_box = _link(_Box(children))
    page._recent_request = None
    page._recent_generation = 0
    page._client_selection_pending = False
    page._closed = False
    page.client = SimpleNamespace(list_connections=lambda: [])
    page.client_bridge = bridge if bridge is not None else _Bridge()
    return page


@pytest.fixture(autouse=True)
def _record_messages(monkeypatch):
    """The GTK stub's Label lacks add_css_class; record the text instead."""
    posted = []

    def _append(box, message, *, warning=False):
        posted.append(message)
        box.append(_Child(message))

    monkeypatch.setattr(WelcomePage, "_append_recent_message", staticmethod(_append))
    return posted


def test_refresh_keeps_existing_rows_on_screen(_record_messages):
    existing = [_Child("alpha"), _Child("beta")]
    page = _page(existing)

    page._populate_recent_box()

    # Still showing what the user was already looking at — no blank frame, no
    # "Loading…" placeholder, while the daemon read is in flight.
    assert page._recent_box.children == existing
    assert _record_messages == []


def test_first_fill_still_shows_the_loading_placeholder(_record_messages):
    page = _page([])

    page._populate_recent_box()

    assert _record_messages == ["Loading recent connections…"]


def test_refresh_still_submits_the_read():
    bridge = _Bridge()
    page = _page([_Child("alpha")], bridge=bridge)

    page._populate_recent_box()

    assert len(bridge.submissions) == 1
    assert page._recent_request is not None


def test_refresh_cancels_a_read_already_in_flight():
    page = _page([_Child("alpha")])
    inflight = _Request()
    page._recent_request = inflight

    page._populate_recent_box()

    assert inflight.cancelled is True


def test_missing_client_clears_and_warns_even_with_rows_showing(_record_messages):
    page = _page([_Child("alpha"), _Child("beta")])
    page.client = None

    page._populate_recent_box()

    # Stale rows must not survive next to an error state.
    assert len(_record_messages) == 1
    assert len(page._recent_box.children) == 1


def test_pending_client_selection_clears_and_shows_loading(_record_messages):
    page = _page([_Child("alpha")])
    page._client_selection_pending = True

    page._populate_recent_box()

    assert _record_messages == ["Loading recent connections…"]
    assert len(page._recent_box.children) == 1


def test_generation_advances_so_a_stale_reply_is_dropped():
    page = _page([_Child("alpha")])

    page._populate_recent_box()
    first = page._recent_generation
    page._populate_recent_box()

    assert page._recent_generation == first + 1
