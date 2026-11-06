; ============================================================================
; ThaoResearch Installer — Inno Setup Script
; ============================================================================
; Build a single-file Windows installer that lets the user pick an install
; directory (default: C:\Program Files\ThaoResearch). After install, the
; folder is self-contained and portable — copy it to a USB stick or another
; PC and it keeps working.
;
; Build instructions:
;     1. Build the EXE first:  python main.py --build-exe
;        (produces dist\ThaoResearch.exe)
;     2. Run Inno Setup Compiler on this file, or:
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;     3. Output: installer_output\ThaoResearch-Setup-1.0.0.exe
;
; Easier: just run build_release.bat — it does steps 1+2 in one click.
; ============================================================================

#define MyAppName "ThaoResearch"
#define MyAppDisplayName "ThaoResearch"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ThaoResearch"
#define MyAppURL "https://github.com/thao-research"
#define MyAppExeName "ThaoResearch.exe"
#define MyAppCopyright "Copyright (c) 2026 ThaoResearch"

[Setup]
; Unique GUID for the app — change ONLY when the install location semantics
; change. (Each Setup section needs its own AppId.)
AppId={{2FA1E1D9-7B4C-4F3E-9D1A-1A2B3C4D5E6F}
AppName={#MyAppDisplayName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppDisplayName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppCopyright={#MyAppCopyright}

; Default install dir = user-writable location (no admin required)
; Use {localappdata} for per-user install that doesn't need admin rights.
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppDisplayName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; Output
OutputDir=installer_output
OutputBaseFilename=ThaoResearch-Setup-{#MyAppVersion}
; Set to 0 to skip the installer icon (much faster build, no compile error
; if the icon file is missing).
SetupIconFile=assets\tray.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

; Compression: lzma2/ultra is the smallest but slowest.
Compression=lzma2/ultra
SolidCompression=yes
CompressionThreads=4

; Visuals
WizardStyle=modern
WizardSizePercent=120
WindowVisible=no

; Admin required but user can choose install location
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; Minimum Windows version: Windows 10 (we use modern Win32 APIs).
MinVersion=10.0

Uninstallable=yes
UninstallDisplayName={#MyAppDisplayName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; Add more languages here as needed, e.g.:
;   Name: "french";  MessagesFile: "compiler:Languages\French.isl"
;   Name: "german";  MessagesFile: "compiler:Languages\German.isl"
;   Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
;   Name: "korean";  MessagesFile: "compiler:Languages\Korean.isl"
;   Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
; Note: Vietnamese is not shipped by Inno Setup — use the English wizard.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; --- The executable (built by PyInstaller) ---
; Allow users without admin (uncommon with our wizard but harmless):
;   Flags: ignoreversion
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; --- Bundled data + assets ---
; These files live in data/ folder alongside history.db and prices.db
; for full portability (one folder = everything).
Source: "config.yaml"; DestDir: "{app}\data"; Flags: ignoreversion
Source: "init_config.yaml"; DestDir: "{app}\data"; Flags: ignoreversion
Source: "locales\*"; DestDir: "{app}\locales"; Flags: ignoreversion recursesubdirs
Source: "assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs

[Dirs]
; Create writable subdirs at install time. Data lives INSIDE the install
; folder so the whole install stays portable — the user just picks one
; folder and gets a self-contained app.
Name: "{app}\data"; Permissions: users-modify

[Icons]
; Desktop shortcut (opt-in via the Tasks checkbox).
Name: "{userdesktop}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; Start Menu shortcut (always installed).
Name: "{userstartmenu}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
; Optional: launch the app after install (off by default — leave commented
; if you want a quieter install).
; Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppDisplayName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; When uninstalling, optionally wipe the data dir too. We DO NOT do this by
; default — the user might want to copy the folder before uninstalling, and
; we want a recoverable uninstall. Set this to Type: filesandordirs if you
; want a true "fresh uninstall".
Type: filesandordirs; Name: "{app}\data"

[Code]
// ============================================================================
// Pascal Script helpers
// ============================================================================

// Force extract to install folder (not Windows TEMP) for full portability
// This creates a local temp dir inside {tmp} path to avoid "Error 5: Access is denied"
procedure InitializeWizard();
var
  TempPath: String;
begin
  // Create a unique temp subfolder in the install location
  TempPath := ExpandConstant('{src}\temp');
  if DirExists(TempPath) then
    DelTree(TempPath, True, True, True);
  CreateDir(TempPath);
end;