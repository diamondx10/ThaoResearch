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
| After install, `.env` is missing | No `.env` existed at build time | Create `.env` at project root, rerun `build_release.bat`. |
| After install, app can't find `data/` | Data dir was deleted by antivirus / user | Open Settings → "Open data folder" to recreate it, or reinstall. |

## Why we use a portable layout (not `%APPDATA%`)

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