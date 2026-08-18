# audio-phone-speaker

Ponte de áudio duplex entre um computador e um celular Android conectado por USB/ADB.

```text
Windows
───────
áudio do PC
  → WASAPI loopback
  → TCP :5001
  → AudioTrack Android 48 kHz stereo

microfone Android 48 kHz mono
  → TCP :5002
  → processo WASAPI isolado
  → CABLE Input
  → CABLE Output
  → navegador / chamada


Linux
─────
áudio do PC
  → monitor PipeWire/Pulse
  → TCP :5001
  → AudioTrack Android 48 kHz stereo

microfone Android 48 kHz mono
  → TCP :5002
  → module-pipe-source
  → Audio Phone Microphone
  → navegador / chamada
```

O aplicativo Android é um projeto **Capacitor**, mas o WebView não transporta áudio. O áudio duplex fica no Java nativo dentro de um foreground service. Veja [`docs/android-capacitor.md`](docs/android-capacitor.md).

## Estado atual

| Função | Windows | Linux |
|---|---|---|
| PC → alto-falante do celular | WASAPI loopback | PipeWire/Pulse monitor |
| Celular → microfone do PC | VB-CABLE normal | fonte PipeWire/Pulse criada automaticamente |
| PCM do speaker | 48 kHz, stereo, PCM16 | 48 kHz, stereo, PCM16 |
| PCM do mic | 48 kHz, mono, PCM16 | 48 kHz, mono, PCM16 |
| Transporte | TCP nativo + ADB reverse | TCP nativo + ADB reverse |

O Windows usa somente o VB-CABLE normal para o **microfone do celular**. Não é necessário VB-CABLE A+B.

## Portas

| Porta | Direção | Uso |
|---|---|---|
| `5001` | PC → Android | speaker, PCM16 stereo 48 kHz |
| `5002` | Android → PC | mic, PCM16 mono 48 kHz |
| `5003` | local no PC | status/gravação do bridge de microfone |

O Android acessa `127.0.0.1:5001` e `127.0.0.1:5002`; `adb reverse` leva essas conexões ao processo Python no computador.

## Instalação rápida

O script [`scripts/setup_duplex.py`](scripts/setup_duplex.py):

1. instala as dependências Python adequadas à plataforma;
2. valida os módulos Python;
3. executa o build Vite;
4. sincroniza o Capacitor com Android;
5. compila o APK;
6. recupera o daemon ADB quando necessário;
7. instala o APK com `--no-streaming`;
8. configura `adb reverse` nas portas `5001` e `5002`.

### Windows

Requisitos:

- Python 3.10+;
- Node.js;
- JDK compatível com o projeto Android;
- Android SDK Platform-Tools;
- VB-CABLE normal para expor o mic do telefone às aplicações.

```powershell
python .\scripts\setup_duplex.py
python .\audio_sender.py
```

No Windows:

- a saída de áudio normal do sistema deve ser o endpoint cujo som será enviado ao telefone;
- o VB-CABLE fica reservado para o mic;
- no Chrome/WhatsApp/Discord selecione `CABLE Output (VB-Audio Virtual Cable)` como microfone.

Para forçar o endpoint capturado:

```powershell
$env:PHONE_SPEAKER_CAPTURE_DEVICE="Saída Digital"
python .\audio_sender.py
```

Para forçar o endpoint de injeção do mic:

```powershell
$env:PHONE_MIC_VIRTUAL_DEVICE="CABLE Input"
python .\audio_sender.py
```

### Linux / PipeWire

Em Arch/Manjaro:

```bash
sudo pacman -S --needed python python-pip android-tools pipewire pipewire-pulse libpulse
```

Instalação manual:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-audio-sender.txt

