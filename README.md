--- START OF README.md ---

ThaoResearch – Commodity News Intelligence & Alerting System
Version: 1.0.0
Language: Python 3.10+
Purpose: A CLI-based automated tool to crawl, filter, and alert on commodity price movements from international financial news sites (Reuters, Investing.com), with real-time notifications via Telegram Bot.

1. System Overview
ThaoResearch operates as a scheduled background service (using a cron-like scheduler). It executes a 4-step pipeline:

Crawl: Fetch RSS feeds or HTML content from configured sources.

Parse: Extract standardized data (title, publication date, summary, link) from raw content.

Filter: Apply rule-based logic to detect relevant commodities (Gold, Crude Oil, Gasoline, Soybean Oil) and price triggers.

Notify: Send filtered alerts to a Telegram chat via the Bot API and store sent IDs in a local database to prevent duplicates.

2. Core Features
Multi-Source Support: Pre-configured for reuters.com and investing.com RSS feeds. Users can easily add or disable sources in the configuration file.

Commodity Watchlist: Enable/disable monitoring for specific commodities and define custom keywords for each (e.g., "Gold", "XAU", "Crude", "WTI", "Soybean").

Smart Filtering Engine:

Keyword Matching: Detects commodity-specific terms and high-priority words (e.g., "forecast", "closing price", "shock").

Price Threshold Detection: Scans article text using Regex to extract numeric values and triggers alerts if a price exceeds a defined threshold (e.g., Crude > $85/barrel).

Blacklist: Ignores articles containing spam or irrelevant keywords (e.g., "sponsored", "advertisement").

Telegram Integration: Sends beautifully formatted Markdown messages including the article title, summary, original link, and timestamp.

Anti-Duplicate Mechanism: Uses a local SQLite database to store unique hashes (based on title + link) to ensure each article is reported only once.

Scheduled Execution: Built-in APScheduler runs the scraping loop automatically (default: every 5 minutes). Also supports one-time manual execution.

Clean Logging: Outputs detailed logs to both the console and a persistent file (logs/app.log) for debugging and monitoring.

3. Technology Stack
Component	Technology / Library	Purpose
Language	Python 3.10+	Core scripting with dataclasses and typing.
HTTP Client	requests (v2.31+)	Fetching web pages and RSS XML files.
RSS Parsing	feedparser (v6.0+)	Parsing standard RSS/XML feeds from news providers.
HTML Parsing	beautifulsoup4 (v4.12+)	Fallback parser for non-RSS HTML pages (extracting title/meta).
Config Management	pyyaml (v6.0+)	Reading the main config.yaml file.
Scheduling	apscheduler (v3.10+)	Running background jobs (BackgroundScheduler).
Database	sqlite3 (built-in)	Lightweight local storage for deduplication.
Telegram API	requests (direct calls)	POST requests to api.telegram.org. No external wrapper.
Environment	python-dotenv (v1.0+)	Loading sensitive tokens (Bot Token, Chat ID) from .env.
4. Project Directory Structure
The AI Agent must create the following standard folder structure:

text
ThaoResearch/
├── main.py                   # Entry point (CLI & Scheduler)
├── config.yaml               # User configuration (sources, commodities, filters)
├── .env                      # TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
├── requirements.txt          # Python dependencies
├── README.md                 # This file
│
├── core/
│   ├── __init__.py
│   ├── crawler.py            # Crawling logic (RSS + HTML fallback)
│   ├── parser.py             # Data standardization (Article object)
│   ├── filter_engine.py      # Applying keyword, price, and blacklist rules
│   └── notifier.py           # Sending messages to Telegram
│
├── utils/
│   ├── __init__.py
│   ├── database.py           # SQLite init, duplicate checks, insert logic
│   └── logger.py             # Logging configuration (file + console)
│
├── models/
│   └── __init__.py
│   └── article.py            # Dataclass for Article (title, link, pub_date, summary, source)
│
└── data/
    └── history.db            # Auto-generated SQLite database (do not commit)
5. Configuration Reference (config.yaml)
The user will edit this YAML file to customize the tool. The AI Agent must implement parsing for this exact schema.

yaml
# ThaoResearch Configuration File

# 1. NEWS SOURCES (RSS preferred)
sources:
  - name: "Reuters Commodities"
    type: "rss"
    url: "http://feeds.reuters.com/reuters/commoditiesNews"
    enabled: true

  - name: "Investing Oil"
    type: "rss"
    url: "https://www.investing.com/rss/news_Oil.rss"
    enabled: true

  - name: "Investing Gold"
    type: "rss"
    url: "https://www.investing.com/rss/news_Gold.rss"
    enabled: true

