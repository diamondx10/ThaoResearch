# Building ThaoResearch Releases

This document explains how to ship ThaoResearch as a **portable installer**
to your customers.

## What you get

Two artifacts:

| File | What it is | When you give it |
|------|-----------|------------------|
| `dist\ThaoResearch.exe` | Single-file PyInstaller binary | (only if you skip the installer) |
| `installer_output\ThaoResearch-Setup-1.0.0.exe` | Windows installer with a wizard | Send this to customers |

After running the installer, the customer's folder looks like:

```
C:\Program Files\ThaoResearch\
├─ ThaoResearch.exe              <-- the actual program
├─ .env                          <-- Telegram bot credentials (portable)
├─ config.yaml
├─ init_config.yaml
├─ locales\                      <-- translations
├─ assets\                       <-- icons, fonts
└─ data\                         <-- auto-created on first run
   ├─ history.db
   ├─ prices.db
   └─ license.dat
```

The whole folder is **portable**: zip it, copy it to a USB stick, drop it on
another PC — it still works, no install needed.

## How to build

### Prerequisites

1. Python 3.10+ on `PATH`.
2. [Inno Setup 6](https://jrsoftware.org/isdl.php) — only required for the
   installer step. Skip it if you only need a standalone `.exe`.

### One-click build

```cmd
build_release.bat
```

This runs:

1. Cleans `build\` and `dist\`.
2. `py -m PyInstaller ThaoResearch.spec` → produces `dist\ThaoResearch.exe`.
3. `ISCC.exe installer.iss` → produces
   `installer_output\ThaoResearch-Setup-1.0.0.exe`.

### Skip the installer step

```cmd
build_release.bat /skip-installer
```

Use this when Inno Setup is not installed and you only need the bare `.exe`.

## The Inno Setup wizard

The installer asks the customer two things:

1. **Where to install the program?** (default: `C:\Program Files\ThaoResearch`)
2. **Where to store user data?** (default: `<install dir>\data`)

The user-data folder is forced to live **inside** the install folder — that
is what makes the install portable. If the customer picks a folder outside
(e.g. `Documents\ThaoResearch`), the wizard refuses with a clear error.

## Customization

### Change the default install location

Edit `installer.iss`:

```ini
DefaultDirName={autopf}\{#MyAppName}     ; → C:\Program Files\ThaoResearch
DefaultDirName={localappdata}\Programs\{#MyAppName}   ; per-user, no admin
DefaultDirName={userdocs}\{#MyAppName}   ; user-controlled
```

### Change the install icon

Replace `assets\tray.ico` with your `.ico` file (256x256 recommended).

### Always launch after install

Uncomment the `[Run]` section in `installer.iss`.

### Bundle a default license / `.env`

The installer only copies files that exist on your build machine:

- `config.yaml` — copied if it exists (skip with `Check: FileExists`).
- `.env` — copied if it exists.
- `data\*` — NOT copied (always fresh for the customer).

If you want to ship a default `.env` (e.g. for an internal release with
your shared bot token), just create `.env` in the project root before
running `build_release.bat`.

## Verification checklist before shipping

Run these in PowerShell from the project root:

```powershell
# 1. Compile + import every module
py _smoke_imports.py

# 2. Confirm the helper paths still resolve correctly
py _smoke_paths.py

# 3. Smoke-test a fake "from System32 cwd" launch
py _smoke_portable.py
```

Then build with `build_release.bat` and **run the installer on a clean
Windows VM** before sending it to a customer.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `ISCC.exe` not found | Inno Setup not installed | Install from <https://jrsoftware.org/isdl.php> or pass `/skip-installer`. |
| Customer can't pick install dir outside `Program Files` | UAC / permissions | The installer requests admin — that's intentional for `Program Files`. Choose a user folder instead. |
| Data folder wizard says "must be inside install folder" | You chose `Documents\ThaoResearch` etc. | Pick `<install dir>\data` (the default). |
| After install, `.env` is missing | No `.env` existed at build time | Create `.env` at project root, run `python scripts/embed_credentials.py`, rerun `build_release.bat`. |
| After install, app can't find `data/` | Data dir was deleted by antivirus / user | Open Settings → "Open data folder" to recreate it, or reinstall. |
| **Nothing happens on double-click (works on the build machine)** | The folder holding the exe is not writable, so the logger crashed before any window appeared | Fixed: the data folder is now write-probed and falls back to `%LOCALAPPDATA%\ThaoResearch`. If an old build does this, run `ThaoResearch.exe --show-paths` from a console, or start it with `--data-dir "%LOCALAPPDATA%\ThaoResearch"`. |
| Telegram alerts never arrive from the exe | Built without embedded credentials | `python scripts/embed_credentials.py`, then rebuild. `build_exe.py` warns when it has to build without them. |
| `ZoneInfoNotFoundError` on the first scheduler tick | `tzdata` missing — Windows has no system IANA database | It is pinned in `requirements.txt` and collected by `build_exe.py`. Reinstall requirements and rebuild. |
| GUI looks like plain grey Tk | `sv-ttk` theme data not bundled | `build_exe.py` passes `--collect-data sv_ttk`. Check it is installed before building. |

## Where the data folder lives

`APP_ROOT` (the exe's folder) and the **data folder** are two different
things, and conflating them is what used to make a build run on the
developer's machine and die on a customer's.

The data folder is resolved at startup, in this order, and each candidate is
**write-probed** (create the directory, write a file, read it back, delete it
— `os.access()` lies about Windows ACLs):

1. `--data-dir <path>` on the command line
2. `%THAORESEARCH_DATA_DIR%`
3. `datadir.txt` next to the exe — ships inside a portable folder
4. `%LOCALAPPDATA%\ThaoResearch\datadir.txt` — written by the GUI picker
5. `<exe folder>\data` — the true portable case (own folder, USB stick)
6. `%LOCALAPPDATA%\ThaoResearch\data` — used when the exe folder is read-only
7. `%TEMP%\ThaoResearch\data` — last resort, so the app can still start and
   explain itself

Config, both SQLite databases, `license.dat` and `logs/` all live there, so one
folder is still the whole backup. Users change it in **Settings → General →
Storage**, which offers to copy the existing files across and writes the
pointer file.

Check what a given build resolved to:

```powershell
ThaoResearch.exe --show-paths
```

Startup failures are no longer silent: a crash before the window exists writes
`<data-dir>\logs\startup-error.log` and shows a message box with the path
report, because a `--windowed` build has no console to print to.

## Why the data folder is not simply `%APPDATA%`


ThaoResearch is a single-user tool. Using `%APPDATA%` (the standard Windows
convention) means:

- The customer **cannot** copy the install folder to another PC and keep
  their history.
- Customer service has to navigate hidden folders to inspect data.

By keeping `data\` next to the EXE:

- Everything is visible and recoverable.
- The folder IS the backup — `zip it, send it, restore it`.
- Uninstaller keeps `data\` intact (we use `UninstallDelete` carefully — it
  wipes only if you mark `Type: filesandordirs`).