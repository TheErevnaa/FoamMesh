#define AppName "FoamMesh"
#define AppVersion "1.0.0"
#define AppPublisher "Erevnaa"
#define AppExeName "FoamMesh.exe"

[Setup]
AppId={{95AE446A-08C9-48DB-B4F1-9FF5B170920F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\FoamMesh
DefaultGroupName=FoamMesh
OutputBaseFilename=FoamMesh-{#AppVersion}-Setup
SetupIconFile=..\..\src\resources\branding\foammesh.ico
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile=..\..\LICENSE
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\..\dist\FoamMesh\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\FoamMesh"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\FoamMesh"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; WorkingDir: "{app}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch FoamMesh"; Flags: nowait postinstall skipifsilent
