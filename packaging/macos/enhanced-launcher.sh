#!/bin/bash

# Enhanced launcher script for macOS app bundle
# Fixes common double-click issues by setting proper environment

# Debug logging
echo "Starting SSHPilot..." > /tmp/sshpilot_debug.log
echo "Timestamp: $(date)" >> /tmp/sshpilot_debug.log
echo "Original PATH: $PATH" >> /tmp/sshpilot_debug.log
echo "Original PWD: $PWD" >> /tmp/sshpilot_debug.log
echo "Original USER: $USER" >> /tmp/sshpilot_debug.log
echo "Original HOME: $HOME" >> /tmp/sshpilot_debug.log

# Get the app bundle directory
APP_DIR="$(cd "$(dirname "$0")/.."; pwd -P)"
RESOURCES_DIR="$APP_DIR/Resources"
echo "App directory: $APP_DIR" >> /tmp/sshpilot_debug.log
echo "Resources directory: $RESOURCES_DIR" >> /tmp/sshpilot_debug.log

# Set explicit PATH with system directories first
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
echo "New PATH: $PATH" >> /tmp/sshpilot_debug.log

# Change to the resources directory (explicit working directory)
cd "$RESOURCES_DIR"
echo "Changed to directory: $(pwd)" >> /tmp/sshpilot_debug.log

# Set up Python environment (use system Python with bundled site-packages)
export PYTHONPATH="$RESOURCES_DIR/app:$RESOURCES_DIR/lib/python3.13/site-packages:$RESOURCES_DIR/lib/python3.12/site-packages:${PYTHONPATH:-}"
echo "PYTHONPATH: $PYTHONPATH" >> /tmp/sshpilot_debug.log

# GTK/GLib/GObject Introspection paths (bundled inside app)
export GI_TYPELIB_PATH="$RESOURCES_DIR/lib/girepository-1.0"
export GSETTINGS_SCHEMA_DIR="$RESOURCES_DIR/share/glib-2.0/schemas"
export XDG_DATA_DIRS="$RESOURCES_DIR/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

# GTK-specific paths
export GTK_DATA_PREFIX="$RESOURCES_DIR"
export GTK_EXE_PREFIX="$RESOURCES_DIR"
export GTK_PATH="$RESOURCES_DIR"

# macOS loader paths (bundled libraries)
export DYLD_LIBRARY_PATH="$RESOURCES_DIR/lib"
export DYLD_FALLBACK_LIBRARY_PATH="$RESOURCES_DIR/lib:$RESOURCES_DIR/../Frameworks:${DYLD_FALLBACK_LIBRARY_PATH:-}"

# Icon and theme settings
export GTK_ICON_THEME="Adwaita"
export XDG_ICON_THEME="Adwaita"

# Theme detection and system integration
export GTK_THEME_VARIANT=""
export GTK_APPLICATION_PREFERS_DARK_THEME=""

# Only detect system appearance if the app is set to "Follow System"
APP_CONFIG_DIR="${HOME}/.local/share/sshPilot"
if [ -f "${APP_CONFIG_DIR}/config.json" ]; then
    # Extract the saved theme from config (default to 'default' if not found)
    SAVED_THEME=$(grep -o '"app-theme": *"[^"]*"' "${APP_CONFIG_DIR}/config.json" 2>/dev/null | cut -d'"' -f4 || echo "default")
    
    if [ "$SAVED_THEME" = "default" ]; then
        # Only detect system theme when app is set to "Follow System"
        if command -v defaults >/dev/null 2>&1; then
            # Check if macOS is in dark mode
            if defaults read -g AppleInterfaceStyle 2>/dev/null | grep -q "Dark"; then
                export GTK_THEME_VARIANT="dark"
                export GTK_APPLICATION_PREFERS_DARK_THEME="1"
            else
                export GTK_THEME_VARIANT="light"
                export GTK_APPLICATION_PREFERS_DARK_THEME="0"
            fi
        fi
    fi
    # If theme is manually set to 'light' or 'dark', don't override with system detection
fi

export GTK_USE_PORTAL="1"
export GTK_CSD="1"

# Set basic system environment variables
export HOME="${HOME:-/tmp}"
export USER="${USER:-$(whoami)}"
export TMPDIR="${TMPDIR:-/tmp}"

# Clear any potentially problematic environment variables
unset DBUS_SESSION_BUS_ADDRESS

# Log final environment
echo "Final environment:" >> /tmp/sshpilot_debug.log
echo "  PATH: $PATH" >> /tmp/sshpilot_debug.log
echo "  PYTHONPATH: $PYTHONPATH" >> /tmp/sshpilot_debug.log
echo "  GI_TYPELIB_PATH: $GI_TYPELIB_PATH" >> /tmp/sshpilot_debug.log
echo "  Working directory: $(pwd)" >> /tmp/sshpilot_debug.log

# Start the application using system Python with bundled libraries
echo "Launching Python application..." >> /tmp/sshpilot_debug.log
exec python3 -c "
import sys
import os
sys.path.insert(0, os.path.join(os.getcwd(), 'app'))
from app.main import main
main()
" 2>&1 | tee -a /tmp/sshpilot_debug.log
