#define AppVer "1.9.4"
[Setup]
AppId={{A8D3C9F2-5B1E-4C7A-9E2D-3F4B6A7C8D90}
AppName=ArchiveDrop
AppVersion={#AppVer}
AppVerName=ArchiveDrop {#AppVer}
AppPublisher=JB
DefaultDirName={localappdata}\Programs\ArchiveDrop
DefaultGroupName=ArchiveDrop
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=out
OutputBaseFilename=ArchiveDrop-Setup
SetupIconFile=archivedrop.ico
UninstallDisplayIcon={app}\ArchiveDrop.exe
UninstallDisplayName=ArchiveDrop
WizardStyle=modern
WizardImageFile=wizard_big.bmp,wizard_big_2x.bmp
WizardSmallImageFile=wizard_small.bmp,wizard_small_2x.bmp
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Messages]
WelcomeLabel2=This installs ArchiveDrop {#AppVer} on this PC.%n%nPaste an archive.org link, pick the file types, and the files download to the folder you choose. Books, old software, audio and video.

[Tasks]
Name: "desktopicon"; Description: "Add a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "bgtask"; Description: "Keep downloading in the background (checks the list every 15 minutes)"; GroupDescription: "Background downloads:"

[Files]
Source: "dist\ArchiveDrop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "task.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\ArchiveDrop"; Filename: "{app}\ArchiveDrop.exe"; AppUserModelID: "JB.ArchiveDrop"
Name: "{userdesktop}\ArchiveDrop"; Filename: "{app}\ArchiveDrop.exe"; AppUserModelID: "JB.ArchiveDrop"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\task.ps1"" -Exe ""{app}\ArchiveDrop.exe"""; Flags: runhidden; Tasks: bgtask; StatusMsg: "Setting up background downloads..."
Filename: "{app}\ArchiveDrop.exe"; Description: "Open ArchiveDrop"; Flags: nowait postinstall skipifsilent
; in-app updates run the installer with /SILENT: reopen the app afterwards
Filename: "{app}\ArchiveDrop.exe"; Flags: nowait skipifnotsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -Command ""Unregister-ScheduledTask -TaskName 'ArchiveDrop' -Confirm:$false -ErrorAction SilentlyContinue"""; Flags: runhidden; RunOnceId: "RemoveTask"
