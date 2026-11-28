"""The Flathub manifest bundles a working mosh client (#1432).

The Mosh plugin runs ``mosh`` inside the sandbox so that the askpass helpers
carrying saved passwords and key passphrases reach its ssh; the host's mosh
cannot use them. ``mosh`` is a Perl script and the GNOME runtime ships no
Perl, so the manifest builds Perl too and trims it.

flatpak-builder applies ``cleanup`` after every module's build commands have
run, so the build-time checks in the manifest cannot see a cleanup rule that
deletes a module mosh loads. These tests do.
"""

import fnmatch
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "flatpak" / "io.github.mfat.sshpilot.yaml").read_text())

# The modules scripts/mosh.pl in mosh 1.4.0 loads (``use``/``require``).
MOSH_PERL_MODULES = (
    "Getopt::Long",
    "IO::Socket",
    "IO::Socket::IP",
    "Text::ParseWords",
    "Socket",
    "Errno",
    "POSIX",
)
PERL_VERSION_DIR = "/lib/perl5/5.44.0"
PERL_ARCH_DIR = PERL_VERSION_DIR + "/x86_64-linux"


def _modules():
    return [m for m in MANIFEST["modules"] if isinstance(m, dict)]


def _module(name):
    return next(m for m in _modules() if m.get("name") == name)


def _names():
    return [m["name"] if isinstance(m, dict) else m for m in MANIFEST["modules"]]


def _removed_by(patterns, path):
    """The cleanup pattern that removes ``path`` (relative to /app), if any.

    Mirrors flatpak-builder: a pattern starting with ``/`` names a path under
    the prefix and takes everything below it; any other pattern is matched
    against file names.
    """
    parts = path.strip("/").split("/")
    for pattern in patterns:
        if pattern.startswith("/"):
            for depth in range(1, len(parts) + 1):
                if fnmatch.fnmatchcase("/" + "/".join(parts[:depth]), pattern):
                    return pattern
        elif fnmatch.fnmatchcase(parts[-1], pattern):
            return pattern
    return None


def test_mosh_client_and_its_build_deps_come_before_sshpilot():
    names = _names()
    for name in ("protobuf", "perl", "mosh"):
        assert names.index(name) < names.index("sshpilot")
    assert names.index("protobuf") < names.index("mosh")


def test_only_the_mosh_client_is_built():
    opts = _module("mosh")["config-opts"]
    assert "--enable-client" in opts
    assert "--disable-server" in opts


def test_protobuf_is_build_only():
    module = _module("protobuf")
    assert "--disable-shared" in module["config-opts"]
    assert module["cleanup"] == ["*"]


@pytest.mark.parametrize("program", ["/bin/mosh", "/bin/mosh-client", "/bin/perl"])
def test_programs_mosh_runs_survive_cleanup(program):
    patterns = list(MANIFEST.get("cleanup", ())) + list(_module("perl").get("cleanup", ()))
    assert _removed_by(patterns, program) is None


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs a perl to list module files")
def test_perl_files_mosh_loads_survive_cleanup():
    """Every file the mosh script's modules load stays in the Flatpak.

    The host perl lists the files (and their dependencies); core module file
    names are the same across perl 5 releases.
    """
    script = "".join(f"require {name}; " for name in MOSH_PERL_MODULES)
    listed = subprocess.run(
        ["perl", "-e", script + 'print "$_\\n" for sort keys %INC'],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert "Getopt/Long.pm" in listed

    patterns = list(MANIFEST.get("cleanup", ())) + list(_module("perl").get("cleanup", ()))
    removed = {}
    for rel in listed:
        stem = rel[:-3] if rel.endswith(".pm") else rel
        candidates = (
            f"{PERL_VERSION_DIR}/{rel}",
            f"{PERL_ARCH_DIR}/{rel}",
            f"{PERL_ARCH_DIR}/auto/{stem}/{stem.rsplit('/', 1)[-1]}.so",
        )
        for path in candidates:
            pattern = _removed_by(patterns, path)
            if pattern:
                removed[path] = pattern
    assert removed == {}


def test_cleanup_check_catches_a_rule_that_removes_a_needed_module():
    """Guard the guard: the matcher must see a rule that would break mosh."""
    assert _removed_by(["/lib/perl5/*/Getopt"], f"{PERL_VERSION_DIR}/Getopt/Long.pm")
    assert _removed_by(["*.pm"], f"{PERL_VERSION_DIR}/POSIX.pm")
    assert _removed_by(["/lib/perl5/*/Pod"], f"{PERL_VERSION_DIR}/Getopt/Long.pm") is None