# 2. COMMODITY WATCHLIST
commodities:
  - name: "Gold"
    keywords: ["gold", "XAU", "bullion", "kim loại quý"]
    enabled: true
    conditions:
      price_threshold: 1900   # Alert if price > 1900

  - name: "Crude Oil"
    keywords: ["crude", "WTI", "Brent", "OPEC", "dầu thô"]
    enabled: true
    conditions:
      price_threshold: 85

  - name: "Gasoline"
    keywords: ["gasoline", "RBOB", "xăng"]
    enabled: true

  - name: "Soybean Oil"
    keywords: ["soybean oil", "SBO", "dầu đậu nành"]
    enabled: true

# 3. GLOBAL FILTERS
filters:
  # Urgent keywords to prioritize
  high_priority_keywords: ["close", "settlement", "forecast", "record", "surge"]
  
  # Keywords to exclude (spam prevention)
  blacklist_keywords: ["advertisement", "sponsored", "free webinar"]

# 4. GENERAL SETTINGS
general:
  timezone: "Asia/Ho_Chi_Minh"
  check_interval_minutes: 5
  max_articles_per_run: 10
6. Core Processing Logic (Implementation Specs)
The AI Agent must implement the following workflows strictly:

6.1. Data Flow
Initialization:

Load .env (Bot Token, Chat ID).

Load config.yaml.

Initialize SQLite (data/history.db). Create table sent_articles with column article_hash (UNIQUE).

Fetching (crawler.py):

Send GET request with a realistic User-Agent header.

If type: "rss", parse using feedparser.

If type: "html", parse using BeautifulSoup (extract <title> and meta description).

Return a standardized list of Article objects.

Filtering (filter_engine.py):

Step 1 (Blacklist): Drop any article containing words in blacklist_keywords (case-insensitive).

Step 2 (Commodity Tagging): Check title and summary against each commodity's keywords. If matched, assign the commodity tag to the article.

Step 3 (Price Extraction): Use Regex r'\b\d+[.,]?\d*\s*(USD|\$)?' to find prices. Compare against price_threshold. Set is_price_alert = True if exceeded.

Step 4 (Urgency): If high_priority_keywords are found, set is_urgent = True.

Deduplication & Sending (notifier.py):

Generate MD5 hash from title + link.

Query SQLite: If hash exists, skip.

If new, format message in Markdown:

text
🚨 *[COMMODITY_NAME]* - PRICE ALERT!
📰 *{title}*
📝 {summary}...
🔗 [Read full article]({link})
🕒 {pub_date}
(Add ⚠️ prefix if is_urgent is True)

Send POST payload to https://api.telegram.org/bot<TOKEN>/sendMessage.

On success (ok: true), insert hash into SQLite.

Scheduling (main.py):

Use BackgroundScheduler to loop job_execute().

CLI arguments:

python main.py --now (Run once immediately).

python main.py --daemon (Run continuously in background).

7. Installation & Usage Guide
For End-Users (to be included in README):

bash
# 1. Clone the repository
git clone <your-repo-url>
cd ThaoResearch

# 2. Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
Contents of requirements.txt:

text
requests==2.31.0
feedparser==6.0.10
beautifulsoup4==4.12.2
pyyaml==6.0.1
apscheduler==3.10.4
python-dotenv==1.0.0
Environment Variables (.env):

text
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=-1234567890
Running the Tool:

bash
# Test run (one-time fetch)
python main.py --now

# Run as a background daemon
python main.py --daemon

7.5. Settings Persistence (`init_config.yaml`) — autosave + auto-load

Every change in the Settings window is autosaved 500 ms after the last keystroke / toggle, and the result is reloaded automatically on the next launch. No need to click **Save** unless you want to force-flush immediately.

**Files**

| File | Role |
|---|---|
| `config.yaml` | The "source of truth" shipped with the repo / your initial bootstrap. Read-only after first run. |
| `init_config.yaml` | The autosave target — every GUI edit lands here. **Preferred on next launch.** |
| `data/license.dat` | License activation blob (separate file, unrelated to config). |

**Load order at startup**

1. `--config <path>` (CLI override) — wins over everything.
2. `init_config.yaml` next to the .exe (or in CWD, or up to 3 levels above `dist/` for frozen builds) — used **if it exists**.
3. `config.yaml` — fallback for first launch / fresh install.

