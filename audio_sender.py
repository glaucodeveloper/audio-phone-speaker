from __future__ import annotations

import asyncio
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import warnings
from functools import partial
from pathlib import Path

import numpy as np

from phone_microphone_bridge import (
    CONTROL_PORT,
    MIC_PORT,
    MIC_RECORDING_EVENT,
    PhoneMicrophoneBridge,
)

HOST = "127.0.0.1"
APP_ID = "glauco.phone.audiospeaker"
APP_VERSION_CODE = 2
SPEAKER_DISCOVERY_PORT = 5004
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2
CHUNK_FRAMES = 480       # 10 ms transport packet
CAPTURE_FRAMES = 480     # 10 ms capture block keeps transport packets evenly paced
CHUNK_BYTES = CHUNK_FRAMES * CHANNELS * SAMPLE_WIDTH
SPEAKER_FRAME_INTERVAL_SECONDS = CHUNK_FRAMES / SAMPLE_RATE
QUEUE_MAX = 4            # absorb brief WASAPI jitter; cap backlog at ~40 ms
ADB_REVERSE_MONITOR_INTERVAL_SECONDS = 2.0

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

if IS_WINDOWS:
    import pyaudiowpatch as pyaudio
elif IS_LINUX:
    # SoundCard is used only for the PipeWire/Pulse monitor capture path.
    import soundcard as sc
else:
    raise RuntimeError(
        "audio-phone-speaker currently supports the PC bridge on Windows and Linux."
    )

MUTE_SPEAKER_DURING_MIC = os.environ.get(
    "PHONE_MIC_MUTE_SPEAKER",
    "0",
).lower() in {"1", "true", "yes", "on"}

SPEAKER_CAPTURE_DEVICE = os.environ.get(
    "PHONE_SPEAKER_CAPTURE_DEVICE",
    "",
).strip()


def _is_reserved_virtual_mic_device(name: str) -> bool:
    lowered = name.casefold()
    return (
        "vb-audio virtual cable" in lowered
        or "cable input" in lowered
        or "cable in 16ch" in lowered
    )


def choose_windows_loopback(p: "pyaudio.PyAudio") -> dict:
    loopbacks = list(p.get_loopback_device_info_generator())

    if not loopbacks:
        raise RuntimeError(
            "No WASAPI loopback devices were found. "
            "Run: python -m pyaudiowpatch"
        )

    if SPEAKER_CAPTURE_DEVICE:
        wanted = SPEAKER_CAPTURE_DEVICE.casefold()
        for device in loopbacks:
            name = str(device.get("name", ""))
            if wanted == name.casefold() or wanted in name.casefold():
                return device

        available = "\n  - ".join(str(d.get("name", "")) for d in loopbacks)
        raise RuntimeError(
            "PHONE_SPEAKER_CAPTURE_DEVICE was not found among WASAPI "
            "loopback devices.\nAvailable:\n  - " + available
        )

    try:
        default_loopback = p.get_default_wasapi_loopback()
    except Exception:
        default_loopback = None

    if default_loopback:
        name = str(default_loopback.get("name", ""))
        if not _is_reserved_virtual_mic_device(name):
            return default_loopback

    # If the Windows default output is the VB-CABLE reserved for the phone
    # microphone, capture another real render endpoint instead of creating
    # feedback through the cable.
    for device in loopbacks:
        name = str(device.get("name", ""))
        if not _is_reserved_virtual_mic_device(name):
            print(
                "Windows default output is the virtual cable reserved for "
                "the phone microphone; using WASAPI loopback:",
                name,
            )
            return device

    available = "\n  - ".join(str(d.get("name", "")) for d in loopbacks)
    raise RuntimeError(
        "Only the virtual microphone cable is available as a WASAPI loopback "
        "source.\nAvailable:\n  - " + available
    )


