[CmdletBinding()]
param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$TaskName = "Audio Phone Speaker",
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
        @{ File = "py"; Args = @("-3.13") },
        @{ File = "py"; Args = @("-3.12") },
        @{ File = "py"; Args = @("-3.11") },
        @{ File = "py"; Args = @("-3") },
        @{ File = "python"; Args = @() },
        @{ File = "python3"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.File -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }

        try {
            & $command.Source @($candidate.Args) -c "import sys; print(sys.executable)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{
                    File = $command.Source
                    Args = @($candidate.Args)
                }
            }
        }
        catch {
            continue
        }
    }

    throw "Python 3 não encontrado. Instale Python 3.11, 3.12 ou 3.13 e execute novamente."
}

function Resolve-Adb {
    $command = Get-Command adb -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"),
        (Join-Path $env:USERPROFILE "AppData\Local\Android\Sdk\platform-tools\adb.exe"),
        "C:\Android\Sdk\platform-tools\adb.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "adb não encontrado. Instale Android SDK Platform-Tools."
}

function Resolve-JavaHome {
    if ($env:JAVA_HOME -and (Test-Path (Join-Path $env:JAVA_HOME "bin\java.exe"))) {
        return $env:JAVA_HOME
    }

    $candidates = @(
        "C:\Program Files\Android\Android Studio\jbr",
        "C:\Program Files\Eclipse Adoptium\jdk-21*",
        "C:\Program Files\Java\jdk-21*"
    )

    foreach ($candidate in $candidates) {
        $matches = Get-ChildItem $candidate -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending

        foreach ($match in $matches) {
            if (Test-Path (Join-Path $match.FullName "bin\java.exe")) {
                return $match.FullName
            }
        }

        if (Test-Path (Join-Path $candidate "bin\java.exe")) {
            return $candidate
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
$Apk = Join-Path $ProjectDir "android\app\build\outputs\apk\debug\app-debug.apk"

foreach ($required in @($Requirements, $Sender, $Runner)) {
    if (-not (Test-Path $required)) {
        throw "Arquivo obrigatório não encontrado: $required"
    }
}

Write-Step "Localizando Python"
$PythonCommand = Resolve-PythonCommand
Write-Host "Python: $($PythonCommand.File) $($PythonCommand.Args -join ' ')"

if ($ForceRecreateVenv -and (Test-Path $VenvDir)) {
    Write-Step "Removendo ambiente virtual existente"
    Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $VenvPython)) {
    Write-Step "Criando ambiente virtual"
    & $PythonCommand.File @($PythonCommand.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao criar o ambiente virtual."
    }
}

Write-Step "Instalando dependências Python"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r $Requirements

# O SoundCard 0.4.6 pode consultar sys.argv[1] durante o import.
& $VenvPython -c "import sys; sys.argv.append('audio-phone-speaker'); import numpy, soundcard, websockets; print('Dependencias do sender: OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Falha ao validar as dependências Python."
}

Write-Step "Localizando ADB"
$Adb = Resolve-Adb
$AdbDir = Split-Path $Adb -Parent
$env:PATH = "$AdbDir;$env:PATH"
Write-Host "ADB: $Adb"

& $Adb start-server
& $Adb devices -l

if ($BuildAndroid -or $InstallAndroidApp) {
    Write-Step "Preparando ambiente de build Android"

    foreach ($commandName in @("node", "npm", "npx")) {
        if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
            throw "$commandName não encontrado. Instale Node.js antes de compilar o Android."
        }
    }

    $JavaHome = Resolve-JavaHome
    if (-not $JavaHome) {
        throw "JDK 21 não encontrado. Instale Android Studio ou um JDK 21."
    }

    $env:JAVA_HOME = $JavaHome
    $env:PATH = "$(Join-Path $JavaHome 'bin');$env:PATH"
    Write-Host "JAVA_HOME: $JavaHome"

    Push-Location $ProjectDir
    try {
        Write-Step "Instalando dependências Node"
        if (Test-Path (Join-Path $ProjectDir "package-lock.json")) {
            & npm ci
        }
        else {
            & npm install
        }
        if ($LASTEXITCODE -ne 0) {
            throw "npm install falhou."
        }

        Write-Step "Gerando aplicação web"
        & npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "npm run build falhou."
        }

        Write-Step "Sincronizando Capacitor"
        & npx cap sync android
        if ($LASTEXITCODE -ne 0) {
            throw "npx cap sync android falhou."
        }

        Write-Step "Compilando APK debug"
        Push-Location (Join-Path $ProjectDir "android")
        try {
            & .\gradlew.bat clean assembleDebug --no-daemon --console=plain
            if ($LASTEXITCODE -ne 0) {
                throw "Gradle falhou."
            }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        Pop-Location
    }

    if (-not (Test-Path $Apk)) {
        throw "APK não encontrado após o build: $Apk"
    }

    Write-Host "APK: $Apk"
}

if ($InstallAndroidApp) {
    Write-Step "Instalando APK no aparelho"

    $devices = & $Adb devices |
        Select-Object -Skip 1 |
        Where-Object { $_ -match "\sdevice$" }

    if (-not $devices) {
        throw "Nenhum aparelho ADB autorizado foi encontrado."
    }

    & $Adb shell am force-stop glauco.phone.audiospeaker 2>$null
    & $Adb install -r $Apk
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao instalar o APK."
    }

    & $Adb reverse --remove tcp:5000 2>$null
    & $Adb reverse tcp:5000 tcp:5000
    & $Adb shell pm grant glauco.phone.audiospeaker android.permission.POST_NOTIFICATIONS 2>$null
    & $Adb shell dumpsys deviceidle whitelist +glauco.phone.audiospeaker
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

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Executa o sender do Audio Phone Speaker na sessão interativa do usuário." `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

if ($InstallAndroidApp) {
    Start-Sleep -Seconds 3
    & $Adb shell am start -n glauco.phone.audiospeaker/.MainActivity
}

Write-Step "Instalação concluída"

Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State

Write-Host ""
Write-Host "Logs do sender:"
Write-Host "  Get-Content `"$env:LOCALAPPDATA\AudioPhoneSpeaker\sender.log`" -Tail 100 -Wait"
Write-Host ""
Write-Host "Remover inicialização automática:"
Write-Host "  .\scripts\windows\uninstall-autostart.ps1"
