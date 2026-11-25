# Builds dist\ArchiveDrop (PyInstaller) then out\ArchiveDrop-Setup.exe (Inno Setup).
$ErrorActionPreference = 'Continue'   # PyInstaller logs to stderr; success is checked by file below
Set-Location $PSScriptRoot
$py = "$env:LOCALAPPDATA\Programs\Python\Python315\python.exe"
# Every build ships the latest release of every dependency
& $py -m pip install --upgrade --quiet --disable-pip-version-check --no-warn-script-location pip 2>&1 | Out-Null
& $py -m pip install --upgrade --quiet --disable-pip-version-check --no-warn-script-location `
    internetarchive pywebview pythonnet clr_loader pyinstaller pyinstaller-hooks-contrib requests urllib3 certifi qrcode 2>&1 | Out-Null
"DEPS: " + ((& $py -m pip list --disable-pip-version-check 2>$null | Select-String 'internetarchive|pywebview|pythonnet|pyinstaller |certifi|urllib3' | % { ($_.Line -split '\s+')[0..1] -join ' ' }) -join ', ') + " on $(& $py --version)"
Remove-Item -Recurse -Force build_py, dist, out -ErrorAction SilentlyContinue
& $py -m PyInstaller --noconfirm --clean --windowed --name ArchiveDrop --icon "$PSScriptRoot\archivedrop.ico" `
    --workpath build_py --distpath dist --specpath build_py `
    --add-data "$PSScriptRoot\ui;ui" --add-data "$PSScriptRoot\archivedrop.ico;." `
    --collect-all webview --collect-all clr_loader --hidden-import clr `
    --collect-submodules internetarchive --hidden-import ntfy_inbox --hidden-import qrcode --hidden-import qrcode.image.svg --collect-submodules qrcode --exclude-module tkinter --exclude-module PIL `
    archivedrop.pyw 2>&1 | Select-String -Pattern 'ERROR|Building EXE|completed successfully' | % { $_.Line }
if (-not (Test-Path dist\ArchiveDrop\ArchiveDrop.exe)) { throw 'PyInstaller build failed' }
& "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe" /Q installer.iss
if (-not (Test-Path out\ArchiveDrop-Setup.exe)) { throw 'Inno Setup build failed' }
# Portable zip: same files plus portable.txt, so settings and queue live in a data folder beside the exe
$port = "build_py\portable\ArchiveDrop"
Remove-Item -Recurse -Force build_py\portable -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $port | Out-Null
Copy-Item -Recurse dist\ArchiveDrop\* $port
Set-Content "$port\portable.txt" "This copy keeps its settings, queue and logs in the data folder next to ArchiveDrop.exe. Delete this file to use the installed-app location instead." -Encoding UTF8
Compress-Archive -Path $port -DestinationPath out\ArchiveDrop-Portable.zip -CompressionLevel Optimal -Force
if (-not (Test-Path out\ArchiveDrop-Portable.zip)) { throw 'Portable zip failed' }
"BUILT: " + (Get-Item out\ArchiveDrop-Setup.exe).Length / 1MB + " MB, portable " + [math]::Round((Get-Item out\ArchiveDrop-Portable.zip).Length / 1MB, 1) + " MB"
