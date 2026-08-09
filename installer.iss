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

; Default install dir = C:\Program Files\ThaoResearch
; (User can change it in the wizard.)
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppDisplayName}
DisableProgramGroupPage=yes

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
; These files MUST sit next to the EXE so the portable install works.
Source: "config.yaml"; DestDir: "{app}"; Flags: ignoreversion; Check: FileExists('config.yaml')
Source: "init_config.yaml"; DestDir: "{app}"; Flags: ignoreversion; Check: FileExists('init_config.yaml')
Source: ".env"; DestDir: "{app}"; Flags: ignoreversion; Check: FileExists('.env')
Source: "locales\*"; DestDir: "{app}\locales"; Flags: ignoreversion recursesubdirs
Source: "assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs

; NOTE: data\*.db and data\license.dat are NOT shipped by the installer —
; they are created on first run. If you want a default data dir, uncomment
; below. Leave commented for a clean first-run experience.
; Source: "data\*"; DestDir: "{app}\data"; Flags: ignoreversion recursesubdirs; Check: DirExists('data')

[Dirs]
; Create writable subdirs at install time. {code:GetDataDir} resolves to
; the install dir so everything stays portable.
Name: "{code:GetDataDir}"; Permissions: users-modify

[Icons]
; Desktop shortcut (opt-in via the Tasks checkbox).
Name: "{autodesktop}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; Start Menu shortcut (always installed).
Name: "{group}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
; Optional: launch the app after install (off by default — leave commented
; if you want a quieter install).
; Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppDisplayName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; When uninstalling, optionally wipe the data dir too. We DO NOT do this by
; default — the user might want to copy the folder before uninstalling, and
; we want a recoverable uninstall. Set this to Type: filesandordirs if you
; want a true "fresh uninstall".
Type: filesandordirs; Name: "{code:GetDataDir}"

[Code]
// ============================================================================
// Pascal Script helpers
// ============================================================================
var
  DataDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  // Add a wizard page asking where user data should live. We default to a
  // folder INSIDE the install dir so the whole install stays portable.
  DataDirPage := CreateInputDirPage(wpSelectDir,
    'User Data Location',
    'Where should ThaoResearch store its database and logs?',
    'ThaoResearch stores history.db, prices.db, license.dat and log files '
    + 'in the location you pick. Leave it inside the install folder (the '
    + 'default) so the whole app stays portable — you can copy or move it.',
    False, '');
  DataDirPage.Add('User data folder:');
  // Pre-fill with the install dir + '\data' once the user picks one.
end;

function GetDataDir(Param: String): String;
begin
  if DataDirPage = nil then
    Result := ExpandConstant('{app}\data')
  else if DataDirPage.Values[0] = '' then
    Result := ExpandConstant('{app}\data')
  else
    Result := DataDirPage.Values[0];
end;

// Default the data dir field to "<install-dir>\data" whenever the user
// changes the install dir on wpSelectDir.
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectDir then
  begin
    // No-op here — the user just picked the install dir on this page.
  end;
  if (CurPageID = DataDirPage.ID) and (DataDirPage.Values[0] = '') then
    DataDirPage.Values[0] := ExpandConstant('{app}\data');
end;

// Block obvious mistakes: the data dir MUST live inside the install dir
// (we want a portable install — no AppData, no Documents).
function NextButtonClick(CurPageID: Integer): Boolean;
var
  InstallDir, DataDir: String;
begin
  Result := True;
  if CurPageID = DataDirPage.ID then
  begin
    DataDir := DataDirPage.Values[0];
    InstallDir := ExpandConstant('{app}');
    if Pos(Lowercase(InstallDir), Lowercase(DataDir)) <> 1 then
    begin
      MsgBox('For a portable install, the user data folder must live INSIDE '
        + 'the install folder:' + #13#10 + #13#10
        + 'Install: ' + InstallDir + #13#10
        + 'Data:    ' + DataDir + #13#10 + #13#10
        + 'Please pick a folder inside the install directory.',
        mbError, MB_OK);
      Result := False;
    end;
  end;
end;