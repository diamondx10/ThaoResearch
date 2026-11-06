"""ThaoResearch — main entry point.

CLI:
    python main.py --daemon           # background with tray icon
    python main.py --cli --now        # one-shot CLI
    python main.py --cli --now --dry-run
    python main.py --test-telegram
    python main.py --license-info
    python main.py --reset-license
    python main.py --install-autostart / --uninstall-autostart
    python main.py --build-exe

GUI (default when no flag is passed on Windows):
    python main.py                    # → license gate → MainWindow (3-tab)
    python main.py --gui              # same as above (explicit)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from core import crawler as crawler_module
from core.filter_engine import apply_filters
from core.notifier import Notifier
from core.parser import normalize_pub_dates
from core.telegram_bot import TelegramNotifier, validate_chat_id, validate_token
from models.article import Article
from utils import autostart, database, i18n, license as license_mod, price_tracker

# Module-level state shared between ``job_execute`` (which runs on the
# scheduler thread) and ``clear_cache`` (which runs on the GUI thread).
# ``_job_state["running"]`` flips True when a job starts and False when
# it finishes; ``clear_cache`` polls it and waits up to ``_job_state["timeout_s"]``
# seconds for the current job to drain before unlinking the SQLite
# files (which would fail on Windows while the job holds them open).
_job_state = {
    "running": False,
    "started_at": 0.0,
    "last_finished_at": 0.0,
    "timeout_s": 120.0,  # worst-case crawl + filter pipeline
}
from utils.paths import APP_ROOT, resolve_user_path
from utils.errors import (
    EmptyChatIdError,
    EmptyTokenError,
    EnvFileError,
    InvalidChatIdError,
    InvalidTokenError,
    ThaoResearchError,
    format_error,
)
from utils.logger import get_logger, setup_logger
from utils.confirm_exit import start_exit_watcher
from utils.ui import (
    print_alert,
    print_banner,
    print_config_summary,
    print_run_summary,
    print_telegram_setup_help,
    print_welcome_help,
)

VERSION = "1.0.0"
# Config files now live in the data/ folder alongside history.db and prices.db
# for full portability (one folder = everything)
from utils import paths as paths_mod
from utils.paths import APP_ROOT
# Credentials: ``utils/credentials.py`` (generated, gitignored) is preferred so
# release builds can bake the token into the .exe. ``.env`` is the dev fallback
# and the single source of truth — regenerate the module with
# ``python scripts/embed_credentials.py``.
DEFAULT_ENV = Path(".env")
DEFAULT_WELCOME = True  # print welcome help on first run

# =============================================================================
# Default config template (used when no config file exists)
# =============================================================================
DEFAULT_CONFIG_TEMPLATE = {
    "general": {
        "enable_desktop_toast": True,
        "enable_telegram_alert": True,
        "max_articles_per_run": 50,
        "timezone": "Asia/Ho_Chi_Minh",
        "language": "en",
        "theme": "system",
        "force_send_duplicates": False,
        "autosave_settings": True,
    },
    "scheduler": {
        "mode": "fixed",
        "fixed_interval_minutes": 5,
    },
    "sources": [
        {
            "name": "Google News - Soybean Oil (all)",
            "type": "rss",
            "url": "https://news.google.com/rss/search?q=%22soybean+oil%22+OR+%22soyoil%22+OR+%22SBO%22&hl=en-US&gl=US&ceid=US:en",
            "enabled": True,
        },
    ],
    "commodities": [
        {
            "name": "Soybean Oil",
            "keywords": ["soybean oil", "soyoil", "SBO", "biodiesel", "palm oil"],
            "enabled": True,
            "price_threshold": 50.0,
        },
    ],
    "filters": {
        "high_priority_keywords": ["halt", "surge", "crash", "sanctions", "embargo", "record"],
        "blacklist_keywords": ["advertisement", "sponsored", "premium content", "signup"],
        "enable_delta_alert": True,
        "delta_threshold_pct": 5.0,
    },
}


def _create_default_config(path: Path) -> None:
    """Create a default config file if it doesn't exist."""
    try:
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(DEFAULT_CONFIG_TEMPLATE, f, allow_unicode=True, sort_keys=False)
        print(f"[INFO] Created default config at: {path}")
    except Exception as e:
        print(f"[WARN] Could not create default config: {e}")


def _migrate_legacy_config(target: Path) -> bool:
    """Copy a pre-existing config from the old location into the data folder.

    Config used to live beside the program (``<app>/config.yaml`` and
    ``<app>/init_config.yaml``); it now lives in the data folder so that one
    directory holds everything and can be moved or put on a USB stick.

    Without this step the move looks like data loss: a user who had tuned 32
    sources and 24 keywords would start the new build and be handed the
    built-in one-source default, with their real file still sitting on disk
    one directory up. Returns True when something was migrated.
    """
    legacy_candidates = [
        APP_ROOT / "init_config.yaml",  # the old autosave target wins
        APP_ROOT / "config.yaml",
    ]
    for legacy in legacy_candidates:
        try:
            if not legacy.is_file():
                continue
            data = yaml.safe_load(legacy.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict) or not data.get("sources"):
                continue  # empty or unusable; keep looking
            target.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(legacy, target)
            print(f"[INFO] Migrated your existing settings: {legacy} -> {target}")
            print(
                f"       ({len(data.get('sources') or [])} sources, "
                f"{len(data.get('commodities') or [])} commodities). "
                "The original file was left in place."
            )
            return True
        except Exception as e:
            print(f"[WARN] Could not migrate {legacy}: {e}")
    return False


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into `base`, returning a new dict.

    Values in `override` win. For dict values, we recurse so that adding
    a new key to `override` doesn't wipe out other keys from `base`.
    For list values, `override` completely replaces `base` (no element-
    wise merge — too complex and rarely needed for this app).
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_with_defaults(config: dict) -> dict:
    """Ensure all default keys exist in `config`.

    Critical for portable builds: when a user copies a partial config
    (or when the GUI autosave writes a stripped section), keys the
    rest of the app expects (e.g. `sources`, `commodities`, `filters`)
    may be missing. Without this merge, the GUI crashes when it tries
    to iterate `config["sources"]`, and the scheduler silently runs
    with zero sources.

    Existing user values always win; defaults only fill in missing
    keys. Lists are replaced wholesale (not merged element-wise) so
    the user's source list is preserved exactly.
    """
    try:
        merged = _deep_merge(DEFAULT_CONFIG_TEMPLATE, config or {})
        return merged
    except Exception as e:
        logger_.warning(f"merge_with_defaults failed: {e}") if False else None
        return config or {}


