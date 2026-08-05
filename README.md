# audio-phone-speaker

Ponte de áudio duplex entre um computador e um celular Android conectado por ADB.

```text
áudio do computador ── WebSocket 5001 ──> alto-falante do celular
microfone do celular ── TCP PCM 5002 ──> microfone virtual do computador
controle do microfone ── HTTP 5003
```

O aplicativo Android permanece em foreground service. O processo Python configura `adb reverse`, captura o loopback do computador e recebe o microfone do celular em PCM mono de 16 kHz.

## Plataformas do computador

| Plataforma | Saída no celular | Celular como microfone do sistema |
|---|---:|---:|
| Linux + PipeWire/Pulse | Sim | `glauco_phone_mic`, criado automaticamente |
| Windows | Sim | Sim, por VB-CABLE, VoiceMeeter ou cabo virtual equivalente |
| macOS | Sim | Sim, por BlackHole ou dispositivo virtual equivalente |

## Requisitos comuns

- Python 3.11 ou superior;
- Android SDK Platform-Tools (`adb`);
- celular Android com depuração USB autorizada;
- Node.js, JDK 21 e Android SDK apenas para recompilar o APK.

O aplicativo Android solicita `RECORD_AUDIO`, notificação e exclusão de otimização de bateria.

## Linux

Instale as dependências de áudio do sistema. Em Arch/Manjaro:

```bash
sudo pacman -S --needed python python-pip android-tools pipewire-pulse libpulse
```

Instale e inicie o serviço do usuário:

```bash
git clone https://github.com/glaucodeveloper/audio-phone-speaker.git
cd audio-phone-speaker
chmod +x scripts/linux/*.sh
./scripts/linux/install-service.sh
```

Verifique:

```bash
systemctl --user status audio-speaker
journalctl --user -u audio-speaker -f
pactl list sources short | grep glauco_phone_mic
pactl get-default-source
```

A fonte aparece como **Glauco Phone Microphone**. O estado `SUSPENDED` é normal quando nenhum programa está gravando.

Remoção:

```bash
./scripts/linux/uninstall-service.sh
```

## Windows

O Windows precisa de um cabo de áudio virtual para expor o PCM recebido como dispositivo de gravação. Com VB-CABLE:

```text
sender Python escreve em: CABLE Input
aplicativos usam como mic: CABLE Output
```

Depois de instalar o cabo virtual, abra PowerShell no repositório:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\install.ps1
```

Para compilar e instalar também o APK:

```powershell
.\scripts\windows\install.ps1 -BuildAndroid -InstallAndroidApp
```

Para VoiceMeeter ou outro endpoint:

```powershell
.\scripts\windows\install.ps1 `
  -VirtualMicrophonePlaybackDevice "VoiceMeeter Input"
```

A instalação cria uma tarefa no Agendador do Windows para iniciar a ponte na sessão interativa do usuário. Isso é necessário para acesso aos dispositivos de áudio.

Verificação:

```powershell
.\scripts\windows\check-virtual-microphone.ps1
Get-ScheduledTask -TaskName "Audio Phone Speaker"
Get-Content "$env:LOCALAPPDATA\AudioPhoneSpeaker\sender.log" -Tail 100 -Wait
```

Remoção da inicialização automática:

```powershell
.\scripts\windows\uninstall-autostart.ps1
```

## Execução manual

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
python -m pip install -r requirements-audio-sender.txt
python audio_sender.py
```

Windows:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-audio-sender.txt
$env:PHONE_MIC_PLAYBACK_DEVICE = "CABLE Input"
python .\audio_sender.py
```

Saída esperada:

```text
Speaker server running on ws://127.0.0.1:5001
Phone microphone transport: tcp://127.0.0.1:5002
Phone microphone control:   http://127.0.0.1:5003
Phone microphone connected
```

## API local do microfone

```bash
curl http://127.0.0.1:5003/status
curl -X POST http://127.0.0.1:5003/microphone/start
curl -X POST http://127.0.0.1:5003/microphone/stop
```

Essa API permite que aplicações como o GlaucoPlastic ativem ou suspendam o encaminhamento sem depender de `SpeechRecognition` do WebView.

## Build Android

Use JDK 21 convencional ou o JBR do Android Studio. GraalVM pode falhar na transformação `androidJdkImage` do Gradle.

```bash
npm ci
npm run build
npx cap sync android
cd android
./gradlew assembleDebug
```

No Windows:

```powershell
npm ci
npm run build
npx cap sync android
cd android
.\gradlew.bat assembleDebug
```

APK:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

Instale e configure as portas:

```bash
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:5001 tcp:5001
adb reverse tcp:5002 tcp:5002
adb reverse tcp:5003 tcp:5003
```

## Configuração por ambiente

| Variável | Padrão | Função |
|---|---|---|
| `PHONE_SPEAKER_PORT` | `5001` | áudio do computador para o celular |
| `PHONE_MIC_PORT` | `5002` | PCM do celular para o computador |
| `PHONE_MIC_CONTROL_PORT` | `5003` | API HTTP local |
| `PHONE_MIC_SOURCE_NAME` | `glauco_phone_mic` | nome da fonte no Linux |
| `PHONE_MIC_SET_DEFAULT` | `1` | torna a fonte Linux padrão |
| `PHONE_MIC_PLAYBACK_DEVICE` | automático | endpoint virtual no Windows/macOS |
| `PHONE_MIC_OUTPUT_RATE` | `48000` | taxa do cabo virtual |

## Arquitetura

- `audio_sender.py`: servidor duplex, ADB, loopback, fonte virtual e API local;
- `PhoneMicrophoneBridge.java`: captura Android com `AudioRecord` e transmite PCM;
- `BackgroundAudioService.java`: foreground service de playback e microfone;
- `scripts/linux`: serviço `systemd --user`;
- `scripts/windows`: instalação, tarefa de logon e validação do cabo virtual.
