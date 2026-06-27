"""About / help / keyboard-shortcuts window for MainWindow.

Extracted verbatim from window.py as a mixin (matching WindowActions /
WindowBroadcastMixin / WindowSessionMixin) to shrink the window.py god-object.
MainWindow inherits this; methods keep their signatures and `self.` state
access, so this is a pure code move with no behavior change.
"""

import logging

from gi.repository import Adw, Gio, Gtk
from gettext import gettext as _

from .platform_utils import is_macos

logger = logging.getLogger(__name__)


class WindowHelpMixin:
    """About dialog, help-URL launcher, and the keyboard-shortcuts window."""

    def show_about_dialog(self):
        """Show about dialog"""
        # Use Adw.AboutDialog to get support-url and issue-url properties
        about = Adw.AboutDialog()
        about.set_application_name('SSH Pilot')
        try:
            from . import __version__ as APP_VERSION
        except Exception:
            APP_VERSION = "0.0.0"
        about.set_version(APP_VERSION)
        about.set_application_icon('io.github.mfat.sshpilot')
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_website('https://sshpilot.app')
        about.set_issue_url('https://github.com/mfat/sshpilot/issues')
        about.set_copyright('© 2025 mFat')
        about.set_developers(['mFat <newmfat@gmail.com>'])
        about.set_translator_credits('')
        
        # Present the dialog as a child of this window
        about.present(self)

    def open_help_url(self):
        """Open the SSH Pilot wiki using a portal-friendly launcher."""
        url = "https://github.com/mfat/sshpilot/wiki"
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
            logger.info("Opened help URL via default handler: %s", url)
            return
        except Exception as exc:
            logger.debug("Portal-friendly launcher failed for %s: %s", url, exc)

        # Fall back to old webbrowser module as a last resort
        try:
            import webbrowser

            if not webbrowser.open(url):
                raise RuntimeError("webbrowser.open returned False")
            logger.info("Opened help URL via webbrowser fallback: %s", url)
            return
        except Exception as exc:
            logger.error("Failed to open help URL: %s", exc)

        # Display a minimal error dialog if all launchers fail
        try:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Failed to open help",
                secondary_text=f"Please open this page manually:\n{url}"
            )
            dialog.present()
        except Exception:
            pass

    def show_shortcuts_window(self):
        """Display keyboard shortcuts using Gtk.ShortcutsWindow"""
        # Always rebuild to show current shortcuts (including user customizations)
        self._shortcuts_window = self._build_shortcuts_window()
        try:
            self.set_help_overlay(self._shortcuts_window)
        except Exception:
            pass
        self._shortcuts_window.present()

    def _build_shortcuts_window(self):
        mac = is_macos()
        primary = '<Meta>' if mac else '<primary>'

        win = Gtk.ShortcutsWindow(transient_for=self, modal=True)
        win.set_title(_('Keyboard Shortcuts'))

        # Don't set custom titlebar for ShortcutsWindow to avoid GTK stack issues
        # The edit shortcuts functionality can be accessed via the main menu instead

        section = Gtk.ShortcutsSection()
        section.set_property('title', _('Keyboard Shortcuts'))

        # Use enhanced static shortcuts that can show customizations without causing crashes
        self._add_safe_current_shortcuts(section, primary)

        win.add_section(section)
        return win

    def _add_safe_current_shortcuts(self, section, primary):
        """Add shortcuts with current customizations using a safe approach"""
        # Get current shortcuts safely
        current_shortcuts = self._get_safe_current_shortcuts()
        
        # General shortcuts group
        group_general = Gtk.ShortcutsGroup()

        # Add general shortcuts with current values
        general_actions = [
            ('toggle_sidebar', _('Toggle Sidebar')),
            ('quit', _('Quit')),
            ('preferences', _('Settings')),
            ('help', _('Documentation')),
            ('shortcuts', _('Keyboard Shortcuts')),
            ('edit-ssh-config', _('SSH Config Editor')),
        ]
        
        for action_name, title in general_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_general.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_general)

        # Connection management shortcuts
        group_connections = Gtk.ShortcutsGroup()
        connection_actions = [
            ('new-connection', _('New Connection')),
            ('search', _('Search Connections')),
            ('toggle-list', _('Focus Connection List')),
            ('open-new-connection-tab', _('Open New Tab')),
            ('new-key', _('Copy Key to Server')),
            ('manage-files', _('Manage Files')),
        ]
        
        for action_name, title in connection_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_connections.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_connections)

        # Terminal shortcuts
        group_terminal = Gtk.ShortcutsGroup()
        terminal_actions = [
            ('local-terminal', _('Local Terminal')),
            ('terminal-search', _('Search in Terminal')),
            ('broadcast-command', _('Broadcast Command')),
        ]

        for action_name, title in terminal_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_terminal.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_terminal)

        # Tab navigation shortcuts
        group_tabs = Gtk.ShortcutsGroup()
        tab_actions = [
            ('tab-next', _('Next Tab')),
            ('tab-prev', _('Previous Tab')),
            ('tab-move-left', _('Move Tab Left')),
            ('tab-move-right', _('Move Tab Right')),
            ('tab-close', _('Close Tab')),
            ('tab-overview', _('Tab Overview')),
            ('new-split-view-tab', _('New Split View Tab')),
        ]
        
        for action_name, title in tab_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_tabs)

        # Split view shortcuts (hardcoded — not registered as actions)
        group_split = Gtk.ShortcutsGroup(title=_('Split View'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane left'), accelerator='<Ctrl><Alt>h'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane down'), accelerator='<Ctrl><Alt>j'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane up'), accelerator='<Ctrl><Alt>k'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane right'), accelerator='<Ctrl><Alt>l'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane left'), accelerator='<Ctrl><Alt><Shift>h'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane down'), accelerator='<Ctrl><Alt><Shift>j'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane up'), accelerator='<Ctrl><Alt><Shift>k'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane right'), accelerator='<Ctrl><Alt><Shift>l'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Side-by-side layout'), accelerator='<Ctrl><Shift>backslash'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Top / bottom layout'), accelerator='<Ctrl><Shift>minus'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Add pane'), accelerator='<Ctrl><Shift>n'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Close focused pane'), accelerator='<Ctrl><Shift>w'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane 1–4'), accelerator='<Alt>1'))
        section.add_group(group_split)

    def _get_safe_current_shortcuts(self):
        """Safely get current shortcuts including customizations"""
        shortcuts = {}
        try:
            app = self.get_application()
            if not app:
                return shortcuts
            
            # Get defaults first
            if hasattr(app, 'get_registered_shortcut_defaults'):
                defaults = app.get_registered_shortcut_defaults()
                shortcuts.update(defaults)
            
            # Apply overrides
            if hasattr(app, 'config') and app.config:
                for action_name in shortcuts.keys():
                    try:
                        override = app.config.get_shortcut_override(action_name)
                        if override is not None:
                            if override:  # Not empty
                                shortcuts[action_name] = override
                            else:  # Disabled
                                shortcuts.pop(action_name, None)
                    except Exception:
                        continue
            
        except Exception as e:
            logger.debug(f"Error getting current shortcuts: {e}")
        
        return shortcuts

    def _add_fallback_shortcuts(self, section, primary):
        """Add fallback static shortcuts if dynamic generation fails"""
        # General shortcuts
        group_general = Gtk.ShortcutsGroup()
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Toggle Sidebar'), accelerator='F9'))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('SSH Config Editor'), accelerator=f"{primary}<Shift>e"))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Settings'), accelerator=f"{primary}comma"))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Documentation'), accelerator='F1'))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Keyboard Shortcuts'), accelerator=f"{primary}question"))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Quit'), accelerator=f"{primary}<Shift>q"))
        section.add_group(group_general)

        # Connection management shortcuts
        group_connections = Gtk.ShortcutsGroup()
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('New Connection'), accelerator=f"{primary}n"))
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Search Connections'), accelerator=f"{primary}f"))
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus Connection List'), accelerator=f"{primary}<Shift>l"))
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Manage Files'), accelerator=f"{primary}<Shift>o"))
        section.add_group(group_connections)

        # Terminal shortcuts
        group_terminal = Gtk.ShortcutsGroup()
        group_terminal.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Local Terminal'), accelerator=f"{primary}<Shift>t"))
        group_terminal.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Search in Terminal'), accelerator=f"{primary}<Shift>f"))
        group_terminal.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Broadcast Command'), accelerator=f"{primary}<Shift>b"))
        section.add_group(group_terminal)

        # Tab navigation shortcuts
        group_tabs = Gtk.ShortcutsGroup()
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Open New Tab'), accelerator=f"{primary}<Alt>n"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Next Tab'), accelerator=f"{primary}Page_Down"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Previous Tab'), accelerator=f"{primary}Page_Up"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Move Tab Left'), accelerator=f"{primary}<Shift>Page_Up"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Move Tab Right'), accelerator=f"{primary}<Shift>Page_Down"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Close Tab'), accelerator=f"{primary}<Shift>w"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Tab Overview'), accelerator=f"{primary}<Shift>Tab"))
        section.add_group(group_tabs)

        # Split view shortcuts
        group_split = Gtk.ShortcutsGroup(title=_('Split View'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane left'), accelerator='<Ctrl><Alt>h'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane down'), accelerator='<Ctrl><Alt>j'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane up'), accelerator='<Ctrl><Alt>k'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane right'), accelerator='<Ctrl><Alt>l'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane left'), accelerator='<Ctrl><Alt><Shift>h'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane down'), accelerator='<Ctrl><Alt><Shift>j'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane up'), accelerator='<Ctrl><Alt><Shift>k'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Resize pane right'), accelerator='<Ctrl><Alt><Shift>l'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Side-by-side layout'), accelerator='<Ctrl><Shift>backslash'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Top / bottom layout'), accelerator='<Ctrl><Shift>minus'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Add pane'), accelerator='<Ctrl><Shift>n'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Close focused pane'), accelerator='<Ctrl><Shift>w'))
        group_split.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus pane 1–4'), accelerator='<Alt>1'))
        section.add_group(group_split)
