"""
Icon utility functions for loading bundled icons with fallback to system icons.

Bundled icons are registered on Gtk.IconTheme via set_resource_path() in main.py.
We resolve icons through Gio.ThemedIcon so GTK's icon-theme cache is used and
symbolic icons are recolored correctly.  Alias entries in _ICON_RESOURCE_MAP
(names that map to a different resource file) still load via Gio.FileIcon.
Resolved Gio.Icon instances are cached per icon name for the process lifetime.
"""

import logging
from typing import Dict, Optional

from gi.repository import Gtk, Gio

logger = logging.getLogger(__name__)

# Process-wide cache: icon name -> Gio.Icon (ThemedIcon or FileIcon).
_gicon_cache: Dict[str, Gio.Icon] = {}

# Track if we've patched Gtk.Image
_patched = False

# Map of icon names to bundled resource paths.  Direct name/file matches are
# resolved via Gtk.IconTheme; alias entries fall back to Gio.FileIcon.
_ICON_RESOURCE_MAP = {
    'folder-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-symbolic.svg',
    'text-x-generic-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/text-x-generic-symbolic.svg',
    'folder-open-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-open-symbolic.svg',
    'folder-new-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-new-symbolic.svg',
    'folder-remote-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-remote-symbolic.svg',
    'go-previous-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-previous-symbolic.svg',
    'go-up-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-up-symbolic.svg',
    'go-down-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-down-symbolic.svg',
    'top-large-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/top-large-symbolic.svg',
    'bottom-large-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/bottom-large-symbolic.svg',
    'go-home-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-home-symbolic.svg',
    'view-refresh-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-refresh-symbolic.svg',
    'view-list-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-list-symbolic.svg',
    'view-grid-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-grid-symbolic.svg',
    'view-conceal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-conceal-symbolic.svg',
    'view-reveal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-reveal-symbolic.svg',
    'view-dual-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-dual-symbolic.svg',
    'view-pin-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-pin-symbolic.svg',
    'double-ended-arrows-horizontal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/double-ended-arrows-horizontal-symbolic.svg',
    'double-ended-arrows-vertical-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/double-ended-arrows-vertical-symbolic.svg',
    'terminal-split-horizontal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/terminal-split-horizontal.svg',
    'terminal-split-vertical-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/terminal-split-vertical.svg',
    'format-justify-fill-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/format-justify-fill-symbolic.svg',
    'list-add-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/list-add-symbolic.svg',
    'user-trash-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/user-trash-symbolic.svg',
    'document-edit-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/document-edit-symbolic.svg',
    'dot-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dot-symbolic.svg',
    'big-dot-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/big-dot-symbolic.svg',
    'edit-copy-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-copy-symbolic.svg',
    'edit-undo-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-undo-symbolic.svg',
    'edit-redo-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-redo-symbolic.svg',
    'system-search-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/system-search-symbolic.svg',
    'system-run-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/system-run-symbolic.svg',
    'sidebar-show-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/sidebar-show-symbolic.svg',
    'open-menu-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/open-menu-symbolic.svg',
    'org.gnome.Settings-system-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/org.gnome.Settings-system-symbolic.svg',
    'window-close-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/window-close-symbolic.svg',
    'utilities-terminal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/utilities-terminal-symbolic.svg',
    'dialog-password-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-password-symbolic.svg',
    'password-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/password-symbolic.svg',
    'document-send-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/document-send-symbolic.svg',
    'vertical-arrows-long-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/vertical-arrows-long-symbolic.svg',
    'view-sort-ascending-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-sort-ascending-symbolic.svg',
    'preferences-system-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/preferences-system-symbolic.svg',
    'settings-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/settings-symbolic.svg',
    'tab-new-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/tab-new-symbolic.svg',
    'dialog-warning-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-warning-symbolic.svg',
    'dialog-error-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-error-symbolic.svg',
    'error-outline-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/error-outline-symbolic.svg',
    'info-outline-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/info-outline-symbolic.svg',
    'warning-outline-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/warning-outline-symbolic.svg',
    'color-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/color-symbolic.svg',
    'brand-docker-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/brand-docker-symbolic.svg',
    'success-small-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/success-small-symbolic.svg',
    'network-server-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-server-symbolic.svg',
    'preferences-desktop-keyboard-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/preferences-desktop-keyboard-symbolic.svg',
    'help-browser-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/help-browser-symbolic.svg',
    'help-about-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/help-about-symbolic.svg',
    'pan-end-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/pan-end-symbolic.svg',
    'pan-down-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/pan-down-symbolic.svg',
    'computer-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/computer-symbolic.svg',
    'dark-mode-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dark-mode-symbolic.svg',
    'network-offline-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-offline-symbolic.svg',
    'network-idle-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-idle-symbolic.svg',
    'input-keyboard-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/input-keyboard-symbolic.svg',
    'zoom-out-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/zoom-out-symbolic.svg',
    'zoom-fit-best-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/zoom-fit-best-symbolic.svg',
    'zoom-in-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/zoom-in-symbolic.svg',
    'network-transmit-receive-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-transmit-receive-symbolic.svg',
    'network-shield-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-shield-symbolic.svg',
    # Connection-status indicators (recolored by the .success/.warning/.error classes).
    'wired-lock-closed-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/wired-lock-closed-symbolic.svg',
    'wired-lock-dots-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/wired-lock-dots-symbolic.svg',
    'wired-lock-none-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/wired-lock-none-symbolic.svg',
    # Full-color port-forwarding badges (local/remote/dynamic).
    'L': '/io/github/mfat/sshpilot/icons/scalable/actions/L.svg',
    'R': '/io/github/mfat/sshpilot/icons/scalable/actions/R.svg',
    'D': '/io/github/mfat/sshpilot/icons/scalable/actions/D.svg',
    'network-shield-dots-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-shield-dots-symbolic.svg',
    'network-shield-crossed-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-shield-crossed-symbolic.svg',
    'text-editor-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/text-editor-symbolic.svg',
    'edit-cut-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-cut-symbolic.svg',
    'edit-paste-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-paste-symbolic.svg',
    'document-save-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/document-save-symbolic.svg',
    'document-properties-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/document-properties-symbolic.svg',
    # Aliases for icons that don't exist but are requested
    'preferences-desktop-keyboard-shortcuts-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/preferences-desktop-keyboard-symbolic.svg',
    'software-update-available-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-refresh-symbolic.svg',
    'applications-graphics-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/preferences-system-symbolic.svg',
    'network-workgroup-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-server-symbolic.svg',
    'applications-system-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/preferences-system-symbolic.svg',
    'tag-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/tag-symbolic.svg',
    # Additional missing icons - map to closest available
    'network-receive-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-transmit-receive-symbolic.svg',
    'security-high-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-warning-symbolic.svg',
    'process-working-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-refresh-symbolic.svg',
    'media-record-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-idle-symbolic.svg',
    'io.github.mfat.sshpilot': '/io/github/mfat/sshpilot/sshpilot.svg',  # App icon
    # Adwaita non-symbolic mimetype icons (vendored under resources/icons/scalable/mimetypes)
    'application-certificate': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/application-certificate.svg',
    'application-x-addon': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/application-x-addon.svg',
    'application-x-executable': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/application-x-executable.svg',
    'application-x-firmware': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/application-x-firmware.svg',
    'application-x-generic': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/application-x-generic.svg',
    'application-x-sharedlib': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/application-x-sharedlib.svg',
    'audio-x-generic': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/audio-x-generic.svg',
    'font-x-generic': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/font-x-generic.svg',
    'image-x-generic': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/image-x-generic.svg',
    'inode-directory': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/inode-directory.svg',
    'inode-symlink': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/inode-symlink.svg',
    'model': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/model.svg',
    'package-x-generic': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/package-x-generic.svg',
    'text-html': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/text-html.svg',
    'text-x-generic': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/text-x-generic.svg',
    'text-x-preview': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/text-x-preview.svg',
    'text-x-script': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/text-x-script.svg',
    'video-x-generic': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/video-x-generic.svg',
    'x-office-addressbook': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-addressbook.svg',
    'x-office-document': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-document.svg',
    'x-office-document-template': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-document-template.svg',
    'x-office-drawing': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-drawing.svg',
    'x-office-presentation': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-presentation.svg',
    'x-office-presentation-template': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-presentation-template.svg',
    'x-office-spreadsheet': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-spreadsheet.svg',
    'x-office-spreadsheet-template': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-office-spreadsheet-template.svg',
    'x-package-repository': '/io/github/mfat/sshpilot/icons/scalable/mimetypes/x-package-repository.svg',
}


