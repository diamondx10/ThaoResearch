"""CLI text-only UI helpers. No Rich, no emoji — clean light output."""
from __future__ import annotations

import os
import sys
from typing import Iterable, Optional

from models.article import Article


SEPARATOR = "─" * 60
THICK_SEP = "═" * 60


def print_banner(version: str = "1.0.0") -> None:
    """Print app banner."""
    print(THICK_SEP)
    print(f"  ThaoResearch v{version}")
    print("  Commodity News Intelligence & Alerting")
    print(THICK_SEP)


def print_config_summary(config: dict) -> None:
    """Print a compact summary of the loaded configuration."""
    general = config.get("general", {})
    sources = config.get("sources", [])
    commodities = config.get("commodities", [])
    scheduler = config.get("scheduler", {})

    print("\nConfiguration")
    print(SEPARATOR)

    print(f"Sources ({sum(1 for s in sources if s.get('enabled', True))} enabled):")
    for s in sources:
        flag = "[x]" if s.get("enabled", True) else "[ ]"
        print(f"  {flag} {s.get('name','?'):<25} {s.get('type','?'):<4} {s.get('url','?')}")

    print("\nCommodities:")
    for c in commodities:
        if not c.get("enabled", True):
            continue
        threshold = c.get("price_threshold")
        th = f"threshold {threshold} USD" if threshold else "(no threshold)"
        print(f"  - {c.get('name','?'):<15} {th}")

    if scheduler.get("mode") == "adaptive":
        interval = f"adaptive (business: {scheduler.get('adaptive', {}).get('weekday_business_hours', '?')}min)"
    else:
        interval = f"every {scheduler.get('fixed_interval_minutes', 5)} min"
    print(f"\nScheduler: {interval}")
    print(f"Timezone:   {general.get('timezone', 'UTC')}")
    print(f"Language:   {general.get('language', 'en')}")
    print(SEPARATOR)


def print_alert(article: Article, sent: bool = True, dry_run: bool = False) -> None:
    """Print a single article in console (text-only block).

    Note: This function only renders the article body. The send status
    (SENT / DRY-RUN / SKIPPED) is printed by the caller AFTER attempting
    to send, so we never show a misleading "FAILED" placeholder.
    """
    print()
    print(article.to_console_block())


def print_run_summary(stats: dict) -> None:
    """Print final summary table for a run."""
    print()
    print("Run summary")
    print(SEPARATOR)
    width = 12
    rows = [
        ("Crawled", stats.get("crawled", 0)),
        ("Filtered", stats.get("filtered", 0)),
        ("Sent", stats.get("sent", 0)),
        ("Skipped", stats.get("skipped", 0)),
        ("Errors", stats.get("errors", 0)),
        ("Duration", f"{stats.get('duration_s', 0):.1f}s"),
    ]
    for label, value in rows:
        print(f"{label:<{width}}: {value}")
    failures = stats.get("failures") or []
    if failures:
        print(f"\nFailures ({len(failures)}):")
        for name, err in failures:
            print(f"  - {name}: {err}")
    print(SEPARATOR)


def confirm_action(prompt: str, default: bool = False) -> bool:
    """Ask user for yes/no confirmation."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        answer = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def print_welcome_help() -> None:
    """Print the welcome/help message (shown on first run)."""
    print()
    print(THICK_SEP)
    print("Welcome to ThaoResearch v1.0.0")
    print(THICK_SEP)
    print()
    print("Quick start:")
    print("  1. Edit config.yaml to choose which sources & commodities to track")
    print("  2. (Optional) Set up Telegram:")
    print("     - Copy .env.example to .env")
    print("     - See INSTALL_TELEGRAM.md for step-by-step")
    print("  3. Run: python main.py --daemon")
    print()
    print("When something goes wrong:")
    print("  - Look for [E####] code in console or logs/app.log")
    print("  - Each code has a clear suggestion printed below")
    print("  - Run python main.py --help for all CLI flags")
    print("  - Run python main.py --verbose for debug logs")
    print()
    print("Happy monitoring!")
    print(THICK_SEP)
    print()


def print_telegram_setup_help() -> None:
    """Print Telegram setup instructions (shown when token missing)."""
    print(THICK_SEP)
    print("Telegram setup required")
    print(THICK_SEP)
    print("No TELEGRAM_BOT_TOKEN found in .env file.")
    print()
    print("To create your Telegram bot:")
    print("  1. Open Telegram app, search for @BotFather")
    print("  2. Send command:  /newbot")
    print("  3. Follow prompts:")
    print("     - Bot name:       ThaoResearch Alerts (or any name you like)")
    print("     - Bot username:   thao_<your_name>_bot (must end with 'bot')")
    print("  4. BotFather replies with a token like:")
    print("     123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    print("  5. Copy that token to your .env file:")
    print("     TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    print()
    print("To find your Chat ID:")
    print("  1. Open Telegram, search for @userinfobot (or @RawDataBot)")
    print("  2. Send any message to it")
    print("  3. It replies with your numeric Chat ID (e.g. 987654321)")
    print("  4. For a GROUP chat:")
    print("     - Add your bot to the group")
    print("     - Send a message in the group")
    print("     - Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates")
    print("     - Find \"chat\":{\"id\": -100XXXXXXXXXX} in the response")
    print("  5. Copy that ID to your .env file:")
    print("     TELEGRAM_CHAT_ID=987654321")
    print()
    print("After saving .env, run:")
    print("  python main.py --test-telegram")
    print("to verify the connection.")
    print()
    print("For full guide with screenshots, see INSTALL_TELEGRAM.md")
    print(THICK_SEP)
