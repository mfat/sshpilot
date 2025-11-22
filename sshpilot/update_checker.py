"""
Update Checker for sshPilot
Checks GitHub releases for newer versions
"""

import logging
import json
from typing import Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import threading

from . import __version__
from .platform_utils import is_macos, is_flatpak

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com/repos/mfat/sshpilot/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/mfat/sshpilot/releases"
FLATHUB_URL = "https://flathub.org/apps/io.github.mfat.sshpilot"


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse version string into tuple of integers for comparison.
    
    Args:
        version_str: Version string like "4.4.1" or "v4.4.1"
    
    Returns:
        Tuple of version numbers like (4, 4, 1)
    """
    # Remove 'v' prefix if present
    version_str = version_str.lstrip('v')
    
    try:
        parts = version_str.split('.')
        return tuple(int(part) for part in parts)
    except (ValueError, AttributeError):
        logger.warning(f"Failed to parse version string: {version_str}")
        return (0, 0, 0)


def compare_versions(current: str, latest: str) -> bool:
    """Compare two version strings.
    
    Args:
        current: Current version string
        latest: Latest version string from GitHub
    
    Returns:
        True if latest > current, False otherwise
    """
    current_tuple = parse_version(current)
    latest_tuple = parse_version(latest)
    
    return latest_tuple > current_tuple


def get_latest_version() -> Optional[str]:
    """Fetch the latest version from GitHub releases.
    
    Returns:
        Latest version string (e.g., "4.5.0") or None if check fails
    """
    try:
        # Add User-Agent header to avoid GitHub API rate limiting
        headers = {
            'User-Agent': f'sshPilot/{__version__}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        request = Request(GITHUB_API_URL, headers=headers)
        
        with urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            # Get tag_name (e.g., "v4.4.1")
            tag_name = data.get('tag_name', '')
            
            # Remove 'v' prefix if present
            version = tag_name.lstrip('v')
            
            logger.info(f"Latest version from GitHub: {version}")
            return version
            
    except (URLError, HTTPError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to check for updates: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error checking for updates: {e}")
        return None


def check_for_updates() -> Optional[str]:
    """Check if a newer version is available.
    
    Returns:
        Latest version string if update available, None otherwise
    """
    current_version = __version__
    latest_version = get_latest_version()
    
    if latest_version is None:
        return None
    
    if compare_versions(current_version, latest_version):
        logger.info(f"Update available: {current_version} -> {latest_version}")
        return latest_version
    else:
        logger.info(f"Current version {current_version} is up to date")
        return None


def check_for_updates_async(callback):
    """Check for updates in background thread.
    
    Args:
        callback: Function to call with result (version string or None)
    """
    def _check():
        try:
            result = check_for_updates()
            callback(result)
        except Exception as e:
            logger.error(f"Error in async update check: {e}")
            callback(None)
    
    thread = threading.Thread(target=_check, daemon=True)
    thread.start()


def get_update_url() -> str:
    """Get platform-appropriate update URL.
    
    Returns:
        URL string for downloading updates
    """
    if is_flatpak():
        return FLATHUB_URL
    else:
        # For both macOS and native Linux installs
        return GITHUB_RELEASES_URL


def get_platform_install_method() -> str:
    """Get human-readable install method string.
    
    Returns:
        String describing the installation method
    """
    if is_flatpak():
        return "Flatpak"
    elif is_macos():
        return "macOS"
    else:
        return "Native Linux"

