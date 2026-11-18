[Setup]
AppName=AI Shorts Studio Pro
AppVersion=2.4.0
AppPublisher=Devexibit
DefaultDirName={autopf}\AI Shorts Studio Pro
DefaultGroupName=AI Shorts Studio Pro
SetupIconFile=app.ico
OutputDir=installer_output
OutputBaseFilename=AI_Shorts_Studio_Pro_v2.4_Setup
Compression=lzma2/normal
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "C:\Build_AISP_v2.4\dist\AI Shorts Studio Pro v2\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AI Shorts Studio Pro"; Filename: "{app}\AI Shorts Studio Pro v2.exe"
Name: "{autodesktop}\AI Shorts Studio Pro"; Filename: "{app}\AI Shorts Studio Pro v2.exe"

[Run]
Filename: "{app}\AI Shorts Studio Pro v2.exe"; Description: "Launch AI Shorts Studio Pro"; Flags: nowait postinstall skipifsilent