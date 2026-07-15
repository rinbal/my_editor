; Inno Setup script for MyEditor (Windows installer wizard).
;
; Build (after `pyinstaller packaging\my_editor.spec` produced dist\my-editor\):
;     iscc /DMyAppVersion=1.0.0 packaging\windows\installer.iss
; CI passes the version extracted from constants.py. If omitted, the default
; below is used so the script still compiles standalone.
;
; Produces: dist\my-editor-<version>-windows-setup.exe
; Per-user install (no admin / no UAC prompt): installs under the user's
; Local AppData, adds a Start Menu shortcut and an uninstaller.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "MyEditor"
#define MyAppExeName "my-editor.exe"
#define MyAppPublisher "rinbal"
#define MyAppURL "https://github.com/rinbal/my_editor"

[Setup]
; Stable AppId so upgrades replace the prior version and the uninstaller is tracked.
AppId={{A7F3C2E1-9B4D-4E6A-8C1F-2D5B7E9A0C34}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}

; Per-user install: no administrator rights, no UAC elevation prompt.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\my-editor
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; Fewer clicks for non-technical users: the install location is fixed (per-user),
; so skip the folder-selection and the pre-install summary pages. The Welcome,
; task checkboxes, progress and finish pages remain.
DisableDirPage=yes
DisableReadyPage=yes

; Only Windows 10 and later are supported (the PySide6 / Python 3.12 runtime),
; so fail with a clear message on older Windows instead of crashing at launch.
MinVersion=10.0

; On an upgrade, offer to close a running MyEditor, then relaunch it afterwards,
; instead of failing with a "file in use" error.
CloseApplications=yes
RestartApplications=yes

; Notify the shell so file-type icons refresh when the associations below are on.
ChangesAssociations=yes

; Installer appearance and output.
WizardStyle=modern
SetupIconFile=..\icons\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=my-editor-{#MyAppVersion}-windows-setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Desktop shortcut on by default: the target audience relies on it to relaunch.
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"
; File association stays opt-in so the installer never quietly hijacks .md / .txt.
Name: "associatefiles"; Description: "Open .md and .txt files with {#MyAppName}"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
; The whole PyInstaller onedir folder.
Source: "..\..\dist\my-editor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Optional per-user (HKCU, no admin needed) file association for .md and .txt.
Root: HKCU; Subkey: "Software\Classes\myeditor.textfile"; ValueType: string; ValueName: ""; ValueData: "Text Document"; Tasks: associatefiles; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\myeditor.textfile\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: associatefiles
Root: HKCU; Subkey: "Software\Classes\myeditor.textfile\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: associatefiles
Root: HKCU; Subkey: "Software\Classes\.md\OpenWithProgids"; ValueType: string; ValueName: "myeditor.textfile"; ValueData: ""; Tasks: associatefiles; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.txt\OpenWithProgids"; ValueType: string; ValueName: "myeditor.textfile"; ValueData: ""; Tasks: associatefiles; Flags: uninsdeletevalue

[Run]
; No skipifsilent: the in-app updater installs with /SILENT and relies on this
; entry to relaunch MyEditor once the update finishes. Interactive installs still
; show this as the "Launch MyEditor" checkbox on the final page.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall
