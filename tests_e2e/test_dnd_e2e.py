"""End-to-end test for the drag-and-drop demo application."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

dogtail = pytest.importorskip("dogtail.tree")
from dogtail import predicate, rawinput, tree


@pytest.mark.e2e
def test_drag_row_one_below_row_four(tmp_path):
    script = Path(__file__).with_name("dnd_sample_app.py")
    env = os.environ.copy()
    env.setdefault("GDK_BACKEND", "x11")

    process = subprocess.Popen([sys.executable, str(script)], env=env)
    try:
        window = _wait_for_window("DnD Demo")

        list_node = window.child(roleName="list")
        row1 = list_node.child(predicate.GenericPredicate(name="Row 1", roleName="list item"))
        row4 = list_node.child(predicate.GenericPredicate(name="Row 4", roleName="list item"))

        row1.drag(row4)
        time.sleep(0.8)

        names = [child.name for child in list_node.children if child.roleName == "list item"]
        assert names[-1] == "Row 1"
        assert names[:-1] == ["Row 2", "Row 3", "Row 4"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _wait_for_window(title: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return tree.root.child(name=title, roleName="frame")
        except tree.SearchError:
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for window '{title}'")