**Where the file lives**

The autosave target is **always co-located with `config.yaml`** so the two never drift. For a frozen build, that's the project root (one level above `dist\`). For a dev install, it's wherever you launched the app.

**Manual Save vs Autosave**

- **Autosave** (default ON) — every widget change writes to `init_config.yaml` after 500 ms of inactivity. Cancels itself on each new change so the user sees the file written exactly once per "burst" of edits.
- **Manual Save** — same `init_config.yaml`, but the button flushes immediately and shows a "Settings saved." confirmation. Useful when you've finished editing and want to verify on disk.
- **Cancel** — flushes any pending autosave first, then closes the dialog.

**Toggling it off**

In **Settings → General** you'll see:

```
☑ Autosave settings (init_config.yaml)
```

Untick it and the file stops being written automatically. Manual Save still works. Useful if you're editing `config.yaml` by hand and don't want the GUI to clobber your changes mid-run.

**What gets persisted**

Everything the Settings window exposes: timezone, language, scheduler mode + adaptive windows, watchlist (sources, commodities), filters (high-priority + blacklist keywords, delta alert), Telegram + desktop notification toggles, **force-send-duplicates**, **autosave-settings** itself, and the new license key (via the Submit button — separate code path).

What **does not** get persisted in `init_config.yaml`: Telegram token + chat ID (they live in `.env` for security), the `.dat` license blob, the `data/history.db` SQLite dedup table.

**Reset to factory**

If `init_config.yaml` ever gets corrupted or you want to start fresh, just delete it — the app falls back to `config.yaml` on the next launch:

```powershell
# Wipe autosaved state and re-bootstrap from the shipped defaults
Remove-Item C:\Unillog\ThaoResearch\init_config.yaml
C:\Unillog\ThaoResearch\dist\ThaoResearch.exe
```

7.6. Exiting the App — full shutdown, not "hide to tray"

The **Close** button (status bar bottom-right) and the window's **X** button both perform a **full shutdown** — the scheduler stops, the Telegram notifier closes its async loop, the tray icon disappears, and the process exits. To confirm the action, the dialog asks:

> *Exit ThaoResearch? This will stop the scheduler and Telegram alerts. Continue?*

Choose **Yes** to fully exit, **No** to return to the dashboard.

This matches the **tray menu's "Exit" item** — three entry points, identical behaviour.

**What gets torn down on Exit**

| Component | Action |
|---|---|
| APScheduler (background cron) | `scheduler.shutdown(wait=False)` — cancels any in-flight job |
| Telegram bot | `notifier.telegram.shutdown()` — closes the asyncio loop, kills the polling thread |
| System tray icon | `tray.stop()` — icon disappears from the notification area |
| Tk main window | `window.destroy()` — mainloop returns |
| Fetch Now worker thread | Daemon thread, dies with the process |

**Files persisted on Exit**

- `init_config.yaml` (autosave) — anything you edited in the last 500 ms is already on disk.
- `data/history.db` — every dedup record from the session is already committed (writes are synchronous).
- `data/license.dat` — license activation blob (untouched by Exit).
- `.env` — Telegram credentials (untouched).

**Files NOT persisted**

- Dashboard counters / recent-articles list / log tail — these are in-memory only.
- Running job progress — `Fetch complete` / `Fetch failed` labels disappear with the window.

**If the dialog doesn't appear**

On rare occasions the message-box can fail (window already destroyed, focus loss on multi-monitor setups). The fallback path calls `on_exit()` directly without confirmation, so the app still exits cleanly.

8. Telegram Bot Setup (BotFather)
Open Telegram and search for @BotFather.

Send /newbot and follow the instructions to get your Bot Token.

Send a dummy message to your new bot.

Get your Chat ID by visiting https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates and extracting the id field from the response.

Paste both values into the .env file.

9. Mandatory Requirements for the AI Agent (Delivery Checklist)
Before finalizing the code, the AI Agent must ensure the following:

