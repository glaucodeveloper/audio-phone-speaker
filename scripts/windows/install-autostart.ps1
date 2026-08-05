[CmdletBinding()]
param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$TaskName = "Audio Phone Speaker",
    [string]$VirtualMicrophonePlaybackDevice = "CABLE Input"
)

& (Join-Path $PSScriptRoot "install.ps1") `
    -ProjectDir $ProjectDir `
    -TaskName $TaskName `
    -VirtualMicrophonePlaybackDevice $VirtualMicrophonePlaybackDevice
