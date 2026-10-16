; Lazarus Windows installer (stage 19, updates 19b). Built by build.ps1 -Setup / -Release with
; Inno Setup 6 (ISCC):
;   ISCC /DAppVersion=<lazarus.__version__> /DDistDir=<dist\Lazarus> /O<output folder> installer\lazarus.iss
; The PyInstaller onedir build (dist\Lazarus) goes to Program Files (64-bit). Inno Setup is a
; build tool only: nothing of it ships, and Lazarus never needs it.
;
; Per-user parts stay per-user: the default-PDF-reader registration runs as the ORIGINAL
; (unelevated) user who started Setup, through Lazarus's own assoc.py (Lazarus.exe
; --register-default), so it lands in that user's HKCU, never the admin context. Windows then
; asks the user to confirm the default in Settings, as Settings > Default PDF reader does.
; Uninstall reverses the registration (only when it points at this install) and keeps every
; piece of user data: settings, saved signatures, recent files, output folders.
;
; Updates (19b): Lazarus's updater starts this Setup with /SILENT /SP- /SUPPRESSMSGBOXES
; /NORESTART /RELAUNCH=1. The same AppId upgrades the installed copy in place (its folder, tasks,
; shortcuts and Apps entry); nothing of the user's is touched; the default-reader step is not
; repeated on an upgrade (the registration already points here); /RELAUNCH=1 starts Lazarus again
; afterwards as the original, unelevated user.
;
; /DTestBuild=1 makes a TEST installer with its own identity (AppId, name, folder, shortcut
; names), so testing never touches a real installation's Apps entry or shortcuts.

#ifndef AppVersion
  #error AppVersion must be given: ISCC /DAppVersion=x.y.z
#endif
#ifndef DistDir
  #define DistDir "..\dist\Lazarus"
#endif
#ifdef TestBuild
  #define AppGuid "{B3D0F6A1-7C42-4E19-8D5B-6A2E9C1F4D70}"
  #define ProductName "Lazarus Test"
  #define SetupBase "Lazarus-Test-Setup-"
#else
  #define AppGuid "{6E1B7A2C-5D3F-4C8E-9A41-2F7D0B9C3E58}"
  #define ProductName "Lazarus"
  #define SetupBase "Lazarus-Setup-"
#endif

[Setup]
AppId={{#AppGuid}
AppName={#ProductName}
AppVersion={#AppVersion}
AppVerName={#ProductName} {#AppVersion}
AppPublisher=DarkRelay
AppPublisherURL=https://github.com/Crazy8697/Lazarus
AppSupportURL=https://github.com/Crazy8697/Lazarus/issues
AppUpdatesURL=https://github.com/Crazy8697/Lazarus/releases
AppComments=Local PDF reader and editor
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#ProductName} Setup
DefaultDirName={autopf}\{#ProductName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
SetupIconFile=..\lazarus\assets\lazarus.ico
UninstallDisplayIcon={app}\Lazarus.exe
UninstallDisplayName={#ProductName}
OutputBaseFilename={#SetupBase}{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "defaultreader"; Description: "Set Lazarus as my default PDF reader (Windows asks you to confirm in Settings)"; GroupDescription: "PDF files:"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#ProductName}"; Filename: "{app}\Lazarus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Lazarus.exe"; Comment: "Lazarus - local PDF reader and editor"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\Lazarus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Lazarus.exe"; Comment: "Lazarus - local PDF reader and editor"; Tasks: desktopicon

[Run]
; the default-reader choice, for the user who ran Setup (unelevated: their own HKCU); not repeated on an upgrade
Filename: "{app}\Lazarus.exe"; Parameters: "--register-default"; StatusMsg: "Registering Lazarus as a PDF reader for your account..."; Tasks: defaultreader; Check: not WasInstalled; Flags: runasoriginaluser waituntilterminated
; offered on the last page; postinstall entries run as the original (unelevated) user
Filename: "{app}\Lazarus.exe"; Description: "Launch Lazarus"; Flags: postinstall nowait skipifsilent
; an update from inside Lazarus (/RELAUNCH=1): start it again when done, unelevated
Filename: "{app}\Lazarus.exe"; Check: ShouldRelaunch; Flags: nowait runasoriginaluser

[UninstallRun]
; reverse the .pdf registration, exactly as Settings > Remove, and only if it points at this install
Filename: "{app}\Lazarus.exe"; Parameters: "--unregister-default"; RunOnceId: "UnregisterPdf"; Flags: runhidden waituntilterminated

[Code]
var
  Installed: Boolean;

function InitializeSetup(): Boolean;
var
  Key: String;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1';
  Installed := RegKeyExists(HKLM64, Key) or RegKeyExists(HKLM32, Key);
  Result := True;
end;

function WasInstalled(): Boolean;
begin
  Result := Installed;
end;

function ShouldRelaunch(): Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;
