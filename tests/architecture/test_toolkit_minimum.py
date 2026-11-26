"""Nothing in the app needs a newer toolkit than the oldest one supported.

The app runs on Ubuntu 24.04's GTK 4.14 and libadwaita 1.5 (README,
"Operating system"); developers run something newer, where a widget or a
method added since simply works. This reads which API is newer than the
floors (``tests/fixtures/toolkit_since.json``, written from the toolkit's
own introspection data by ``scripts/generate_toolkit_since.py``) and fails
when the source uses any of it without a guard.

What it can see, without a display:

* a class, function or enumeration value named through its namespace
  (``Adw.ToggleGroup``, ``Gtk.SymbolicColor.ACCENT``), in Python and in the
  ``.ui``/``.blp`` templates;
* a property given to a constructor (``Adw.Banner(button_style=...)``);
* a method or signal whose *name* is new everywhere in the toolkit. A name
  an older class also has (``set_title``) cannot be judged without knowing
  the object, so it is not: that part is the job of the GUI suite, which
  ``.github/workflows/gui-contracts.yml`` runs on Ubuntu 24.04;
* CSS that GTK before 4.16 rejects (custom properties, relative colours).

A use is fine when the same file asks first (``hasattr(Adw, "ButtonRow")``,
``hasattr(row, "set_x")``) or tries and catches (``try: ... except
AttributeError``, the use in the ``try`` body); anything else is listed in
the tables below with its reason.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "sshpilot"
DATA = json.loads(
    (REPO / "tests" / "fixtures" / "toolkit_since.json").read_text(encoding="utf-8")
)
NAMESPACES = frozenset(DATA["floors"])

# Files whose CSS is for a web view (WebKit), not for GTK.
WEB_CSS_FILES = frozenset({
    "host_info_shell.py",
    "xterm_shell.py",
})

# rel path -> reason. GTK CSS that needs 4.16, applied only where it works.
GUARDED_CSS_FILES = {
    "sidebar.py": "built only when _SUPPORTS_STANDALONE_OKLAB (GTK 4.16, Adw 1.6)",
    "file_manager/pane.py": "a cosmetic accent; older GTK drops the rule",
    "resources/style.css": "names the syntax in a comment explaining its absence",
}

# (rel path, name) -> reason, for a use this test cannot see is guarded.
ALLOWED_NAMES: dict = {}

# Method names that are new in the toolkit and also ordinary Python: the
# calls found are on the app's own objects. name -> what it is here.
NOT_TOOLKIT_METHODS = {
    "get_transfer": "the transfer service's own lookup",
    "get_previous": "the session manager's saved session",
    "set_layout": "the connection service's group layout",
}

# Signal names new in the toolkit that other libraries have always had.
NOT_TOOLKIT_SIGNALS = {
    "error": "GStreamer, WebKit and Gio objects emit their own 'error'",
}

NEW_CSS = re.compile(r"var\(--|\boklab\(|\boklch\(|\bcolor-mix\(|\bcolor\(from\b")


_SOURCES: list = []


def _python_sources():
    """``(rel, attribute nodes, call nodes, names asked about, ids of nodes
    in a guarding try)`` of every source file, parsed and walked once."""
    if not _SOURCES:
        for path in sorted(SOURCE.rglob("*.py")):
            rel = path.relative_to(SOURCE).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            nodes = list(ast.walk(tree))
            _SOURCES.append((
                rel,
                [n for n in nodes if isinstance(n, ast.Attribute)],
                [n for n in nodes if isinstance(n, ast.Call)],
                _guards(nodes),
                _tried(nodes),
            ))
    return _SOURCES


_CATCHES_MISSING = frozenset({"Exception", "BaseException", "AttributeError"})


def _catches_missing(handler) -> bool:
    """The handler catches the AttributeError a missing name raises."""
    kind = handler.type
    if kind is None:
        return True
    names = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    return any(isinstance(n, ast.Name) and n.id in _CATCHES_MISSING for n in names)


def _tried(nodes) -> set:
    """Every node in the body of a ``try`` that catches a missing name.
    (Not its handlers: what runs there is not protected by it.)"""
    inside = set()
    for node in nodes:
        if isinstance(node, ast.Try) and any(map(_catches_missing, node.handlers)):
            for statement in node.body:
                inside.update(id(child) for child in ast.walk(statement))
    return inside


def _guards(nodes) -> set:
    """Names a file asks about before using: the second argument of every
    ``hasattr``/``getattr`` call with a literal name."""
    asked = set()
    for node in nodes:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("hasattr", "getattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            asked.add(node.args[1].value)
    return asked


def _qualified(node):
    """``Adw.ToggleGroup`` / ``Gtk.SymbolicColor.ACCENT`` for an attribute
    chain rooted at a toolkit namespace, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id in NAMESPACES:
        return ".".join([node.id, *reversed(parts)])
    return None


def _allowed(rel, name) -> bool:
    return (rel, name) in ALLOWED_NAMES


def test_the_data_is_about_the_documented_minimum():
    # The floors are the oldest supported distribution's versions. Raising
    # the minimum means regenerating the data, not editing this assertion
    # alone.
    assert DATA["floors"]["Gtk"] == "4.14"
    assert DATA["floors"]["Adw"] == "1.5"
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "| Ubuntu | 24.04 (noble) |" in readme, (
        "the supported minimum changed: regenerate tests/fixtures/"
        "toolkit_since.json with the new floors"
    )
    # The data must know of API newer than the floors, or it guards nothing.
    assert DATA["types"] and DATA["methods"]
    assert "Adw.ToggleGroup" in DATA["types"]          # libadwaita 1.7


