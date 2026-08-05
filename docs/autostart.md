# Inicialização automática

## Linux

A ponte precisa acessar a sessão PipeWire do usuário. Por isso, a instalação usa `systemd --user`, não um serviço global executado como root.

```bash
./scripts/linux/install-service.sh
systemctl --user status audio-speaker
journalctl --user -u audio-speaker -f
```

A unit fica em:

```text
~/.config/systemd/user/audio-speaker.service
```

O serviço aguarda PipeWire/Pulse, configura ADB e reinicia em caso de falha.

## Windows

A ponte precisa rodar na sessão interativa para acessar WASAPI e o cabo virtual. O instalador registra uma tarefa no logon:

```powershell
.\scripts\windows\install.ps1
Get-ScheduledTask -TaskName "Audio Phone Speaker"
```

Log:

```powershell
Get-Content "$env:LOCALAPPDATA\AudioPhoneSpeaker\sender.log" -Tail 100 -Wait
```

Remoção:

```powershell
.\scripts\windows\uninstall-autostart.ps1
```
