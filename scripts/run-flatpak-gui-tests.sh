#!/bin/bash
# Run GUI tests inside the installed sshPilot Flatpak.
#
# The tests import the app from the Flatpak's /app, not from this checkout
# (SSHPILOT_TEST_INSTALLED_APP), so they exercise what users install: its Python
# modules and resources, its bundled ssh and mosh, its terminal backends, and
# its flatpak-spawn --host route to docker, kubectl, telnet and picocom. The
# checkout only supplies the tests; test servers run on the host
# (tests/fixtures/host_tools.py).
#
# The Flatpak runs with a scratch HOME, so the app sees a fresh
# ~/.var/app/io.github.mfat.sshpilot and its --filesystem=~/.ssh is the scratch
# one: a developer's real Flatpak data and SSH config are never in reach.
#
#   xvfb-run -a scripts/run-flatpak-gui-tests.sh tests/gui/test_terminal_backends.py
#
# Needs: a display (DISPLAY, X11), a session bus, pip on the host, and the app
# installed (FLATPAK_APP, default io.github.mfat.sshpilot; append //branch to
# pick one). Extra arguments go to pytest.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${FLATPAK_APP:-io.github.mfat.sshpilot}"
# Short: the tests' daemon sockets live under it, and a unix socket path has
# ~100 bytes.
WORK="${SSHPILOT_FLATPAK_TEST_WORK:-$(mktemp -d /tmp/spft.XXXXXX)}"
mkdir -p "$WORK/tmp" "$WORK/home" "$WORK/pydeps"

: "${DISPLAY:?needs an X11 display (run under xvfb-run)}"
: "${DBUS_SESSION_BUS_ADDRESS:?needs a session bus (run under dbus-run-session)}"

# Where the user installation really is: HOME changes below.
export FLATPAK_USER_DIR="${FLATPAK_USER_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/flatpak}"

run_in_flatpak() {
    HOME="$WORK/home" flatpak run --no-documents-portal \
        --nosocket=wayland --socket=x11 \
        --filesystem="$ROOT:ro" --filesystem="$WORK" \
        --env=DISPLAY="$DISPLAY" --env=GDK_BACKEND=x11 \
        --env=TMPDIR="$WORK/tmp" \
        --env=PYTHONPATH="$WORK/pydeps:$ROOT" \
        --env=PYTHONDONTWRITEBYTECODE=1 \
        "$@"
}

PYVER="$(run_in_flatpak --command=python3 "$APP" -c \
    'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
echo "Flatpak $APP: Python $PYVER"

if [ ! -d "$WORK/pydeps/pytest" ]; then
    # pytest is pure Python: wheels for the sandbox's interpreter, from here.
    PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --quiet --disable-pip-version-check \
        --target "$WORK/pydeps" --python-version "$PYVER" \
        --only-binary=:all: --implementation cp pytest
fi

cd "$ROOT"
run_in_flatpak --cwd="$ROOT" --command=python3 \
    --env=SSHPILOT_GUI_TESTS=1 --env=SSHPILOT_TEST_INSTALLED_APP=1 \
    --env=SSHPILOT_HOST_HOME="$HOME" \
    --env=SSHPILOT_EXPECT_BACKENDS="${SSHPILOT_EXPECT_BACKENDS:-}" \
    "$APP" -m pytest -p no:cacheprovider -o addopts="" -m gui -ra \
    --basetemp="$WORK/p" "$@"
