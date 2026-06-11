"""Tests for the multi-file editor model backing the connection dialog.

The repo's conftest stubs out `gi`, so real Adwaita widgets can't be
instantiated (and the stub gets mutated by other test modules). The editor's
add/remove/order/dedup logic therefore lives in the GTK-free ``PathList``, which
we test directly here. The widget shell (FileListEditor) and the full
load→save round-trip are verified separately with real libadwaita.
"""

from sshpilot.path_list import PathList


class TestPathList:
    def test_set_get_round_trip(self):
        pl = PathList()
        pl.set(["/a/key1", "/a/key2", "/a/key3"])
        assert pl.get() == ["/a/key1", "/a/key2", "/a/key3"]

    def test_set_dedupes_and_trims_and_preserves_order(self):
        pl = PathList()
        pl.set(["/a/k", "/a/k", "/b/k", "  ", "/a/k", ""])
        assert pl.get() == ["/a/k", "/b/k"]

    def test_add_returns_true_only_when_changed(self):
        pl = PathList()
        assert pl.add("/a/k1") is True
        assert pl.add("/a/k1") is False      # duplicate
        assert pl.add("   ") is False         # empty
        assert pl.add("/a/k2") is True
        assert pl.get() == ["/a/k1", "/a/k2"]

    def test_remove(self):
        pl = PathList()
        pl.set(["/a/k1", "/a/k2", "/a/k3"])
        assert pl.remove("/a/k2") is True
        assert pl.remove("/nope") is False
        assert pl.get() == ["/a/k1", "/a/k3"]

    def test_len(self):
        pl = PathList()
        assert len(pl) == 0
        pl.set(["/a", "/b"])
        assert len(pl) == 2

    def test_set_replaces_previous(self):
        pl = PathList()
        pl.set(["/a", "/b"])
        pl.set(["/c"])
        assert pl.get() == ["/c"]

    def test_move_forward_and_backward(self):
        pl = PathList()
        pl.set(["/a", "/b", "/c"])
        assert pl.move("/a", 2) is True
        assert pl.get() == ["/b", "/c", "/a"]
        assert pl.move("/a", 0) is True
        assert pl.get() == ["/a", "/b", "/c"]

    def test_move_clamps_out_of_range_index(self):
        pl = PathList()
        pl.set(["/a", "/b", "/c"])
        assert pl.move("/a", 99) is True
        assert pl.get() == ["/b", "/c", "/a"]
        assert pl.move("/c", -5) is True
        assert pl.get() == ["/c", "/b", "/a"]

    def test_move_returns_false_when_unchanged(self):
        pl = PathList()
        pl.set(["/a", "/b"])
        assert pl.move("/a", 0) is False      # already there
        assert pl.move("/nope", 1) is False   # unknown path
        assert pl.get() == ["/a", "/b"]
