# Nuitka entry point for the experimental Windows build (build.ps1 here).
#
# Nuitka has no runtime hooks, so this does the two things PyInstaller's do
# that sshPilot depends on, then hands over to run.py:
#   sys.frozen     the app picks its frozen daemon/askpass paths from it
#                  (daemon/launcher.py); Nuitka does not set it.
#   XDG_DATA_DIRS  without it GLib falls back to its own Windows list of data
#                  directories and GTK searches them all at startup, about
#                  0.5 s slower to the first window (PyInstaller's
#                  pyi_rth_glib sets it the same way).
#
# __compiled__ is set by Nuitka on compiled modules; containing_dir is the
# app folder (https://nuitka.net/user-documentation/common-issue-solutions.html).

import os
import sys

sys.frozen = True

_share = os.path.join(__compiled__.containing_dir, "share")  # noqa: F821
os.environ["XDG_DATA_DIRS"] = os.pathsep.join(
    entry for entry in (_share, os.environ.get("XDG_DATA_DIRS")) if entry
)

import run  # noqa: E402  (the --daemon / --internal-* dispatch runs on import)

run.main()
