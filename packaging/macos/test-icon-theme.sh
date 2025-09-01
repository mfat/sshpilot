#!/bin/bash

# Test script to verify icon theme environment variables
echo "Testing icon theme environment variables..."

# Set the same environment variables as the launcher script
export XDG_DATA_DIRS="/usr/local/share:/tmp"
export GTK_THEME="Adwaita"
export GTK_ICON_THEME="Adwaita"
export XDG_ICON_THEME="Adwaita"

echo "XDG_DATA_DIRS: $XDG_DATA_DIRS"
echo "GTK_THEME: $GTK_THEME"
echo "GTK_ICON_THEME: $GTK_ICON_THEME"
echo "XDG_ICON_THEME: $XDG_ICON_THEME"

# Check if icon themes are accessible
echo ""
echo "Checking icon theme accessibility:"
echo "Adwaita theme exists: $([ -d "/usr/local/share/icons/Adwaita" ] && echo "YES" || echo "NO")"
echo "hicolor theme exists: $([ -d "/usr/local/share/icons/hicolor" ] && echo "YES" || echo "NO")"

# List available icon themes
echo ""
echo "Available icon themes in /usr/local/share/icons/:"
ls -la /usr/local/share/icons/ 2>/dev/null || echo "No icon themes found"
