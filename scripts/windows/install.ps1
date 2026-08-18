[CmdletBinding()]
param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$TaskName = "Audio Phone Speaker",
    [string]$VirtualMicrophonePlaybackDevice = "CABLE Input",
    [switch]$RequireVirtualMicrophone,
    [switch]$BuildAndroid,
    [switch]$InstallAndroidApp,
    [switch]$ForceRecreateVenv
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-PythonCommand {
    foreach ($name in @("python", "py", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }

        try {
            if ($name -eq "py") {
                & $command.Source -3 -c "import sys; print(sys.executable)" *> $null
                if ($LASTEXITCODE -eq 0) {
                    return @{ File = $command.Source; Args = @("-3") }
                }
            }
            else {
                & $command.Source -c "import sys; print(sys.executable)" *> $null
                if ($LASTEXITCODE -eq 0) {
                    return @{ File = $command.Source; Args = @() }
                }
            }
        }
        catch {}
    }

    throw "Python 3 não encontrado."
}

if ($env:OS -ne "Windows_NT") {
    throw "Este instalador deve ser executado no Windows."
}

$ProjectDir = (Resolve-Path $ProjectDir).Path
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $ProjectDir "requirements-audio-sender.txt"
$SetupScript = Join-Path $ProjectDir "scripts\setup_duplex.py"
$Runner = Join-Path $ProjectDir "scripts\windows\run-sender.ps1"
$Config = Join-Path $ProjectDir ".audio-speaker.windows.ps1"

Write-Step "Preparando ambiente Python"
$PythonCommand = Resolve-PythonCommand

if ($ForceRecreateVenv -and (Test-Path $VenvDir)) {
    Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $VenvPython)) {
    & $PythonCommand.File @($PythonCommand.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao criar o ambiente virtual."
    }
}

& $VenvPython -m pip install --upgrade pip wheel setuptools
& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) {
    throw "Falha ao instalar dependências Python."
}

& $VenvPython -m py_compile `
    (Join-Path $ProjectDir "audio_sender.py") `
    (Join-Path $ProjectDir "phone_microphone_bridge.py") `
    (Join-Path $ProjectDir "virtual_mic_sink_v15.py")
if ($LASTEXITCODE -ne 0) {
    throw "Falha ao validar os módulos Python."
}

Write-Step "Configurando o microfone virtual"
$escapedDevice = $VirtualMicrophonePlaybackDevice.Replace("'", "''")

@"
`$env:PHONE_MIC_VIRTUAL_DEVICE = '$escapedDevice'
`$env:PHONE_MIC_PLAYBACK_DEVICE = '$escapedDevice'
`$env:PHONE_MIC_VIRTUAL_ENABLED = '1'
`$env:PHONE_MIC_SAMPLE_RATE = '48000'
`$env:PHONE_MIC_CHANNELS = '1'
"@ | Set-Content -Path $Config -Encoding UTF8

$check = @'
import pyaudiowpatch as pyaudio
import sys

needle = sys.argv[1].casefold()
p = pyaudio.PyAudio()

try:
    found = False

    for index in range(p.get_device_count()):
        device = p.get_device_info_by_index(index)

        if int(device.get("maxOutputChannels", 0)) <= 0:
            continue

        name = str(device.get("name", ""))
        marker = "*" if needle in name.casefold() else " "
        found = found or marker == "*"
        print(f"{marker} {index}: {name}")

    raise SystemExit(0 if found else 2)
finally:
    p.terminate()
'@

& $VenvPython -c $check $VirtualMicrophonePlaybackDevice
$virtualMicFound = $LASTEXITCODE -eq 0

if (-not $virtualMicFound) {
    $message = @"
O endpoint '$VirtualMicrophonePlaybackDevice' não foi encontrado.
Instale o VB-CABLE normal. O bridge escreve em CABLE Input e os programas
usam CABLE Output como microfone. VB-CABLE A+B não é necessário.
"@

    if ($RequireVirtualMicrophone) {
        throw $message
    }

    Write-Warning $message
}

Write-Step "Executando setup duplex"
$setupArgs = @($SetupScript, "--repo", $ProjectDir)

if (-not $BuildAndroid) {
    $setupArgs += "--skip-android-build"
}

if (-not $InstallAndroidApp) {
    $setupArgs += "--skip-apk-install"
}

$setupArgs += "--skip-python-deps"

& $VenvPython @setupArgs
if ($LASTEXITCODE -ne 0) {
    throw "scripts\setup_duplex.py falhou."
}

Write-Step "Registrando inicialização automática"
$PowerShellExe = (Get-Command powershell.exe).Source
$Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ProjectDir "{1}"' -f $Runner, $ProjectDir
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Action = New-ScheduledTaskAction `
    -Execute $PowerShellExe `
    -Argument $Arguments `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
    -AtLogOn `
    -User $CurrentUser

$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -MultipleInstances IgnoreNew

Stop-ScheduledTask `
    -TaskName $TaskName `
    -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Ponte duplex de áudio entre Windows e Android." `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Step "Instalação concluída"
Get-ScheduledTask `
    -TaskName $TaskName |
    Select-Object TaskName, State

Write-Host "Mic do navegador: CABLE Output (VB-Audio Virtual Cable)"
Write-Host "Log: $env:LOCALAPPDATA\AudioPhoneSpeaker\sender.log"
