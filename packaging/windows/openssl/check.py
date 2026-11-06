#!/usr/bin/env python3
"""Check MD4 and RC4 through the actual bundled WinPR/OpenSSL runtime.

Run in a fresh process so another loaded OpenSSL cannot mask missing files.
    python packaging/windows/openssl/check.py --runtime vendor/gtk/bin
"""

import argparse
import ctypes
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--provider", type=Path,
                        default=ROOT / "vendor/openssl/x64/ossl-modules")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    from sshpilot.platform.windows.rdp_crypto import prepare_rdp_crypto

    with os.add_dll_directory(str(args.runtime.resolve())):
        if not prepare_rdp_crypto(args.provider.resolve()):
            raise SystemExit("RDP provider configuration failed")
        winpr = ctypes.CDLL(str(args.runtime.resolve() / "winpr3.dll"))
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
    print("RDP crypto OK: WinPR MD4 and RC4 known-answer checks passed")


if __name__ == "__main__":
    main()
