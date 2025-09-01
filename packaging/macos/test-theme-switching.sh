#!/bin/bash

# Test script to verify theme switching environment variables
echo "Testing theme switching environment variables..."

# Set the same environment variables as the launcher script
export XDG_DATA_DIRS="/usr/local/share:/tmp"
export GTK_ICON_THEME="Adwaita"
export XDG_ICON_THEME="Adwaita"
export GTK_APPLICATION_PREFERS_DARK_THEME=""
export GTK_THEME_VARIANT=""
export GTK_THEME=""
export GTK_USE_PORTAL="1"
export GTK_CSD="1"

echo "Theme switching configuration:"
echo "GTK_ICON_THEME: $GTK_ICON_THEME"
echo "GTK_THEME: $GTK_THEME (empty = system default)"
echo "GTK_APPLICATION_PREFERS_DARK_THEME: $GTK_APPLICATION_PREFERS_DARK_THEME"
echo "GTK_THEME_VARIANT: $GTK_THEME_VARIANT"
echo "GTK_USE_PORTAL: $GTK_USE_PORTAL"
echo "GTK_CSD: $GTK_CSD"

echo ""
echo "Theme switching is now enabled!"
echo "- GTK_THEME is empty, allowing automatic theme detection"
echo "- The app will automatically switch between light/dark themes"
echo "- Theme changes based on macOS System Preferences > General > Appearance"
echo ""
echo "To test theme switching:"
echo "1. Go to System Preferences > General > Appearance"
echo "2. Switch between 'Light' and 'Dark'"
echo "3. The sshPilot app should automatically follow the system theme"
