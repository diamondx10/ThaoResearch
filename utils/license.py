"""License authentication (v2: HMAC-SHA256 encoded expiration date).

A valid license is a 16-character hex string that encodes an
expiration date ``YYYYMMDD``. Generate with::

    python license_generator.py 20261029

Verify offline via::

    python license_checker.py <16-char-token>

The tool ships with these flags baked into its UI:
- ``enable_desktop_toast`` (Windows notifications)
- ``enable_telegram_alert`` (Telegram bot)

Both flags are **disabled when there is no valid (and unexpired)
license**. Once a user enters a valid license key, the flags become
editable in *Settings → General*.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from utils.paths import DATA_DIR

logger = logging.getLogger("thaoresearch.utils.license")

LICENSE_FILE = DATA_DIR / "license.dat"

# Status strings used in the UI. Keep these stable — locale files key
# off them for translations.
STATUS_NOT_ACTIVATED = "not_activated"
STATUS_INVALID = "invalid"
STATUS_EXPIRED = "expired"
STATUS_ACTIVATED = "activated"

# Keys we always write to the on-disk JSON. ``license_key`` stores the
# raw 16-char string the user entered (the spec says it's fine to
# display it; we don't mask it). ``valid_to`` is the decoded
# ``YYYY-MM-DD`` expiration date.
# Bypass flag: set BYPASS_LICENSE=1 (via --bypass-license build arg) to
# make license always valid without any key or license file. This is for
# distributors who want a zero-setup build.
_BYPASS_LICENSE = os.environ.get("BYPASS_LICENSE", "").strip() in ("1", "true", "yes")


def _check_token(token: str) -> Tuple[bool, str]:
    """Validate a 32-char AES token via the standalone checker script.

    Returns:
        ``(is_valid, status)`` where status is one of the STATUS_*
        constants.

    We delegate the actual crypto to ``license_checker`` so the rules
    here stay in lock-step with the CLI script the host uses for
    manual debugging.

    The frozen ``.exe`` bundle ships ``license_generator.py`` and
    ``license_checker.py`` via PyInstaller's ``--add-data``; we have to
    put the bundle directory on ``sys.path`` before importing them.
    """
    try:
        import license_checker  # type: ignore
    except ImportError:
        # Try the PyInstaller extraction directory (``_MEIPASS``).
        import sys
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            try:
                sys.path.insert(0, meipass)
                import license_checker  # type: ignore  # noqa: F811
            except Exception as e:
                logger.warning("Could not import license_checker: %s", e)
                return (False, STATUS_INVALID)
        else:
            logger.warning("Could not import license_checker.")
            return (False, STATUS_INVALID)

    valid, status, _detail = license_checker.check_license(token)
    if status == "VALID":
        return (True, STATUS_ACTIVATED)
    if status == "EXPIRED":
        return (False, STATUS_EXPIRED)
    return (False, STATUS_INVALID)


def save_license(license_key: str) -> dict:
    """Save a license to disk.

    Returns the metadata dict. Raises LicenseInvalidError on bad input.
    """
    is_valid, status = _check_token(license_key)
    if status == STATUS_INVALID:
        raise LicenseInvalidError("License key is invalid.")
    if status == STATUS_EXPIRED:
        raise LicenseExpiredError("License has expired.")
    if not is_valid:
        raise LicenseInvalidError("License key is invalid.")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    valid_to = _extract_valid_to(license_key)
    data = {
        "version": 2,
        "license_key": license_key,
        "valid_to": valid_to,
        "issued_at": now,
        "last_verified": now,
    }
    LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("License saved (valid to %s)", valid_to)
    return data


def load_license_file() -> dict:
    """Load the license file. Raises LicenseCorruptedError on bad/missing file."""
    if not LICENSE_FILE.exists():
        raise LicenseCorruptedError(
            f"License file not found at {LICENSE_FILE}. Please enter your license key."
        )
    try:
        text = LICENSE_FILE.read_text(encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        raise LicenseCorruptedError(f"License file is corrupted: {e}") from e

    required = ("license_key", "valid_to")
    if not all(k in data for k in required):
        raise LicenseCorruptedError(
            "License file is missing required fields. Delete it and re-enter your license."
        )
    return data


def verify_license(license_key: str) -> dict:
    """Verify a license key.

    Raises:
        LicenseInvalidError: wrong key
        LicenseExpiredError: license past its expiration date
    """
    is_valid, status = _check_token(license_key)
    if status == STATUS_INVALID:
        raise LicenseInvalidError("License key is invalid.")
    if status == STATUS_EXPIRED:
        raise LicenseExpiredError("License has expired.")

    # Persist the new key — submitting a different valid token
    # replaces the stored one. This matches the spec: "Submit" just
    # needs a valid+unexpired token to enable the gated features.
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        data = load_license_file()
        if data.get("license_key") != license_key:
            # New key: rewrite the file so the saved token matches
            # what the user just submitted.
            data["license_key"] = license_key
            data["valid_to"] = _extract_valid_to(license_key)
            if not data.get("issued_at"):
                data["issued_at"] = now
        data["last_verified"] = now
        LICENSE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data
    except LicenseCorruptedError:
        pass

    return {
        "version": 2,
        "license_key": license_key,
        "valid_to": _extract_valid_to(license_key),
        "issued_at": now,
        "last_verified": now,
    }


def is_license_valid() -> Tuple[bool, Optional[str]]:
    """Return (is_valid, error_message). error_message is None when valid.

    When ``BYPASS_LICENSE=1`` is set, always returns ``(True, None)`` so
    the license check is always satisfied.
    """
    if _BYPASS_LICENSE:
        return (True, None)

    try:
        data = load_license_file()
    except LicenseCorruptedError as e:
        return (False, str(e))
    is_valid, status = _check_token(data.get("license_key", ""))
    if status == STATUS_EXPIRED:
        return (False, f"License expired on {data.get('valid_to', '?')}")
    if status == STATUS_INVALID:
        return (False, "License key is invalid.")
    if not is_valid:
        return (False, "License key is invalid.")
    return (True, None)


def is_expired() -> bool:
    """Return True if license exists but is past its expiration date."""
    if _BYPASS_LICENSE:
        return False
    try:
        data = load_license_file()
        is_valid, status = _check_token(data.get("license_key", ""))
        return status == STATUS_EXPIRED
    except LicenseCorruptedError:
        return False


def get_license_info() -> Optional[dict]:
    """Return license metadata for display, or None if no file."""
    if _BYPASS_LICENSE:
        return {
            "version": 2,
            "license_key": "(bypass)",
            "valid_to": "2099-12-31",
            "issued_at": "2026-01-01T00:00:00Z",
            "last_verified": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "bypass": True,
        }
    try:
        return load_license_file()
    except LicenseCorruptedError:
        return None


def get_license_status() -> str:
    """Return one of the STATUS_* strings describing the current state."""
    if _BYPASS_LICENSE:
        return STATUS_ACTIVATED
    try:
        data = load_license_file()
    except LicenseCorruptedError:
        return STATUS_NOT_ACTIVATED
    is_valid, status = _check_token(data.get("license_key", ""))
    if is_valid:
        return STATUS_ACTIVATED
    return status


def clear_license() -> None:
    """Remove the license file (used by --reset-license)."""
    if LICENSE_FILE.exists():
        LICENSE_FILE.unlink()
        logger.info("License file removed")


class LicenseCorruptedError(LicenseError):
    """License file is unreadable or missing required fields."""


def _extract_valid_to(token: str) -> str:
    """Return ``YYYY-MM-DD`` for a valid token; empty string on mismatch."""
    try:
        import license_checker  # type: ignore
    except ImportError:
        import sys
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            sys.path.insert(0, meipass)
            try:
                import license_checker  # type: ignore  # noqa: F811
            except Exception:
                return ""
        else:
            return ""
    date_str = license_checker._decrypt_token(token)  # noqa: SLF001
    if not date_str or len(date_str) != 8:
        return ""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def prompt_for_license(cli_mode: bool = False, max_attempts: int = 3) -> Optional[str]:
    """Prompt the user to enter a license key.

    cli_mode=True reads from stdin; otherwise, opens a Tkinter dialog.
    Returns the entered key on success, or None if user cancelled / too many failures.
    """
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        if cli_mode:
            try:
                key = input("Enter license key: ").strip()
            except EOFError:
                return None
        else:
            try:
                from desktop.license_dialog import LicenseDialog
                dialog = LicenseDialog()
                key = dialog.show()
            except Exception as e:
                logger.warning("Tkinter dialog failed (%s), falling back to stdin", e)
                try:
                    key = input("Enter license key: ").strip()
                except EOFError:
                    return None
        if not key:
            print("[E0901] License key cannot be empty.")
            continue
        try:
            if not LICENSE_FILE.exists():
                save_license(key)
            verify_license(key)
            print("[OK] License verified successfully.")
            return key
        except LicenseInvalidError as e:
            print(f"[E0901] {e}")
            print(f"Attempts remaining: {max_attempts - attempts}")
        except LicenseExpiredError as e:
            print(f"[E0902] {e}")
            print("Please contact vendor for a new license.")
            return None
    print("[E0904] Too many invalid attempts. Exiting.")
    return None