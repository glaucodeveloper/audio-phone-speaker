[CmdletBinding()]
param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$Device = "CABLE Input"
)

$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Ambiente virtual não encontrado. Execute scripts\windows\install.ps1."
}

$script = @'
import sounddevice as sd
import sys
needle = sys.argv[1].casefold()
found = False
for index, device in enumerate(sd.query_devices()):
    if int(device["max_output_channels"]) <= 0:
        continue
    marker = "*" if needle in str(device["name"]).casefold() else " "
    found = found or marker == "*"
    print(f"{marker} {index}: {device['name']} ({device['default_samplerate']:.0f} Hz)")
raise SystemExit(0 if found else 2)
'@

& $Python -c $script $Device
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Instale VB-CABLE/VoiceMeeter ou informe outro endpoint com -Device."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "No Windows, selecione 'CABLE Output' como microfone nos aplicativos."