def test_no_class_function_or_value_newer_than_the_minimum_is_used_unguarded():
    types, members = DATA["types"], DATA["enum_members"]
    offenders = []
    for rel, attributes, _calls, asked, tried in _python_sources():
        for node in attributes:
            name = _qualified(node)
            if name is None:
                continue
            since = types.get(name) or members.get(name)
            if since is None:
                continue
            short = name.split(".", 1)[1].split(".")[0]
            if short in asked or id(node) in tried or _allowed(rel, name):
                continue
            offenders.append(f"{rel}:{node.lineno}: {name} (since {since})")
    assert not offenders, (
        "newer than the oldest supported toolkit; guard with hasattr() and "
        "fall back, or raise the minimum:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_no_constructor_is_given_a_property_newer_than_the_minimum():
    by_type = DATA["constructor_properties"]
    offenders = []
    for rel, _attributes, calls, _asked, _tried_nodes in _python_sources():
        for node in calls:
            name = _qualified(node.func)
            if name not in by_type:
                continue
            for keyword in node.keywords:
                since = by_type[name].get(keyword.arg or "")
                if since and not _allowed(rel, f"{name}:{keyword.arg}"):
                    offenders.append(
                        f"{rel}:{node.lineno}: {name}({keyword.arg}=...) (since {since})"
                    )
    assert not offenders, (
        "a constructor property the oldest supported toolkit does not have "
        "makes the constructor fail there:\n  " + "\n  ".join(offenders)
    )


def test_no_method_or_signal_new_everywhere_in_the_toolkit_is_used_unguarded():
    methods, signals, properties = DATA["methods"], DATA["signals"], DATA["properties"]
    offenders = []
    for rel, _attributes, calls, asked, tried in _python_sources():
        for node in calls:
            if not isinstance(node.func, ast.Attribute):
                continue
            if id(node) in tried:
                continue
            called = node.func.attr
            if (
                called in methods
                and called not in asked
                # A method of classes the file asked about before using.
                and not all(
                    owner.split(" ")[0].split(".", 1)[1] in asked
                    for owner in methods[called]
                )
                and called not in NOT_TOOLKIT_METHODS
                and not _allowed(rel, called)
                # Namespace functions are the previous test's.
                and _qualified(node.func) is None
            ):
                offenders.append(
                    f"{rel}:{node.lineno}: .{called}() ({'; '.join(methods[called])})"
                )
            if called in ("connect", "connect_after") and node.args and isinstance(
                node.args[0], ast.Constant
            ) and isinstance(node.args[0].value, str):
                signal = node.args[0].value
                if signal.startswith("notify::"):
                    key, table = signal[len("notify::"):].replace("-", "_"), properties
                else:
                    key, table = signal.split("::")[0].replace("-", "_"), signals
                if (
                    key in table
                    and key not in NOT_TOOLKIT_SIGNALS
                    and not _allowed(rel, signal)
                ):
                    offenders.append(
                        f"{rel}:{node.lineno}: connect({signal!r}) "
                        f"({'; '.join(table[key])})"
                    )
    assert not offenders, (
        "not in the oldest supported toolkit (calling it raises there); "
        "guard with hasattr() or list it with its reason:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_no_template_names_a_class_newer_than_the_minimum():
    c_types, types = DATA["c_types"], DATA["types"]
    offenders = []
    for path in sorted(SOURCE.rglob("*.ui")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'class="([A-Za-z0-9]+)"', text):
            if match.group(1) in c_types:
                offenders.append(
                    f"{path.relative_to(SOURCE).as_posix()}: {match.group(1)} "
                    f"(since {types[c_types[match.group(1)]]})"
                )
    for path in sorted(SOURCE.rglob("*.blp")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\b((?:%s)\.[A-Za-z0-9]+)\b" % "|".join(sorted(NAMESPACES)), text):
            if match.group(1) in types:
                offenders.append(
                    f"{path.relative_to(SOURCE).as_posix()}: {match.group(1)} "
                    f"(since {types[match.group(1)]})"
                )
    assert not offenders, (
        "a template with a class the oldest supported toolkit lacks does not "
        "load there:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_gtk_css_uses_nothing_gtk_before_4_16_rejects():
    offenders = []
    files = [(p.relative_to(SOURCE).as_posix(), p) for p in sorted(SOURCE.rglob("*.css"))]
    files += [(p.relative_to(SOURCE).as_posix(), p) for p in sorted(SOURCE.rglob("*.py"))]
    for rel, path in files:
        if rel in WEB_CSS_FILES or rel in GUARDED_CSS_FILES:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if NEW_CSS.search(line):
                offenders.append(f"{rel}:{number}: {line.strip()[:90]}")
    assert not offenders, (
        "CSS custom properties and relative colours need GTK 4.16: older GTK "
        "drops the whole rule. Use @named_colors and alpha()/mix(), or apply "
        "it only where supported:\n  " + "\n  ".join(offenders)
    )


def test_the_exception_tables_name_files_that_exist():
    for rel in list(WEB_CSS_FILES) + list(GUARDED_CSS_FILES):
        assert (SOURCE / rel).exists(), f"{rel} is gone: drop it from the table"
    for rel, _name in ALLOWED_NAMES:
        assert (SOURCE / rel).exists(), f"{rel} is gone: drop it from ALLOWED_NAMES"
