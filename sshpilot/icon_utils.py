"""
Icon utility functions for loading bundled icons with fallback to system icons.
This ensures bundled icons are used when available, providing a consistent look
across different distributions and desktop environments.

Following GNOME documentation (https://developer.gnome.org/documentation/tutorials/themed-icons.html)
and GTK4 IconTheme API (https://docs.gtk.org/gtk4/class.IconTheme.html), we:
1. Add resource path to icon theme using set_resource_path() API
2. Manually check resources first to ensure bundled icons take priority
3. Fall back to icon theme system (which may still find system themes)

This hybrid approach ensures bundled icons are used while maintaining compatibility
with GTK's icon theme system for symbolic icon recoloring.
"""

import logging
from gi.repository import Gtk, Gio, GLib

logger = logging.getLogger(__name__)

# Track if we've patched Gtk.Image
_patched = False

# Store original methods before patching (will be set by patch_gtk_image)
_original_new_from_icon_name = None
_original_set_from_icon_name = None

# Map of icon names to their resource paths (kept for reference/documentation)
# Note: We no longer use this for manual loading - the icon theme system handles it
# This map documents which icons we have bundled and their locations
_ICON_RESOURCE_MAP = {
    'folder-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-symbolic.svg',
    'text-x-generic-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/text-x-generic-symbolic.svg',
    'folder-open-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-open-symbolic.svg',
    'folder-new-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-new-symbolic.svg',
    'folder-remote-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/folder-remote-symbolic.svg',
    'go-previous-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-previous-symbolic.svg',
    'go-up-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-up-symbolic.svg',
    'go-down-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-down-symbolic.svg',
    'go-home-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/go-home-symbolic.svg',
    'view-refresh-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-refresh-symbolic.svg',
    'view-list-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-list-symbolic.svg',
    'view-grid-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-grid-symbolic.svg',
    'view-conceal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-conceal-symbolic.svg',
    'view-reveal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-reveal-symbolic.svg',
    'view-dual-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-dual-symbolic.svg',
    'format-justify-fill-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/format-justify-fill-symbolic.svg',
    'list-add-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/list-add-symbolic.svg',
    'user-trash-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/user-trash-symbolic.svg',
    'document-edit-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/document-edit-symbolic.svg',
    'edit-copy-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-copy-symbolic.svg',
    'edit-undo-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-undo-symbolic.svg',
    'edit-redo-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/edit-redo-symbolic.svg',
    'system-search-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/system-search-symbolic.svg',
    'sidebar-show-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/sidebar-show-symbolic.svg',
    'open-menu-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/open-menu-symbolic.svg',
    'window-close-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/window-close-symbolic.svg',
    'utilities-terminal-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/utilities-terminal-symbolic.svg',
    'dialog-password-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-password-symbolic.svg',
    'document-send-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/document-send-symbolic.svg',
    'vertical-arrows-long-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/vertical-arrows-long-symbolic.svg',
    'view-sort-ascending-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-sort-ascending-symbolic.svg',
    'preferences-system-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/preferences-system-symbolic.svg',
    'tab-new-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/tab-new-symbolic.svg',
    'dialog-warning-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-warning-symbolic.svg',
    'dialog-error-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-error-symbolic.svg',
    'network-server-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-server-symbolic.svg',
    'preferences-desktop-keyboard-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/preferences-desktop-keyboard-symbolic.svg',
    'help-browser-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/help-browser-symbolic.svg',
    'help-about-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/help-about-symbolic.svg',
    'pan-end-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/pan-end-symbolic.svg',
    'pan-down-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/pan-down-symbolic.svg',
    'computer-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/computer-symbolic.svg',
    'network-offline-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-offline-symbolic.svg',
    'network-idle-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-idle-symbolic.svg',
    'input-keyboard-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/input-keyboard-symbolic.svg',
    'zoom-out-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/zoom-out-symbolic.svg',
    'zoom-fit-best-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/zoom-fit-best-symbolic.svg',
    'zoom-in-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/zoom-in-symbolic.svg',
    'network-transmit-receive-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-transmit-receive-symbolic.svg',
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
    # Additional missing icons - map to closest available
    'network-receive-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-transmit-receive-symbolic.svg',
    'security-high-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/dialog-warning-symbolic.svg',
    'process-working-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/view-refresh-symbolic.svg',
    'media-record-symbolic': '/io/github/mfat/sshpilot/icons/scalable/actions/network-idle-symbolic.svg',
    'io.github.mfat.sshpilot': '/io/github/mfat/sshpilot/sshpilot.svg',  # App icon
}

