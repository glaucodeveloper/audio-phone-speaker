# audio-phone-speaker

Android app + Python sender para tocar no celular o audio capturado do speaker/loopback do PC via `ws://127.0.0.1:5000`.

## O que este projeto faz

- O app Android roda em foreground service para continuar ativo em background.
- O sender Python abre `adb reverse`, sobe o servidor WebSocket local e tenta manter o app reconectado.
- O sender tambem monitora `droppedChunks` e `underflows`; se detectar degradacao, ele faz resync automatico do ADB/WebSocket.

## Estrutura

- [audio_sender.py](/C:/dev/audio-phone-speaker/audio_sender.py:1): sender Python incluido no repo.
- [src/js/capacitor-welcome.js](/C:/dev/audio-phone-speaker/src/js/capacitor-welcome.js:1): cliente WebSocket/AudioWorklet do app.
- [android/app/src/main/java/glauco/phone/audiospeaker/MainActivity.java](/C:/dev/audio-phone-speaker/android/app/src/main/java/glauco/phone/audiospeaker/MainActivity.java:1): inicializacao do foreground service e pedidos de permissao/excecao de bateria.
- [android/app/src/main/java/glauco/phone/audiospeaker/BackgroundAudioService.java](/C:/dev/audio-phone-speaker/android/app/src/main/java/glauco/phone/audiospeaker/BackgroundAudioService.java:1): servico nativo de background.

## Requisitos

- Node.js com `npm`
- Python 3
- Android SDK com `adb`
- Android Studio JBR 21 disponivel em `C:/Program Files/Android/Android Studio/jbr`
- Dependencias Python:

```powershell
python -m pip install -r requirements-audio-sender.txt
```

## Build do app

```powershell
npm install
npm run build
npx cap sync android
cd android
.\gradlew.bat assembleDebug
```

APK debug gerado em:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

## Execucao em modo depuracao

1. Conecte o celular por USB e autorize o ADB.
2. Instale o APK debug:

```powershell
adb install -r android\app\build\outputs\apk\debug\app-debug.apk
```

3. Rode o sender:

```powershell
python .\audio_sender.py
```

## Requisitos de background forever ativo

Para o app ficar o mais persistente possivel durante depuracao:

- O app abre um `ForegroundService` automaticamente.
- O app pede permissao de notificacao em Android 13+.
- O app pede exclusao de otimização de bateria ao abrir.
- O sender tenta adicionar o pacote na whitelist de `deviceidle`.

Se quiser forcar manualmente no aparelho conectado:

```powershell
adb shell dumpsys deviceidle whitelist +glauco.phone.audiospeaker
adb shell dumpsys deviceidle whitelist
adb reverse tcp:5000 tcp:5000
```

## Debug de streaming

Na tela do app:

- `buffer`: quantidade de audio em buffer
- `dropped`: chunks descartados por atraso
- `underflows`: frames zerados por falta de audio

Se `dropped` ou `underflows` piorarem, o sender tenta:

- refazer `adb reverse`
- manter o app aberto
- fechar a conexao atual para forcar reconexao limpa

## Observacoes

- O endereco de conexao e fixo em `127.0.0.1`.
- O fluxo depende de `adb reverse`, entao o servidor Python deve rodar no PC.
- O sender usa loopback do speaker padrao via `soundcard`.
