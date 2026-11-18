"""The local-test Flatpak manifest is the Flathub copy with only its source changed.

flatpak/io.github.mfat.sshpilot.yaml is the copy of the Flathub manifest that
releases publish; scripts/flatpak-dev-manifest.py derives the manifest that
builds the working tree. Anything else differing would mean local test builds
no longer test what Flathub builds.
"""

import difflib
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COPY = ROOT / "flatpak" / "io.github.mfat.sshpilot.yaml"

_spec = importlib.util.spec_from_file_location("flatpak_dev_manifest", ROOT / "scripts" / "flatpak-dev-manifest.py")
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)


def _sshpilot_module(manifest):
    return next(m for m in manifest["modules"] if isinstance(m, dict) and m.get("name") == "sshpilot")


def test_the_copy_builds_a_pinned_commit_like_flathub():
    source = _sshpilot_module(yaml.safe_load(COPY.read_text()))["sources"][0]
    assert source["type"] == "git" and len(source["commit"]) == 40
    assert "python3-deps.yml" in yaml.safe_load(COPY.read_text())["modules"]


def test_dev_manifest_builds_this_checkout():
    dev = yaml.safe_load(generator.dev_manifest(COPY.read_text()))
    source = _sshpilot_module(dev)["sources"][0]
    assert source["type"] == "dir" and source["path"] == "."
    assert "flatpak/python3-deps.yml" in dev["modules"]


def test_nothing_else_differs_from_the_flathub_copy():
    copy = COPY.read_text().splitlines()
    dev = generator.dev_manifest(COPY.read_text()).splitlines()
    changed = [line[1:].strip() for line in difflib.unified_diff(copy, dev, lineterm="", n=0)
               if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    allowed = ("- type: git", "- type: dir", "url:", "#tag:", "commit:", "path: .", "skip:", "- .", "- venv",
               "- builddir", "- build", "- repo", "# GENERATED", "# (the copy", "# Flathub builds",
               "# local build", "- python3-deps.yml", "- flatpak/python3-deps.yml")
    assert [line for line in changed if not line.startswith(allowed)] == []