□ Error Resilience: Implement retry logic (max 3 attempts) with exponential backoff for network requests. The program must never crash on a single failed request.
□ Comprehensive Logging: Log every critical step (crawl count, filtered count, sent count) to both the console and a rotating file logs/app.log at the INFO level.
□ Timezone Standardization: Convert all publication dates (pub_date) to Asia/Ho_Chi_Minh timezone before formatting.
□ Database Integrity: Use INSERT OR IGNORE in SQLite to prevent race conditions during concurrent job runs.
□ Extensibility: The crawler module must be structured with a base BaseCrawler class, allowing easy addition of new news sources in the future.
□ No Hardcoding: All URLs, tokens, keywords, and intervals must be sourced from config.yaml or .env. Absolutely zero hardcoded credentials or endpoints in the .py files.
□ Rate Limiting: Respect third-party servers by setting a timeout=10s and adding a 2-second delay (time.sleep(2)) between requests.
10. Legal & Ethical Disclaimer
This tool strictly uses publicly available RSS feeds provided by the respective news websites.

It does not perform heavy web scraping or brute-force crawling. All requests are throttled to avoid server overload.

The data fetched is intended solely for internal analysis and informational purposes. Redistribution of copyrighted news content is strictly prohibited.

Users are responsible for ensuring their usage complies with the Terms of Service of the data sources.

11. Expected Telegram Output Example
When successfully executed, the user will receive a message like this:

text
🚨 *Crude Oil* - PRICE ALERT!
📰 *WTI crude surges past $90 as OPEC+ maintains output cuts*
📝 Growing supply concerns from the Middle East and OPEC+ production cuts pushed oil prices to a 3-month high during Asian trading...
🔗 [Read full article](https://reuters.com/markets/commodities/...)
🕒 2026-08-08 15:30:07 +07:00

12. Build & Distribution (Windows .exe)

This section explains how to rebuild the standalone Windows executable (`dist\ThaoResearch.exe`) that bundles the GUI, the tray icon, and all dependencies into a single file.

### 12.1 Prerequisites

The build environment needs the same dependencies as runtime, plus **PyInstaller** and **ruamel.yaml** (used for round-tripping `config.yaml` with preserved comments/quotes):

```powershell
# Activate the virtualenv first (if you created one)
cd C:\Unillog\ThaoResearch
.\venv\Scripts\Activate.ps1

# Install runtime + build dependencies
pip install -r requirements.txt
pip install pyinstaller ruamel.yaml
```

`requirements.txt` includes (pinned versions used during development):

```text
requests==2.31.0
feedparser==6.0.10
beautifulsoup4==4.12.2
pyyaml==6.0.1
apscheduler==3.10.4
python-dotenv==1.0.0
pystray==0.19.5
Pillow==10.2.0
win11toast==1.1.1
plyer==2.1.0
pypiwin32==223
tkhtmlview==0.3.1
python-dateutil==2.8.2
tzdata==2024.1
```

> **Note:** PyInstaller is *not* in `requirements.txt` — it's only needed at build time. `ruamel.yaml` is also build-time only (the runtime falls back to `yaml.safe_dump` if it's missing, but round-tripping with preserved quotes is lost).

### 12.2 One-command build (recommended)

The repo ships a wrapper that calls PyInstaller with all the flags we need (`--onefile`, `--windowed`, hidden imports, bundled `locales/`, `config.yaml`, `.env`, `assets/`, and the two license scripts):

```powershell
cd C:\Unillog\ThaoResearch
python build_exe.py
```