python scripts/setup_duplex.py --skip-python-deps
python audio_sender.py
```

O bridge cria automaticamente uma fonte:

```text
Audio Phone Microphone
source: audio_phone_speaker_mic
48 kHz mono
```

Verificação:

```bash
pactl list sources short | grep audio_phone_speaker_mic
pactl get-default-source
```

Para forçar outra saída do PC como fonte do speaker:

```bash
PHONE_SPEAKER_CAPTURE_DEVICE="nome do dispositivo" python audio_sender.py
```

### Serviço systemd do usuário

```bash
chmod +x scripts/linux/*.sh
./scripts/linux/install-service.sh
```

Verifique:

```bash
systemctl --user status audio-phone-speaker
journalctl --user -u audio-phone-speaker -f
```

Remova:

```bash
./scripts/linux/uninstall-service.sh
```

## Execução sem rebuild do APK

Se o app já está instalado:

```bash
python audio_sender.py
```

O sender configura `adb reverse` uma vez. Abra **Audio Phone Speaker** no celular; os bridges Java reconectam sozinhos.

Se quiser configurar manualmente:

```bash
adb reverse tcp:5001 tcp:5001
adb reverse tcp:5002 tcp:5002
```

## Arquitetura de áudio

### PC → telefone

`audio_sender.py` escolhe o backend conforme o host:

- Windows: `PyAudioWPatch` + WASAPI loopback;
- Linux: `SoundCard` + monitor PipeWire/Pulse.

O capturador lê blocos de 40 ms e os divide em pacotes de 20 ms. O servidor `tcp:5001` aceita somente o bridge Android nativo, identificado pelo handshake `SPK1`.

No Android, `PhoneSpeakerBridge.java`:

- conecta a `127.0.0.1:5001`;
- envia `SPK1`;
- lê frames `length + PCM`;
- reproduz em `AudioTrack`;
- usa 48 kHz, stereo, PCM16;
- usa prebuffer curto e modo low-latency quando disponível;
- reconecta em caso de queda.

### Telefone → PC

`PhoneMicrophoneBridge.java` usa `AudioRecord`:

- 48 kHz;
- mono;
- PCM16;
- blocos de 20 ms;
- TCP `5002`.

No PC, `phone_microphone_bridge.py` encaminha os frames para um processo de áudio isolado.

No Windows, `virtual_mic_sink_v15.py` mantém um jitter buffer com correção suave de drift antes de escrever no VB-CABLE. O isolamento em outro processo evita conflito entre os dois usos de PortAudio/WASAPI.

No Linux, `virtual_mic_sink_linux.py` cria uma fonte PipeWire/Pulse com `module-pipe-source` e escreve o PCM de 48 kHz diretamente nela.

## API local de status/gravação

```bash
curl http://127.0.0.1:5003/status
curl -X POST http://127.0.0.1:5003/record/start
curl -X POST http://127.0.0.1:5003/record/stop
```

A gravação WAV é opcional. Parar uma gravação não desliga o microfone virtual: o encaminhamento do mic permanece contínuo.

## Build Android / Capacitor

Build direto:

```bash
npm install
npm run build
npx cap sync android

cd android
./gradlew assembleDebug
```

Windows:

```powershell
npm install
npm run build
npx cap sync android

cd android
.\gradlew.bat assembleDebug
```

APK:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

Instalação:

```bash
adb install --no-streaming -r android/app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:5001 tcp:5001
adb reverse tcp:5002 tcp:5002
```

## Variáveis de ambiente

| Variável | Plataforma | Padrão | Função |
|---|---|---|---|
| `PHONE_SPEAKER_CAPTURE_DEVICE` | Windows/Linux | automático | endpoint de reprodução capturado |
| `PHONE_MIC_VIRTUAL_ENABLED` | Windows/Linux | `1` | ativa o mic virtual |
| `PHONE_MIC_VIRTUAL_DEVICE` | Windows | `CABLE Input` | endpoint de playback do cabo virtual |
| `PHONE_MIC_PLAYBACK_DEVICE` | Windows | fallback | alias legado para o endpoint virtual |
| `PHONE_MIC_SOURCE_NAME` | Linux | `audio_phone_speaker_mic` | nome da fonte PipeWire/Pulse |
| `PHONE_MIC_DESCRIPTION` | Linux | `Audio Phone Microphone` | descrição visível da fonte |
| `PHONE_MIC_SET_DEFAULT` | Linux | `1` | torna a fonte criada o mic padrão |
| `PHONE_MIC_KEEP_AUDIO` | ambos | `0` | mantém WAVs criados pela API |
| `PHONE_MIC_MUTE_SPEAKER` | ambos | `0` | opcionalmente silencia speaker durante gravação |

## Arquivos principais

```text
audio_sender.py
  captura o áudio do PC e serve tcp:5001

phone_microphone_bridge.py
  recebe tcp:5002 e controla o mic virtual

virtual_mic_sink_v15.py
  renderer adaptativo do mic no Windows

virtual_mic_sink_linux.py
  fonte PipeWire/Pulse do mic no Linux

android/.../PhoneSpeakerBridge.java
  playback Android nativo

android/.../PhoneMicrophoneBridge.java
  captura Android nativa

android/.../BackgroundAudioService.java
  foreground service duplex

src/js/capacitor-welcome.js
  somente interface/controlador Capacitor
```
