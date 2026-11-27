# sshPilot - SSH connection manager with integrated terminal
# Copyright (C) 2025 mFat <newmfat@gmail.com>
# Licensed under GPL-3.0





__version__ = "6.5.2"

# Inside the AppImage, programs the app starts for the user must not inherit the
# bundled GTK/WebKit environment or AppRun's working directory. Every bundled
# Python process imports this package, so the guard is installed here; outside
# the AppImage it is a no-op (see appimage_env).
import os as _os

if _os.environ.get("SSHPILOT_APPIMAGE_VARS"):
    from .appimage_env import install_spawn_guard as _install_spawn_guard

    _install_spawn_guard()
del _os





__author__ = "mFat"
__email__ = "newmfat@gmail.com"
