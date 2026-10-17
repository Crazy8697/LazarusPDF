; Lazarus PDF Windows installer (stage 19, updates 19b, the rename 22). Built by build.ps1 -Setup /
; -Release with Inno Setup 6 (ISCC):
;   ISCC /DAppVersion=<lazaruspdf.__version__> /DDistDir=<dist\LazarusPDF> /O<output folder> installer\lazaruspdf.iss
; The PyInstaller onedir build (dist\LazarusPDF) goes to Program Files (64-bit). Inno Setup is a
; build tool only: nothing of it ships, and Lazarus PDF never needs it.
;
; Per-user parts stay per-user: the default-PDF-reader registration runs as the ORIGINAL
; (unelevated) user who started Setup, through Lazarus PDF's own assoc.py (LazarusPDF.exe
; --register-default), so it lands in that user's HKCU, never the admin context. Windows then
; asks the user to confirm the default in Settings, as Settings > Default PDF reader does.
; Uninstall reverses the registration (only when it points at this install) and keeps every
; piece of user data: settings, saved signatures, recent files, output folders.
;
; Updates (19b): the updater starts this Setup with /SILENT /SP- /SUPPRESSMSGBOXES /NORESTART
; /RELAUNCH=1. The same AppId upgrades the installed copy in place (tasks, shortcuts, Apps entry);
; nothing of the user's is touched; the default-reader step is not repeated on an upgrade;
; /RELAUNCH=1 starts the program again afterwards as the original, unelevated user.
;
; The rename (22): Lazarus 0.21.0 and older installed to {autopf}\Lazarus as Lazarus.exe with
; shortcuts named "Lazarus". The AppId is unchanged, so this Setup is an upgrade of it, not a
; second program: it installs to {autopf}\LazarusPDF (never the previous folder), then removes
; the old folder (only if it holds the old Lazarus.exe) and the old shortcuts; the Apps entry is
; the same AppId's, rewritten as "Lazarus PDF". As the original user it then runs
; LazarusPDF.exe --migrate: settings, saved signatures and the .pdf registration move over
; (migrate.py). If the old program is still running, Setup stops and says so.
;
; /DTestBuild=1 makes a TEST installer with its own identity (AppId, name, folder, shortcut
; names), so testing never touches a real installation's Apps entry or shortcuts.

#ifndef AppVersion
  #error AppVersion must be given: ISCC /DAppVersion=x.y.z
#endif
#ifndef DistDir
  #define DistDir "..\dist\LazarusPDF"
#endif
#define ExeName "LazarusPDF.exe"
#define OldExeName "Lazarus.exe"
#ifdef TestBuild
  #define AppGuid "{B3D0F6A1-7C42-4E19-8D5B-6A2E9C1F4D70}"
  #define ProductName "Lazarus PDF Test"
  #define DirName "LazarusPDF Test"
  #define OldProductName "Lazarus Test"
  #define SetupBase "LazarusPDF-Test-Setup-"
#else
  #define AppGuid "{6E1B7A2C-5D3F-4C8E-9A41-2F7D0B9C3E58}"
  #define ProductName "Lazarus PDF"
  #define DirName "LazarusPDF"
  #define OldProductName "Lazarus"
  #define SetupBase "LazarusPDF-Setup-"
#endif

