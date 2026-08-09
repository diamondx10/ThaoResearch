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
DEFAULT_CONFIG = Path("config.yaml")
# ``init_config.yaml`` is the autosave target — written every time the
# user changes a setting in the GUI. On next launch we prefer it over
# ``config.yaml`` so the user always gets the most recent state they
# edited. ``config.yaml`` is treated as a fallback (and as the bootstrap
# file the project ships with).
DEFAULT_INIT_CONFIG = Path("init_config.yaml")
DEFAULT_ENV = Path(".env")
DEFAULT_WELCOME = True  # print welcome help on first run


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
    }
    cmd = getattr(args, "command", None)

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


def load_env(path: Path) -> dict:
    """Load .env file. Returns dict of variables. Does not raise."""
    if not path.exists():
        return {}
    load_dotenv(path, override=False)
    return {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }


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
        return stats

    # Localize dates
    normalize_pub_dates(all_articles, tz_name=tz_name)

    # Filter
    matched = apply_filters(all_articles, config)
    stats["filtered"] = len(matched)

    if not matched:
        print("No articles match the filter.")
        stats["duration_s"] = time.time() - t0
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
        gui_queue.put(("shutdown", None))
        try:
            scheduler.shutdown(wait=False)
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
        """Wipe local data/history.db, data/prices.db, logs/*."""
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
        gui_queue.put(("shutdown", None))
        try:
            scheduler.shutdown(wait=False)
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

    logger_.debug("creating tray thread")
    tray_thread = threading.Thread(target=_start_tray_in_thread, daemon=True, name="tray-icon")
    tray_thread.start()

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
        scheduler=scheduler,
        job_id="main_job",
    )
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
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config file")
    parser.add_argument("--lang", choices=["en", "vi", "ko"], help="Interface language")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument("--test-telegram", action="store_true", help="Test Telegram connection and exit")
    parser.add_argument("--license-info", action="store_true", help="Show license info and exit")
    parser.add_argument("--reset-license", action="store_true", help="Remove license file and exit")
    parser.add_argument("--install-autostart", action="store_true", help="Register Windows autostart and exit")
    parser.add_argument("--uninstall-autostart", action="store_true", help="Unregister Windows autostart and exit")
    parser.add_argument("--build-exe", action="store_true", help="Build .exe via PyInstaller and exit")

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

    Search order:
      1. The user-supplied path (if absolute and exists, use it).
      2. CWD-relative (the default behavior).
      3. Next to sys.executable (the exe location) — common for frozen apps.
      4. Inside PyInstaller's _MEIPASS (the bundle temp dir).
      5. The user's home folder (ThaoResearch/<name>) for portability.
    """
    if user_path.is_absolute() and user_path.exists():
        return user_path

    candidates = []

    try:
        candidates.append(Path.cwd() / user_path)
    except Exception:
        pass

    try:
        exe_dir = Path(sys.executable).resolve().parent
        # Direct neighbour of the exe.
        candidates.append(exe_dir / user_path)
        # When running as a frozen PyInstaller build, the user's `.env`
        # and `config.yaml` typically live in the project root, which is
        # the parent of the `dist/` folder that contains the exe. Walk up
        # a few levels so we can find them without manual copying.
        if getattr(sys, "frozen", False):
            cur = exe_dir
            for _ in range(3):
                cur = cur.parent
                candidates.append(cur / user_path)
    except Exception:
        pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / user_path)

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
    if args.config == DEFAULT_CONFIG:
        init_path = _resolve_path(DEFAULT_INIT_CONFIG)
        if init_path.exists():
            print(f"[INFO] Loading autosaved config from: {init_path}")
            args.config = init_path
    resolved_config = _resolve_config_path(args.config)
    if str(resolved_config) != str(args.config):
        print(f"[INFO] Config found at: {resolved_config}")
        args.config = resolved_config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"[E0100] Config file not found: {args.config}")
        print("        Copy config.yaml.example to config.yaml first.")
        return 1
    except ValueError as e:
        print(f"[E0101] {e}")
        return 1

    # Language: --lang > config > env > default
    lang = args.lang or config.get("general", {}).get("language")
    i18n.init_language(lang)

    env_path = _resolve_path(DEFAULT_ENV)
    if str(env_path) != str(DEFAULT_ENV):
        print(f"[INFO] .env found at: {env_path}")
    env = load_env(env_path)

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
                crash_log = Path("logs") / "crash.log"
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
                crash_log = Path("logs") / "crash.log"
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


if __name__ == "__main__":
    sys.exit(main())
