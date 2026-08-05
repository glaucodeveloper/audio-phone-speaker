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
    $candidates = @(
        @{ File = "py"; Args = @("-3.14") },
        @{ File = "py"; Args = @("-3.13") },
        @{ File = "py"; Args = @("-3.12") },
        @{ File = "py"; Args = @("-3.11") },
        @{ File = "py"; Args = @("-3") },
        @{ File = "python"; Args = @() },
        @{ File = "python3"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.File -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        try {
            & $command.Source @($candidate.Args) -c "import sys; print(sys.executable)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{ File = $command.Source; Args = @($candidate.Args) }
            }
        }
        catch { continue }
    }

    throw "Python 3 não encontrado. Instale Python 3.11 ou superior."
}

function Resolve-Adb {
    $command = Get-Command adb -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"),
        (Join-Path $env:USERPROFILE "AppData\Local\Android\Sdk\platform-tools\adb.exe"),
        "C:\Android\Sdk\platform-tools\adb.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }

    throw "adb não encontrado. Instale Android SDK Platform-Tools."
}

function Resolve-JavaHome {
    if ($env:JAVA_HOME -and (Test-Path (Join-Path $env:JAVA_HOME "bin\java.exe"))) {
        return $env:JAVA_HOME
    }

    $directCandidates = @(
        "C:\Program Files\Android\Android Studio\jbr",
        "C:\Program Files\Java\jdk-21"
    )
    foreach ($candidate in $directCandidates) {
        if (Test-Path (Join-Path $candidate "bin\java.exe")) { return $candidate }
    }

    foreach ($root in @("C:\Program Files\Eclipse Adoptium", "C:\Program Files\Java")) {
        if (-not (Test-Path $root)) { continue }
        $match = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*21*" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($match -and (Test-Path (Join-Path $match.FullName "bin\java.exe"))) {
            return $match.FullName
        }
    }

    return $null
}

if ($env:OS -ne "Windows_NT") {
    throw "Este instalador deve ser executado no Windows."
}

$ProjectDir = (Resolve-Path $ProjectDir).Path
$Requirements = Join-Path $ProjectDir "requirements-audio-sender.txt"
$Sender = Join-Path $ProjectDir "audio_sender.py"
$Runner = Join-Path $ProjectDir "scripts\windows\run-sender.ps1"
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$WindowsConfig = Join-Path $ProjectDir ".audio-speaker.windows.ps1"
$Apk = Join-Path $ProjectDir "android\app\build\outputs\apk\debug\app-debug.apk"

foreach ($required in @($Requirements, $Sender, $Runner)) {
    if (-not (Test-Path $required)) { throw "Arquivo obrigatório não encontrado: $required" }
}

Write-Step "Preparando ambiente Python"
$PythonCommand = Resolve-PythonCommand
Write-Host "Python: $($PythonCommand.File) $($PythonCommand.Args -join ' ')"