[Setup]
AppId={{#AppGuid}
AppName={#ProductName}
AppVersion={#AppVersion}
AppVerName={#ProductName} {#AppVersion}
AppPublisher=DarkRelay
AppPublisherURL=https://github.com/Crazy8697/LazarusPDF
AppSupportURL=https://github.com/Crazy8697/LazarusPDF/issues
AppUpdatesURL=https://github.com/Crazy8697/LazarusPDF/releases
AppComments=Local PDF reader and editor
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#ProductName} Setup
VersionInfoProductName={#ProductName}
DefaultDirName={autopf}\{#DirName}
UsePreviousAppDir=no
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
SetupIconFile=..\lazaruspdf\assets\lazaruspdf.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#ProductName}
OutputBaseFilename={#SetupBase}{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "defaultreader"; Description: "Set Lazarus PDF as my default PDF reader (Windows asks you to confirm in Settings)"; GroupDescription: "PDF files:"; Flags: unchecked

[InstallDelete]
; the shortcuts Lazarus 0.21.0 and older made (stage 22)
Type: files; Name: "{autoprograms}\{#OldProductName}.lnk"
Type: files; Name: "{autodesktop}\{#OldProductName}.lnk"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#ProductName}"; Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#ExeName}"; Comment: "Lazarus PDF - local PDF reader and editor"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#ExeName}"; Comment: "Lazarus PDF - local PDF reader and editor"; Tasks: desktopicon

[Run]
; an upgrade: move the user's settings, signatures and .pdf registration over (as the original user: their HKCU)
Filename: "{app}\{#ExeName}"; Parameters: "--migrate"; StatusMsg: "Moving your settings to Lazarus PDF..."; Check: WasInstalled; Flags: runasoriginaluser runhidden waituntilterminated
; the default-reader choice, for the user who ran Setup (unelevated: their own HKCU); not repeated on an upgrade
Filename: "{app}\{#ExeName}"; Parameters: "--register-default"; StatusMsg: "Registering Lazarus PDF as a PDF reader for your account..."; Tasks: defaultreader; Check: not WasInstalled; Flags: runasoriginaluser waituntilterminated
; offered on the last page; postinstall entries run as the original (unelevated) user
Filename: "{app}\{#ExeName}"; Description: "Launch Lazarus PDF"; Flags: postinstall nowait skipifsilent
; an update from inside the program (/RELAUNCH=1): start it again when done, unelevated
Filename: "{app}\{#ExeName}"; Check: ShouldRelaunch; Flags: nowait runasoriginaluser

[UninstallRun]
; reverse the .pdf registration, exactly as Settings > Remove, and only if it points at this install
Filename: "{app}\{#ExeName}"; Parameters: "--unregister-default"; RunOnceId: "UnregisterPdf"; Flags: runhidden waituntilterminated
; remove the icon fonts older versions copied into the user fonts folder (only QtAwesome's own, byte for byte)
Filename: "{app}\{#ExeName}"; Parameters: "--clean-fonts"; RunOnceId: "CleanIconFonts"; Flags: runhidden waituntilterminated

[Code]
var
  Installed: Boolean;
  OldDir: String;

function InitializeSetup(): Boolean;
var
  Key: String;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1';
  Installed := RegKeyExists(HKLM64, Key) or RegKeyExists(HKLM32, Key);
  OldDir := '';
  if not RegQueryStringValue(HKLM64, Key, 'Inno Setup: App Path', OldDir) then
    RegQueryStringValue(HKLM32, Key, 'Inno Setup: App Path', OldDir);
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

{ the old (0.21.0 and older) installation, if it lives somewhere other than the new folder }
function OldInstall(): String;
begin
  Result := '';
  if (OldDir <> '') and FileExists(AddBackslash(OldDir) + '{#OldExeName}') then
    Result := RemoveBackslash(OldDir);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Old, Probe: String;
begin
  Result := '';
  Old := OldInstall();
  if Old = '' then
    exit;
  { a running program keeps its exe locked: renaming it fails then }
  Probe := AddBackslash(Old) + '{#OldExeName}';
  if RenameFile(Probe, Probe + '.probe') then
    RenameFile(Probe + '.probe', Probe)
  else
    Result := '{#OldProductName} is still running. Close it, then run Setup again.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Old: String;
begin
  if CurStep = ssPostInstall then begin
    Old := OldInstall();
    if (Old <> '') and (CompareText(Old, RemoveBackslash(ExpandConstant('{app}'))) <> 0) then
      DelTree(Old, True, True, True);
  end;
end;