def _linux_speaker_by_hint():
    speakers = list(sc.all_speakers())

    if not speakers:
        raise RuntimeError("No Linux playback devices were found.")

    if SPEAKER_CAPTURE_DEVICE:
        wanted = SPEAKER_CAPTURE_DEVICE.casefold()

        for speaker in speakers:
            if speaker.name.casefold() == wanted:
                return speaker

        for speaker in speakers:
            if wanted in speaker.name.casefold():
                return speaker

        available = "\n  - ".join(s.name for s in speakers)
        raise RuntimeError(
            "PHONE_SPEAKER_CAPTURE_DEVICE was not found among Linux playback "
            "devices.\nAvailable:\n  - " + available
        )

    return sc.default_speaker()


def choose_linux_loopback() -> dict:
    speaker = _linux_speaker_by_hint()

    try:
        monitor = sc.get_microphone(
            speaker.name,
            include_loopback=True,
        )
    except Exception as error:
        raise RuntimeError(
            f"Could not open the PipeWire/Pulse monitor for {speaker.name!r}: "
            f"{error}"
        ) from error

    return {
        "speaker_name": speaker.name,
        "monitor_name": monitor.name,
    }


def find_adb() -> str:
    adb = shutil.which("adb")
    if adb:
        return adb

    if IS_WINDOWS:
        candidates = [
            Path("C:/Android/Sdk/platform-tools/adb.exe"),
            Path.home()
            / "AppData"
            / "Local"
            / "Android"
            / "Sdk"
            / "platform-tools"
            / "adb.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

    return "adb"


ADB = find_adb()


def configure_adb_reverse_once(speaker_port: int) -> None:
    try:
        result = subprocess.run(
            [ADB, "devices"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as error:
        print("ADB unavailable:", repr(error))
        return

    print(result.stdout.strip())

    devices = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    for device in devices:
        try:
            package = subprocess.run(
                [ADB, "-s", device, "shell", "dumpsys", "package", APP_ID],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as error:
            raise RuntimeError(
                f"Could not verify the Android app on {device}: {error}. "
                "Run scripts/setup_duplex.py first."
            ) from error

        match = re.search(r"\bversionCode=(\d+)\b", package.stdout)
        if package.returncode != 0 or not match:
            raise RuntimeError(
                f"{APP_ID} is not installed on {device}. "
                "Run scripts/setup_duplex.py to build and install the app."
            )

        installed_version_code = int(match.group(1))
        if installed_version_code < APP_VERSION_CODE:
            raise RuntimeError(
                f"The Android app on {device} is outdated "
                f"(versionCode {installed_version_code}; "
                f"need {APP_VERSION_CODE}). Run scripts/setup_duplex.py "
                "to rebuild and install the matching app."
            )

    for device in devices:
        for legacy_port in (5000, 5001):
            try:
                subprocess.run(
                    [ADB, "-s", device, "reverse", "--remove", f"tcp:{legacy_port}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except Exception as error:
                print(
                    f"Could not remove legacy ADB reverse {device}:"
                    f"{legacy_port}: {error}",
                    flush=True,
                )

        for port in (SPEAKER_DISCOVERY_PORT, speaker_port, MIC_PORT):
            try:
                reverse = subprocess.run(
                    [
                        ADB,
                        "-s",
                        device,
                        "reverse",
                        f"tcp:{port}",
                        f"tcp:{port}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if reverse.returncode == 0:
                    print(
                        f"ADB reverse {device}: "
                        f"tcp:{port} -> tcp:{port}"
                    )
                else:
                    print(
                        f"ADB reverse failed {device}:{port}: "
                        f"{reverse.stderr.strip()}"
                    )
            except Exception as error:
                print(
                    f"ADB reverse failed {device}:{port}:",
                    repr(error),
                )

    print("Initial ADB reverse setup complete.")


def monitor_adb_reverse(
    stop_event: threading.Event,
    speaker_port: int,
) -> None:
    """Restore reverse tunnels automatically after USB/ADB reconnects."""
    states = {}
    ports = (SPEAKER_DISCOVERY_PORT, speaker_port, MIC_PORT)
    last_monitor_status = None

    while not stop_event.is_set():
        try:
            result = subprocess.run(
                [ADB, "devices"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "adb devices failed")
            devices = {
                parts[0]
                for line in result.stdout.splitlines()[1:]
                if len(parts := line.split()) >= 2 and parts[1] == "device"
            }
        except Exception as error:
            status = f"unavailable: {error}"
            if status != last_monitor_status:
                print(f"ADB reconnect monitor {status}", flush=True)
                last_monitor_status = status
            stop_event.wait(ADB_REVERSE_MONITOR_INTERVAL_SECONDS)
            continue

        status = "online" if devices else "waiting for an authorized USB device"
        if status != last_monitor_status:
            print(f"ADB reconnect monitor: {status}", flush=True)
            last_monitor_status = status

        for key in list(states):
            if key[0] not in devices:
                del states[key]

        for device in devices:
            try:
                reverse_list = subprocess.run(
                    [ADB, "-s", device, "reverse", "--list"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if reverse_list.returncode != 0:
                    raise RuntimeError(
                        reverse_list.stderr.strip()
                        or "adb reverse --list failed"
                    )
                existing_mappings = {
                    (parts[-2], parts[-1])
                    for line in reverse_list.stdout.splitlines()
                    if len(parts := line.split()) >= 3
                }
            except Exception as error:
                for port in ports:
                    key = (device, port)
                    if states.get(key) is not False:
                        print(
                            f"ADB reverse status unavailable {device}: "
                            f"{error}",
                            flush=True,
                        )
                    states[key] = False
                continue

            for port in ports:
                key = (device, port)
                endpoint = f"tcp:{port}"
                if (endpoint, endpoint) in existing_mappings:
                    active = True
                    detail = ""
                else:
                    try:
                        reverse = subprocess.run(
                            [
                                ADB,
                                "-s",
                                device,
                                "reverse",
                                endpoint,
                                endpoint,
                            ],
                            capture_output=True,
                            text=True,
                            timeout=3,
                        )
                        active = reverse.returncode == 0
                        detail = reverse.stderr.strip()
                    except Exception as error:
                        active = False
                        detail = repr(error)

                if states.get(key) != active:
                    if active:
                        print(
                            f"ADB reverse restored {device}: "
                            f"tcp:{port} -> tcp:{port}",
                            flush=True,
                        )
                    else:
                        print(
                            f"ADB reverse unavailable {device}:{port}: "
                            f"{detail}",
                            flush=True,
                        )
                states[key] = active

        stop_event.wait(ADB_REVERSE_MONITOR_INTERVAL_SECONDS)


def normalize_channels(data: np.ndarray) -> np.ndarray:
    data = np.asarray(data, dtype=np.float32)

    if data.ndim == 1:
        data = data.reshape(-1, 1)

    if data.shape[1] == 1:
        data = np.repeat(data, CHANNELS, axis=1)
    elif data.shape[1] > CHANNELS:
        data = data[:, :CHANNELS]

    return data


def pcm16(data: np.ndarray) -> bytes:
    data = np.clip(data, -1.0, 1.0)
    return (
        data * 32767.0
    ).astype("<i2", copy=False).tobytes()


class SpeakerCapture:
    def __init__(self) -> None:
        self.queue = queue.Queue(maxsize=QUEUE_MAX)
        self.stop_event = threading.Event()
        self.thread = None
        self.dropped_chunks = 0
        self.latest_chunk_lock = threading.Lock()
        self.latest_chunk = None
        self.latest_chunk_at = 0.0
        self.loop = None
        self.queue_event = None

        self.device_index = None
        self.device_name = None

        self.linux_speaker_name = None
        self.linux_monitor_name = None

    def configure_windows(self, device: dict) -> None:
        self.device_index = int(device["index"])
        self.device_name = str(device.get("name", "unknown"))

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue_event = asyncio.Event()

    def notify_queue(self) -> None:
        loop = self.loop
        event = self.queue_event
        if loop is not None and event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    async def get_next_chunk(self, writer: asyncio.StreamWriter):
        event = self.queue_event
        if event is None:
            raise RuntimeError("Speaker queue event loop is not initialized.")

        while not writer.is_closing():
            try:
                return self.queue.get_nowait()
            except queue.Empty:
                event.clear()

            # Check again after clearing to avoid losing a producer wakeup.
            try:
                return self.queue.get_nowait()
            except queue.Empty:
                try:
                    await asyncio.wait_for(
                        event.wait(),
                        timeout=CHUNK_FRAMES / SAMPLE_RATE,
                    )
                except asyncio.TimeoutError:
                    if writer.is_closing():
                        return None
                    # Keep AudioTrack fed on a fixed 10 ms clock while WASAPI
                    # has no frame ready; real captured PCM resumes immediately.
                    return bytes(CHUNK_BYTES)

        return None

    def configure_linux(self, device: dict) -> None:
        self.linux_speaker_name = str(device["speaker_name"])
        self.linux_monitor_name = str(device["monitor_name"])

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return

        self.thread = threading.Thread(
            target=self._run,
            name="speaker-loopback-capture",
            daemon=True,
        )
        self.thread.start()

    def clear(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return

    def put_latest(self, chunk: bytes) -> None:
        if len(chunk) != CHUNK_BYTES:
            return

        with self.latest_chunk_lock:
            self.latest_chunk = chunk
            self.latest_chunk_at = time.monotonic()

        try:
            self.queue.put_nowait(chunk)
            self.notify_queue()
            return
        except queue.Full:
            pass

        try:
            self.queue.get_nowait()
            self.dropped_chunks += 1
        except queue.Empty:
            pass

        try:
            self.queue.put_nowait(chunk)
            self.notify_queue()
        except queue.Full:
            pass

    def seed_latest(self, max_age_seconds: float = 0.05) -> bool:
        with self.latest_chunk_lock:
            chunk = self.latest_chunk
            age = time.monotonic() - self.latest_chunk_at

        if chunk is None or age > max_age_seconds:
            self.clear()
            return False

        self.clear()
        try:
            self.queue.put_nowait(chunk)
        except queue.Full:
            return False
        self.notify_queue()
        return True

    def _packetize_stereo_i16(self, stereo: np.ndarray) -> None:
        total = len(stereo)
        full = (total // CHUNK_FRAMES) * CHUNK_FRAMES

        for offset in range(0, full, CHUNK_FRAMES):
            chunk = stereo[
                offset:offset + CHUNK_FRAMES
            ].astype("<i2", copy=False).tobytes()
            self.put_latest(chunk)

    def _run_windows(self) -> None:
        if self.device_index is None:
            raise RuntimeError(
                "WASAPI capture device was not configured."
            )

        with pyaudio.PyAudio() as p:
            device = p.get_device_info_by_index(
                self.device_index
            )
            device_name = str(
                self.device_name
                or device.get("name", "unknown")
            )
            device_rate = int(
                round(
                    float(
                        device.get(
                            "defaultSampleRate",
                            SAMPLE_RATE,
                        )
                    )
                )
            )
            device_channels = int(
                device.get(
                    "maxInputChannels",
                    CHANNELS,
                )
            )
            capture_channels = max(
                1,
                min(device_channels, CHANNELS),
            )

            print(
                "PC -> phone WASAPI loopback:",
                device_name,
            )
            print(
                "WASAPI source format:",
                f"{device_rate} Hz / {capture_channels} ch",
            )

            with p.open(
                format=pyaudio.paInt16,
                channels=capture_channels,
                rate=SAMPLE_RATE,
                frames_per_buffer=CAPTURE_FRAMES,
                input=True,
                input_device_index=self.device_index,
            ) as stream:
                while not self.stop_event.is_set():
                    raw = stream.read(
                        CAPTURE_FRAMES,
                        exception_on_overflow=False,
                    )

                    samples = np.frombuffer(
                        raw,
                        dtype="<i2",
                    )

                    if capture_channels == 1:
                        mono = samples.reshape(-1, 1)
                        stereo = np.repeat(
                            mono,
                            2,
                            axis=1,
                        )
                    else:
                        frames = samples.reshape(
                            -1,
                            capture_channels,
                        )
                        stereo = frames[:, :2]

                    self._packetize_stereo_i16(
                        stereo,
                    )

    def _run_linux(self) -> None:
        if not self.linux_speaker_name:
            raise RuntimeError(
                "Linux PipeWire/Pulse capture device was not configured."
            )

        # SoundCard opens a monitor source for the selected render endpoint.
        monitor = sc.get_microphone(
            self.linux_speaker_name,
            include_loopback=True,
        )

        print(
            "PC -> phone PipeWire/Pulse loopback:",
            self.linux_speaker_name,
        )
        print(
            "Linux monitor source:",
            monitor.name,
        )

        # Pulse backends may report a discontinuity after suspend/resume.
        warnings.filterwarnings(
            "once",
            message="data discontinuity in recording",
        )

        with monitor.recorder(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=CAPTURE_FRAMES,
        ) as recorder:
            while not self.stop_event.is_set():
                data = recorder.record(
                    numframes=CAPTURE_FRAMES
                )
                data = normalize_channels(data)
                raw = pcm16(data)
                stereo = np.frombuffer(
                    raw,
                    dtype="<i2",
                ).reshape(-1, CHANNELS)
                self._packetize_stereo_i16(stereo)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if IS_WINDOWS:
                    self._run_windows()
                else:
                    self._run_linux()
            except Exception as error:
                if not self.stop_event.is_set():
                    backend = (
                        "WASAPI"
                        if IS_WINDOWS
                        else "PipeWire/Pulse"
                    )
                    print(
                        f"{backend} capture restart:",
                        repr(error),
                    )
                    time.sleep(0.5)


capture = SpeakerCapture()
active_speaker_writer = None
active_writer_lock = asyncio.Lock()
last_speaker_queue_report_at = 0.0


async def handle_speaker(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    global active_speaker_writer, last_speaker_queue_report_at

    peer = writer.get_extra_info("peername")

    # Native Android bridge identifies itself with SPK1. This also prevents
    # an old WebView/WebSocket client from taking ownership of the TCP stream.
    try:
        hello = await asyncio.wait_for(
            reader.readexactly(4),
            timeout=2.0,
        )
    except (
        asyncio.TimeoutError,
        asyncio.IncompleteReadError,
        ConnectionError,
        OSError,
    ) as error:
        print(
            "Rejected speaker client",
            peer,
            "without SPK1:",
            repr(error),
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    if hello != b"SPK1":
        print(
            "Rejected non-native speaker client",
            peer,
            "prefix=",
            repr(hello),
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    async with active_writer_lock:
        previous = active_speaker_writer
        active_speaker_writer = writer
        if (
            previous is not None
            and previous is not writer
        ):
            previous.close()
            capture.notify_queue()

    writer.transport.set_write_buffer_limits(
        high=CHUNK_BYTES,
        low=0,
    )

    if capture.seed_latest():
        print("Native phone speaker reconnect: resent latest PCM packet")
    print(
        "Native phone speaker connected:",
        peer,
    )

    loop = asyncio.get_running_loop()
    next_frame_at = loop.time()
    try:
        while True:
            chunk = await capture.get_next_chunk(writer)
            if chunk is None:
                break

            # WASAPI can return several captured blocks together after a
            # scheduler pause. Keep the TCP stream at the 10 ms PCM cadence
            # instead of flushing that burst into Android's tiny low-latency
            # AudioTrack buffer.
            delay = next_frame_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

            now = loop.time()
            if now - next_frame_at > 0.030:
                # Do not catch up by sending a burst after a real stall. The
                # capture queue already retains only the newest audio.
                next_frame_at = now

            now = time.monotonic()
            if now - last_speaker_queue_report_at >= 5.0:
                last_speaker_queue_report_at = now
                queued_frames = capture.queue.qsize()
                print(
                    "PC audio queue:",
                    f"{queued_frames}/{QUEUE_MAX} frames",
                    f"({queued_frames * CHUNK_FRAMES * 1000 // SAMPLE_RATE} ms)",
                    f"dropped={capture.dropped_chunks}",
                )

            if (
                MUTE_SPEAKER_DURING_MIC
                and MIC_RECORDING_EVENT.is_set()
            ):
                chunk = bytes(len(chunk))

            writer.write(
                struct.pack(">I", len(chunk))
                + chunk
            )
            await writer.drain()
            next_frame_at += SPEAKER_FRAME_INTERVAL_SECONDS

    except (
        ConnectionError,
        BrokenPipeError,
        OSError,
    ) as error:
        print(
            "Native phone speaker disconnected:",
            repr(error),
        )

    finally:
        async with active_writer_lock:
            if active_speaker_writer is writer:
                active_speaker_writer = None

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def handle_speaker_discovery(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    speaker_port: int,
) -> None:
    """Return the current PCM port to the native Android bridge."""
    try:
        request = await asyncio.wait_for(reader.readline(), timeout=2.0)
        if request.rstrip(b"\r\n") == b"SPK?":
            writer.write(f"SPK1 {speaker_port}\n".encode("ascii"))
        else:
            writer.write(b"ERR\n")
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    if IS_WINDOWS:
        with pyaudio.PyAudio() as p:
            selected = choose_windows_loopback(p)
            print(
                "Selected PC audio loopback:",
                selected.get("name", "unknown"),
            )
            capture.configure_windows(selected)
        speaker_backend = "Windows WASAPI loopback"

    else:
        selected = choose_linux_loopback()
        print(
            "Selected PC audio output:",
            selected["speaker_name"],
        )
        capture.configure_linux(selected)
        speaker_backend = "Linux PipeWire/Pulse monitor"

    capture.bind_event_loop(asyncio.get_running_loop())
    speaker_server = await asyncio.start_server(
        handle_speaker,
        HOST,
        0,
    )
    speaker_port = int(speaker_server.sockets[0].getsockname()[1])
    discovery_server = await asyncio.start_server(
        partial(handle_speaker_discovery, speaker_port=speaker_port),
        HOST,
        SPEAKER_DISCOVERY_PORT,
    )

    microphone_bridge = PhoneMicrophoneBridge()
    adb_monitor_stop = threading.Event()
    adb_monitor = None
    try:
        await microphone_bridge.start()
        try:
            await asyncio.to_thread(configure_adb_reverse_once, speaker_port)
        except Exception as error:
            print(
                "ADB/app startup check failed; audio servers remain active "
                f"while USB recovery continues: {error}",
                flush=True,
            )

        adb_monitor = threading.Thread(
            target=monitor_adb_reverse,
            args=(adb_monitor_stop, speaker_port),
            name="adb-reverse-monitor",
            daemon=True,
        )
        adb_monitor.start()
        capture.start()

        print(
            f"Native phone speaker transport: tcp://{HOST}:{speaker_port}"
        )
        print(
            f"Speaker port discovery: tcp://{HOST}:{SPEAKER_DISCOVERY_PORT}"
        )
        print(
            "Speaker: PCM s16le / 48 kHz / "
            "stereo / 10 ms native TCP"
        )
        print(
            f"Speaker capture: {speaker_backend} "
            "-> 10 ms TCP packets"
        )
        print(
            "Phone microphone: PCM s16le / "
            "48 kHz / mono / 10 ms chunks"
        )

        if IS_WINDOWS:
            print(
                "Phone -> PC: mic 48 kHz -> tcp:5002 -> "
                "CABLE Input -> CABLE Output -> browser"
            )
        else:
            print(
                "Phone -> PC: mic 48 kHz -> tcp:5002 -> "
                "PipeWire/Pulse source -> browser"
            )

        print(
            "Open the Android app manually. "
            "It discovers the speaker port and reconnects by itself."
        )

        async with speaker_server, discovery_server:
            await speaker_server.serve_forever()
    finally:
        adb_monitor_stop.set()
        capture.stop_event.set()
        speaker_server.close()
        discovery_server.close()
        await speaker_server.wait_closed()
        await discovery_server.wait_closed()
        await microphone_bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        capture.stop_event.set()
