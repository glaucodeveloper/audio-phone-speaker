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

        print(
            f"{marker} {index}: {name} "
            f"({float(device.get('defaultSampleRate', 0)):.0f} Hz)"
        )

    raise SystemExit(0 if found else 2)

finally:
    p.terminate()
'@

& $Python -c $script $Device

if ($LASTEXITCODE -ne 0) {
    Write-Warning "Instale o VB-CABLE normal ou informe outro endpoint com -Device."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Nos aplicativos, selecione CABLE Output como microfone."
