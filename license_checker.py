"""License key checker (CLI).

Decode a 32-character AES-128-CBC license string and check whether it
is valid AND not yet expired.

Usage:
    python license_checker.py <32-char-token>

Examples:
    python license_checker.py aabbcc...32chars

Exit codes:
    0  license is valid AND not expired
    1  license key is malformed / AES mismatch
    2  CLI argument error
    3  license exists but has expired

Stdout (one line):
    either ``VALID YYYY-MM-DD`` or ``INVALID <reason>`` / ``EXPIRED YYYY-MM-DD``
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from Crypto.Cipher import AES

# Mirror ``license_generator.py`` so the two stay in sync.
_KEY = bytes.fromhex("00000000000000000000000000000123")
_IV = b"\x00" * 16
_TOKEN_LEN = 32  # 16 bytes ciphertext → 32 hex chars


def _decrypt_token(token: str) -> str:
    """Decrypt the 32-char hex token back into its ``YYYYMMDD`` date.

    Returns ``""`` on any decryption / format mismatch. Errors are
    intentionally silent here — the caller maps the empty result to
    ``INVALID``.
    """
    token = token.strip()
    if len(token) != _TOKEN_LEN:
        return ""
    try:
        ciphertext = bytes.fromhex(token)
    except ValueError:
        return ""
    if len(ciphertext) != 16:
        return ""
    try:
        cipher = AES.new(_KEY, AES.MODE_CBC, _IV)
        plaintext = cipher.decrypt(ciphertext)
    except Exception:
        return ""
    # Strip PKCS#7 padding. Block size 16 → padding is 1..16 bytes;
    # the unpadded payload is the 8 ASCII date digits.
    pad = plaintext[-1]
    if pad < 1 or pad > 16 or plaintext[-pad:] != bytes([pad]) * pad:
        return ""
    date_bytes = plaintext[: -pad]
    if len(date_bytes) != 8:
        return ""
    try:
        date_str = date_bytes.decode("ascii")
    except UnicodeDecodeError:
        return ""
    if not date_str.isdigit():
        return ""
    try:
        datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        return ""
    return date_str


def check_license(token: str) -> tuple[bool, str, str]:
    """Check a license token.

    Returns:
        ``(is_valid, status, detail)`` where status is one of
        ``"VALID"``, ``"EXPIRED"``, ``"INVALID"``.
    """
    if not token or len(token) != _TOKEN_LEN:
        return (False, "INVALID", "License key must be 32 characters.")
    date_str = _decrypt_token(token)
    if not date_str:
        return (False, "INVALID", "License key is not recognized.")
    # Compare against end-of-day UTC for the encoded date so the license
    # is good through the entire expiration day.
    exp = datetime.strptime(date_str, "%Y%m%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    now = datetime.now(timezone.utc)
    pretty = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    if now > exp:
        return (False, "EXPIRED", f"Expired on {pretty}.")
    return (True, "VALID", f"Valid until {pretty}.")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("Usage: python license_checker.py <32-char-token>", file=sys.stderr)
        return 2
    token = argv[0]
    valid, status, detail = check_license(token)
    if status == "VALID":
        # ``detail`` looks like ``Valid until 2026-10-29.``
        print(f"VALID {detail.split(' ', 2)[-1].rstrip('.')}")
        return 0
    if status == "EXPIRED":
        # ``detail`` looks like ``Expired on 2026-10-29.``
        print(f"EXPIRED {detail.split(' ', 3)[-1].rstrip('.')}")
        return 3
    print(f"INVALID {detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())