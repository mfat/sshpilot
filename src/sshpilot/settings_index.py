"""Searchable index of the rows on the Settings pages.

Omni-search offers individual settings without building the Settings window
(its pages take hundreds of milliseconds to build), so the rows are listed
here. Settings' own search walks the live rows instead and takes only the
extra keywords from this table.

Titles are the exact msgids the pages use; ``tests/test_gui_settings_search.py``
fails when a row is added, renamed or removed on a page without updating this
table. Rows on the Shortcuts and Plugins pages are generated, so only Settings'
own search finds them.

A titled group with no rows (radio buttons, a palette, a custom editor) is
indexed as one entry by its group title.
"""

from __future__ import annotations

import gettext
from dataclasses import dataclass
from typing import Optional, Tuple

from .i18n import N_

_ = gettext.gettext


@dataclass(frozen=True)
class SettingEntry:
    page_id: str
    group: Optional[str]  # msgid, None for a group without a title
    title: str  # msgid
    keywords: Optional[str] = None  # semicolon-separated msgid

    def keyword_list(self) -> Tuple[str, ...]:
        """English and translated keywords."""
        if not self.keywords:
            return ()
        words = [*self.keywords.split(";"), *_(self.keywords).split(";")]
        return tuple(dict.fromkeys(word.strip() for word in words if word.strip()))


# Page id -> title msgid, as registered by PreferencesWindow.add_page_to_layout.
PAGE_TITLES = {
    "interface": N_("Interface"),
    "appearance": N_("Appearance"),
    "sidebar": N_("Sidebar"),
    "terminal": N_("Terminal"),
    "file-management": N_("File Management"),
    "shortcuts": N_("Shortcuts"),
    "ssh-options": N_("SSH Options"),
    "security-&-credentials": N_("Security & Credentials"),
    "updates": N_("Updates"),
    "plugins": N_("Plugins"),
    "command-snippets": N_("Command Snippets"),
    "advanced": N_("Advanced"),
}

# Pages whose rows are generated (one per shortcut or plugin).
GENERATED_PAGES = frozenset({"shortcuts", "plugins"})

