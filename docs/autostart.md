# Inicialização automática do sender

O sender e o aplicativo usam a porta `5000`. Não use `5001` no script de
inicialização.

## Linux

```bash
chmod +x scripts/linux/*.sh
./scripts/linux/install-service.sh
```

Verificação:

```bash
systemctl --user status audio-phone-speaker.service --no-pager --full
journalctl --user -u audio-phone-speaker.service -f
adb reverse --list
ss -ltnp | grep ':5000'
```

Remoção:

```bash
./scripts/linux/uninstall-service.sh
```

## Windows

Abra PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd C:\dev\audio-phone-speaker
.\scripts\windows\install-autostart.ps1
```

Verificação:

```powershell
Get-ScheduledTask -TaskName "Audio Phone Speaker"
Get-Content "$env:LOCALAPPDATA\AudioPhoneSpeaker\sender.log" -Tail 100 -Wait
adb reverse --list
Get-NetTCPConnection -LocalPort 5000 -State Listen
```

Remoção:

```powershell
.\scripts\windows\uninstall-autostart.ps1
```
