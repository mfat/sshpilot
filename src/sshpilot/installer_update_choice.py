"""The Windows installer's "Check for updates on startup" choice.

The installer writes HKCU\\Software\\sshPilot ``CheckForUpdates`` (0 or 1) and
sets ``CheckForUpdatesPending``. The next launch copies that choice into the
config and clears the flag. Otherwise the current setting is mirrored back,
so the next installer run starts from what the user chose in Preferences.
"""

import logging

from .platform.detect import is_windows

logger = logging.getLogger(__name__)

SETTING = 'updates.check_on_startup'
_KEY = r"Software\sshPilot"
_CHOICE = "CheckForUpdates"
_PENDING = "CheckForUpdatesPending"


def apply_installer_choice(config) -> None:
    """Adopt a choice made in the installer, or mirror the current setting."""
    if not is_windows():
        return
    try:
        import winreg

        # The key only exists for installer-based installs; a portable or
        # source run raises FileNotFoundError here and is left alone.
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                pending, _type = winreg.QueryValueEx(key, _PENDING)
            except FileNotFoundError:
                pending = 0
            if pending:
                choice, _type = winreg.QueryValueEx(key, _CHOICE)
                config.set_setting(SETTING, bool(choice))
                winreg.DeleteValue(key, _PENDING)
            else:
                enabled = bool(config.get_setting(SETTING, True))
                winreg.SetValueEx(key, _CHOICE, 0, winreg.REG_DWORD, int(enabled))
    except OSError as exc:
        logger.debug("Installer update choice not applied: %s", exc)


def mirror_choice(enabled: bool) -> None:
    """Record a Preferences change for the next installer run."""
    if not is_windows():
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _CHOICE, 0, winreg.REG_DWORD, int(bool(enabled)))
    except OSError as exc:
        logger.debug("Update choice not mirrored for the installer: %s", exc)
