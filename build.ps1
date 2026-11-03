# Builds release\BorddersClipper-Setup-<version>.exe
# Needs: pip install pyinstaller, Inno Setup 6, and an FFmpeg shared build
# (https://github.com/BtbN/FFmpeg-Builds, win64-gpl-shared) unzipped somewhere.
param([Parameter(Mandatory)][string]$FFmpegDir)

$ErrorActionPreference = "Stop"
python -m PyInstaller clipper.spec --noconfirm

$ff = "dist\Clipper\ffmpeg"
New-Item -ItemType Directory -Force $ff | Out-Null
Copy-Item "$FFmpegDir\bin\ffmpeg.exe", "$FFmpegDir\bin\ffprobe.exe", "$FFmpegDir\bin\*.dll" $ff
Copy-Item "$FFmpegDir\LICENSE.txt" $ff

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
& $iscc installer.iss
