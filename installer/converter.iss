; Windows installer for Converter (Inno Setup 6).
; Built by .github/workflows/build.yml after `flet build windows`:
;   iscc /DAppVersion=2.1.0 installer\converter.iss
; Input:  build\windows\  (Converter.exe, DLLs, data, bundled ffmpeg)
; Output: dist\Converter-<version>-windows-x64-setup.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8F4B1C2E-5D7A-4E3B-9C1F-2A6D8E0B7C35}
AppName=Converter
AppVersion={#AppVersion}
AppVerName=Converter {#AppVersion}
AppPublisher=alexkonstich-byte
AppPublisherURL=https://github.com/alexkonstich-byte/converter
AppSupportURL=https://github.com/alexkonstich-byte/converter/issues
AppUpdatesURL=https://github.com/alexkonstich-byte/converter/releases
DefaultDirName={autopf}\Converter
DefaultGroupName=Converter
DisableProgramGroupPage=yes
; Per-user install by default (no admin prompt); the user may choose all users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Converter-{#AppVersion}-windows-x64-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Converter.exe
UninstallDisplayName=Converter
LicenseFile=..\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\build\windows\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Converter"; Filename: "{app}\Converter.exe"
Name: "{autodesktop}\Converter"; Filename: "{app}\Converter.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Converter.exe"; Description: "{cm:LaunchProgram,Converter}"; Flags: nowait postinstall skipifsilent