What this does:
- Generates `dist\ThaoResearch.exe` (single-file, ~35 MB).
- Bundles `locales\*.json` for the multilingual GUI.
- Bundles `config.yaml` and `.env` next to the exe (so the .exe has sensible defaults out of the box).
- Bundles `assets\tray.ico` for the system-tray icon.
- Bundles `license_checker.py` + `license_generator.py` so the in-app license dialog can call them at runtime (they're imported dynamically inside `utils/license.py`).
- Hides the console window (`--windowed`).
- Collects all of `apscheduler`, `pystray`, and `tkhtmlview` to avoid module-discovery issues.

Build output is logged to `build_run.log` in the project root.

### 12.3 Manual PyInstaller command (equivalent)

If you want to call PyInstaller directly (e.g. for CI or customisation), this is the same command `build_exe.py` runs on Windows:

```powershell
cd C:\Unillog\ThaoResearch

pyinstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --name ThaoResearch `
  --paths "C:\Unillog\ThaoResearch" `
  --add-data "C:\Unillog\ThaoResearch\locales;locales" `
  --add-data "C:\Unillog\ThaoResearch\config.yaml;." `
  --add-data "C:\Unillog\ThaoResearch\.env;." `
  --add-data "C:\Unillog\ThaoResearch\assets;assets" `
  --add-data "C:\Unillog\ThaoResearch\license_generator.py;." `
  --add-data "C:\Unillog\ThaoResearch\license_checker.py;." `
  --hidden-import apscheduler.jobstores.memory `
  --hidden-import apscheduler.executors.pool `
  --hidden-import apscheduler.triggers.interval `
  --hidden-import apscheduler.triggers.cron `
  --hidden-import telegram.ext._application `
  --hidden-import tkinter --hidden-import tkinter.ttk `
  --hidden-import tkinter.scrolledtext --hidden-import tkinter.filedialog `
  --hidden-import tkinter.messagebox `
  --hidden-import pystray --hidden-import PIL --hidden-import PIL.Image `
  --hidden-import PIL.ImageDraw --hidden-import PIL.ImageFont `
  --hidden-import win11toast --hidden-import win10toast `
  --hidden-import plyer --hidden-import plyer.platforms.win `
  --hidden-import plyer.platforms.win.notification `
  --hidden-import win32api --hidden-import win32con `
  --hidden-import win32gui --hidden-import win32gui_struct `
  --collect-all apscheduler --collect-all pystray --collect-all tkhtmlview `
  --icon "C:\Unillog\ThaoResearch\assets\tray.ico" `
  "C:\Unillog\ThaoResearch\main.py"
```

The `;` separator in `--add-data` is **Windows-only** — on Linux/macOS use `:` instead.

### 12.4 Clean rebuild

If the .exe behaves oddly (stale modules, missing files), wipe the build artefacts and rebuild from scratch:

```powershell
cd C:\Unillog\ThaoResearch

# 1. Kill any running instance
Get-Process | Where-Object { $_.Path -like 'C:\Unillog\ThaoResearch\dist\*' } | Stop-Process -Force

# 2. Remove the build / dist folders + the generated .spec file
Remove-Item -Recurse -Force build -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force dist   -ErrorAction SilentlyContinue
Remove-Item -Force       ThaoResearch.spec -ErrorAction SilentlyContinue

# 3. (Optional) also wipe the in-app license so the new build starts clean
Remove-Item -Force data\license.dat -ErrorAction SilentlyContinue

# 4. Rebuild
python build_exe.py
```

### 12.5 Where files live after build

```text
C:\Unillog\ThaoResearch\
├── dist\
│   └── ThaoResearch.exe          <-- single-file build (~35 MB)
├── build\                        <-- intermediate PyInstaller cache (safe to delete)
├── ThaoResearch.spec             <-- generated spec file (gitignore if you don't commit builds)
└── build_run.log                 <-- PyInstaller stdout/stderr from the last build
```

### 12.6 Running the built .exe

```powershell
# Normal launch (uses the config.yaml next to the .exe or up to 3 levels up)
C:\Unillog\ThaoResearch\dist\ThaoResearch.exe

# One-time license check (useful for debugging)
$env:THRESEARCH_LICENSE = '8280e5b347cda80cfccb363e33e56693'
C:\Unillog\ThaoResearch\dist\ThaoResearch.exe --license-info

# Show current license info without changing anything
C:\Unillog\ThaoResearch\dist\ThaoResearch.exe --license-info
```

The .exe searches for `config.yaml` in this order:
1. Absolute path supplied via `--config`.
2. Current working directory.
3. Folder next to `ThaoResearch.exe`.
4. Up to 3 levels above the .exe (so it finds `config.yaml` in the project root when the .exe is in `dist\`).
5. PyInstaller's `_MEIPASS` (the temp extraction folder).
6. `%USERPROFILE%\ThaoResearch\config.yaml`.

### 12.7 Verifying the build

After a successful build, sanity-check that the .exe can activate a license:

```powershell
cd C:\Unillog\ThaoResearch
Remove-Item data\license.dat -ErrorAction SilentlyContinue

$env:THRESEARCH_LICENSE = '8280e5b347cda80cfccb363e33e56693'
.\dist\ThaoResearch.exe --license-info
```

Expected output:

```text
[INFO] thaoresearch.utils.license: License saved (valid to 2026-12-01)
License info
────────────────────────────────────────
  Status:     Activated
  License key: 8280e5b347cda80cfccb363e33e56693
  Valid from: 2026-08-09
  Valid to:   2026-12-01
```

### 12.8 Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `--license-info` says "Could not import license_checker" | `license_checker.py` / `license_generator.py` not bundled | Confirm both files exist at the project root and rerun `python build_exe.py`. They're listed in the spec under `datas`. |
| Tray icon missing / generic icon | `assets\tray.ico` not bundled or `--icon` flag missing | Make sure `assets\tray.ico` exists and `build_exe.py` is the latest version (it auto-detects the .ico on Windows). |
| `tkinter.TclError` on launch | Tkinter not installed in the active Python | Reinstall Python with the official installer and tick "tcl/tk and IDLE" — or `pip install tk` on some distros. |
| `--windowed` mode hides errors | Console output is suppressed | Drop `--windowed` temporarily, or read `dist\logs\app.log` after launch. |
| `License is not activated or invalid` after entering a valid key | Stale `.dat` file from a previous build | Delete `data\license.dat` and re-enter the key in the Settings dialog. The new build also logs the exact key bytes to `dist\logs\app.log` so you can confirm what the dialog sent. |
| `UnicodeDecodeError` on launch | `locale` folder not bundled | Confirm `locales\en.json` (and any other locale files you want) exist; they're included via `--add-data`. |


Conclusion
This document serves as the complete blueprint for developing ThaoResearch. The AI Agent is expected to deliver a fully functional, modular, and well-documented Python application that meets all the specified criteria for market news analysis and alerting.

---

## 13. Build commands (cheat sheet)

Two one-shot commands to produce a release build. Run them from the project root (`C:\Unillog\ThaoResearch`) in **PowerShell**.

### 13.1 Build the standalone EXE only

Produces `dist\ThaoResearch.exe` (single-file PyInstaller bundle, ~36 MB). Skips the Inno Setup installer so the build finishes in 1-3 minutes.

```powershell
taskkill /F /IM ThaoResearch.exe 2>$null; Remove-Item -Recurse -Force build, dist, installer_output -ErrorAction SilentlyContinue; py -m PyInstaller --noconfirm --clean ThaoResearch.spec
```

Expected tail:

```text
Appending archive to EXE C:\Unillog\ThaoResearch\dist\ThaoResearch.exe
Building EXE from EXE-00.toc completed successfully.
```

Result: `C:\Unillog\ThaoResearch\dist\ThaoResearch.exe`

### 13.2 Build the EXE + Windows installer

Requires [Inno Setup 6](https://jrsoftware.org/isdl.php) installed (the script auto-detects it at `C:\Program Files\Inno Setup 6\ISCC.exe` or `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`). Produces both the EXE and the customer-facing installer.

```powershell
cmd.exe /c build_release.bat
```

Expected tail:

```text
 Successful compile (3.8xx sec). Resulting Setup program filename is:
 C:\Unillog\ThaoResearch\installer_output\ThaoResearch-Setup-1.0.0.exe

 ============================================================
  Release build complete.
 ============================================================

  Artifacts:
    dist\ThaoResearch.exe
    installer_output\ThaoResearch-Setup-1.0.0.exe
```

Result: `C:\Unillog\ThaoResearch\installer_output\ThaoResearch-Setup-1.0.0.exe` (~37 MB) — hand this file to the customer.

> **Tip:** to build the installer without also running Inno Setup, pass `/skip-installer`:
> ```powershell
> cmd.exe /c build_release.bat /skip-installer
> ```


Note:

Build EXE (chỉ PyInstaller, ~36 MB, 1-3 phút):
taskkill /F /IM ThaoResearch.exe 2>$null; Remove-Item -Recurse -Force build, dist, installer_output -ErrorAction SilentlyContinue; py -m PyInstaller --noconfirm --clean ThaoResearch.spec

Build EXE + Installer (~37 MB, ~50 giây sau khi EXE xong):
cmd.exe /c build_release.bat


--- END OF README.md ---

PS C:\Unillog\ThaoResearch> python license_generator.py 20261029
f1c92532eef6291e
PS C:\Unillog\ThaoResearch> python license_generator.py 20261029
3dc22ba3099cc11363bec5f380ec3baa
PS C:\Unillog\ThaoResearch> python license_generator.py 20261029
3dc22ba3099cc11363bec5f380ec3baa
PS C:\Unillog\ThaoResearch> python license_generator.py 20261201
8280e5b347cda80cfccb363e33e56693
PS C:\Unillog\ThaoResearch> python license_generator.py 20260101
3893c0b963ade8ff502b8270969a7d8d
PS C:\Unillog\ThaoResearch> python license_generator.py 20261201
8280e5b347cda80cfccb363e33e56693