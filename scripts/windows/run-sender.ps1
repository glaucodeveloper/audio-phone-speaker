param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

$ErrorActionPreference = "Stop"

$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Sender = Join-Path $ProjectDir "audio_sender.py"
$LogDir = Join-Path $env:LOCALAPPDATA "AudioPhoneSpeaker"
$LogFile = Join-Path $LogDir "sender.log"

if (-not (Test-Path $Python)) {
    throw "Ambiente virtual não encontrado: $Python. Execute scripts\windows\install-autostart.ps1."
}

$AdbCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools"),
    "C:\Android\Sdk\platform-tools"
)

foreach ($Directory in $AdbCandidates) {
    if (Test-Path $Directory) {
        $env:PATH = "$Directory;$env:PATH"
    }
}

if (-not (Get-Command adb -ErrorAction SilentlyContinue)) {
    throw "adb não encontrado no PATH."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONUNBUFFERED = "1"

Push-Location $ProjectDir
try {
    Start-Transcript -Path $LogFile -Append | Out-Null
    adb start-server
    & $Python -u $Sender
    $ExitCode = $LASTEXITCODE
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
    Pop-Location
}

exit $ExitCode
