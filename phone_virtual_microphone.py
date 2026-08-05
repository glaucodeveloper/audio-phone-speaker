from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class NullVirtualMicrophone:
    enabled = False
    source_name = ""

    def __init__(self, reason: str = "disabled") -> None:
        self.reason = reason

    def start(self) -> None:
        return

    def write(self, payload: bytes) -> None:
        del payload

    def stop(self) -> None:
        return

    def status(self) -> dict:
        return {"enabled": False, "backend": "none", "reason": self.reason}


class PipeWireVirtualMicrophone:
    """Expose PCM s16le/16 kHz/mono as a PipeWire/PulseAudio source."""

    def __init__(self) -> None:
        self.enabled = True
        self.source_name = os.environ.get("PHONE_MIC_SOURCE_NAME", "glauco_phone_mic")
        self.description = os.environ.get(
            "PHONE_MIC_DESCRIPTION",
            os.environ.get("PHONE_MIC_SOURCE_DESCRIPTION", "Glauco Phone Microphone"),
        )
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/tmp/glauco-phone-mic-{os.getuid()}")
        )
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.fifo_path = runtime_dir / "glauco-phone-mic.pcm"
        self.module_id: str | None = None
        self.fd: int | None = None
        self.lock = threading.Lock()
        self.dropped_bytes = 0
        self.written_bytes = 0
        self.set_default = _flag("PHONE_MIC_SET_DEFAULT", True)
        self.error = ""

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), check=check, capture_output=True, text=True)

    def _remove_stale_modules(self) -> None:
        result = self._run("pactl", "list", "modules", "short", check=False)
        if result.returncode != 0:
            return
        marker = f"source_name={self.source_name}"
        for line in result.stdout.splitlines():
            if marker not in line:
                continue
            module_id = line.split("\t", 1)[0].split(None, 1)[0]
            if module_id:
                self._run("pactl", "unload-module", module_id, check=False)

    def start(self) -> None:
        if self.fd is not None:
            return
        if not shutil.which("pactl"):
            raise RuntimeError("pactl is unavailable; install pipewire-pulse or PulseAudio tools")

        self._remove_stale_modules()
        self.fifo_path.unlink(missing_ok=True)
        os.mkfifo(self.fifo_path, 0o600)

        # O_RDWR avoids blocking while module-pipe-source opens the read side.
        self.fd = os.open(self.fifo_path, os.O_RDWR | os.O_NONBLOCK)
        description_arg = f'source_properties=device.description="{self.description}"'
        result = self._run(
            "pactl",
            "load-module",
            "module-pipe-source",
            f"file={self.fifo_path}",
            f"source_name={self.source_name}",
            "format=s16le",
            "rate=16000",
            "channels=1",
            "channel_map=mono",
            description_arg,
            check=False,
        )
        if result.returncode != 0:
            os.close(self.fd)
            self.fd = None
            self.fifo_path.unlink(missing_ok=True)
            raise RuntimeError(
                "failed to create PipeWire source: "
                + (result.stderr.strip() or result.stdout.strip())
            )

        self.module_id = result.stdout.strip()
        if self.set_default:
            self._run("pactl", "set-default-source", self.source_name, check=False)

        print(
            f"System microphone ready: {self.description} "
            f"({self.source_name}, 16000 Hz mono)"
        )

    def write(self, payload: bytes) -> None:
        fd = self.fd
        if fd is None or not payload:
            return
        with self.lock:
            view = memoryview(payload)
            while view:
                try:
                    count = os.write(fd, view)
                except (BlockingIOError, OSError):
                    self.dropped_bytes += len(view)
                    return
                if count <= 0:
                    self.dropped_bytes += len(view)
                    return
                self.written_bytes += count
                view = view[count:]

    def stop(self) -> None:
        with self.lock:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
        if self.module_id:
            self._run("pactl", "unload-module", self.module_id, check=False)
            self.module_id = None
        self.fifo_path.unlink(missing_ok=True)

    def status(self) -> dict:
        return {
            "enabled": self.fd is not None,
            "backend": "pipewire-pulse-module-pipe-source",
            "sourceName": self.source_name,
            "description": self.description,
            "fifo": str(self.fifo_path),
            "writtenBytes": self.written_bytes,
            "droppedBytes": self.dropped_bytes,
            "defaultSource": self.set_default,
            "error": self.error,
        }


