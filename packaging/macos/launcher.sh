#!/bin/sh

if test "x$GTK_DEBUG_LAUNCHER" != x; then
    set -x
fi

if test "x$GTK_DEBUG_GDB" != x; then
    EXEC="gdb --args"
else
    EXEC=exec
fi

name=`basename "$0"`
tmp="$0"
tmp=`dirname "$tmp"`
tmp=`dirname "$tmp"`
bundle=`dirname "$tmp"`
bundle_contents="$bundle"/Contents
bundle_res="$bundle_contents"/Resources
bundle_lib="$bundle_res"/lib
bundle_bin="$bundle_res"/bin
bundle_data="$bundle_res"/share
bundle_etc="$bundle_res"/etc

export XDG_CONFIG_DIRS="$bundle_etc"/xdg
export XDG_DATA_DIRS="/usr/local/share:$bundle_data"
export GTK_DATA_PREFIX="$bundle_res"
export GTK_EXE_PREFIX="$bundle_res"
export GTK_PATH="$bundle_res"

# Use system Homebrew GTK libraries instead of bundled ones
export DYLD_FALLBACK_LIBRARY_PATH="/usr/local/lib:$DYLD_FALLBACK_LIBRARY_PATH"
export GI_TYPELIB_PATH="/usr/local/lib/girepository-1.0:$GI_TYPELIB_PATH"

# Set icon theme to use system Adwaita theme (but allow theme switching)
export GTK_ICON_THEME="Adwaita"
export XDG_ICON_THEME="Adwaita"

# Enable theme switching by not hardcoding GTK_THEME
# GTK will automatically detect light/dark mode from macOS
export GTK_APPLICATION_PREFERS_DARK_THEME=""
export GTK_THEME_VARIANT=""

# Force GTK to use system default theme (enables light/dark switching)
export GTK_THEME=""

# Enable macOS appearance detection for theme switching
export GTK_USE_PORTAL="1"
export GTK_CSD="1"

# Strip out the argument added by the OS.
if /bin/expr "x$1" : '^x-psn_' > /dev/null; then
    shift 1
fi

# Execute the Python launcher script from Resources
cd "$bundle_res"
$EXEC "python3" "sshPilot-launcher-bin" "$@"
