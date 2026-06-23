"""prettify_path collapses the home dir to ~ for the editor's path subtitle
(uses GLib.get_home_dir() at runtime so it's consistent inside/outside Flatpak)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.text_editor import prettify_path


def test_collapses_path_under_home():
    assert prettify_path("/home/mahdi/.ssh/config", "/home/mahdi") == "~/.ssh/config"


def test_exactly_home_becomes_tilde():
    assert prettify_path("/home/mahdi", "/home/mahdi") == "~"


def test_path_outside_home_unchanged():
    assert prettify_path("/etc/ssh/ssh_config", "/home/mahdi") == "/etc/ssh/ssh_config"


def test_sibling_prefix_not_collapsed():
    # /home/mahdi2 must not be treated as under /home/mahdi
    assert prettify_path("/home/mahdi2/file", "/home/mahdi") == "/home/mahdi2/file"


def test_missing_home_or_path_is_safe():
    assert prettify_path("/home/mahdi/x", "") == "/home/mahdi/x"
    assert prettify_path("/home/mahdi/x", None) == "/home/mahdi/x"
    assert prettify_path("", "/home/mahdi") == ""
