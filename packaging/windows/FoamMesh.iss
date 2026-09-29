#define AppName "FoamMesh"
#define AppVersion "1.1.0"
#define AppPublisher "Erevnaa"
#define AppExeName "FoamMesh.exe"

; Plan 35 CR10. The Microsoft Visual C++ 2015-2022 x64 redistributable is
; bundled, never downloaded: the installer must work offline and install the
; exact file that was tested. Put Microsoft's vc_redist.x64.exe (from
; https://aka.ms/vs/17/release/vc_redist.x64.exe, on the build machine) at
; packaging\windows\redist\vc_redist.x64.exe before compiling. It is not
; committed (see redist\.gitignore); the compile stops here if it is missing.
#define VCRedistSource AddBackslash(SourcePath) + "redist\vc_redist.x64.exe"
#if !FileExists(VCRedistSource)
  #error packaging\windows\redist\vc_redist.x64.exe is missing. Place the Microsoft Visual C++ 2015-2022 x64 redistributable there before compiling (Plan 35 CR10); the installer bundles it, it never downloads it.
#endif

; WER LocalDumps (Plan 35 CR0 step 5). Machine-wide by design, which is why
; this installer runs elevated. The key names FoamMesh.exe only, so a source
; build (python.exe) is never affected.
#define LocalDumpsKey "SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\" + AppExeName

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
; HKLM LocalDumps and the VC++ runtime both need an elevated install.
PrivilegesRequired=admin
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\..\dist\FoamMesh\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#VCRedistSource}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: VCRuntimeNeedsInstall

[Dirs]
; The folder the "FoamMesh logs" shortcut opens, for the installing user; the
; app creates it for any other user on first start. Kept on uninstall: it
; holds the user's logs, not the program.
Name: "{%USERPROFILE}\.FoamMesh\logs"; Flags: uninsneveruninstall

[Registry]
Root: HKLM64; Subkey: "{#LocalDumpsKey}"; Flags: uninsdeletekey
Root: HKLM64; Subkey: "{#LocalDumpsKey}"; ValueType: expandsz; ValueName: "DumpFolder"; ValueData: "%LOCALAPPDATA%\FoamMesh\CrashDumps"; Flags: uninsdeletekey
Root: HKLM64; Subkey: "{#LocalDumpsKey}"; ValueType: dword; ValueName: "DumpType"; ValueData: "1"; Flags: uninsdeletekey
Root: HKLM64; Subkey: "{#LocalDumpsKey}"; ValueType: dword; ValueName: "DumpCount"; ValueData: "5"; Flags: uninsdeletekey

[Icons]
Name: "{autoprograms}\FoamMesh"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
; Plan 35 CR8/CR10: the fast render preset, software GL for Qt's widgets.
Name: "{autoprograms}\FoamMesh (safe mode)"; Filename: "{app}\{#AppExeName}"; Parameters: "--safe-mode"; WorkingDir: "{app}"; Comment: "Start FoamMesh with the safest graphics settings"
; shell:Profile resolves per user when the shortcut is opened, so the one
; all-users entry opens each user's own log folder.
Name: "{autoprograms}\FoamMesh logs"; Filename: "{win}\explorer.exe"; Parameters: """shell:Profile\.FoamMesh\logs"""; Comment: "Open the FoamMesh log folder (%USERPROFILE%\.FoamMesh\logs)"
Name: "{autodesktop}\FoamMesh"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; WorkingDir: "{app}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
; Runs before the optional launch below, so a first start has the runtime.
; /install /quiet /norestart: 0 = done, 1638 = a newer one is present,
; 3010 = done but a reboot is due; none of them should fail FoamMesh's setup.
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing the Microsoft Visual C++ 2015-2022 runtime (x64)..."; Flags: waituntilterminated; Check: VCRuntimeNeedsInstall
Filename: "{app}\{#AppExeName}"; Description: "Launch FoamMesh"; Flags: nowait postinstall skipifsilent

[Code]
const
  VCRuntimeKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  { 14.40 (VS 2022 17.10) changed std::mutex; code built with it can crash on
    an older runtime, and PySide6 6.9 ships DLLs built with 14.44. }
  VCRuntimeMinMajor = 14;
  VCRuntimeMinMinor = 40;

function VCRuntimeNeedsInstall: Boolean;
var
  Installed, Major, Minor: Cardinal;
begin
  Result := True;
  if not RegQueryDWordValue(HKLM64, VCRuntimeKey, 'Installed', Installed) then
    Exit;
  if Installed <> 1 then
    Exit;
  if not RegQueryDWordValue(HKLM64, VCRuntimeKey, 'Major', Major) then
    Exit;
  if not RegQueryDWordValue(HKLM64, VCRuntimeKey, 'Minor', Minor) then
    Exit;
  if Major > VCRuntimeMinMajor then
    Result := False
  else if (Major = VCRuntimeMinMajor) and (Minor >= VCRuntimeMinMinor) then
    Result := False;
end;