def _is_direct_theme_lookup(icon_name: str, resource_path: str) -> bool:
    """True when the bundled resource file name matches the themed icon name."""
    return resource_path.endswith(f'/{icon_name}.svg')


def get_gicon_for_icon_name(icon_name: str) -> Gio.Icon:
    """Return a cached Gio.Icon for icon_name (ThemedIcon or alias FileIcon)."""
    cached = _gicon_cache.get(icon_name)
    if cached is not None:
        return cached

    resource_path = _ICON_RESOURCE_MAP.get(icon_name)
    if resource_path and not _is_direct_theme_lookup(icon_name, resource_path):
        file_obj = Gio.File.new_for_uri(f'resource://{resource_path}')
        icon: Gio.Icon = Gio.FileIcon.new(file_obj)
    else:
        icon = Gio.ThemedIcon.new(icon_name)

    _gicon_cache[icon_name] = icon
    return icon


def new_image_from_icon_name(icon_name: str, size: Optional[int] = None) -> Gtk.Image:
    """
    Create a Gtk.Image from an icon name, preferring bundled icons over system icons.

    Args:
        icon_name: The name of the icon (e.g., 'folder-symbolic')
        size: Optional pixel size for the icon

    Returns:
        A Gtk.Image widget with the icon loaded
    """
    image = Gtk.Image.new_from_gicon(get_gicon_for_icon_name(icon_name))
    if size:
        image.set_pixel_size(size)
    return image


