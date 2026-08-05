[CmdletBinding()]
param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Sender = Join-Path $ProjectDir "audio_sender.py"
$Config = Join-Path $ProjectDir ".audio-speaker.windows.ps1"
$LogDir = Join-Path $env:LOCALAPPDATA "AudioPhoneSpeaker"
$LogFile = Join-Path $LogDir "sender.log"

if (-not (Test-Path $Python)) {
    throw "Ambiente virtual não encontrado: $Python. Execute scripts\windows\install.ps1."
}
if (-not (Test-Path $Sender)) {
    throw "Sender não encontrado: $Sender"
}
if (Test-Path $Config) {
    . $Config
}
if (-not $env:PHONE_MIC_PLAYBACK_DEVICE) {
    $env:PHONE_MIC_PLAYBACK_DEVICE = "CABLE Input"
}
if (-not $env:PHONE_MIC_OUTPUT_RATE) { $env:PHONE_MIC_OUTPUT_RATE = "48000" }

$AdbCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools"),
    (Join-Path $env:USERPROFILE "AppData\Local\Android\Sdk\platform-tools"),
    "C:\Android\Sdk\platform-tools"
)
foreach ($directory in $AdbCandidates) {
    if (Test-Path $directory) { $env:PATH = "$directory;$env:PATH" }
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
