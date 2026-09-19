"""A bundled dylib whose dependency is missing must fail the build (GH #1266).

libadwaita's reference to ``@rpath/libappstream.5.dylib`` was rewritten by
PyInstaller but the library itself never copied, so every GTK import died at
launch with "could not create new GType … (subclass of void)".
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "packaging", "pyinstaller")
    ),
)

from dylib_closure import (  # noqa: E402
    ClosureReport,
    close_dylib_graph,
    find_library,
    format_report,
    homebrew_search_roots,
    is_leaf_reference,
    is_system_reference,
    iter_macho_files,
    loader_path_reference,
    parse_otool_dependencies,
)

OTOOL_LIBADWAITA = """\
/opt/homebrew/lib/libadwaita-1.0.dylib:
\t@rpath/libadwaita-1.0.dylib (compatibility version 1.0.0, current version 1.0.0)
\t@rpath/libappstream.5.dylib (compatibility version 6.0.0, current version 6.0.0)
\t/opt/homebrew/opt/glib/lib/libgio-2.0.0.dylib (compatibility version 1.0.0, current version 1.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)
\t/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation (compatibility version 150.0.0, current version 2602.0.0)
"""


class FakeMachOTool:
    """Records install_name_tool calls; serves canned otool output."""

    def __init__(self, deps):
        self.deps = deps
        self.ids = []
        self.changes = []
        self.rpaths = []

    def dependencies(self, path):
        return list(self.deps.get(Path(path).name, []))

    def set_id(self, path, new_id):
        self.ids.append((Path(path).name, new_id))

    def change_reference(self, path, old, new):
        self.changes.append((Path(path).name, old, new))

    def add_rpath(self, path, rpath):
        self.rpaths.append((Path(path).name, rpath))


def _bundle(tmp_path, names):
    frameworks = tmp_path / "SSHPilot.app" / "Contents" / "Frameworks"
    frameworks.mkdir(parents=True)
    for name in names:
        (frameworks / name).write_bytes(b"fake")
    return frameworks


def _homebrew(tmp_path, names):
    lib = tmp_path / "homebrew" / "lib"
    lib.mkdir(parents=True)
    for name in names:
        (lib / name).write_bytes(b"fake")
    return [str(lib)]


def test_parse_otool_dependencies_keeps_only_load_commands():
    deps = parse_otool_dependencies(OTOOL_LIBADWAITA)
    # The header line naming the inspected file is not a dependency.
    assert "/opt/homebrew/lib/libadwaita-1.0.dylib" not in deps
    assert "@rpath/libappstream.5.dylib" in deps
    assert "/usr/lib/libSystem.B.dylib" in deps


def test_system_references_are_left_to_dyld():
    assert is_system_reference("/usr/lib/libSystem.B.dylib")
    assert is_system_reference(
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
    )
    assert not is_system_reference("@rpath/libappstream.5.dylib")
    assert not is_system_reference("/opt/homebrew/lib/libgio-2.0.0.dylib")


def test_missing_homebrew_dylib_is_copied_in(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib"])
    roots = _homebrew(tmp_path, ["libappstream.5.dylib"])
    tool = FakeMachOTool(
        {
            "libadwaita-1.0.dylib": ["@rpath/libappstream.5.dylib"],
            "libappstream.5.dylib": ["/usr/lib/libSystem.B.dylib"],
        }
    )

    report = close_dylib_graph(frameworks, roots, tool=tool)

    assert report.is_complete
    assert report.copied == ["libappstream.5.dylib"]
    assert (frameworks / "libappstream.5.dylib").is_file()
    # The newly copied library gets an ABI id and can find its own neighbours.
    assert ("libappstream.5.dylib", "@rpath/libappstream.5.dylib") in tool.ids
    assert ("libappstream.5.dylib", "@loader_path") in tool.rpaths
    # The reference that was broken is repointed at the copy.
    assert (
        "libadwaita-1.0.dylib",
        "@rpath/libappstream.5.dylib",
        "@loader_path/libappstream.5.dylib",
    ) in tool.changes


def test_transitive_dependencies_are_followed(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib"])
    roots = _homebrew(tmp_path, ["libappstream.5.dylib", "libxmlb.2.dylib"])
    tool = FakeMachOTool(
        {
            "libadwaita-1.0.dylib": ["@rpath/libappstream.5.dylib"],
            "libappstream.5.dylib": ["@rpath/libxmlb.2.dylib"],
            "libxmlb.2.dylib": ["/usr/lib/libSystem.B.dylib"],
        }
    )

    report = close_dylib_graph(frameworks, roots, tool=tool)

    assert report.is_complete
    assert sorted(report.copied) == ["libappstream.5.dylib", "libxmlb.2.dylib"]


def test_unresolvable_dependency_fails_the_build(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib"])
    roots = _homebrew(tmp_path, [])  # appstream not installed on the builder
    tool = FakeMachOTool(
        {"libadwaita-1.0.dylib": ["@rpath/libappstream.5.dylib"]}
    )

    report = close_dylib_graph(frameworks, roots, tool=tool)

    assert not report.is_complete
    assert report.unresolved == [
        ("libadwaita-1.0.dylib", "@rpath/libappstream.5.dylib")
    ]
    assert "libappstream.5.dylib" in format_report(report)


def test_absolute_build_machine_paths_are_rewritten(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib", "libgio-2.0.0.dylib"])
    roots = _homebrew(tmp_path, [])
    tool = FakeMachOTool(
        {
            "libadwaita-1.0.dylib": [
                "/opt/homebrew/opt/glib/lib/libgio-2.0.0.dylib"
            ],
            "libgio-2.0.0.dylib": [],
        }
    )

    report = close_dylib_graph(frameworks, roots, tool=tool)

    # Present in the bundle already, so nothing is copied — but a path into
    # the build machine's Homebrew would not exist on the user's Mac.
    assert report.is_complete
    assert report.copied == []
    assert (
        "libadwaita-1.0.dylib",
        "/opt/homebrew/opt/glib/lib/libgio-2.0.0.dylib",
        "@loader_path/libgio-2.0.0.dylib",
    ) in tool.changes


def test_resolved_rpath_reference_is_left_alone(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib", "libgtk-4.1.dylib"])
    roots = _homebrew(tmp_path, [])
    tool = FakeMachOTool(
        {
            "libadwaita-1.0.dylib": ["@rpath/libgtk-4.1.dylib"],
            "libgtk-4.1.dylib": [],
        }
    )

    report = close_dylib_graph(frameworks, roots, tool=tool)

    assert report.is_complete
    assert tool.changes == []


def test_nested_binaries_are_inspected_and_point_back_to_root(tmp_path):
    frameworks = _bundle(tmp_path, ["libappstream.5.dylib"])
    nested = frameworks / "gi"
    nested.mkdir()
    (nested / "_gi_cairo.so").write_bytes(b"fake")

    assert (nested / "_gi_cairo.so") in iter_macho_files(frameworks)
    assert (
        loader_path_reference(nested / "_gi_cairo.so", frameworks, "libcairo.2.dylib")
        == "@loader_path/../libcairo.2.dylib"
    )

    roots = _homebrew(tmp_path, ["libcairo.2.dylib"])
    tool = FakeMachOTool(
        {
            "_gi_cairo.so": ["/opt/homebrew/lib/libcairo.2.dylib"],
            "libcairo.2.dylib": [],
            "libappstream.5.dylib": [],
        }
    )

    report = close_dylib_graph(frameworks, roots, tool=tool)

    assert report.copied == ["libcairo.2.dylib"]
    assert (
        "_gi_cairo.so",
        "/opt/homebrew/lib/libcairo.2.dylib",
        "@loader_path/../libcairo.2.dylib",
    ) in tool.changes


def test_symlinks_are_not_inspected_twice(tmp_path):
    frameworks = _bundle(tmp_path, ["libgtksourceview-5.0.0.dylib"])
    (frameworks / "libgtksourceview-5.0.dylib").symlink_to(
        "libgtksourceview-5.0.0.dylib"
    )
    files = iter_macho_files(frameworks)
    assert [p.name for p in files] == ["libgtksourceview-5.0.0.dylib"]


def test_find_library_searches_opt_and_cellar(tmp_path):
    prefix = tmp_path / "homebrew"
    opt_lib = prefix / "opt" / "appstream" / "lib"
    opt_lib.mkdir(parents=True)
    (opt_lib / "libappstream.5.dylib").write_bytes(b"fake")

    roots = homebrew_search_roots(str(prefix))
    # Not linked into <prefix>/lib, which is exactly the case a bare glob of
    # that directory misses.
    assert find_library("libappstream.5.dylib", roots) == str(
        opt_lib / "libappstream.5.dylib"
    )
    assert find_library("libnotthere.1.dylib", roots) is None


def test_framework_references_are_not_treated_as_loose_dylibs():
    assert is_leaf_reference("@rpath/libappstream.5.dylib")
    assert not is_leaf_reference("@rpath/Python.framework/Versions/3.13/Python")
    assert not is_leaf_reference("/opt/homebrew/lib/libgio-2.0.0.dylib")


def test_framework_dependency_already_in_the_bundle_is_accepted(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib"])
    python_bin = frameworks / "Python.framework" / "Versions" / "3.13"
    python_bin.mkdir(parents=True)
    (python_bin / "Python").write_bytes(b"fake")

    tool = FakeMachOTool(
        {
            "libadwaita-1.0.dylib": [
                "@rpath/Python.framework/Versions/3.13/Python"
            ],
            "Python": [],
        }
    )

    report = close_dylib_graph(frameworks, _homebrew(tmp_path, []), tool=tool)

    # Shipped as a framework, so it must not be flattened into a stray file.
    assert report.is_complete
    assert report.copied == []
    assert tool.changes == []


def test_missing_framework_dependency_is_reported_not_flattened(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib"])
    roots = _homebrew(tmp_path, ["Python"])
    tool = FakeMachOTool(
        {
            "libadwaita-1.0.dylib": [
                "@rpath/Python.framework/Versions/3.13/Python"
            ]
        }
    )

    report = close_dylib_graph(frameworks, roots, tool=tool)

    assert not report.is_complete
    assert report.copied == []


def test_absolute_reference_is_copied_from_where_it_points(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib"])
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    (stray / "libappstream.5.dylib").write_bytes(b"fake")
    ref = str(stray / "libappstream.5.dylib")

    tool = FakeMachOTool(
        {"libadwaita-1.0.dylib": [ref], "libappstream.5.dylib": []}
    )

    # Nothing under the Homebrew roots, but the reference itself says where
    # the library lives on the build machine.
    report = close_dylib_graph(frameworks, _homebrew(tmp_path, []), tool=tool)

    assert report.is_complete
    assert report.copied == ["libappstream.5.dylib"]
    assert (frameworks / "libappstream.5.dylib").is_file()


def test_absolute_framework_path_is_not_flattened(tmp_path):
    frameworks = _bundle(tmp_path, ["libadwaita-1.0.dylib"])
    framework_bin = tmp_path / "Python.framework" / "Versions" / "3.13"
    framework_bin.mkdir(parents=True)
    (framework_bin / "Python").write_bytes(b"fake")

    tool = FakeMachOTool(
        {"libadwaita-1.0.dylib": [str(framework_bin / "Python")]}
    )

    report = close_dylib_graph(frameworks, _homebrew(tmp_path, []), tool=tool)

    # Reported so the build fails, rather than dropping a bare "Python" file
    # at Frameworks root and calling it fixed.
    assert not report.is_complete
    assert report.copied == []
    assert not (frameworks / "Python").exists()


def test_report_of_a_closed_graph_says_so():
    assert "nothing to do" in format_report(ClosureReport())
