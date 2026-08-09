"""License key generator — AES-128-CBC.

Encode a license expiration date as a 32-character hex string the tool
can later verify offline.

Usage:
    python license_generator.py YYYYMMDD

Examples:
    # Create a license valid until 2026-10-29
    python license_generator.py 20261029
    >>> <32 hex chars>

Format
------
Plaintext:  the 8 ASCII digits of ``YYYYMMDD`` (e.g. ``"20261029"``).
            PKCS#7-padded to a single 16-byte AES block.
Cipher:     AES-128-CBC with a fixed 16-byte key and a fixed 16-byte IV
            (see ``_KEY`` / ``_IV`` below).
Output:     ``ciphertext.hex()`` — 32 lowercase hex characters.

The matcher lives in ``license_checker.py`` (and ``utils/license.py``
delegates to it for in-app verification).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime

from Crypto.Cipher import AES

# --- Cryptographic parameters ---------------------------------------------
#
# User-provided key literal: ``0x123``. AES-128 requires 16 bytes, so the
# literal is right-aligned into a 16-byte buffer with leading zeros:
#
#     _KEY = 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 23
#
# Anyone who reads this script can extract the key — that's the limit of
# "obscurity" here. For real protection, swap to Ed25519/RSA.
_KEY = bytes.fromhex("00000000000000000000000000000123")
_IV = b"\x00" * 16  # fixed IV (deterministic encoding — fine because dates are unique)


def _validate_date(date_str: str) -> bytes:
    """Validate ``YYYYMMDD`` and return the 8-byte ASCII plaintext.

    Raises:
        ValueError: if the string isn't 8 digits or isn't a real
            calendar date.
    """
    if not re.match(r"^\d{8}$", date_str):
        raise ValueError(f"Date must be 8 digits in YYYYMMDD format, got {date_str!r}.")
    try:
        datetime.strptime(date_str, "%Y%m%d")
    except ValueError as e:
        raise ValueError(f"Not a valid calendar date: {date_str} ({e}).") from e
    return date_str.encode("ascii")


def encode_date(date_str: str) -> str:
    """Encode ``YYYYMMDD`` as a 32-character hex string (AES-128-CBC)."""
    plaintext = _validate_date(date_str)
    # PKCS#7 pad 8 → 16 bytes manually. CBC mode in pycryptodome does
    # not auto-pad, so we have to do it ourselves.
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    cipher = AES.new(_KEY, AES.MODE_CBC, _IV)
    ciphertext = cipher.encrypt(padded)
    return ciphertext.hex()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("Usage: python license_generator.py YYYYMMDD", file=sys.stderr)
        print("Example: python license_generator.py 20261029", file=sys.stderr)
        return 2
    try:
        token = encode_date(argv[0])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())