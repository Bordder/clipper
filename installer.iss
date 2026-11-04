; Inno Setup script. Build the exe first (see build.ps1), then: iscc installer.iss

#define AppName "Bordders Clipper"
#define AppVersion "1.1.0"
#define AppExe "Clipper.exe"

[Setup]
AppId={{6C1E2B4D-8A47-4E3B-9C5A-1F0D2E7B3A91}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Bordder
AppPublisherURL=https://github.com/Bordder/clipper
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=release
OutputBaseFilename=BorddersClipper-Setup-{#AppVersion}
SetupIconFile=clipper.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=Local\ClipperSingleInstance
CloseApplications=yes
LicenseFile=LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\Clipper\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "Clipper.InstantReplay"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "Clipper.InstantReplay"; Tasks: desktopicon

[Registry]
; the app adds itself to startup; make sure uninstall takes it out again
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Clipper"; ValueType: none; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; temp files only, settings are kept
Type: filesandordirs; Name: "{localappdata}\Clipper\buffer"
Type: filesandordirs; Name: "{localappdata}\Clipper\tmp"
Type: filesandordirs; Name: "{localappdata}\Clipper\thumbs"
Type: filesandordirs; Name: "{localappdata}\Clipper\updates"
