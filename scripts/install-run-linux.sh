#!/usr/bin/env bash
set -euo pipefail

# One-shot source installer/runner for sshPilot on Linux.
#
#   - Detects your distribution and installs the system GTK4 stack + PyGObject
#     (the "hybrid" approach: system bindings + a --system-site-packages venv).
#   - Clones the repo (or uses the current checkout) and creates the venv.
#   - Installs the pure-Python dependencies and launches the app.
#
# Usage:
#   ./scripts/install-run-linux.sh [options]
#   curl -fsSL https://raw.githubusercontent.com/mfat/sshpilot/main/scripts/install-run-linux.sh | bash
#
# Options:
#   -y, --yes         Don't prompt before installing system packages.
#       --no-run      Set everything up but don't launch the app.
#       --with-webkit Also install the optional WebKit 6.0 package (only the
#                     PyXterm.js terminal backend needs it; default is VTE).
#   -h, --help        Show this help and exit.
#
# Environment:
#   SSHPILOT_DRYRUN=1 Print the package-install command instead of running it
#                     (and skip clone/venv/launch). For safe testing.

REPO_URL="https://github.com/mfat/sshpilot.git"

# --- output helpers ---------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi
info()  { printf '%s\n' "${C_BLUE}==>${C_RESET} $*"; }
ok()    { printf '%s\n' "${C_GREEN}✓${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}!${C_RESET} $*" >&2; }
die()   { printf '%s\n' "${C_RED}error:${C_RESET} $*" >&2; exit 1; }

trap 'die "failed at line $LINENO. Re-run with the command shown above, or follow docs/running-from-source.md."' ERR

usage() { sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# --- args -------------------------------------------------------------------
ASSUME_YES=0; DO_RUN=1; WITH_WEBKIT=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)      ASSUME_YES=1 ;;
        --no-run)      DO_RUN=0 ;;
        --with-webkit) WITH_WEBKIT=1 ;;
        -h|--help)     usage ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done
# Non-interactive stdin (e.g. curl | bash) can't prompt — proceed automatically.
[[ -t 0 ]] || ASSUME_YES=1
DRYRUN="${SSHPILOT_DRYRUN:-0}"

# --- platform guard ---------------------------------------------------------
[[ "$(uname -s)" == "Linux" ]] || die "This script is for Linux. On macOS see docs/INSTALL-macos.md."

# --- distro detection -------------------------------------------------------
detect_ids() {
    [[ -r /etc/os-release ]] || return 1
    # shellcheck disable=SC1091
    ( . /etc/os-release; printf '%s %s\n' "${ID:-}" "${ID_LIKE:-}" )
}

PM=""              # package manager: apt|dnf|pacman|zypper
PKGS=()            # base package list
WEBKIT_PKG=""

ids="$(detect_ids || true)"
case " $ids " in
    *" debian "*|*" ubuntu "*|*" linuxmint "*|*" pop "*|*" raspbian "*|*" kali "*|*" devuan "*)
        PM="apt"
        PKGS=(python3 python3-venv python3-gi python3-gi-cairo
              libgtk-4-1 gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1
              libvte-2.91-gtk4-0 gir1.2-vte-3.91
              libgtksourceview-5-0 gir1.2-gtksource-5
              libsecret-1-0 gir1.2-secret-1
              python3-cryptography sshpass ssh-askpass)
        WEBKIT_PKG="gir1.2-webkit-6.0" ;;
    *" fedora "*|*" rhel "*|*" centos "*|*" rocky "*|*" almalinux "*|*" ol "*)
        PM="dnf"
        PKGS=(python3 python3-gobject gtk4 libadwaita vte291-gtk4 gtksourceview5
              libsecret python3-cryptography sshpass openssh-askpass)
        WEBKIT_PKG="webkitgtk6" ;;
    *" arch "*|*" manjaro "*|*" endeavouros "*|*" cachyos "*|*" arcolinux "*)
        PM="pacman"
        PKGS=(python python-gobject python-cairo gtk4 libadwaita vte4 gtksourceview5
              libsecret python-cryptography sshpass)
        WEBKIT_PKG="webkitgtk-6.0" ;;
    *opensuse*|*" sles "*|*" suse "*)
        PM="zypper"
        PKGS=(python3 python3-gobject typelib-1_0-Gtk-4_0 gtk4 libadwaita
              typelib-1_0-Adw-1 typelib-1_0-Vte-3_91 typelib-1_0-GtkSource-5
              typelib-1_0-Secret-1 python3-cryptography
              sshpass openssh-askpass-gnome)
        WEBKIT_PKG="typelib-1_0-WebKit-6_0" ;;
    *)
        warn "Unsupported or undetected distribution (ID: '${ids:-unknown}')."
        cat >&2 <<EOF

