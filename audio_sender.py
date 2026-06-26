import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import soundcard as sc
import websockets

HOST = "127.0.0.1"
PORT = 5000
APP_ID = "glauco.phone.audiospeaker"
PROJECT_DIR = Path(__file__).resolve().parent
DEBUG_APK = PROJECT_DIR / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
DEVICE_POLL_SECONDS = 10
APP_OPEN_COOLDOWN_SECONDS = 30
PLAYBACK_RESYNC_COOLDOWN_SECONDS = 8
DROPPED_CHUNKS_RESYNC_DELTA = 2

SAMPLE_RATE = 48000
CHANNELS = 2
UNDERFLOW_FRAMES_RESYNC_DELTA = SAMPLE_RATE // 2

# 960 = 20 ms em 48 kHz. Menos overhead que enviar blocos de 5 ms.
BLOCK_FRAMES = 960

QUEUE_MAX = 24
last_open_attempt_by_device = {}


def find_adb() -> str:
    adb = shutil.which("adb")
    if adb:
        return adb

    candidates = [
        Path("C:/Android/Sdk/platform-tools/adb.exe"),
        Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return "adb"


ADB = find_adb()


def run_adb(args, *, device=None, timeout=10):
    command = [ADB]
    if device:
        command += ["-s", device]
    command += args

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def list_adb_devices():
    try:
        result = run_adb(["devices"], timeout=10)
    except FileNotFoundError:
        print("adb nao encontrado. Instale/ative Android platform-tools no PATH.")
        return []
    except subprocess.TimeoutExpired:
        print("adb devices demorou demais.")
        return []

    print(result.stdout.strip())

    devices = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
        elif len(parts) >= 2:
            print(f"Ignorando dispositivo {parts[0]} com estado {parts[1]}")

    if not devices:
        print("Nenhum celular autorizado. Conecte por USB e aceite a autorizacao ADB.")

    return devices


def setup_adb_reverse(port: int, devices):
    for device in devices:
        try:
            result = run_adb(
                ["reverse", f"tcp:{port}", f"tcp:{port}"],
                device=device,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            print(f"adb reverse demorou demais em {device}. Verifique cabo/autorizacao.")
            continue

        if result.returncode == 0:
            print(f"ADB reverse ativo em {device}: tcp:{port} -> tcp:{port}")
        else:
            print(f"Falha ao executar adb reverse em {device}")
            print("stdout:", result.stdout.strip())
            print("stderr:", result.stderr.strip())


def reset_adb_reverse(port: int, devices):
    for device in devices:
        try:
            run_adb(["reverse", "--remove", f"tcp:{port}"], device=device, timeout=5)
        except subprocess.TimeoutExpired:
            print(f"adb reverse --remove demorou demais em {device}.")

    setup_adb_reverse(port, devices)


def ensure_deviceidle_whitelist(device):
    try:
        run_adb(["shell", "dumpsys", "deviceidle", "whitelist", f"+{APP_ID}"], device=device, timeout=5)
    except subprocess.TimeoutExpired:
        print(f"deviceidle whitelist demorou demais em {device}.")


def install_debug_apk_if_needed(device):
    if not DEBUG_APK.exists():
        return

    try:
        result = run_adb(["shell", "pm", "path", APP_ID], device=device, timeout=5)
    except subprocess.TimeoutExpired:
        print(f"Nao consegui verificar se o app esta instalado em {device}.")
        return

    if result.returncode == 0 and result.stdout.strip():
        return

    print(f"App nao instalado em {device}; instalando {DEBUG_APK.name}...")
    result = run_adb(["install", "-r", str(DEBUG_APK)], device=device, timeout=90)
    if result.returncode == 0:
        print(f"APK instalado em {device}.")
    else:
        print(f"Falha ao instalar APK em {device}:")
        print(result.stdout.strip())
        print(result.stderr.strip())


def open_android_app(devices):
    for device in devices:
        open_android_app_on_device(device)


def open_android_app_on_device(device):
    install_debug_apk_if_needed(device)
    ensure_deviceidle_whitelist(device)

    try:
        result = run_adb(
            ["shell", "monkey", "-p", APP_ID, "-c", "android.intent.category.LAUNCHER", "1"],
            device=device,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        print(f"adb monkey demorou demais em {device}.")
        return

    if result.returncode == 0:
        print(f"App aberto no celular {device}.")
    else:
        print(f"Nao consegui abrir o app em {device}.")
        print(result.stdout.strip())
        print(result.stderr.strip())


def is_app_foreground(device):
    try:
        result = run_adb(
            ["shell", "dumpsys", "window"],
            device=device,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False

    if result.returncode != 0:
        return False

    output = result.stdout
    return (
        "mCurrentFocus" in output and APP_ID in output
    ) or (
        "mFocusedApp" in output and APP_ID in output
    )


def prepare_android_device():
    print("Android tools: adb devices")
    devices = list_adb_devices()
    reset_adb_reverse(PORT, devices)
    open_android_app(devices)


async def android_device_poll_loop():
    while True:
        try:
            devices = await asyncio.to_thread(list_adb_devices)
            if devices:
                await asyncio.to_thread(setup_adb_reverse, PORT, devices)

            now = asyncio.get_running_loop().time()
            for device in devices:
                app_foreground = await asyncio.to_thread(is_app_foreground, device)
                last_open_attempt = last_open_attempt_by_device.get(device, 0)

                if app_foreground:
                    continue

                if now - last_open_attempt < APP_OPEN_COOLDOWN_SECONDS:
                    continue

                last_open_attempt_by_device[device] = now
                await asyncio.to_thread(open_android_app_on_device, device)

        except Exception as e:
            print("Android polling error:", repr(e))

        await asyncio.sleep(DEVICE_POLL_SECONDS)


def float32_to_pcm16(data: np.ndarray) -> bytes:
    data = np.clip(data, -1.0, 1.0)
    return (data * 32767.0).astype("<i2", copy=False).tobytes()


async def handler(websocket, path=None):
    speaker = sc.default_speaker()
    microphone = sc.get_microphone(speaker.name, include_loopback=True)

    print("Capturing from:", speaker.name)
    print("Using loopback:", microphone.name)
    print("Client connected")

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=QUEUE_MAX)
    stop_event = threading.Event()
    last_stats = None
    last_resync = 0

    def push_frame(pcm: bytes):
        while queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        try:
            queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    def capture_thread():
        try:
            with microphone.recorder(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                blocksize=BLOCK_FRAMES,
            ) as recorder:
                while not stop_event.is_set():
                    data = recorder.record(numframes=BLOCK_FRAMES)
                    pcm = float32_to_pcm16(data)
                    loop.call_soon_threadsafe(push_frame, pcm)
        except Exception as e:
            print("Capture error:", repr(e))

    thread = threading.Thread(target=capture_thread, daemon=True)
    thread.start()

    async def resync(reason: str):
        nonlocal last_resync

        now = asyncio.get_running_loop().time()
        if now - last_resync < PLAYBACK_RESYNC_COOLDOWN_SECONDS:
            return

        last_resync = now
        print(f"Playback resync: {reason}")

        devices = await asyncio.to_thread(list_adb_devices)
        if devices:
            await asyncio.to_thread(reset_adb_reverse, PORT, devices)

        try:
            await websocket.close(code=1012, reason=reason[:120])
        except Exception as e:
            print("WebSocket close during resync failed:", repr(e))

    async def receive_stats():
        nonlocal last_stats

        async for message in websocket:
            if not isinstance(message, str):
                continue

            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue

            if payload.get("type") != "playbackStats":
                continue

            dropped = int(payload.get("droppedChunks") or 0)
            underflows = int(payload.get("underflows") or 0)
            buffered = int(payload.get("bufferedFrames") or 0)

            if last_stats is None:
                last_stats = {
                    "dropped": dropped,
                    "underflows": underflows,
                }
                continue

            dropped_delta = dropped - last_stats["dropped"]
            underflow_delta = underflows - last_stats["underflows"]
            last_stats = {
                "dropped": dropped,
                "underflows": underflows,
            }

            if dropped_delta >= DROPPED_CHUNKS_RESYNC_DELTA:
                await resync(f"dropped audio chunks increased by {dropped_delta}")
                return

            if underflow_delta >= UNDERFLOW_FRAMES_RESYNC_DELTA and buffered < BLOCK_FRAMES:
                await resync(f"audio underflow increased by {underflow_delta} frames")
                return

    async def send_audio():
        while True:
            pcm = await queue.get()
            await websocket.send(pcm)

    try:
        done, pending = await asyncio.wait(
            {
                asyncio.create_task(send_audio()),
                asyncio.create_task(receive_stats()),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        for task in done:
            task.result()

    except websockets.ConnectionClosed as e:
        print("Client disconnected:", e.code, e.reason)
    except Exception as e:
        print("Handler error:", repr(e))
    finally:
        stop_event.set()
        print("Stopped capture")


async def main():
    prepare_android_device()
    poll_task = asyncio.create_task(android_device_poll_loop())
    async with websockets.serve(
        handler,
        HOST,
        PORT,
        compression=None,
        max_queue=1,
        write_limit=65536,
        ping_interval=None,
    ):
        print(f"Server running on ws://{HOST}:{PORT}")
        try:
            await asyncio.Future()
        finally:
            poll_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