# =============================================================================
# License check (must run before anything else)
# =============================================================================
def check_license_at_startup(args) -> bool:
    """Verify license at startup. Returns True if OK to proceed.

    Some commands (--reset-license, --build-exe, --help, --install-autostart,
    --uninstall-autostart) do not require a valid license.
    --license-info is allowed to run even without license (it shows the message).
    """
    bypass_commands = {
        "reset_license",
        "build_exe",
        "help",
        "install_autostart",
        "uninstall_autostart",
        "bypass_license",
    }
    cmd = getattr(args, "command", None)

    # If --bypass-license was passed (set by the bypass-license build),
    # set the env var so utils/license.py picks it up at import time.
    if getattr(args, "bypass_license", False):
        import os as _os
        _os.environ["BYPASS_LICENSE"] = "1"

    # Env var bypass (for CI/testing) — applies to all commands except bypass ones
    env_license = os.environ.get("THRESEARCH_LICENSE")
    if env_license and cmd not in bypass_commands:
        try:
            # If a license file exists, verify against it; otherwise just save.
            if license_mod.LICENSE_FILE.exists():
                license_mod.verify_license(env_license)
            else:
                license_mod.save_license(env_license)
            print("[OK] License verified via THRESEARCH_LICENSE env var.")
            return True
        except Exception as e:
            print(f"[FAIL] THRESEARCH_LICENSE env var invalid: {e}")
            return False

    if cmd in bypass_commands:
        return True
    if cmd == "license_info":
        # --license-info can run even without license (just shows the message)
        return True

    # Fast path: file exists + not expired
    is_valid, err = license_mod.is_license_valid()
    if is_valid:
        print("[OK] License valid.")
        return True

    # Auto-accept: skip license gate and proceed straight to the app.
    # The license module is kept for future re-enable; demo key '1234' still works
    # if the user enters it, but we no longer block startup on missing license.
    print("[INFO] No valid license — proceeding without license gate.")
    return True


