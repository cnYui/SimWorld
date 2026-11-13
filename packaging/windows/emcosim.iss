; Inno Setup script for the emcosim Windows installer.
; Build after PyInstaller:  iscc /DAppVersion=0.3.0 packaging\windows\emcosim.iss
#ifndef AppVersion
  #define AppVersion "0.3.0"
#endif
#define SrcDir "..\..\dist\emcosim"

[Setup]
AppId={{87391C45-4C13-45C9-BFC0-D49BC4DB5335}
AppName=emcosim
AppVersion={#AppVersion}
AppPublisher=emcosim authors
AppPublisherURL=https://github.com/cnYui/switch-robot-diy
DefaultDirName={autopf}\emcosim
DefaultGroupName=emcosim
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=emcosim-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
LicenseFile={#SrcDir}\licenses\emcosim-LICENSE.txt
UninstallDisplayIcon={app}\emcosim-app.exe

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "addtopath"; Description: "把 emcosim 命令行加入 PATH（给 Claude Code 等工具用）"; Flags: unchecked

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist

[Icons]
Name: "{group}\emcosim"; Filename: "{app}\emcosim-app.exe"
Name: "{group}\卸载 emcosim"; Filename: "{uninstallexe}"
Name: "{autodesktop}\emcosim"; Filename: "{app}\emcosim-app.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
  Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "安装 WebView2 运行时…"; \
  Check: NeedsWebView2; Flags: skipifdoesntexist
Filename: "{app}\emcosim-app.exe"; Description: "{cm:LaunchProgram,emcosim}"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsWebView2: Boolean;
var v: String;
begin
  Result := not (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v)
    or RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v));
end;

function NeedsAddPath(Param: string): boolean;
var OrigPath: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OrigPath) then begin
    Result := True; exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
