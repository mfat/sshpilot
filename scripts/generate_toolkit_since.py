#!/usr/bin/env python3
"""Record which toolkit API is newer than the oldest versions the app supports.

Writes ``tests/fixtures/toolkit_since.json``, read by
``tests/architecture/test_toolkit_minimum.py``. The data comes from the
toolkit's own introspection files (``*.gir``, in the ``-dev`` packages), which
state the version each class, method, property and signal appeared in.

Run it on a machine whose toolkit is *newer* than the floors below -- only
what the installed files describe can be recorded -- whenever the floors
change or a newer toolkit is out:

    python3 scripts/generate_toolkit_since.py

The floors are what the oldest supported distribution ships (README,
"Operating system": Ubuntu 24.04).
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# namespace -> (gir file, oldest supported version)
FLOORS = {
    "Gtk": ("Gtk-4.0.gir", (4, 14)),
    "Gdk": ("Gdk-4.0.gir", (4, 14)),
    "Gsk": ("Gsk-4.0.gir", (4, 14)),
    "Adw": ("Adw-1.gir", (1, 5)),
    "Pango": ("Pango-1.0.gir", (1, 52)),
    "GLib": ("GLib-2.0.gir", (2, 80)),
    "Gio": ("Gio-2.0.gir", (2, 80)),
    "GObject": ("GObject-2.0.gir", (2, 80)),
}
GIR_DIRS = (Path("/usr/share/gir-1.0"), Path("/usr/local/share/gir-1.0"))
CORE = "{http://www.gtk.org/introspection/core/1.0}"
C = "{http://www.gtk.org/introspection/c/1.0}"
OUTPUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "toolkit_since.json"

TYPE_TAGS = ("class", "interface", "record", "enumeration", "bitfield")
CALL_TAGS = ("method", "constructor", "function")


def _version(text: str):
    return tuple(int(part) for part in re.findall(r"\d+", text)[:2])


def _find(name: str) -> Path:
    for directory in GIR_DIRS:
        if (directory / name).exists():
            return directory / name
    sys.exit(f"{name} not found: install the toolkit's -dev packages")


def main() -> None:
    generated_from = {}
    new_types = {}        # "Adw.ToggleGroup" -> "1.7"
    new_c_types = {}      # "AdwToggleGroup" -> "Adw.ToggleGroup" (.ui files)
    new_enum_members = {}  # "Gtk.SymbolicColor.ACCENT" -> "4.22"
    parents = {}          # "Adw.Banner" -> "Gtk.Widget"
    own_new_props = {}    # "Adw.Banner" -> {"button_style": "1.7"}
    old_names = {"method": set(), "signal": set(), "property": set()}
    new_names = {"method": {}, "signal": {}, "property": {}}

    for namespace, (gir, floor) in FLOORS.items():
        root = ET.parse(_find(gir)).getroot()
        ns = root.find(f"{CORE}namespace")
        generated_from[namespace] = _described_version(ns, floor)

        def is_new(version, inherited=None):
            version = version or inherited
            return bool(version) and _version(version) > floor

        # Top-level functions: Gtk.something().
        for function in ns.findall(f"{CORE}function"):
            if is_new(function.get("version")):
                new_types[f"{namespace}.{function.get('name')}"] = function.get("version")
        for tag in TYPE_TAGS:
            for node in ns.findall(f"{CORE}{tag}"):
                name = f"{namespace}.{node.get('name')}"
                type_version = node.get("version")
                type_new = is_new(type_version)
                if type_new:
                    new_types[name] = type_version
                    if node.get(f"{C}type"):
                        new_c_types[node.get(f"{C}type")] = name
                parent = node.get("parent")
                if parent:
                    parents[name] = parent if "." in parent else f"{namespace}.{parent}"
                for member in node:
                    kind = member.tag[len(CORE):] if member.tag.startswith(CORE) else ""
                    member_name = (member.get("name") or "").replace("-", "_")
                    version = member.get("version")
                    if kind == "member":      # an enumeration value
                        if not type_new and is_new(version):
                            new_enum_members[f"{name}.{member_name.upper()}"] = version
                        continue
                    bucket = (
                        "method" if kind in CALL_TAGS
                        else "signal" if kind == "glib:signal" else
                        "property" if kind == "property" else None
                    )
                    if kind == "" and member.tag.endswith("}signal"):
                        bucket = "signal"
                    if bucket is None:
                        continue
                    if type_new or is_new(version):
                        since = version or type_version
                        new_names[bucket].setdefault(member_name, []).append(
                            f"{name} {since}"
                        )
                        if bucket == "property" and not type_new:
                            own_new_props.setdefault(name, {})[member_name] = since
                    else:
                        old_names[bucket].add(member_name)

    # A name that some older class also has cannot be judged without knowing
    # the object it is called on: only names that are new everywhere are kept.
    unique = {
        bucket: {
            name: sorted(where)
            for name, where in sorted(names.items())
            if name not in old_names[bucket]
        }
        for bucket, names in new_names.items()
    }

    # Properties a class gained, its ancestors' included: Adw.X(prop=...).
    def inherited_props(name, seen=()):
        found = dict(own_new_props.get(name, {}))
        parent = parents.get(name)
        if parent and parent not in seen:
            for key, value in inherited_props(parent, seen + (name,)).items():
                found.setdefault(key, value)
        return found

    constructor_props = {
        name: props
        for name in sorted(set(parents) | set(own_new_props))
        if (props := inherited_props(name))
    }

    data = {
        "comment": "Generated by scripts/generate_toolkit_since.py. Do not edit.",
        "floors": {ns: ".".join(map(str, floor)) for ns, (_g, floor) in FLOORS.items()},
        "generated_from": generated_from,
        "types": dict(sorted(new_types.items())),
        "c_types": dict(sorted(new_c_types.items())),
        "enum_members": dict(sorted(new_enum_members.items())),
        "methods": unique["method"],
        "signals": unique["signal"],
        "properties": unique["property"],
        "constructor_properties": constructor_props,
    }
    OUTPUT.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(
        f"{OUTPUT.relative_to(OUTPUT.parents[2])}: {len(new_types)} types, "
        f"{len(unique['method'])} methods, {len(unique['signal'])} signals, "
        f"{len(unique['property'])} properties newer than the floors "
        f"(from {generated_from})"
    )


def _described_version(ns, floor) -> str:
    """The newest version the file mentions (of the floor's major series):
    the toolkit it describes, and so the newest API this run can know of."""
    newest = floor
    for node in ns.iter():
        version = node.get("version") or ""
        if re.fullmatch(r"\d+\.\d+(\.\d+)?", version):
            found = _version(version)
            if found[0] == floor[0]:
                newest = max(newest, found)
    return ".".join(map(str, newest))


if __name__ == "__main__":
    main()