# =============================================================================
# Config & env loading
# =============================================================================
def load_config(path: Path) -> dict:
    """Load and validate the YAML config."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML at {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"Invalid config: expected mapping, got {type(data).__name__}")
    return data


def load_env(path: Path | None = None) -> dict:
    """Load Telegram credentials. Returns {} when none are available.

    Resolution order:

    1. ``utils/credentials.py`` — generated by ``scripts/embed_credentials.py``
       and baked into the .exe by PyInstaller, so an end user does not need
       their own ``.env``. The module is gitignored; only a ``.example``
       template with placeholders is committed. When the module is absent or
       still holds placeholders, we fall through.
    2. ``.env`` next to the app (or CWD in dev) — the source of truth that the
       generator reads from.
    3. Process environment — lets CI or a launcher inject the values without
       any file on disk.

    Never raises: a missing or unreadable source just means "no credentials",
    and ``build_telegram_notifier`` degrades to desktop-toast-only mode.
    """
    # 1. Embedded module (release builds)
    try:
        from utils.credentials import (
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            has_embedded_credentials,
        )

        if has_embedded_credentials():
            return {
                "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
                "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
            }
    except Exception:
        # ImportError (file not generated yet) or AttributeError (stale file
        # from an older layout). Either way: fall through to .env.
        pass

    # 2. .env file — resolved next to the EXE first for portable builds.
    env_path = resolve_user_path(path or DEFAULT_ENV)
    try:
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except Exception as e:
        logging.getLogger("thaoresearch").warning("Could not read %s: %s", env_path, e)

    # 3. Process environment (populated by step 2, or set externally).
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {}
    return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}


def build_telegram_notifier(env: dict, config: dict) -> Optional[TelegramNotifier]:
    """Build a TelegramNotifier from env + config. Returns None if disabled or invalid."""
    general = config.get("general", {})
    if not general.get("enable_telegram_alert", True):
        return None
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token:
        return None
    if not validate_token(token):
        return None
    if not chat_id or not validate_chat_id(chat_id):
        return None
    return TelegramNotifier(token, chat_id)


# =============================================================================
# Core job: crawl -> filter -> notify
# =============================================================================
def job_execute(config: dict, notifier: Notifier, dry_run: bool, logger_: logging.Logger) -> dict:
    """Single run of the 4-step pipeline. Returns stats dict."""
    # Notify GUI so the dashboard can show a progress bar during scheduled
    # runs (without this, the user would only see the progress bar when
    # they clicked "Fetch Now" — a scheduled run would run silently).
    try:
        from desktop.gui_queue import gui_queue
        gui_queue.put(("fetch_started", "scheduled"))
    except Exception:
        pass
    tz_name = config.get("general", {}).get("timezone", "Asia/Ho_Chi_Minh")
    sources = [s for s in (config.get("sources") or []) if s.get("enabled", True)]
    target_source = None  # set by --source

    stats = {
        "crawled": 0,
        "filtered": 0,
        "sent": 0,
        "skipped": 0,
        "errors": 0,
        "failures": [],
        "duration_s": 0.0,
    }
    # Clear-cache coordination: set ``_job_running=True`` while we're
    # holding SQLite connections open. ``clear_cache()`` polls this flag
    # and waits for it to flip back to False before unlinking the DB
    # files (which would fail on Windows while we hold them open).
    _job_state["running"] = True
    _job_state["started_at"] = time.time()
    t0 = time.time()

    print()
    print(f"── Run at {datetime.now(ZoneInfo(tz_name)).strftime('%Y-%m-%d %H:%M:%S %z')} ──")
    print("Crawling sources...")
    all_articles: List[Article] = []
    for src in sources:
        try:
            c = crawler_module.build_crawler(src)
        except Exception as e:
            stats["failures"].append((src.get("name", "?"), f"build_crawler: {e}"))
            logger_.error("[E9999] build_crawler for %s failed: %s", src.get("name"), e)
            continue

        try:
            articles, error = c.safe_fetch()
        except Exception as e:
            stats["failures"].append((src.get("name", "?"), f"fetch crashed: {e}"))
            logger_.error("[E9999] fetch crashed for %s: %s", src.get("name"), e)
            continue

        if error:
            stats["failures"].append((src.get("name", "?"), error))
            print(f"  {src.get('name','?'):<30} [FAIL] {error}")
            continue

        all_articles.extend(articles)
        print(f"  {src.get('name','?'):<30} {len(articles)} articles")

    stats["crawled"] = len(all_articles)

    if not all_articles:
        print("No articles crawled.")
        stats["duration_s"] = time.time() - t0
        _job_state["running"] = False
        _job_state["last_finished_at"] = time.time()
        return stats

    # Localize dates
    normalize_pub_dates(all_articles, tz_name=tz_name)

    # Filter
    matched = apply_filters(all_articles, config)
    stats["filtered"] = len(matched)

    if not matched:
        print("No articles match the filter.")
        stats["duration_s"] = time.time() - t0
        _job_state["running"] = False
        _job_state["last_finished_at"] = time.time()
        return stats

    print(f"\nFiltered to {len(matched)} relevant articles\n")

    # Limit per run
    max_articles = int(config.get("general", {}).get("max_articles_per_run", 10))
    matched = matched[:max_articles]

    # Honour the user's "force send (bypass duplicate check)" toggle.
    # When ON we skip the dedup table entirely so every filtered article
    # is pushed, useful for "/news flow" channels that want every alert.
    # Default is OFF (dedup active) so we don't spam the same alert.
    force_send = bool(config.get("general", {}).get("force_send_duplicates", False))

    # Send — check duplicate BEFORE printing so old news is silent
    for article in matched:
        h = article.compute_hash()
        if not dry_run and not force_send and database.is_sent(h):
            stats["skipped"] += 1
            logger_.info("Skipped duplicate: %s", article.title[:60])
            continue
        if force_send and database.is_sent(h):
            logger_.info("Force-sending (bypass dedup): %s", article.title[:60])
        print_alert(article)
        try:
            ok = notifier.send_alert(article, dry_run=dry_run, force=force_send)
            if ok:
                stats["sent"] += 1
                if dry_run:
                    print("  [DRY-RUN — not actually sent]")
                else:
                    print("  [SENT]")
            else:
                stats["skipped"] += 1
                print("  [SKIPPED — already sent or duplicate]")
        except Exception as e:
            stats["errors"] += 1
            logger_.exception("Notifier error: %s", e)
            print(format_error(e))

    stats["duration_s"] = time.time() - t0
    print_run_summary(stats)
    # Notify GUI the scheduled run has finished.
    try:
        from desktop.gui_queue import gui_queue
        gui_queue.put(("fetch_done", stats))
    except Exception:
        pass
    _job_state["running"] = False
    _job_state["last_finished_at"] = time.time()
    return stats


# =============================================================================
# Scheduler with adaptive interval
# =============================================================================
def get_next_interval_minutes(config: dict, now: datetime) -> int:
    """Return the next interval (in minutes) based on adaptive scheduler config."""
    scheduler = config.get("scheduler") or {}
    if scheduler.get("mode", "adaptive") == "fixed":
        return int(scheduler.get("fixed_interval_minutes", 5))

    adaptive = scheduler.get("adaptive") or {}
    tz_name = adaptive.get("timezone", config.get("general", {}).get("timezone", "Asia/Ho_Chi_Minh"))
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    local_now = now.astimezone(tz)
    is_weekend = local_now.weekday() >= 5  # Sat=5, Sun=6
    if is_weekend and adaptive.get("off_on_weekend", False):
        return -1  # signal: skip
    if is_weekend:
        return int(adaptive.get("weekend", 30))

    hour = local_now.hour
    start = int(adaptive.get("business_hours_start", 8))
    end = int(adaptive.get("business_hours_end", 20))
    if start <= hour < end:
        return int(adaptive.get("weekday_business_hours", 3))
    return int(adaptive.get("weekday_evening", 10))


def _now_aware(config: dict) -> datetime:
    """Return the current local datetime (timezone-aware) per config."""
    tz_name = config.get("general", {}).get("timezone", "Asia/Ho_Chi_Minh")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


def reschedule(scheduler: BackgroundScheduler, config: dict, job_id: str = "main_job"):
    """Compute next interval and reschedule the main job.

    Uses scheduler.reschedule_job() with a fresh IntervalTrigger so the new
    interval takes effect immediately. The `next_run_time` is NOT passed as
    a kwarg (reschedule_job only forwards trigger args; the start time is
    controlled separately). After reschedule, we set the job's next_run_time
    to now so the countdown resets.
    """
    interval = get_next_interval_minutes(config, datetime.now())
    if interval < 0:
        # Skip weekend — keep the current schedule but don't fire.
        logger = get_logger()
        logger.info("Skipping run (off_on_weekend=True)")
        return
    scheduler.reschedule_job(
        job_id,
        trigger="interval",
        minutes=interval,
    )
    # Reset countdown to "now + interval" so the dashboard reflects the new
    # interval immediately rather than counting down against the old run time.
    try:
        from datetime import timedelta
        job = scheduler.get_job(job_id)
        if job is not None:
            job.next_run_time = _now_aware(config) + timedelta(minutes=interval)
    except Exception as e:
        logger = get_logger()
        logger.debug("next_run_time reset failed: %s", e)


# =============================================================================
# Telegram setup helper (printed when token missing)
# =============================================================================
def maybe_print_telegram_setup(env: dict):
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print_telegram_setup_help()


# =============================================================================
# Daemon mode (with tray)
# =============================================================================
def run_daemon(args, config: dict, env: dict, notifier: Notifier):
    """Run in daemon mode: APScheduler + system tray icon."""
    logger_ = get_logger("main")

    # Initialize price DB
    try:
        price_tracker.init_db()
    except Exception as e:
        logger_.error("Failed to init prices.db: %s", e)

    # Build scheduler
    scheduler = BackgroundScheduler(timezone=config.get("general", {}).get("timezone", "Asia/Ho_Chi_Minh"))
    initial_minutes = get_next_interval_minutes(config, datetime.now())
    if initial_minutes < 0:
        initial_minutes = 30
    scheduler.add_job(
        lambda: job_execute(config, notifier, dry_run=False, logger_=logger_),
        trigger="interval",
        minutes=initial_minutes,
        id="main_job",
        # Don't use ``now`` as next_run_time — that would make the job
        # due immediately and could fire on the BackgroundScheduler
        # thread before the ``scheduler.pause()`` below takes effect.
        # Schedule it ``initial_minutes`` in the future; ``pause()`` then
        # ensures it doesn't fire until the user clicks Resume Loop.
        next_run_time=_now_aware(config) + timedelta(minutes=initial_minutes),
        max_instances=1,
    )

    # Listener: after a job fires (or errors), reschedule the next run so
    # `next_run_time` reflects the configured interval. Without this listener,
    # the dashboard's "Next Run" countdown can get stuck at "Rescheduling..."
    # because APScheduler's IntervalTrigger doesn't always republish the new
    # next_run_time after the previous fire completes.
    #
    # NOTE: the listener callback runs on the scheduler thread, so we must
    # NOT call sched.reschedule_job() inline (it can deadlock). We hop onto
    # a worker thread first.
    try:
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

        def _on_job_event(event):
            def _deferred():
                try:
                    reschedule(scheduler, config)
                except Exception as e:
                    logger_.debug("reschedule after job event failed: %s", e)

            threading.Thread(target=_deferred, daemon=True, name="reschedule").start()

        scheduler.add_listener(
            _on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )
    except Exception as e:
        logger_.debug("scheduler listener setup failed: %s", e)

    try:
        scheduler.start()
    except Exception as e:
        logger_.error("[E0700] Failed to start scheduler: %s", e)
        return 1
    # Default state: paused. The fetch loop only runs once the user
    # clicks "Resume Loop" in the dashboard (or tray menu). Jobs remain
    # scheduled but won't fire until ``scheduler.resume()`` is called.
    try:
        scheduler.pause()
    except Exception as e:
        logger_.warning("Could not pause scheduler after start: %s", e)
    logger_.info("Scheduler started (interval=%d min) — paused by default", initial_minutes)

    # Wire tray
    from desktop.gui_queue import gui_queue
    from desktop.tray import TrayApp

    def get_status():
        return f"Running (next in ~{initial_minutes} min)"

    def run_now():
        try:
            job_execute(config, notifier, dry_run=False, logger_=logger_)
        except Exception as e:
            logger_.exception("Manual run failed: %s", e)

    def show_history():
        gui_queue.put(("show_history", None))

    def show_settings():
        # Tuple shape: (config, config_path, on_save). The third element
        # is the ``config_saved`` callback so the Settings dialog can
        # notify the host (e.g. when the user activates a license mid-
        # session) — without it the notifier stays stuck with whatever
        # the startup license-gate decided. In daemon mode there's no
        # config_saved callback, so we pass None.
        on_save_cb = None
        try:
            on_save_cb = config_saved  # noqa: F821 (defined in run_gui only)
        except NameError:
            pass
        gui_queue.put(("show_settings", (config, args.config, on_save_cb)))

    def exit_app():
        """Hard-stop every background subsystem. Called from the tray's
        Exit menu and (in the GUI case) the MainWindow's X / Close
        button via the ``on_exit`` callback.

        This directly shuts down all components (scheduler, telegram, tray)
        and sends a shutdown event to the GUI queue to close the window.
        """
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            if notifier and notifier.telegram:
                notifier.telegram.shutdown()
        except Exception:
            pass
        try:
            # Stop the tray icon directly - this will unblock pystray's event loop
            tray.stop()
        except Exception:
            pass
        try:
            # Close all database connections properly
            from utils.database import close_all_connections
            from utils.price_tracker import close_all_connections as close_price_connections
            close_all_connections()
            close_price_connections()
        except Exception:
            pass
        try:
            # Send shutdown to GUI queue to close the Tk window
            gui_queue.put(("shutdown", None))
        except Exception:
            pass

    def open_article(article):
        gui_queue.put(("open_viewer", article))

    tray = TrayApp(
        on_run_now=run_now,
        on_show_history=show_history,
        on_show_settings=show_settings,
        on_exit=exit_app,
        on_open_article=open_article,
        get_status_text=get_status,
    )

    print()
    print("Scheduler started. Tray icon is running. Press Ctrl+C in this window to stop.")
    print()
    # Esc-watcher: only effective when stdin is a real console (i.e. not when
    # the daemon was launched detached via Start-Process)
    import os as _os
    if _os.isatty(0):
        start_exit_watcher(on_confirmed_exit=lambda: exit_app())
    try:
        tray.start_blocking()
    except KeyboardInterrupt:
        print("\nGraceful shutdown (Ctrl+C)...")
        logger_.info("User requested shutdown (Ctrl+C)")
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            notifier.telegram.shutdown() if notifier.telegram else None
        except Exception:
            pass
    return 0


def run_gui(args, config: dict, env: dict, notifier: Notifier) -> int:
    """GUI mode: MainWindow (3-tab, Settings first) + tray icon + scheduler.

    This is the default mode when the app is launched as a frozen .exe
    on Windows (i.e. ThaoResearch.exe).
    """
    logger_ = get_logger("main")

    # Initialize price DB
    try:
        price_tracker.init_db()
    except Exception as e:
        logger_.error("Failed to init prices.db: %s", e)

    # Build scheduler (same as daemon mode)
    scheduler = BackgroundScheduler(timezone=config.get("general", {}).get("timezone", "Asia/Ho_Chi_Minh"))
    initial_minutes = get_next_interval_minutes(config, datetime.now())
    if initial_minutes < 0:
        initial_minutes = 30
    scheduler.add_job(
        lambda: job_execute(config, notifier, dry_run=False, logger_=logger_),
        trigger="interval",
        minutes=initial_minutes,
        id="main_job",
        # Don't use ``now`` as next_run_time — that would make the job
        # due immediately and could fire on the BackgroundScheduler
        # thread before the ``scheduler.pause()`` below takes effect.
        # Schedule it ``initial_minutes`` in the future; ``pause()`` then
        # ensures it doesn't fire until the user clicks Resume Loop.
        next_run_time=_now_aware(config) + timedelta(minutes=initial_minutes),
        max_instances=1,
    )
    # Same listener as in run_daemon — see comment there for details.
    try:
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

        def _on_job_event(event):
            def _deferred():
                try:
                    reschedule(scheduler, config)
                except Exception as e:
                    logger_.debug("reschedule after job event failed: %s", e)

            threading.Thread(target=_deferred, daemon=True, name="reschedule").start()

        scheduler.add_listener(
            _on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )
    except Exception as e:
        logger_.debug("scheduler listener setup failed: %s", e)
    try:
        scheduler.start()
    except Exception as e:
        logger_.error("[E0700] Failed to start scheduler: %s", e)
        return 1
    # Default state: paused. The fetch loop only runs once the user
    # clicks "Resume Loop" in the dashboard (or tray menu). Jobs remain
    # scheduled but won't fire until ``scheduler.resume()`` is called.
    try:
        scheduler.pause()
    except Exception as e:
        logger_.warning("Could not pause scheduler after start: %s", e)
    logger_.info("Scheduler started (interval=%d min) — paused by default", initial_minutes)

    # Wire gui_queue for background → GUI thread communication
    from desktop.gui_queue import gui_queue
    from desktop.main_window import MainWindow
    from desktop.tray import TrayApp

    def get_status():
        return f"Running (next in ~{initial_minutes} min)"

    def run_now():
        """Single-shot pipeline run (crawl + filter + send)."""
        try:
            job_execute(config, notifier, dry_run=False, logger_=logger_)
        except Exception as e:
            logger_.exception("Manual run failed: %s", e)

    def toggle_loop(target_running: bool):
        """Pause or resume the scheduler loop.

        ``target_running=True``  -> resume scheduler (start firing jobs).
        ``target_running=False`` -> pause scheduler (jobs remain scheduled
                                    but won't fire until resumed).
        """
        try:
            if target_running:
                scheduler.resume()
                logger_.info("Scheduler resumed")
            else:
                scheduler.pause()
                logger_.info("Scheduler paused")
            # Push a status event so the GUI re-renders its loop state
            gui_queue.put(("status", None))
        except Exception as e:
            logger_.exception("Toggle loop failed: %s", e)
            raise

    def clear_cache():
        """Wipe local data/history.db, data/prices.db, logs/*.

        We pause the scheduler and wait for any in-flight ``job_execute``
        to finish (signalled via ``_job_state["running"]``) before we
        touch the SQLite files. On Windows an open SQLite file is
        locked at the OS level — even ``close_all_connections()``
        can't unlink the file if another thread keeps opening fresh
        connections inside its job loop.

        The BackgroundScheduler uses a ThreadPoolExecutor whose worker
        threads are kept alive *indefinitely* (they sleep waiting for
        the next job). Counting alive threads is therefore useless —
        it always reports ``>0``. The reliable signal is the flag we
        set at the top of ``job_execute`` and clear at the bottom.

        Resume on the way out so the loop picks up at the next interval.
        """
        # Pause the scheduler so no NEW job_execute fires.
        was_running = False
        try:
            from apscheduler.schedulers.base import STATE_RUNNING
            was_running = scheduler.state == STATE_RUNNING
            if was_running:
                try:
                    scheduler.pause()
                    logger_.info("Clear cache: scheduler paused")
                except Exception as e:
                    logger_.warning("Clear cache: pause failed: %s", e)
        except Exception:
            pass

        # Wait for the currently-running job (if any) to release its
        # SQLite connections. ``_job_state["running"]`` is set at the
        # top of ``job_execute`` and cleared at the bottom (and at every
        # early-return path), so polling it is reliable.
        try:
            import time as _time
            deadline = _time.time() + float(_job_state.get("timeout_s", 120))
            while _job_state.get("running") and _time.time() < deadline:
                logger_.info("Clear cache: waiting for in-flight job to finish...")
                _time.sleep(0.25)
            if _job_state.get("running"):
                logger_.warning(
                    "Clear cache: job still running after %s s — proceeding anyway",
                    _job_state.get("timeout_s", 120),
                )
            else:
                logger_.info("Clear cache: scheduler thread is idle")
        except Exception as e:
            logger_.debug("Clear cache: drain wait skipped: %s", e)

        try:
            from utils.clear_cache import clear_local_data

            results = clear_local_data()
            for path, status in results:
                logger_.info("Clear cache: %s -> %s", path, status)
            # Re-initialize the price DB so the next fetch can write to it
            try:
                price_tracker.init_db()
            except Exception as e:
                logger_.warning("price_tracker.init_db after clear failed: %s", e)
            # Refresh counters
            gui_queue.put(("status", None))
            return results
        finally:
            if was_running:
                try:
                    scheduler.resume()
                    logger_.info("Clear cache: scheduler resumed")
                except Exception as e:
                    logger_.warning("Clear cache: resume failed: %s", e)

    def show_history():
        gui_queue.put(("show_history", None))

    def show_settings():
        # Tuple shape: (config, config_path, on_save). The third element
        # is the ``config_saved`` callback so the Settings dialog can
        # notify the host (e.g. when the user activates a license mid-
        # session) — without it the notifier stays stuck with whatever
        # the startup license-gate decided. In daemon mode there's no
        # config_saved callback, so we pass None.
        on_save_cb = None
        try:
            on_save_cb = config_saved  # noqa: F821 (defined in run_gui only)
        except NameError:
            pass
        gui_queue.put(("show_settings", (config, args.config, on_save_cb)))

    def exit_app():
        """Hard-stop every background subsystem. Called from the tray's
        Exit menu and (in the GUI case) the MainWindow's X / Close
        button via the ``on_exit`` callback.

        This directly shuts down all components (scheduler, telegram, tray)
        and sends a shutdown event to the GUI queue to close the window.
        """
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            if notifier and notifier.telegram:
                notifier.telegram.shutdown()
        except Exception:
            pass
        try:
            # Stop the tray icon directly - this will unblock pystray's event loop
            tray.stop()
        except Exception:
            pass
        try:
            # Close all database connections properly
            from utils.database import close_all_connections
            from utils.price_tracker import close_all_connections as close_price_connections
            close_all_connections()
            close_price_connections()
        except Exception:
            pass
        try:
            # Send shutdown to GUI queue to close the Tk window
            gui_queue.put(("shutdown", None))
        except Exception:
            pass

    def open_article(article):
        gui_queue.put(("open_viewer", article))

    def config_saved(new_cfg):
        # The GUI just wrote the new config to disk and updated the in-memory
        # `config` reference. Re-arm the scheduler so the new interval takes
        # effect immediately (otherwise the user sees the new mode/slot label
        # but the old countdown on the Dashboard).
        try:
            # `config` is closed over from run_daemon; mutate it in place so
            # get_next_interval_minutes reads the new values.
            config.clear()
            config.update(new_cfg)
            # Re-check license — the user may have just entered a valid
            # key in the General tab. If so, ungate the two flags the
            # license controls. If the license is missing/expired, force
            # them off so they can't be re-enabled via the YAML alone.
            try:
                lic_ok, _ = license_mod.is_license_valid()
            except Exception:
                lic_ok = False
            general = config.setdefault("general", {})
            if lic_ok:
                general.setdefault("enable_desktop_toast", True)
                general.setdefault("enable_telegram_alert", True)
            else:
                general["enable_desktop_toast"] = False
                general["enable_telegram_alert"] = False
            # Push the new toggle to the notifier so the user sees the
            # effect of `Enable Windows toast notification` without having
            # to restart the app. If `Enable Telegram alerts` flipped on
            # (e.g. after the user activated a license mid-session),
            # build the TelegramNotifier now — ``build_telegram_notifier``
            # returns None at startup when the license gate is closed, so
            # the notifier is otherwise stuck with ``telegram=None``.
            try:
                notifier.desktop_enabled = bool(
                    general.get("enable_desktop_toast", False)
                )
            except Exception:
                pass
            try:
                tg_enabled = bool(general.get("enable_telegram_alert", False))
                if tg_enabled and notifier.telegram is None:
                    new_tg = build_telegram_notifier(env, config)
                    if new_tg is not None:
                        notifier.telegram = new_tg
                        new_tg.register_callback_handler()
                        logger_.info("Telegram notifier built (license activated).")
                elif not tg_enabled and notifier.telegram is not None:
                    # User turned the flag off — shut down the existing
                    # notifier so we don't send anything further.
                    try:
                        notifier.telegram.shutdown()
                    except Exception:
                        pass
                    notifier.telegram = None
                    logger_.info("Telegram notifier shut down (flag disabled).")
            except Exception as e:
                logger_.warning("Telegram notifier rebuild failed: %s", e)
            reschedule(scheduler, config)
            logger_.info(
                "Scheduler rescheduled after settings save (mode=%s)",
                new_cfg.get("scheduler", {}).get("mode"),
            )
        except Exception as e:
            logger_.warning("reschedule after save failed: %s", e)

    tray = TrayApp(
        on_run_now=run_now,
        on_show_history=show_history,
        on_show_settings=show_settings,
        on_exit=exit_app,
        on_open_article=open_article,
        get_status_text=get_status,
    )

    # MainWindow must run on the Tk main thread (because pystray blocks it
    # when start_blocking() is called). Strategy: launch MainWindow first
    # on the main thread, and run the tray icon in a background thread.
    # The MainWindow itself polls gui_queue via after() so background events
    # from the scheduler/notifier get dispatched on the Tk thread.
    def _start_tray_in_thread():
        try:
            tray.start_blocking()
        except Exception as e:
            logger_.exception("Tray thread crashed: %s", e)

    # NOTE ON ORDERING -- this is load-bearing.
    #
    # TrayApp.start_blocking() spawns its own "tk-gui" thread which creates a
    # SECOND tk.Tk() root and withdraws it (see desktop/tray.py). Starting the
    # tray before MainWindow meant two Tk interpreters were being created
    # concurrently on two threads, and which one won the race decided whether
    # the main window ever mapped. In a PyInstaller build the tray thread
    # consistently won and the app started with NO visible window at all --
    # the process ran, the scheduler ticked, and the user saw nothing. Dev
    # runs happened to lose the race and looked fine, which is exactly the
    # "works on my machine" shape of the bug.
    #
    # Create MainWindow's root on the main thread FIRST, then start the tray.
    logger_.debug("creating MainWindow (main thread) before the tray thread")

    # Build and run MainWindow on the main thread
    main_window = MainWindow(
        config=config,
        config_path=args.config,
        on_run_now=run_now,
        on_fetch_now=run_now,
        on_toggle_loop=toggle_loop,
        on_clear_cache=clear_cache,
        on_open_article=open_article,
        on_config_saved=config_saved,
        # ``on_exit`` does the full shutdown — clicking the X button or
        # the in-window Close button now tears everything down instead
        # of just hiding. The tray's "Exit" menu and this callback
        # share the same code path (see ``exit_app`` above), so the
        # user gets identical behaviour from both entry points.
        on_exit=exit_app,
        scheduler=scheduler,
        job_id="main_job",
    )
    # The main Tk root now exists and owns the main thread; the tray's own
    # root is created afterwards, on its own thread.
    logger_.debug("creating tray thread")
    tray_thread = threading.Thread(target=_start_tray_in_thread, daemon=True, name="tray-icon")
    tray_thread.start()

    # Sync the GUI button with the scheduler's actual paused-by-default
    # state. (The MainWindow already defaults to ``_loop_running=False``,
    # but calling ``set_loop_state`` here keeps a single source of truth
    # in case the default ever changes.)
    try:
        from apscheduler.schedulers.base import STATE_RUNNING
        main_window.set_loop_state(running=scheduler.state == STATE_RUNNING)
    except Exception:
        main_window.set_loop_state(running=False)

    # Wire gui_queue dispatch for events that should be handled by the main window
    # (toast notifications, status refresh, etc.). The MainWindow's own _refresh_dashboard
    # already runs on after(); we just need to forward "new_article" to it as a toast.
    def _dispatch(etype, payload):
        try:
            if etype == "new_article":
                # Show toast in background; user can click to open viewer.
                from desktop.toast import show_toast
                try:
                    show_toast(payload, on_click=open_article)
                except Exception:
                    pass
            elif etype == "open_viewer":
                open_article(payload)
            elif etype == "status":
                main_window._refresh_dashboard()  # type: ignore[attr-defined]
            elif etype == "loop_state":
                # payload is bool: True=running, False=paused
                try:
                    main_window.set_loop_state(bool(payload))
                except Exception:
                    pass
            elif etype == "shutdown":
                try:
                    main_window.window.destroy()
                except Exception:
                    pass
                try:
                    tray.stop()
                except Exception:
                    pass
        except Exception as e:
            logger_.exception("Dispatch error %s: %s", etype, e)

    def _poll_queue():
        try:
            event = gui_queue.get(timeout=0.2)
            if event is not None:
                etype, payload = event
                _dispatch(etype, payload)
        except Exception as e:
            logger_.debug("Queue poll error: %s", e)
        try:
            main_window.window.after(200, _poll_queue)
        except tk.TclError:
            pass

    main_window.window.after(200, _poll_queue)

    try:
        main_window.show()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            if notifier.telegram:
                notifier.telegram.shutdown()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass
    return 0


# =============================================================================
# CLI flags
# =============================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="ThaoResearch",
        description="Commodity News Intelligence & Alerting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python main.py --now --dry-run           # one-shot test (no send)
  python main.py --daemon                  # background with tray icon
  python main.py --source "Reuters Commodities"
  python main.py --lang vi --cli --now     # Vietnamese CLI
  python main.py --test-telegram           # test Telegram connection
  python main.py --license-info            # show license info
""",
    )
    parser.add_argument("--now", action="store_true", help="Run once and exit")
    parser.add_argument("--daemon", action="store_true", help="Run in background with tray icon")
    parser.add_argument("--gui", action="store_true", help="Open GUI window (Settings tab first)")
    parser.add_argument("--cli", action="store_true", help="Console mode (no GUI, no tray)")
    parser.add_argument("--cli-loop", action="store_true", help="Console loop: run forever, Ctrl+C to stop")
    parser.add_argument("--dry-run", action="store_true", help="Filter but do not send")
    parser.add_argument("--source", help="Crawl only this source by name")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config file (default: <data-dir>/init_config.yaml)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Folder for config, databases, license and logs. "
            "Overrides the saved choice and $THAORESEARCH_DATA_DIR. "
            "Default: 'data' next to the program when writable, "
            "otherwise %%LOCALAPPDATA%%\\ThaoResearch\\data"
        ),
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="Print the resolved data/log/config paths and exit",
    )
    parser.add_argument("--lang", choices=["en", "vi", "ko"], help="Interface language")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument("--test-telegram", action="store_true", help="Test Telegram connection and exit")
    parser.add_argument("--license-info", action="store_true", help="Show license info and exit")
    parser.add_argument("--reset-license", action="store_true", help="Remove license file and exit")
    parser.add_argument("--install-autostart", action="store_true", help="Register Windows autostart and exit")
    parser.add_argument("--uninstall-autostart", action="store_true", help="Unregister Windows autostart and exit")
    parser.add_argument("--build-exe", action="store_true", help="Build .exe via PyInstaller and exit")
    parser.add_argument(
        "--bypass-license",
        action="store_true",
        help="Ignore license check entirely. "
        "This flag is normally set automatically by the 'bypass-license' PyInstaller build "
        "and should not be passed manually.",
    )

    args = parser.parse_args(argv)
    # --cli-loop implies --cli + loop forever
    if args.cli_loop:
        args.cli = True
    # Default mode selection:
    #   - if running as a frozen .exe on Windows and no mode flag was passed → --gui
    #   - otherwise → --daemon (legacy behavior)
    has_mode_flag = any(
        [
            args.now,
            args.cli,
            args.daemon,
            args.gui,
            args.test_telegram,
            args.license_info,
            args.reset_license,
            args.install_autostart,
            args.uninstall_autostart,
            args.build_exe,
            args.bypass_license,
        ]
    )
    if not has_mode_flag:
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            args.gui = True
        else:
            args.daemon = True
    return args


# =============================================================================
# Sub-commands
# =============================================================================
def cmd_license_info():
    info = license_mod.get_license_info()
    if not info:
        print("No license file found at data/license.dat")
        return 1
    status = license_mod.get_license_status()
    heading = {
        license_mod.STATUS_ACTIVATED: "Activated",
        license_mod.STATUS_EXPIRED: "Expired",
        license_mod.STATUS_INVALID: "Invalid",
    }.get(status, "Unknown")
    print("License info")
    print("─" * 40)
    print(f"  Status:     {heading}")
    print(f"  License key: {info.get('license_key', '?')}")
    valid_from = (info.get('issued_at') or '?')[:10]
    print(f"  Valid from: {valid_from}")
    print(f"  Valid to:   {info.get('valid_to', '?')}")
    return 0


def cmd_reset_license():
    license_mod.clear_license()
    print("License file removed.")
    return 0


def cmd_install_autostart():
    ok, msg = autostart.install_autostart()
    print(msg)
    return 0 if ok else 1


def cmd_uninstall_autostart():
    ok, msg = autostart.uninstall_autostart()
    print(msg)
    return 0 if ok else 1


def cmd_build_exe():
    print("Building .exe with PyInstaller...")
    try:
        import PyInstaller.__main__ as pyi_main  # type: ignore
    except ImportError:
        print("[E9999] PyInstaller is not installed. Run: pip install pyinstaller")
        return 1
    args = [
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "ThaoResearchWatcher",
        "--icon", "assets/tray.ico",
        "--add-data", "locales;locales",
        "--add-data", "config.yaml;.",
        "--add-data", "assets;assets",
        "main.py",
    ]
    pyi_main.run(args)
    print("Done. Output: dist/ThaoResearch.exe")
    return 0


def cmd_test_telegram(args, env: dict):
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not validate_token(token):
        print("[E0202] Bot token missing or invalid format.")
        print_telegram_setup_help()
        return 1
    if not chat_id or not validate_chat_id(chat_id):
        print("[E0204] Chat ID missing or invalid.")
        return 1
    print("Testing Telegram connection...")
    n = TelegramNotifier(token, chat_id)
    ok, msg = n.test_connection()
    n.shutdown()
    if ok:
        print(f"[OK] {msg}")
        return 0
    print(f"[FAIL] {msg}")
    return 1


# =============================================================================
# Main
# =============================================================================
def _resolve_path(user_path: Path) -> Path:
    """Find an existing file given a user path (relative or absolute).

    Search order for FROZEN apps (portable builds):
      1. Next to sys.executable (the portable install folder) — HIGHEST PRIORITY
      2. PyInstaller's _MEIPASS (the bundle temp dir)

    Search order for dev runs:
      1. CWD-relative (the default behavior).
      2. The project root.

    For explicit absolute paths: return as-is if exists.
    """
    # Explicit absolute paths are returned as-is
    if user_path.is_absolute() and user_path.exists():
        return user_path

    candidates = []

    # For frozen (portable) builds: check EXE location FIRST
    if getattr(sys, "frozen", False):
        try:
            exe_dir = Path(sys.executable).resolve().parent
            candidates.append(exe_dir / user_path)
        except Exception:
            pass

    # CWD-relative (works in dev)
    try:
        candidates.append(Path.cwd() / user_path)
    except Exception:
        pass

    # For frozen builds: also check project root (parent of dist/)
    if getattr(sys, "frozen", False):
        try:
            exe_dir = Path(sys.executable).resolve().parent
            cur = exe_dir
            for _ in range(3):
                cur = cur.parent
                candidates.append(cur / user_path)
        except Exception:
            pass

    # _MEIPASS for bundled read-only assets
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / user_path)

    # Home folder fallback
    home = Path(os.path.expanduser("~"))
    candidates.append(home / "ThaoResearch" / user_path)

    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue

    return user_path


def _resolve_config_path(user_path: Path) -> Path:
    return _resolve_path(user_path)


def main(argv=None) -> int:
    args = parse_args(argv)

    # ---- Data directory: resolve BEFORE anything opens a file -------------
    # The logger, the SQLite helpers and the license module all derive their
    # paths from utils.paths, so the override has to land before the first
    # setup_logger() / init_db() call. An unwritable choice is reported
    # rather than obeyed (paths.set_data_dir falls back and flags it).
    paths_mod.set_data_dir(getattr(args, "data_dir", None))

    if getattr(args, "show_paths", False):
        print(paths_mod.describe_paths())
        return 0

    # When running as a portable frozen install, all data must live next to
    # the EXE (so users can copy the install folder around). Any code that
    # still uses a bare ``Path("data/...")`` or ``Path("logs/...")`` will be
    # correct *only* if the process's cwd == APP_ROOT. Force that here.
    if getattr(sys, "frozen", False):
        try:
            os.chdir(str(APP_ROOT))
        except Exception:
            pass
    else:
        # Dev mode: also chdir to APP_ROOT so ``Path("data/...")`` matches
        # the bundled tests, but only if we are NOT already in the project
        # root (avoid surprising users who cd elsewhere and run --test-x).
        try:
            os.chdir(str(APP_ROOT))
        except Exception:
            pass

    # Windows: set AppUserModelID so the taskbar shows the right icon + name
    # instead of the generic Python icon (the PyInstaller --icon flag only sets
    # the EXE metadata; the running process needs AppUserModelID).
    if sys.platform == "win32":
        try:
            import ctypes
            # Use a stable GUID-style AppUserModelID. Bumping the GUID when
            # we change the icon forces Explorer to drop its cached entry
            # and re-resolve from the .exe + iconbitmap().
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ThaoResearch.Watcher.2fa1e1d9"
            )
            # Also call SetCurrentProcessExplicitAppUserModelID for COM
            # apartments via OLE, which is what some Windows shells require.
            try:
                ctypes.windll.ole32.CoInitialize(None)
            except Exception:
                pass
        except Exception:
            pass

    # Force UTF-8 stdout/stderr on Windows so the GUI build (which detaches
    # from the console) doesn't blow up when logging non-ASCII characters
    # (e.g. banner separators). Safe no-op on Linux/macOS.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    # Determine "command" (for license check routing)
    if args.license_info:
        args.command = "license_info"
    elif args.reset_license:
        args.command = "reset_license"
    elif args.install_autostart:
        args.command = "install_autostart"
    elif args.uninstall_autostart:
        args.command = "uninstall_autostart"
    elif args.build_exe:
        args.command = "build_exe"

    # Set up logger early so license check / arg parsing logs go to file too
    setup_logger(level=logging.DEBUG if args.verbose else logging.INFO, verbose=args.verbose)
    logger_ = get_logger("main")

    # Sub-commands that don't need license / config
    # For all of these we still want to honor THRESEARCH_LICENSE for testing/CI.
    args_command = getattr(args, "command", None)
    pre_commands = {"license_info", "reset_license", "build_exe", "install_autostart", "uninstall_autostart"}
    env_license = os.environ.get("THRESEARCH_LICENSE")
    if env_license and args_command in pre_commands:
        # If a license file already exists, verify against it; otherwise just save.
        if license_mod.LICENSE_FILE.exists():
            try:
                license_mod.verify_license(env_license)
            except Exception as e:
                logger_.debug("License env var verify failed: %s", e)
        else:
            try:
                license_mod.save_license(env_license)
                logger_.debug("License saved via env var (no prior file)")
            except Exception as e:
                logger_.debug("License env var save failed: %s", e)

    if args.license_info:
        return cmd_license_info()
    if args.reset_license:
        return cmd_reset_license()
    if args.install_autostart:
        return cmd_install_autostart()
    if args.uninstall_autostart:
        return cmd_uninstall_autostart()
    if args.build_exe:
        return cmd_build_exe()

    # License check (BEFORE loading config)
    args.cli = bool(getattr(args, "cli", False)) or bool(args.now)
    if not check_license_at_startup(args):
        return 1

    # Load config. We prefer ``init_config.yaml`` (the autosave target)
    # over the original ``config.yaml`` if both exist, so the user's
    # last GUI edit is what they get on next launch. If only
    # ``config.yaml`` exists, we fall back to it (first run / frozen
    # build). ``--config <path>`` always wins (explicit override).
    if args.config is None:
        # Both candidates live in the resolved data directory, so read them
        # from utils.paths rather than a module-level snapshot taken before
        # --data-dir was applied.
        init_path = paths_mod.INIT_CONFIG_FILE
        if init_path.exists():
            print(f"[INFO] Loading autosaved config from: {init_path}")
            args.config = init_path
        elif paths_mod.CONFIG_FILE.exists():
            print(f"[INFO] Loading config from: {paths_mod.CONFIG_FILE}")
            args.config = paths_mod.CONFIG_FILE
        elif _migrate_legacy_config(init_path):
            args.config = init_path
        else:
            # First run (fresh portable install): write the shipped defaults.
            try:
                _create_default_config(init_path)
                args.config = init_path
            except Exception as e:
                print(f"[WARN] Could not create default config: {e}")
                args.config = init_path

    resolved_config = _resolve_config_path(args.config)
    if str(resolved_config) != str(args.config):
        print(f"[INFO] Config found at: {resolved_config}")
        args.config = resolved_config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        # Last resort: create default config
        print(f"[INFO] Config not found, creating default: {args.config}")
        try:
            _create_default_config(args.config)
            config = load_config(args.config)
        except Exception as e2:
            print(f"[E0100] Config file not found and could not create default: {e2}")
            return 1
    except ValueError as e:
        print(f"[E0101] {e}")
        return 1

    # CRITICAL: merge defaults into user config so missing keys still work.
    # Portable users sometimes have partial configs (only timezone, etc.)
    # and the GUI will crash if `sources`/`commodities`/`filters` is missing.
    config = _merge_with_defaults(config)

    # Language: --lang > config > env > default
    lang = args.lang or config.get("general", {}).get("language")
    i18n.init_language(lang)

    # Credentials are embedded in utils/credentials.py, no external file needed
    env = load_env(None)

    # Print banner + config (only for CLI modes — GUI mode shows everything in
    # the MainWindow status bar to avoid stdout encoding issues on Windows).
    is_gui_mode = bool(getattr(args, "gui", False))
    if not is_gui_mode:
        print_banner(VERSION)
        print_config_summary(config)

        # Welcome help on first run (no license.dat yet means likely first run)
        if DEFAULT_WELCOME and not license_mod.LICENSE_FILE.exists():
            print_welcome_help()

    # Initialize DBs
    try:
        database.init_db()
        price_tracker.init_db()
    except Exception as e:
        print(f"[E0603] Failed to init database: {e}")
        return 1

    # License gate: without a valid license, both the Telegram alerts and
    # Windows toast notifications must be disabled — even if the YAML
    # still has them set to True. We also stamp the runtime effective
    # values back into the in-memory config so any downstream consumer
    # reads the gated value.
    license_active = False
    try:
        license_active, license_err = license_mod.is_license_valid()
    except Exception:
        license_err = "unknown"
    if not license_active:
        if license_err:
            logger_.warning("License inactive (%s) — disabling Telegram + toast.", license_err)
        else:
            logger_.warning("No valid license — disabling Telegram + toast.")
        config.setdefault("general", {})["enable_desktop_toast"] = False
        config["general"]["enable_telegram_alert"] = False
    else:
        logger_.info("License active — Telegram + toast enabled per config.")

    # Build notifier
    telegram = build_telegram_notifier(env, config)
    notifier = Notifier(
        telegram=telegram,
        desktop_enabled=bool(config.get("general", {}).get("enable_desktop_toast", True)),
    )
    # Register inline-keyboard callbacks (📋 Summary / 🙈 Hide) for Telegram
    if telegram:
        telegram.register_callback_handler()

    # --test-telegram subcommand
    if args.test_telegram:
        return cmd_test_telegram(args, env)

    # Single run modes
    if args.now or args.cli:
        if not telegram:
            maybe_print_telegram_setup(env)
        if args.source:
            # Filter sources
            wanted = args.source
            config = {**config, "sources": [s for s in config.get("sources", []) if s.get("name") == wanted]}
            if not config["sources"]:
                print(f"[E0102] No source named '{wanted}'")
                return 1
        try:
            # Start Esc-watcher so user can exit cleanly with confirmation
            import os as _os
            if _os.isatty(0):  # only when stdin is a real terminal
                start_exit_watcher(on_confirmed_exit=lambda: _os._exit(0))

            if args.cli_loop:
                # Loop forever in foreground. Print interval + next run.
                # Reuse adaptive interval logic from daemon mode.
                from datetime import datetime as _dt
                # get_next_interval_minutes is defined above in this module
                interval_fn = get_next_interval_minutes

                print("[CLI-LOOP] Foreground loop started. Ctrl+C to stop.")
                print("[CLI-LOOP] Press Esc twice within 3s for safe exit.")
                while True:
                    try:
                        job_execute(config, notifier, dry_run=args.dry_run, logger_=logger_)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        logger_.exception("Loop iteration failed: %s", e)
                    if interval_fn:
                        nxt = interval_fn(config, _dt.now())
                        if nxt <= 0:
                            nxt = 30
                    else:
                        nxt = 15
                    print(f"[CLI-LOOP] Sleeping {nxt} min until next run. (Ctrl+C to stop)")
                    try:
                        import time as _time
                        _time.sleep(nxt * 60)
                    except KeyboardInterrupt:
                        raise
            else:
                job_execute(config, notifier, dry_run=args.dry_run, logger_=logger_)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        return 0

    # GUI mode (default when running ThaoResearch.exe on Windows)
    if args.gui:
        if not telegram:
            maybe_print_telegram_setup(env)
        try:
            return run_gui(args, config, env, notifier)
        except Exception as e:
            logger_.exception("GUI crashed: %s", e)
            try:
                crash_log = paths_mod.LOG_DIR / "crash.log"
                crash_log.parent.mkdir(parents=True, exist_ok=True)
                with open(crash_log, "a", encoding="utf-8") as f:
                    f.write(
                        f"\n[{datetime.now().isoformat()}] GUI crashed: {e}\n"
                    )
                    import traceback as _tb
                    _tb.print_exc(file=f)
            except Exception:
                logger_.warning("Failed to write crash log")
            return 1

    # Daemon mode (default for CLI invocation)
    if args.daemon:
        if not telegram:
            maybe_print_telegram_setup(env)
        try:
            return run_daemon(args, config, env, notifier)
        except Exception as e:
            logger_.exception("Daemon crashed: %s", e)
            try:
                crash_log = paths_mod.LOG_DIR / "crash.log"
                crash_log.parent.mkdir(parents=True, exist_ok=True)
                with open(crash_log, "a", encoding="utf-8") as f:
                    f.write(
                        f"\n[{datetime.now().isoformat()}] Daemon crashed: {e}\n"
                    )
                    import traceback as _tb
                    _tb.print_exc(file=f)
            except Exception:
                logger_.warning("Failed to write crash log")
            return 1

    # Should not reach here
    print("No mode selected. Use --daemon, --cli, --now, --gui, or --help.")
    return 1


def _report_fatal(exc: BaseException) -> None:
    """Make a startup crash visible, however the app was launched.

    Release builds are PyInstaller ``--windowed``: there is no console, so a
    traceback on stderr goes nowhere and the user just sees the program fail
    to open. That is exactly how the ``PermissionError`` from an unwritable
    data folder stayed invisible. We now, in order:

      1. print to stderr (console builds, `python main.py`),
      2. append to ``<data-dir>/logs/startup-error.log`` when anything is
         writable, falling back to the temp folder,
      3. show a Tk message box with the path report, so a user who only
         double-clicks the exe still learns what went wrong and where.
    """
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    try:
        report = paths_mod.describe_paths()
    except Exception:
        report = "(path report unavailable)"

    body = (
        f"ThaoResearch {VERSION} failed to start.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"{report}\n\n{tb}"
    )

    try:
        print(body, file=sys.stderr)
    except Exception:
        pass

    # Write somewhere -- the data dir if possible, else temp.
    for target in (paths_mod.LOG_DIR, Path(tempfile.gettempdir()) / "ThaoResearch"):
        try:
            target.mkdir(parents=True, exist_ok=True)
            with open(target / "startup-error.log", "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now().isoformat()} =====\n{body}\n")
            break
        except Exception:
            continue

    # A dialog is the only channel a --windowed build actually has.
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb

        root = _tk.Tk()
        root.withdraw()
        hint = ""
        if isinstance(exc, PermissionError) or "WinError 5" in str(exc):
            hint = (
                "\n\nThis usually means the folder holding the program is not "
                "writable. Move the folder somewhere like your Documents, or "
                "start it with:\n"
                "    ThaoResearch.exe --data-dir \"%LOCALAPPDATA%\\ThaoResearch\""
            )
        _mb.showerror(
            "ThaoResearch could not start",
            f"{type(exc).__name__}: {exc}{hint}\n\n{report}",
        )
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as _e:  # noqa: BLE001 - last line of defence
        _report_fatal(_e)
        sys.exit(1)
