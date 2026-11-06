#!/usr/bin/env python
"""Generate ``utils/credentials.py`` from ``.env``.

The generated module is what PyInstaller bakes into the .exe, so end users
get a working Telegram bot without shipping a separate ``.env`` next to the
executable. The generated file is gitignored — ``.env`` is the single source
of truth for the real values.

Usage::

    python scripts/embed_credentials.py              # read .env at repo root
    python scripts/embed_credentials.py --env path   # read a different file
    python scripts/embed_credentials.py --clear      # reset to placeholders

Run this before every release build (``build_release.bat`` calls it for you).

Note on security: values baked into an .exe are *obfuscated, not secret* —
anyone with the binary can recover them with ``strings``. Treat the bot token
as semi-public: keep the bot locked to its own channel, and rotate it via
``/revoke`` in @BotFather if a build leaks somewhere it should not.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "utils" / "credentials.py"
DEFAULT_ENV = REPO_ROOT / ".env"

PLACEHOLDER_TOKEN = "YOUR_BOT_TOKEN_HERE"
PLACEHOLDER_CHAT_ID = "YOUR_CHAT_ID_HERE"

TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{35,}$")

TEMPLATE = '''"""Embedded credentials for the Telegram bot — GENERATED FILE.

Do not edit by hand and do not commit (``.gitignore`` blocks it).
Edit ``.env`` instead, then regenerate::

    python scripts/embed_credentials.py

Generated from: {source}
"""

TELEGRAM_BOT_TOKEN = "{token}"
TELEGRAM_CHAT_ID = "{chat_id}"

_PLACEHOLDERS = {{"", "{ph_token}", "{ph_chat}"}}


def has_embedded_credentials() -> bool:
    """Return True only when both values have been replaced with real ones."""
    return (
        TELEGRAM_BOT_TOKEN not in _PLACEHOLDERS
        and TELEGRAM_CHAT_ID not in _PLACEHOLDERS
    )
'''


def parse_env(path: Path) -> dict[str, str]:
    """Minimal .env reader — no dependency on python-dotenv."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def render(token: str, chat_id: str, source: str) -> str:
    return TEMPLATE.format(
        token=token,
        chat_id=chat_id,
        source=source,
        ph_token=PLACEHOLDER_TOKEN,
        ph_chat=PLACEHOLDER_CHAT_ID,
    )


def mask(value: str) -> str:
    """Show only enough of a secret to confirm which one it is."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]} ({len(value)} chars)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help="Path to the .env file")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Write placeholders instead of real values (use before sharing the tree)",
    )
    args = parser.parse_args(argv)

    if args.clear:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(
            render(PLACEHOLDER_TOKEN, PLACEHOLDER_CHAT_ID, "--clear (placeholders)"),
            encoding="utf-8",
        )
        print(f"[OK] {TARGET.relative_to(REPO_ROOT)} reset to placeholders.")
        return 0

    if not args.env.exists():
        print(f"[ERROR] {args.env} not found.", file=sys.stderr)
        print("        Copy .env.example to .env and fill in your values.", file=sys.stderr)
        return 1

    env = parse_env(args.env)
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or token == PLACEHOLDER_TOKEN:
        print(f"[ERROR] TELEGRAM_BOT_TOKEN missing or still a placeholder in {args.env}.", file=sys.stderr)
        return 1
    if not chat_id or chat_id == PLACEHOLDER_CHAT_ID:
        print(f"[ERROR] TELEGRAM_CHAT_ID missing or still a placeholder in {args.env}.", file=sys.stderr)
        return 1

    if not TOKEN_PATTERN.match(token):
        print(f"[ERROR] TELEGRAM_BOT_TOKEN does not look like a BotFather token: {mask(token)}", file=sys.stderr)
        print("        Expected format: <digits>:<35+ chars>", file=sys.stderr)
        return 1
    try:
        int(chat_id)
    except ValueError:
        print(f"[ERROR] TELEGRAM_CHAT_ID must be an integer, got: {mask(chat_id)}", file=sys.stderr)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(render(token, chat_id, args.env.name), encoding="utf-8")

    print(f"[OK] wrote {TARGET.relative_to(REPO_ROOT)}")
    print(f"     token   : {mask(token)}")
    print(f"     chat_id : {mask(chat_id)}")
    print("     (gitignored - it will not be committed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
