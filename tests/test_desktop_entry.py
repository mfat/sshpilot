"""The shipped desktop entry must be matchable to the app's own windows.

GNOME Shell decides that a window belongs to this application by comparing the
window's WM_CLASS -- on Wayland that is the ``xdg_toplevel.set_app_id`` string,
which GTK takes from the GApplication id -- against ``StartupWMClass`` first,
and only then against the ``<wm_class>.desktop`` guess. When nothing matches,
the shell believes the app is not running: Super+<n> and dash clicks launch a
second process instead of focusing the open window (issue #1248).

So the three names have to agree, and the desktop entry has to say so out loud
rather than leaning on the implicit guess. These are file checks: no display,
no GTK.
"""

from __future__ import annotations

import ast
import re
from configparser import ConfigParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_IN = ROOT / "data" / "io.github.mfat.sshpilot.desktop.in"
MAIN = ROOT / "src" / "sshpilot" / "main.py"


def _desktop_entry() -> dict:
    parser = ConfigParser(interpolation=None, strict=True)
    # Keys are case-sensitive ("Exec" != "exec") per the desktop entry spec.
    parser.optionxform = str
    parser.read_string(DESKTOP_IN.read_text(encoding="utf-8"))
    return dict(parser["Desktop Entry"])


def _default_application_id() -> str:
    """The id ``SshPilotApplication`` falls back to, read without importing GTK."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # super().__init__(application_id=application_id or 'io.github...', ...)
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        right = node.values[-1]
        if isinstance(right, ast.Constant) and isinstance(right.value, str) \
                and right.value.startswith("io.github.mfat.sshpilot"):
            return right.value
    raise AssertionError("default application_id not found in main.py")


def test_startup_wm_class_matches_the_gapplication_id():
    assert _desktop_entry()["StartupWMClass"] == _default_application_id()


def test_startup_wm_class_is_the_wm_class_not_the_desktop_file_name():
    # A ".desktop" suffix here matches nothing: the window's app id has none.
    assert not _desktop_entry()["StartupWMClass"].endswith(".desktop")


def test_desktop_file_name_matches_the_gapplication_id():
    assert DESKTOP_IN.name == f"{_default_application_id()}.desktop.in"


def test_from_sshpilot_platform_utils_app_id():
    from sshpilot.platform_utils import APP_ID

    assert APP_ID == _default_application_id()


def test_no_translatable_marker_on_startup_wm_class():
    # msgfmt --desktop would translate a key listed as translatable; a
    # localised WM_CLASS would break matching in every non-English locale.
    text = DESKTOP_IN.read_text(encoding="utf-8")
    assert not re.search(r"^_StartupWMClass=", text, re.MULTILINE)


def test_keywords_follow_the_desktop_entry_spec():
    keywords = _desktop_entry()["Keywords"]
    # "Trailing empty strings must always be terminated with a semicolon."
    assert keywords.endswith(";")
    values = [v for v in keywords.split(";") if v]
    assert values == [v.strip() for v in values]
    assert len(values) == len(set(values))
