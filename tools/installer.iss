#ifndef MyAppId
#define MyAppId "{{E57EC96A-654B-4E89-A28B-9B11E1A2A25E}"
#endif

#ifndef MyAppProduct
#define MyAppProduct "TexturePipelineWorkbench"
#endif

#ifndef MyAppName
#define MyAppName "Texture Pipeline Workbench"
#endif

#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

#ifndef MyAppExeName
#define MyAppExeName "TexturePipelineWorkbench.exe"
#endif

#ifndef MyAppPublisher
#define MyAppPublisher "Texture Pipeline Workbench"
#endif

#ifndef MyAppBuildDir
#define MyAppBuildDir "D:\_BUILD\TexturePipelineWorkbench"
#endif

#ifndef MyInstallerOutputDir
#define MyInstallerOutputDir "D:\_BUILD\TexturePipelineWorkbench\installer"
#endif

#ifndef MySetupIconFile
#define MySetupIconFile "D:\Python\Tiff To PNG\ico\favicon.ico"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir={#MyInstallerOutputDir}
OutputBaseFilename={#MyAppProduct}_{#MyAppVersion}_setup
SetupIconFile={#MySetupIconFile}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyAppBuildDir}\*"; DestDir: "{app}"; Excludes: "installer\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
