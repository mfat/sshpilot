#!/usr/bin/env python3
"""Ask Launchpad to build the sshpilot recipe against the commit just pushed.

The recipe (lp:~mfat/+recipe/sshpilot) is a *daily* build over a code-imported
mirror of this repo, so a release lands in ppa:mfat/sshpilot only after two
schedules have run: the GitHub -> Launchpad import (every few hours) and the
daily build. That is why `scripts/release.sh` looked like it never triggered a
PPA build -- it did, eventually, off yesterday's revision.

This nudges both: request the import, wait for it to carry the expected commit,
then request one build per series.

Usage: trigger-ppa-build.py <commit-sha> [--timeout SECONDS]

First run opens a browser to authorize the token (cached under
~/.local/share/launchpadlib afterwards).
"""

import os
import sys
import time

from launchpadlib.launchpad import Launchpad

REPO_PATH = "~mfat/sshpilot/+git/sshpilot"
RECIPE_OWNER = "mfat"
RECIPE_NAME = "sshpilot"
BRANCH = "refs/heads/main"
POLL_SECONDS = 30


def imported_head(repo):
    ref = repo.getRefByPath(path=BRANCH)
    return ref.commit_sha1 if ref else None


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    want = argv[0]
    timeout = 1800
    if "--timeout" in argv:
        timeout = int(argv[argv.index("--timeout") + 1])

    # In CI, point LP_CREDENTIALS_FILE at a stored token to skip the browser.
    lp = Launchpad.login_with(
        "sshpilot-release",
        "production",
        version="devel",
        credentials_file=os.environ.get("LP_CREDENTIALS_FILE"),
    )
    repo = lp.git_repositories.getByPath(path=REPO_PATH)
    if repo is None:
        print(f"ERROR: no Launchpad repository at {REPO_PATH}", file=sys.stderr)
        return 1

    print("Requesting the code import from GitHub...")
    try:
        repo.code_import.requestImport()
    except Exception as exc:  # already running is the common one, and is fine
        print(f"  (import not queued: {exc})")

    deadline = time.monotonic() + timeout
    while imported_head(repo) != want:
        if time.monotonic() > deadline:
            print(
                f"ERROR: {REPO_PATH} still at {imported_head(repo)}, wanted {want}.\n"
                "The import is slow, not broken -- rerun this script later.",
                file=sys.stderr,
            )
            return 1
        print(f"  waiting for the import to reach {want[:12]}...")
        time.sleep(POLL_SECONDS)
    print(f"Import is at {want[:12]}.")

    recipe = lp.people[RECIPE_OWNER].getRecipe(name=RECIPE_NAME)
    archive = recipe.daily_build_archive
    for series in recipe.distroseries:
        # launchpadlib hands back plain link strings for this list rather than
        # resolved entries, unlike most collections.
        if isinstance(series, str):
            series = lp.load(series)
        name = series.name
        try:
            build = recipe.requestBuild(
                archive=archive, distroseries=series, pocket="Release"
            )
            print(f"  {name}: requested -> {build.web_link}")
        except Exception as exc:
            # A build already pending for that series is the usual reason, and
            # it means the thing we wanted is happening anyway.
            print(f"  {name}: not requested ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
