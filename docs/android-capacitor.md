# Aplicativo Android Capacitor

O projeto Android usa **Capacitor** como shell de interface e integração de ciclo de vida. O caminho de áudio, porém, é nativo em Java.

A separação é intencional:

```text
Capacitor / WebView
  └─ interface, permissões e comando keepAlive
       ↓
BackgroundAudioPlugin
       ↓
BackgroundAudioService
       ├─ PhoneSpeakerBridge → AudioTrack
       └─ PhoneMicrophoneBridge → AudioRecord
```

O WebView não abre WebSocket, não usa AudioWorklet e não reproduz PCM. Isso evita suspensão do áudio quando o WebView perde foco ou quando o Android limita execução em background.

## Componentes

### `src/js/capacitor-welcome.js`

É a interface do aplicativo.

Ela:

- apresenta o estado e os formatos de áudio;
- chama `BackgroundAudio.keepAlive()`;
- reativa o foreground service ao retornar ao app;
- esconde o SplashScreen;
- não participa do transporte PCM.

### `BackgroundAudioPlugin.java`

Plugin Capacitor registrado como:

```java
@CapacitorPlugin(name = "BackgroundAudio")
```

A operação `keepAlive()` inicia `BackgroundAudioService` com a ação duplex.

`release()` não derruba os bridges. O serviço foi desenhado para permanecer disponível enquanto o usuário usa o telefone como dispositivo de áudio.

### `BackgroundAudioService.java`

É um Android foreground service.

Mantém:

- wake lock parcial;
- notificação persistente;
- `PhoneSpeakerBridge`;
- `PhoneMicrophoneBridge`.

O manifesto declara:

```xml
android:foregroundServiceType="mediaPlayback|microphone"
```

O serviço continua dono dos bridges quando o WebView pausa.

## Speaker: PC → Android

`PhoneSpeakerBridge.java` conecta a:

```text
127.0.0.1:5001
```

Como a porta existe no PC, o comando:

```bash
adb reverse tcp:5001 tcp:5001
```

faz com que `127.0.0.1:5001` visto pelo Android chegue ao processo Python no computador.

### Handshake

Ao conectar, o Android envia:

```text
SPK1
```

O servidor Python só aceita o cliente que apresenta esse prefixo. Isso impede um frontend WebSocket antigo de tomar a conexão do speaker.

### Frame

Depois do handshake:

```text
uint32_be payload_length
PCM payload
```

Formato:

```text
48.000 Hz
stereo
PCM signed 16-bit little-endian
20 ms por pacote
```

O Java usa `AudioTrack`.

O bridge mantém um prebuffer curto antes de iniciar o playback e, quando a API Android permite, solicita `PERFORMANCE_MODE_LOW_LATENCY`.

## Microfone: Android → PC

`PhoneMicrophoneBridge.java` usa `AudioRecord`.

Formato:

```text
48.000 Hz
mono
PCM signed 16-bit little-endian
20 ms por bloco
```

Conexão:

```text
127.0.0.1:5002
```

ADB:

```bash
adb reverse tcp:5002 tcp:5002
```

### Frame do mic

```text
uint32_be frame_length
uint8 frame_type
payload
```

Tipos:

```text
1 = JSON de controle/status
2 = PCM
```

No início o Android envia um `hello` com formato e taxa. O bridge Python responde `startMic`, mantendo `AudioRecord` ativo continuamente para o microfone virtual do host.

## Permissões Android

O app precisa de:

```xml
android.permission.INTERNET
android.permission.RECORD_AUDIO
android.permission.WAKE_LOCK
android.permission.FOREGROUND_SERVICE
android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK
android.permission.FOREGROUND_SERVICE_MICROPHONE
android.permission.POST_NOTIFICATIONS
```

`MainActivity.java` solicita a permissão de microfone em runtime e inicia o foreground service assim que a permissão está disponível.

## Reconexão

Os bridges Java têm loops de reconexão.

Portanto:

- o processo Python pode ser reiniciado;
- `adb reverse` pode ser recriado;
- o app não precisa ser relançado a cada desconexão;
- quando as portas voltam a existir, os bridges se conectam novamente.

O processo Python não fica executando polling para abrir o aplicativo repetidamente.

## Build

Na raiz:

```bash
npm install
npm run build
npx cap sync android
```

Depois:

```bash
cd android
./gradlew assembleDebug
```

Windows:

```powershell
cd android
.\gradlew.bat assembleDebug
```

APK:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

## Script completo

O script multiplataforma é:

```text
scripts/setup_duplex.py
```

Uso:

```bash
python scripts/setup_duplex.py
```

Ele instala dependências Python, executa Vite + Capacitor, compila o APK, instala pelo ADB e configura as duas portas reverse.

Se o APK já estiver instalado:

```bash
python scripts/setup_duplex.py \
  --skip-android-build \
  --skip-apk-install
```

Isso é útil para preparar somente o runtime Python.

## Desenvolvimento da interface

O diretório web é:

```text
src/
```

Vite gera:

```text
dist/
```

O Capacitor copia o `dist` para:

```text
android/app/src/main/assets/public/
```

através de:

```bash
npx cap sync android
```

Alterações no HTML/CSS/JS precisam passar por `npm run build` e `npx cap sync android` antes do build Android.

## Por que o áudio não usa o WebView

A versão anterior recebia o áudio pelo WebSocket do WebView e reproduzia por `AudioWorklet`.

Esse caminho adicionava três problemas:

1. o WebView podia suspender o `AudioContext`;
2. a conexão concorria com o lifecycle da interface;
3. o buffering ficava submetido ao scheduler JavaScript.

Na arquitetura atual:

```text
PC → TCP → Java Socket → AudioTrack

AudioRecord → Java Socket → TCP → PC
```

A interface pode ser pausada sem parar o caminho de áudio.