def new_image_from_icon_name(icon_name: str, size: int = None) -> Gtk.Image:
    """
    Create a Gtk.Image from an icon name, preferring bundled icons over system icons.
    
    We first check for bundled icons in resources and load them directly. This ensures
    bundled icons are always used instead of system theme icons. For symbolic icons,
    GTK will automatically recolor them based on the `-symbolic` suffix in the filename.
    
    Args:
        icon_name: The name of the icon (e.g., 'folder-symbolic')
        size: Optional pixel size for the icon
        
    Returns:
        A Gtk.Image widget with the icon loaded
    """
    # Check for bundled icon first - this ensures our icons take priority
    resource_path = _ICON_RESOURCE_MAP.get(icon_name)
    if resource_path:
        try:
            # Verify resource exists
            Gio.resources_lookup_data(resource_path, Gio.ResourceLookupFlags.NONE)
            
            # Load from resource using FileIcon
            # Note: According to GNOME docs, icons loaded via FileIcon won't be automatically
            # recolored, but GTK should still handle symbolic icons based on filename suffix
            resource_uri = f"resource://{resource_path}"
            file_obj = Gio.File.new_for_uri(resource_uri)
            file_icon = Gio.FileIcon.new(file_obj)
            image = Gtk.Image.new_from_gicon(file_icon)
            
            logger.debug(f"Loaded bundled icon from resource: {icon_name}")
            
            if size:
                image.set_pixel_size(size)
            return image
        except (GLib.Error, Exception) as e:
            logger.debug(f"Bundled icon not found for {icon_name}, using icon theme: {e}")
            pass
    
    # Fall back to icon theme system (will check resource path, then system themes)
    # This ensures symbolic icons are properly recolored when using system icons
    if _original_new_from_icon_name is not None:
        image = _original_new_from_icon_name(icon_name)
    else:
        image = Gtk.Image.new_from_icon_name(icon_name)
    
    if size:
        image.set_pixel_size(size)
    
    return image

def set_icon_from_name(image: Gtk.Image, icon_name: str) -> None:
    """
    Set an icon on a Gtk.Image widget, preferring bundled icons over system icons.
    
    We first check for bundled icons in resources and load them directly. This ensures
    bundled icons are always used instead of system theme icons.
    
    Args:
        image: The Gtk.Image widget to set the icon on
        icon_name: The name of the icon (e.g., 'folder-symbolic')
    """
    # Check for bundled icon first - this ensures our icons take priority
    resource_path = _ICON_RESOURCE_MAP.get(icon_name)
    if resource_path:
        try:
            # Verify resource exists
            Gio.resources_lookup_data(resource_path, Gio.ResourceLookupFlags.NONE)
            
            # Load from resource using FileIcon
            resource_uri = f"resource://{resource_path}"
            file_obj = Gio.File.new_for_uri(resource_uri)
            file_icon = Gio.FileIcon.new(file_obj)
            image.set_from_gicon(file_icon)
            
            logger.debug(f"Set bundled icon from resource: {icon_name}")
            return
        except (GLib.Error, Exception) as e:
            logger.debug(f"Bundled icon not found for {icon_name}, using icon theme: {e}")
            pass
    
    # Fall back to icon theme system
    if _original_set_from_icon_name is not None:
        _original_set_from_icon_name(image, icon_name)
    else:
        image.set_from_icon_name(icon_name)

def patch_gtk_image():
    """
    Patch Gtk.Image methods to use our helper functions.
    
    Since we add the resource path to the icon theme in main.py, GTK will
    automatically find our bundled icons. This patching ensures we use a
    consistent API and can add logging if needed.
    """
    global _patched, _original_new_from_icon_name, _original_set_from_icon_name
    if _patched:
        return
    
    # Store original methods BEFORE patching
    _original_new_from_icon_name = Gtk.Image.new_from_icon_name
    _original_set_from_icon_name = Gtk.Image.set_from_icon_name
    
    @classmethod
    def new_from_icon_name_patched(cls, icon_name: str):
        """Patched version that uses our helper"""
        return new_image_from_icon_name(icon_name)
    
    def set_from_icon_name_patched(self, icon_name: str):
        """Patched version that uses our helper"""
        set_icon_from_name(self, icon_name)
    
    # Patch the class methods
    Gtk.Image.new_from_icon_name = new_from_icon_name_patched
    Gtk.Image.set_from_icon_name = set_from_icon_name_patched
    
    _patched = True
    logger.debug("Patched Gtk.Image methods to use icon theme system")

def new_gicon_from_icon_name(icon_name: str) -> Gio.Icon:
    """
    Create a Gio.Icon from an icon name, using bundled icons when available.
    
    This is useful for buttons and other widgets that use GIcon instead of Gtk.Image.
    Following GNOME documentation, we use ThemedIcon which will check our resource path
    via the icon theme system, ensuring symbolic icons are properly handled.
    
    Args:
        icon_name: The name of the icon (e.g., 'folder-symbolic')
        
    Returns:
        A Gio.ThemedIcon (icon theme system will find bundled icons from resource path)
    """
    # Use ThemedIcon - the icon theme system will check our resource path first
    # This ensures symbolic icons are properly handled by GTK
    return Gio.ThemedIcon.new(icon_name)

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