class PortAudioVirtualMicrophone:
    """Write phone PCM to a virtual playback cable on Windows or macOS.

    On Windows with VB-CABLE the playback endpoint is CABLE Input and programs
    select CABLE Output as their microphone. On macOS the endpoint can be
    BlackHole or another virtual audio device.
    """

    def __init__(self) -> None:
        import sounddevice as sd

        self.sd = sd
        self.enabled = True
        self.source_name = os.environ.get("PHONE_MIC_SOURCE_NAME", "glauco_phone_mic")
        self.description = os.environ.get("PHONE_MIC_DESCRIPTION", "Glauco Phone Microphone")
        self.input_rate = int(os.environ.get("PHONE_MIC_SAMPLE_RATE", "16000"))
        self.output_rate = int(os.environ.get("PHONE_MIC_OUTPUT_RATE", "48000"))
        self.channels = int(os.environ.get("PHONE_MIC_CHANNELS", "1"))
        self.lock = threading.Lock()
        self.stream = None
        self.written_bytes = 0
        self.dropped_bytes = 0
        self.device_index, self.device_name = self._resolve_device()

    def _candidate_names(self) -> list[str]:
        explicit = os.environ.get("PHONE_MIC_PLAYBACK_DEVICE", "").strip()
        if explicit:
            return [explicit]
        if sys.platform == "win32":
            return ["CABLE Input", "VB-Audio Virtual Cable", "VoiceMeeter Input", "Virtual Cable"]
        if sys.platform == "darwin":
            return ["BlackHole 2ch", "BlackHole", "Loopback Audio"]
        return []

    def _resolve_device(self) -> tuple[int, str]:
        devices = self.sd.query_devices()
        explicit = os.environ.get("PHONE_MIC_PLAYBACK_DEVICE", "").strip()
        if explicit.isdigit():
            index = int(explicit)
            device = devices[index]
            if int(device["max_output_channels"]) < self.channels:
                raise RuntimeError(f"audio device {index} has no output channel")
            return index, str(device["name"])

        for candidate in self._candidate_names():
            needle = candidate.casefold()
            for index, device in enumerate(devices):
                if int(device["max_output_channels"]) < self.channels:
                    continue
                name = str(device["name"])
                if needle in name.casefold():
                    return index, name

        outputs = [
            str(device["name"])
            for device in devices
            if int(device["max_output_channels"]) >= self.channels
        ]
        expected = explicit or ("CABLE Input" if sys.platform == "win32" else "BlackHole")
        raise RuntimeError(
            f"virtual playback endpoint matching {expected!r} not found; outputs: {outputs}"
        )

    def start(self) -> None:
        if self.stream is not None:
            return
        self.stream = self.sd.RawOutputStream(
            samplerate=self.output_rate,
            channels=self.channels,
            dtype="int16",
            device=self.device_index,
            blocksize=0,
            latency="low",
        )
        self.stream.start()
        print(
            f"System microphone cable ready: {self.device_name} "
            f"({self.output_rate} Hz, {self.channels} channel)"
        )
        if sys.platform == "win32":
            print("Select CABLE Output (or the paired recording endpoint) as microphone in Windows apps.")

    def _resample(self, payload: bytes) -> bytes:
        if self.output_rate == self.input_rate:
            return payload
        import numpy as np

        samples = np.frombuffer(payload, dtype="<i2")
        if samples.size == 0:
            return b""
        target_size = max(1, round(samples.size * self.output_rate / self.input_rate))
        source_positions = np.arange(samples.size, dtype=np.float64)
        target_positions = np.linspace(0, samples.size - 1, target_size, dtype=np.float64)
        return np.interp(target_positions, source_positions, samples).astype("<i2").tobytes()

    def write(self, payload: bytes) -> None:
        if self.stream is None or not payload:
            return
        converted = self._resample(payload)
        with self.lock:
            try:
                self.stream.write(converted)
                self.written_bytes += len(payload)
            except Exception:
                self.dropped_bytes += len(payload)

    def stop(self) -> None:
        with self.lock:
            if self.stream is None:
                return
            try:
                self.stream.stop()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def status(self) -> dict:
        return {
            "enabled": self.stream is not None,
            "backend": "portaudio-virtual-cable",
            "sourceName": self.source_name,
            "description": self.description,
            "deviceIndex": self.device_index,
            "deviceName": self.device_name,
            "inputRate": self.input_rate,
            "outputRate": self.output_rate,
            "writtenBytes": self.written_bytes,
            "droppedBytes": self.dropped_bytes,
        }


def create_system_microphone():
    if not _flag("PHONE_MIC_SYSTEM_SOURCE", True):
        return NullVirtualMicrophone("disabled by PHONE_MIC_SYSTEM_SOURCE")

    if sys.platform.startswith("linux"):
        if shutil.which("pactl"):
            return PipeWireVirtualMicrophone()
        return NullVirtualMicrophone("pactl not found")

    if sys.platform in {"win32", "darwin"}:
        try:
            return PortAudioVirtualMicrophone()
        except Exception as error:
            print("Virtual microphone unavailable:", error)
            if sys.platform == "win32":
                print("Install VB-CABLE or VoiceMeeter and set PHONE_MIC_PLAYBACK_DEVICE.")
            else:
                print("Install BlackHole and set PHONE_MIC_PLAYBACK_DEVICE.")
            return NullVirtualMicrophone(str(error))

    return NullVirtualMicrophone(f"unsupported platform: {sys.platform}")
