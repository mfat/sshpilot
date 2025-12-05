#!/bin/bash
# Script to copy Adwaita symbolic icons for bundling

# Don't use set -e as arithmetic operations can return non-zero
set -u

ICON_BASE="/usr/share/icons/Adwaita/symbolic"
ICON_DEST="sshpilot/resources/icons/scalable/actions"

# Create destination directory
mkdir -p "$ICON_DEST"

# Function to find and copy an icon
find_and_copy_icon() {
    local icon_name=$1
    local dest="$ICON_DEST/$icon_name.svg"
    
    # Search in common locations
    local search_paths=(
        "$ICON_BASE/actions"
        "$ICON_BASE/status"
        "$ICON_BASE/devices"
        "$ICON_BASE/places"
        "$ICON_BASE/apps"
        "$ICON_BASE/mimetypes"
    )
    
    for path in "${search_paths[@]}"; do
        local src="$path/$icon_name.svg"
        if [ -f "$src" ]; then
            cp "$src" "$dest"
            return 0
        fi
    done
    
    # If not found, try recursive search
    local found=$(find "$ICON_BASE" -name "$icon_name.svg" 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        cp "$found" "$dest"
        return 0
    fi
    
    return 1
}

# List of icons used in the application
ICONS=(
    "edit-undo-symbolic"
    "edit-redo-symbolic"
    "system-search-symbolic"
    "zoom-out-symbolic"
    "zoom-fit-best-symbolic"
    "zoom-in-symbolic"
    "sidebar-show-symbolic"
    "go-home-symbolic"
    "list-add-symbolic"
    "view-reveal-symbolic"
    "view-conceal-symbolic"
    "open-menu-symbolic"
    "document-edit-symbolic"
    "user-trash-symbolic"
    "edit-copy-symbolic"
    "folder-symbolic"
    "utilities-terminal-symbolic"
    "dialog-password-symbolic"
    "document-send-symbolic"
    "view-sort-ascending-symbolic"
    "preferences-system-symbolic"
    "tab-new-symbolic"
    "dialog-warning-symbolic"
    "view-refresh-symbolic"
    "view-list-symbolic"
    "view-grid-symbolic"
    "network-server-symbolic"
    "preferences-desktop-keyboard-symbolic"
    "help-browser-symbolic"
    "help-about-symbolic"
    "window-close-symbolic"
    "go-up-symbolic"
    "go-down-symbolic"
    "dialog-error-symbolic"
    "pan-end-symbolic"
    "pan-down-symbolic"
    "computer-symbolic"
    "network-offline-symbolic"
    "network-idle-symbolic"
    "folder-open-symbolic"
    "input-keyboard-symbolic"
    "folder-remote-symbolic"
    "text-x-generic-symbolic"
    "folder-new-symbolic"
    "go-previous-symbolic"
    "view-dual-symbolic"
    "network-transmit-receive-symbolic"
    "process-working-symbolic"
    "text-editor-symbolic"
    "edit-cut-symbolic"
    "edit-paste-symbolic"
    "document-save-symbolic"
    "document-properties-symbolic"
)

echo "Copying icons from Adwaita theme to $ICON_DEST..."

copied=0
missing=0

for icon in "${ICONS[@]}"; do
    if find_and_copy_icon "$icon" 2>/dev/null; then
        echo "  ✓ Copied $icon.svg"
        ((copied++))
    else
        echo "  ✗ Missing: $icon.svg"
        ((missing++))
    fi
done

echo ""
echo "Summary: $copied icons copied, $missing icons missing"

if [ $missing -gt 0 ]; then
    echo "Warning: Some icons were not found. They may need to be created or use fallbacks."
    # Don't exit with error - continue with what we have
fi

echo "Icons copied successfully!"