# Translators: the keywords are semicolon-separated search terms for one
# setting, matched in addition to its title. Translate each term; keep the
# semicolons.
SETTINGS = (
    SettingEntry("interface", None, N_("Layout"),
                 N_("layout;view;profile;preset;hide sidebar;plain terminal")),
    SettingEntry("interface", None, N_("App Startup"),
                 N_("startup;start page;restore session;saved session;launch")),
    SettingEntry("interface", N_("Language"), N_("Interface Language"),
                 N_("language;locale;translation")),
    SettingEntry("interface", N_("Header Bar Buttons"), N_("Sidebar Toggle Button")),
    SettingEntry("interface", N_("Header Bar Buttons"), N_("Split View Button")),
    SettingEntry("interface", N_("Header Bar Buttons"), N_("Command Snippets Button")),
    SettingEntry("interface", N_("Header Bar Buttons"), N_("Terminal Theme Button")),
    SettingEntry("interface", N_("Header Bar Buttons"), N_("Theme Menu")),
    SettingEntry("interface", N_("Header Bar Buttons"), N_("Local Terminal Button")),
    SettingEntry("interface", N_("Tips"), N_("Show Tips")),
    SettingEntry("interface", N_("Window"), N_("Remember Window Size"),
                 N_("window size;startup")),
    SettingEntry("appearance", None, N_("Application Theme"),
                 N_("dark mode;light mode;dark theme;light theme;style")),
    SettingEntry("appearance", None, N_("Monospace Font"), N_("font;typeface")),
    SettingEntry("appearance", N_("Color Overrides"), N_("Accent Color"),
                 N_("accent colour;highlight color")),
    SettingEntry("appearance", N_("Color Overrides"), N_("Reset to Default")),
    SettingEntry("appearance", N_("Group Appearance"), N_("Sidebar Group Colors"),
                 N_("group colours;folder colors")),
    SettingEntry("appearance", N_("Group Appearance"), N_("Use Group Color for Child Rows")),
    SettingEntry("appearance", N_("Group Appearance"), N_("Show Group Color in Tabs")),
    SettingEntry("appearance", N_("Group Appearance"), N_("Use Group Color in Terminals"),
                 N_("terminal background")),
    SettingEntry("sidebar", N_("Layout"), N_("Sidebar Mode"), N_("compact;full")),
    SettingEntry("sidebar", N_("Layout"), N_("Row Style"), N_("cards;flat")),
    SettingEntry("sidebar", N_("Layout"), N_("Local Terminal Row")),
    SettingEntry("sidebar", N_("Row Details"), N_("Display user@hostname"),
                 N_("username;hostname")),
    SettingEntry("sidebar", N_("Row Details"), N_("Display Connection Count in Groups")),
    SettingEntry("sidebar", N_("Row Details"), N_("Display Connection Status")),
    SettingEntry("sidebar", N_("Row Details"), N_("Display Port Forwarding Icon")),
    SettingEntry("sidebar", N_("Row Details"), N_("Display Connection Icon")),
    SettingEntry("sidebar", N_("Row Details"), N_("Display Group Icon")),
    SettingEntry("sidebar", N_("Row Buttons"), N_("File Manager Button")),
    SettingEntry("sidebar", N_("Row Buttons"), N_("Dashboard Button")),
    SettingEntry("sidebar", N_("Row Buttons"), N_("Split View Button")),
    SettingEntry("sidebar", N_("Behavior"), N_("Hide Sidebar on Startup"),
                 N_("collapse sidebar")),
    SettingEntry("sidebar", N_("Behavior"),
                 N_("Hide Sidebar When a Terminal Tab Opens"),
                 N_("hide sidebar;collapse sidebar;terminal tab")),
    SettingEntry("sidebar", N_("Behavior"), N_("Show When No Tab Is Open")),
    SettingEntry("sidebar", N_("Behavior"), N_("Peek at the Hidden Sidebar"),
                 N_("hover;reveal;peek;show sidebar")),
    SettingEntry("terminal", None, N_("Preferred Terminal"),
                 N_("external terminal;built-in terminal;terminal emulator")),
    SettingEntry("terminal", None, N_("Color Scheme"),
                 N_("terminal theme;colors;colours;palette")),
    SettingEntry("terminal", N_("Appearance"), N_("Font"), N_("font size;typeface")),
    SettingEntry("terminal", N_("Appearance"), N_("Encoding"), N_("charset;utf-8")),
    SettingEntry("terminal", N_("Cursor"), N_("Cursor Shape"), N_("block;underline;i-beam")),
    SettingEntry("terminal", N_("Cursor"), N_("Cursor Blinking"), N_("blink")),
    SettingEntry(
        "terminal", N_("Highlighting"), N_("Highlight Network Output"),
        N_("syntax;color;colour;cisco;junos;huawei;ip;mac;vlan"),
    ),
    SettingEntry("terminal", N_("Highlighting"), N_("Highlight Profile"), N_("cisco;junos;huawei")),
    SettingEntry("terminal", N_("Backend"), N_("Terminal Backend"), N_("vte;xterm.js")),
    SettingEntry("terminal", N_("Keyboard"), N_("Terminal Shortcut Pass-through")),
    SettingEntry("terminal", N_("Keyboard"), N_("Command autocomplete"),
                 N_("suggestions;history")),
    SettingEntry("terminal", N_("Keyboard"), N_("Suggest from remote history")),
    SettingEntry("terminal", N_("Mouse"), N_("Copy on selection"), N_("clipboard")),
    SettingEntry("terminal", N_("Mouse"), N_("Paste on right-click"), N_("clipboard")),
    SettingEntry("terminal", N_("Mouse"), N_("Mouse wheel scroll lines"),
                 N_("scroll speed;scrolling")),
    SettingEntry("terminal", N_("Remote Clipboard"), N_("Allow remote programs to copy"),
                 N_("clipboard;osc 52")),
    SettingEntry("terminal", N_("Remote Clipboard"), N_("Maximum copy size (KiB)"),
                 N_("clipboard")),
    SettingEntry("terminal", N_("Daemon Terminal Settings"), N_("Tab close policy")),
    SettingEntry("terminal", N_("Daemon Terminal Settings"), N_("Quit policy")),
    SettingEntry("terminal", N_("Daemon Terminal Settings"),
                 N_("Restore daemon sessions on startup"),
                 N_("background service;reattach")),
    SettingEntry("terminal", N_("Daemon Terminal Settings"),
                 N_("Daemon idle shutdown (seconds)"), N_("background service")),
    SettingEntry("terminal", N_("Daemon Terminal Settings"), N_("Daemon status"),
                 N_("background service")),
    SettingEntry("terminal", N_("Daemon Terminal Settings"), N_("Restart daemon"),
                 N_("background service")),
    SettingEntry("file-management", N_("File Manager Options"),
                 N_("Open File Manager in Separate Window"), N_("sftp")),
    SettingEntry("file-management", N_("File Manager Options"), N_("Concurrent Transfers"),
                 N_("uploads;downloads;sftp")),
    SettingEntry("file-management", N_("Terminal Panes"), N_("Visible Terminals")),
    SettingEntry("file-management", N_("Terminal Panes"), N_("Follow Folder Navigation")),
    SettingEntry("ssh-options", None, N_("Custom SSH Options"),
                 N_("ssh config;ssh_config;override")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("Connect Timeout (s)")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("Connection Attempts"), N_("retries")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("Apply default keepalive"),
                 N_("dropped connections;serveralive")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("ServerAlive Interval (s)"),
                 N_("keepalive")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("ServerAlive CountMax"),
                 N_("keepalive")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("StrictHostKeyChecking"),
                 N_("host key;known hosts")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("BatchMode (disable prompts)")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("Enable Compression (-C)")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("Enable SSH connection multiplexing"),
                 N_("controlmaster;controlpersist")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("SSH Verbosity (-v)"),
                 N_("verbose;debug")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("Enable SSH Debug Logging"),
                 N_("verbose;logs")),
    SettingEntry("ssh-options", N_("SSH Settings"), N_("Reset Advanced SSH Settings")),
    SettingEntry("security-&-credentials", N_("Secret Storage"), N_("Secret storage backend"),
                 N_("passwords;keyring;bitwarden;keepass;rbw;vault")),
    SettingEntry("security-&-credentials", N_("Secret Storage"), N_("Bitwarden status")),
    SettingEntry("security-&-credentials", N_("Secret Storage"), N_("rbw status")),
    SettingEntry("security-&-credentials", N_("Secret Storage"),
                 N_("bw data directory (account/profile)"), N_("bitwarden")),
    SettingEntry("security-&-credentials", N_("Secret Storage"), N_("KeePass database (.kdbx)")),
    SettingEntry("security-&-credentials", N_("Secret Storage"), N_("Key file (optional)"),
                 N_("keepass")),
    SettingEntry("security-&-credentials", N_("Secret Storage"),
                 N_("Vault lock timeout (minutes)"), N_("master password;lock")),
    SettingEntry("security-&-credentials", N_("Secret Storage"), N_("Saved master password")),
    SettingEntry("security-&-credentials", N_("Secret Storage"), N_("No secret storage")),
    SettingEntry("security-&-credentials", N_("SSH Identity"), N_("Default SSH agent"),
                 N_("ssh-agent;identityagent;keys")),
    SettingEntry("security-&-credentials", N_("SSH Identity"),
                 N_("Custom agent socket (IdentityAgent)"), N_("ssh-agent")),
    SettingEntry("updates", N_("Update Notifications"), N_("Check for updates on startup")),
    SettingEntry("command-snippets", N_("Behavior"), N_("Insert Only (no execute)"),
                 N_("snippets;paste")),
    SettingEntry("advanced", N_("Operation Mode"), N_("Default Mode"),
                 N_("operation mode;ssh config;known hosts")),
    SettingEntry("advanced", N_("Operation Mode"), N_("Isolated Mode"),
                 N_("operation mode;ssh config;known hosts")),
    SettingEntry("advanced", N_("Application Behavior"), N_("Confirm before disconnecting")),
    SettingEntry("advanced", N_("Logging"), N_("Log Level"), N_("debug;logs")),
)

# Live rows that are details of another row rather than settings of their own
# (the per-mode file paths inside the Operation Mode expanders), and the
# sidebar preview, which shows settings rather than being one.
NOT_INDEXED = frozenset({
    ("appearance", N_("Preview")),
    ("advanced", N_("Configuration file")),
    ("advanced", N_("Known hosts")),
    ("advanced", N_("Imported hosts")),
})


def entry_for(page_id: str, title: str) -> Optional[SettingEntry]:
    """The entry for a live row, by page and its (translated) title."""
    for entry in SETTINGS:
        if entry.page_id == page_id and _(entry.title) == title:
            return entry
    return None