def set_icon_from_name(image: Gtk.Image, icon_name: str) -> None:
    """Set an icon on a Gtk.Image widget, preferring bundled icons over system icons."""
    image.set_from_gicon(get_gicon_for_icon_name(icon_name))

def patch_gtk_image():
    """Patch Gtk.Image methods to use the cached icon resolver."""
    global _patched
    if _patched:
        return

    @classmethod
    def new_from_icon_name_patched(cls, icon_name: str):
        return new_image_from_icon_name(icon_name)

    def set_from_icon_name_patched(self, icon_name: str):
        set_icon_from_name(self, icon_name)

    Gtk.Image.new_from_icon_name = new_from_icon_name_patched
    Gtk.Image.set_from_icon_name = set_from_icon_name_patched

    _patched = True
    logger.debug("Patched Gtk.Image methods to use cached icon resolver")

def new_gicon_from_icon_name(icon_name: str) -> Gio.Icon:
    """Create a Gio.Icon from an icon name (cached; same resolver as Gtk.Image helpers)."""
    return get_gicon_for_icon_name(icon_name)

def new_button_from_icon_name(icon_name: str) -> Gtk.Button:
    """
    Create a Gtk.Button with an icon, preferring bundled icons over system icons.
    
    Args:
        icon_name: The name of the icon (e.g., 'folder-symbolic')
        
    Returns:
        A Gtk.Button with the icon set
    """
    button = Gtk.Button()
    # Use Image widget as child (works in all GTK4 versions)
    image = new_image_from_icon_name(icon_name)
    button.set_child(image)
    return button

def set_button_icon(button: Gtk.Widget, icon_name: str) -> None:
    """
    Set an icon on a button widget (Button, ToggleButton, etc.), preferring bundled icons.
    
    Creates an Image widget using bundled icons and sets it as the child of the button.
    This works for both Button and ToggleButton in GTK4.
    
    Args:
        button: The button widget (Gtk.Button, Gtk.ToggleButton, etc.)
        icon_name: The name of the icon (e.g., 'folder-symbolic')
    """
    # Create an Image widget with bundled icon and set it as the child
    # This works for both Button and ToggleButton in GTK4
    image = new_image_from_icon_name(icon_name)
    button.set_child(image)

