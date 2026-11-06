"""Locate the bundled OpenSSL provider before FreeRDP initializes WinPR."""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path
import sys

logger = logging.getLogger(__name__)
_runtime = None


def prepare_rdp_crypto(provider_directory: Path | None = None) -> bool:
    """Use the matching packaged provider without changing global environment variables."""
    global _runtime
    if sys.platform != "win32" or _runtime is not None:
        return True
    if provider_directory is not None:
        directory = provider_directory
    elif getattr(sys, "frozen", False):
        directory = Path(sys._MEIPASS) / "ossl-modules"
    else:
        directory = Path(__file__).resolve().parents[4] / "vendor" / "openssl" / "x64" / "ossl-modules"
    try:
        if not (directory / "legacy.dll").is_file():
            raise OSError("Missing OpenSSL legacy provider; run packaging/windows/openssl/build.py")
        crypto = ctypes.CDLL("libcrypto-3-x64.dll")
        crypto.OpenSSL_version.argtypes = [ctypes.c_int]
        crypto.OpenSSL_version.restype = ctypes.c_char_p
        version = crypto.OpenSSL_version(0).decode("ascii", "replace")
        # The provider is built from this exact version; update both together.
        if not version.startswith("OpenSSL 3.6.5 "):
            raise OSError(f"Bundled provider requires OpenSSL 3.6.5, found {version}")
        crypto.OSSL_PROVIDER_set_default_search_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        crypto.OSSL_PROVIDER_set_default_search_path.restype = ctypes.c_int
        if not crypto.OSSL_PROVIDER_set_default_search_path(None, str(directory).encode("utf-8")):
            raise OSError("Could not configure bundled OpenSSL provider directory")
        crypto.OSSL_PROVIDER_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        crypto.OSSL_PROVIDER_load.restype = ctypes.c_void_p
        crypto.OSSL_PROVIDER_unload.argtypes = [ctypes.c_void_p]
        crypto.OSSL_PROVIDER_unload.restype = ctypes.c_int
        default = crypto.OSSL_PROVIDER_load(None, b"default")
        legacy = crypto.OSSL_PROVIDER_load(None, b"legacy") if default else None
        if not default or not legacy:
            if default:
                crypto.OSSL_PROVIDER_unload(default)
            raise OSError("Could not load the bundled OpenSSL default and legacy providers")
        # Retain our provider references for the process lifetime. WinPR owns
        # and unloads its own references independently during SSL cleanup.
        _runtime = (crypto, default, legacy)
        logger.info("RDP OpenSSL provider directory configured: %s", directory)
        return True
    except Exception:
        logger.error("RDP crypto initialization failed", exc_info=True)
        return False
