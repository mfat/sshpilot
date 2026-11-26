#!/usr/bin/env bash
# Run smoke-test.py against a finished AppImage, the way users start it.
#
#   packaging/appimage/test-appimage.sh SSHPILOT.AppImage [mount|extract]...
#
# Default: both modes. "mount" uses the AppImage's FUSE mount (what a
# double-click does), "extract" the --appimage-extract tree (what
# APPIMAGE_EXTRACT_AND_RUN=1 and FUSE-less hosts run). Each runs AppRun's own
# environment and working directory, with the C library AppRun picks for this
# host (SSHPILOT_APPIMAGE_LIBC=host|bundled overrides it), and keeps the
# caller's environment so a host GTK/GI setting leaking in shows up here.
#
# With a display (DISPLAY or WAYLAND_DISPLAY) it also renders the xterm.js
# shell and fails unless the helper processes doing it are the AppImage's own,
# running inside WebKit's bubblewrap sandbox. Run it under xvfb-run on CI.
set -euo pipefail

die() { printf 'test-appimage: %s\n' "$*" >&2; exit 1; }

[ $# -ge 1 ] || die "usage: $0 SSHPILOT.AppImage [mount|extract]..."
APPIMAGE=$(readlink -f "$1")
shift
[ -x "$APPIMAGE" ] || die "$APPIMAGE is not an executable AppImage"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODES=("$@")
[ ${#MODES[@]} -gt 0 ] || MODES=(mount extract)

WORK=$(mktemp -d)
MOUNT_PID=
cleanup() {
    if [ -n "$MOUNT_PID" ]; then
        kill "$MOUNT_PID" 2>/dev/null || true
        wait "$MOUNT_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

run_smoke_test() { # APPDIR MODE
    local appdir=$1 mode=$2
    printf '\n==> %s: %s\n' "$mode" "$appdir"
    # AppRun minus its exec line: the environment and working directory users get.
    grep -v '^exec ' "$appdir/AppRun" > "$WORK/apprun-env.sh"
    APPDIR="$appdir" APPIMAGE="$APPIMAGE" \
        bash -c '. "$1"; shift; exec "$SSHPILOT_PYTHON" "$@"' _ "$WORK/apprun-env.sh" \
        - "$appdir" < "$SCRIPT_DIR/smoke-test.py"
}

for mode in "${MODES[@]}"; do
    case "$mode" in
        mount)
            "$APPIMAGE" --appimage-mount > "$WORK/mountpoint" &
            MOUNT_PID=$!
            for _ in $(seq 100); do
                [ -s "$WORK/mountpoint" ] && break
                kill -0 "$MOUNT_PID" 2>/dev/null || die "--appimage-mount failed (FUSE missing?)"
                sleep 0.1
            done
            mountpoint=$(head -n1 "$WORK/mountpoint")
            [ -d "$mountpoint" ] || die "no mount point reported"
            run_smoke_test "$mountpoint" mount
            kill "$MOUNT_PID"
            wait "$MOUNT_PID" 2>/dev/null || true
            MOUNT_PID=
            ;;
        extract)
            (cd "$WORK" && "$APPIMAGE" --appimage-extract > /dev/null)
            run_smoke_test "$WORK/squashfs-root" extract
            ;;
        *)
            die "unknown mode $mode (mount or extract)"
            ;;
    esac
done
printf '\ntest-appimage: all modes passed\n'
