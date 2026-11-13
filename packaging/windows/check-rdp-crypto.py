#!/usr/bin/env python3
"""Verify staged WinPR MD4/RC4 support without an OpenSSL legacy provider."""

import contextlib
import ctypes
import os
from pathlib import Path
import sys
import tempfile


def main():
    runtime = Path(sys.argv[1]).resolve()
    dependencies = Path(sys.argv[2]).resolve()
    with contextlib.ExitStack() as stack:
        stack.enter_context(os.add_dll_directory(str(runtime)))
        stack.enter_context(os.add_dll_directory(str(dependencies)))
        empty = stack.enter_context(tempfile.TemporaryDirectory())
        # Override build-machine module locations and inherited environment.
        os.environ["OPENSSL_MODULES"] = empty
        crypto = ctypes.CDLL(str(runtime / "libcrypto-3-x64.dll"))
        crypto.OSSL_PROVIDER_set_default_search_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        crypto.OSSL_PROVIDER_set_default_search_path.restype = ctypes.c_int
        if not crypto.OSSL_PROVIDER_set_default_search_path(None, empty.encode("utf-8")):
            raise SystemExit("Could not isolate OpenSSL providers")
        winpr = ctypes.CDLL(str(runtime / "winpr3.dll"))
        winpr.winpr_get_build_config.argtypes = []
        winpr.winpr_get_build_config.restype = ctypes.c_char_p
        config = winpr.winpr_get_build_config().decode("ascii", "replace")
        for option in ("WITH_INTERNAL_MD4=ON", "WITH_INTERNAL_RC4=ON"):
            if option not in config.split():
                raise SystemExit(f"Staged WinPR is missing {option}")
        winpr.winpr_InitializeSSL.argtypes = [ctypes.c_uint32]
        winpr.winpr_InitializeSSL.restype = ctypes.c_int
        if not winpr.winpr_InitializeSSL(0):
            raise SystemExit("WinPR SSL initialization failed")
        winpr.winpr_Digest.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_size_t]
        winpr.winpr_Digest.restype = ctypes.c_int
        digest = ctypes.create_string_buffer(16)
        if not winpr.winpr_Digest(2, b"abc", 3, digest, 16):
            raise SystemExit("WinPR MD4 unavailable")
        if digest.raw.hex() != "a448017aaf21d8525fc10ae87aa6729d":
            raise SystemExit("WinPR MD4 returned incorrect digest")
        winpr.winpr_RC4_New.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        winpr.winpr_RC4_New.restype = ctypes.c_void_p
        winpr.winpr_RC4_Update.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.c_void_p, ctypes.c_void_p]
        winpr.winpr_RC4_Update.restype = ctypes.c_int
        winpr.winpr_RC4_Free.argtypes = [ctypes.c_void_p]
        winpr.winpr_RC4_Free.restype = None
        context = winpr.winpr_RC4_New(b"Key", 3)
        if not context:
            raise SystemExit("WinPR RC4 unavailable")
        try:
            output = ctypes.create_string_buffer(9)
            if not winpr.winpr_RC4_Update(context, 9, b"Plaintext", output):
                raise SystemExit("WinPR RC4 encryption failed")
            if output.raw.hex() != "bbf316e8d940af0ad3":
                raise SystemExit("WinPR RC4 returned incorrect ciphertext")
        finally:
            winpr.winpr_RC4_Free(context)
    print("Staged WinPR MD4/RC4 checks passed without the OpenSSL legacy provider")


if __name__ == "__main__":
    main()