if ($ForceRecreateVenv -and (Test-Path $VenvDir)) {
    Remove-Item -Recurse -Force $VenvDir
}
if (-not (Test-Path $VenvPython)) {
    & $PythonCommand.File @($PythonCommand.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Falha ao criar o ambiente virtual." }
}

& $VenvPython -m pip install --upgrade pip wheel setuptools
& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as dependências Python." }

& $VenvPython -c "import sys; sys.argv.append('audio-phone-speaker'); import numpy, soundcard, sounddevice, websockets; print('Dependencias do sender: OK')"
if ($LASTEXITCODE -ne 0) { throw "Falha ao validar as dependências Python." }

Write-Step "Configurando microfone virtual do Windows"
$escapedDevice = $VirtualMicrophonePlaybackDevice.Replace("'", "''")
@"
`$env:PHONE_MIC_PLAYBACK_DEVICE = '$escapedDevice'
`$env:PHONE_MIC_OUTPUT_RATE = '48000'
`$env:PHONE_MIC_SAMPLE_RATE = '16000'
`$env:PHONE_MIC_CHANNELS = '1'
`$env:PHONE_SPEAKER_PORT = '5001'
`$env:PHONE_MIC_PORT = '5002'
`$env:PHONE_MIC_CONTROL_PORT = '5003'
"@ | Set-Content -Path $WindowsConfig -Encoding UTF8

$deviceCheckScript = @'
import sounddevice as sd
import sys
needle = sys.argv[1].casefold()
matches = [
    d["name"] for d in sd.query_devices()
    if int(d["max_output_channels"]) > 0 and needle in str(d["name"]).casefold()
]
if matches:
    print("Dispositivo de saída do cabo virtual:", matches[0])
    raise SystemExit(0)
print("Dispositivo não encontrado:", sys.argv[1])
print("Saídas disponíveis:")
for d in sd.query_devices():
    if int(d["max_output_channels"]) > 0:
        print(" -", d["name"])
raise SystemExit(2)
'@

& $VenvPython -c $deviceCheckScript $VirtualMicrophonePlaybackDevice
$virtualMicFound = $LASTEXITCODE -eq 0
if (-not $virtualMicFound) {
    $message = @"
O endpoint de reprodução '$VirtualMicrophonePlaybackDevice' não foi encontrado.
Instale VB-CABLE ou VoiceMeeter. No VB-CABLE, o sender escreve em 'CABLE Input'
e os programas usam 'CABLE Output' como microfone.
"@
    if ($RequireVirtualMicrophone) { throw $message }
    Write-Warning $message
}

Write-Step "Localizando ADB"
$Adb = Resolve-Adb
$AdbDir = Split-Path $Adb -Parent
$env:PATH = "$AdbDir;$env:PATH"
Write-Host "ADB: $Adb"
& $Adb start-server
& $Adb devices -l

if ($BuildAndroid -or $InstallAndroidApp) {
    Write-Step "Compilando o aplicativo Android"
    foreach ($commandName in @("node", "npm", "npx")) {
        if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
            throw "$commandName não encontrado. Instale Node.js."
        }
    }

    $JavaHome = Resolve-JavaHome
    if (-not $JavaHome) { throw "JDK 21 não encontrado. Instale Android Studio ou OpenJDK 21." }
    $env:JAVA_HOME = $JavaHome
    $env:PATH = "$(Join-Path $JavaHome 'bin');$env:PATH"
    Write-Host "JAVA_HOME: $JavaHome"

    Push-Location $ProjectDir
    try {
        if (Test-Path (Join-Path $ProjectDir "package-lock.json")) { & npm ci } else { & npm install }
        if ($LASTEXITCODE -ne 0) { throw "npm install falhou." }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build falhou." }
        & npx cap sync android
        if ($LASTEXITCODE -ne 0) { throw "npx cap sync android falhou." }

        Push-Location (Join-Path $ProjectDir "android")
        try {
            & .\gradlew.bat clean assembleDebug --no-daemon --console=plain
            if ($LASTEXITCODE -ne 0) { throw "Gradle falhou." }
        }
        finally { Pop-Location }
    }
    finally { Pop-Location }

    if (-not (Test-Path $Apk)) { throw "APK não encontrado: $Apk" }
    Write-Host "APK: $Apk"
}

if ($InstallAndroidApp) {
    Write-Step "Instalando APK no celular"
    $devices = & $Adb devices | Select-Object -Skip 1 | Where-Object { $_ -match "\sdevice$" }
    if (-not $devices) { throw "Nenhum aparelho ADB autorizado foi encontrado." }

    & $Adb install -r $Apk
    if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar o APK." }

    foreach ($port in @(5001, 5002, 5003)) {
        & $Adb reverse "tcp:$port" "tcp:$port"
    }
    & $Adb shell pm grant glauco.phone.audiospeaker android.permission.RECORD_AUDIO 2>$null
    & $Adb shell pm grant glauco.phone.audiospeaker android.permission.POST_NOTIFICATIONS 2>$null
    & $Adb shell dumpsys deviceidle whitelist +glauco.phone.audiospeaker
    & $Adb shell am start -n glauco.phone.audiospeaker/.MainActivity
}

Write-Step "Registrando inicialização automática"
$PowerShellExe = (Get-Command powershell.exe).Source
$Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ProjectDir "{1}"' -f $Runner, $ProjectDir
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -MultipleInstances IgnoreNew

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Ponte duplex entre o áudio do Windows e o celular Android." `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Step "Instalação concluída"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Write-Host "Log: $env:LOCALAPPDATA\AudioPhoneSpeaker\sender.log"
if (-not $virtualMicFound) {
    Write-Warning "O speaker funcionará, mas o microfone do celular só aparecerá após instalar/configurar um cabo virtual."
}