Install these manually, then create a --system-site-packages venv and
'pip install -r requirements.txt' (see docs/running-from-source.md):

  GTK4, libadwaita, VTE (GTK4 build), GtkSourceView 5, libsecret,
  PyGObject + pycairo, plus: python3-cryptography sshpass
EOF
        die "cannot auto-install on this distro." ;;
esac
[[ "$WITH_WEBKIT" -eq 1 ]] && PKGS+=("$WEBKIT_PKG")
ok "Detected $PM-based distribution."

# --- build the install command ----------------------------------------------
SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || die "need root to install packages but 'sudo' is not available. Re-run as root."
    SUDO="sudo"
fi

case "$PM" in
    apt)    INSTALL=("$SUDO" apt-get install -y "${PKGS[@]}") ; PREP=("$SUDO" apt-get update) ;;
    dnf)    INSTALL=("$SUDO" dnf install -y "${PKGS[@]}") ; PREP=() ;;
    pacman) INSTALL=("$SUDO" pacman -S --needed --noconfirm "${PKGS[@]}") ; PREP=() ;;
    zypper) INSTALL=("$SUDO" zypper --non-interactive install "${PKGS[@]}") ; PREP=() ;;
esac

info "System packages to install:"
printf '    %s\n' "${PKGS[*]}"

if [[ "$DRYRUN" == "1" ]]; then
    info "[dry run] would run:"
    [[ ${#PREP[@]} -gt 0 ]] && printf '    %s\n' "${PREP[*]}"
    printf '    %s\n' "${INSTALL[*]}"
    ok "Dry run complete (no changes made)."
    exit 0
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
    printf '%s' "${C_BOLD}Proceed with the install above? [Y/n] ${C_RESET}"
    read -r reply
    case "$reply" in [nN]*) die "aborted by user." ;; esac
fi

# --- install system packages ------------------------------------------------
info "Installing system dependencies (this may ask for your password)…"
[[ ${#PREP[@]} -gt 0 ]] && { "${PREP[@]}" || die "package index update failed."; }
"${INSTALL[@]}" || die "system package installation failed. Check the package names in docs/running-from-source.md for your distro."
ok "System dependencies installed."

# --- locate or clone the source tree ----------------------------------------
is_checkout() { [[ -f "$1/run.py" && -d "$1/sshpilot" && -f "$1/requirements.txt" ]]; }

PROJECT_DIR=""
if is_checkout "$PWD"; then
    PROJECT_DIR="$PWD"
else
    # If invoked as ./scripts/install-run-linux.sh from a checkout, use that.
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        maybe="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"
        [[ -n "$maybe" ]] && is_checkout "$maybe" && PROJECT_DIR="$maybe"
    fi
fi
if [[ -z "$PROJECT_DIR" ]]; then
    command -v git >/dev/null 2>&1 || die "'git' is required to clone the repository."
    if [[ -d sshpilot/.git ]]; then
        info "Reusing existing ./sshpilot checkout."
    else
        info "Cloning $REPO_URL …"
        git clone "$REPO_URL" sshpilot || die "git clone failed."
    fi
    PROJECT_DIR="$PWD/sshpilot"
fi
cd "$PROJECT_DIR"
ok "Using source tree: $PROJECT_DIR"

# --- python check -----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found even after install."
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)'; then
    warn "Python $(python3 -c 'import platform; print(platform.python_version())') detected; sshPilot is tested on 3.12/3.13. Continuing anyway."
fi

# --- venv + python deps -----------------------------------------------------
if [[ ! -d .venv ]]; then
    info "Creating virtual environment (.venv, --system-site-packages)…"
    python3 -m venv --system-site-packages .venv || die "failed to create the venv (is python3-venv installed?)."
fi
# shellcheck disable=SC1091
source .venv/bin/activate
info "Installing Python dependencies…"
pip install --upgrade pip >/dev/null || die "failed to upgrade pip."
pip install -r requirements.txt || die "failed to install Python dependencies."
ok "Environment ready."

# --- run --------------------------------------------------------------------
if [[ "$DO_RUN" -eq 1 ]]; then
    info "Launching sshPilot…"
    exec python3 run.py
else
    cat <<EOF

${C_GREEN}Done.${C_RESET} To run sshPilot later:

  cd "$PROJECT_DIR"
  source .venv/bin/activate
  python3 run.py
EOF
fi
