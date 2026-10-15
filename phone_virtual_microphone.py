from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class NullVirtualMicrophone:
    enabled = False
    source_name = ""
    sample_rate = 0

    def start(self, sample_rate=None) -> None:
        return

    def ensure_rate(self, sample_rate: int) -> None:
        return

    def write(self, payload: bytes) -> None:
        return

    def stop(self) -> None:
        return

    def status(self) -> dict:
        return {
            "enabled": False,
            "backend": "none",
            "sampleRate": 0,
        }


class PipeWireVirtualMicrophone:
    """
    Fonte PipeWire/Pulse que conserva exatamente a frequência
    recebida pelo celular.

    Nenhum upsample ou downsample acontece aqui.
    """

    def __init__(self) -> None:
        self.enabled = True

        self.source_name = os.environ.get(
            "PHONE_MIC_SOURCE_NAME",
            "glauco_phone_mic",
        )

        self.description = (
            os.environ.get(
                "PHONE_MIC_DESCRIPTION",
            )
            or
            os.environ.get(
                "PHONE_MIC_SOURCE_DESCRIPTION",
            )
            or
            "Glauco Phone Microphone"
        )

        runtime_dir = Path(
            os.environ.get(
                "XDG_RUNTIME_DIR",
                f"/tmp/glauco-phone-mic-{os.getuid()}",
            )
        )

        runtime_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.fifo_path = (
            runtime_dir
            /
            "glauco-phone-mic.pcm"
        )

        self.module_id: str | None = None
        self.fd: int | None = None

        self.lock = threading.RLock()

        self.sample_rate = 0

        self.dropped_bytes = 0
        self.written_bytes = 0

        self.set_default = _flag(
            "PHONE_MIC_SET_DEFAULT",
            True,
        )


    def _run(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:

        return subprocess.run(
            list(args),
            check=check,
            capture_output=True,
            text=True,
        )


    def _remove_stale_modules(self) -> None:
        result = self._run(
            "pactl",
            "list",
            "modules",
            "short",
            check=False,
        )

        if result.returncode != 0:
            return

        marker = (
            f"source_name={self.source_name}"
        )

        for line in result.stdout.splitlines():
            if marker not in line:
                continue

            fields = line.split()

            if not fields:
                continue

            self._run(
                "pactl",
                "unload-module",
                fields[0],
                check=False,
            )


    def _stop_unlocked(self) -> None:
        if self.fd is not None:
            try:
                os.close(
                    self.fd,
                )
            except OSError:
                pass

            self.fd = None

        if self.module_id:
            self._run(
                "pactl",
                "unload-module",
                self.module_id,
                check=False,
            )

            self.module_id = None

        try:
            self.fifo_path.unlink()
        except FileNotFoundError:
            pass

        self.sample_rate = 0


    def start(
        self,
        sample_rate=None,
    ) -> None:

        if sample_rate is None:
            try:
                sample_rate = int(
                    os.environ.get(
                        "PHONE_MIC_SAMPLE_RATE",
                        "0",
                    )
                )
            except ValueError:
                sample_rate = 0

        if sample_rate:
            self.ensure_rate(
                int(sample_rate),
            )


    def ensure_rate(
        self,
        sample_rate: int,
    ) -> None:

        rate = int(sample_rate)

        if not 8000 <= rate <= 384000:
            raise RuntimeError(
                f"sample rate inválido: {rate}"
            )

        with self.lock:
            if (
                self.fd is not None
                and
                self.sample_rate == rate
            ):
                return

            self._stop_unlocked()

            self._remove_stale_modules()

            try:
                self.fifo_path.unlink()
            except FileNotFoundError:
                pass

            os.mkfifo(
                self.fifo_path,
                0o600,
            )

            # O_RDWR evita esperar a ponta leitora.
            self.fd = os.open(
                self.fifo_path,
                os.O_RDWR | os.O_NONBLOCK,
            )

            description_arg = (
                "source_properties="
                f'device.description="{self.description}"'
            )

            result = self._run(
                "pactl",
                "load-module",
                "module-pipe-source",

                f"file={self.fifo_path}",
                f"source_name={self.source_name}",

                "format=s16le",
                f"rate={rate}",
                "channels=1",
                "channel_map=mono",

                description_arg,

                check=False,
            )

            if result.returncode != 0:
                error = (
                    result.stderr.strip()
                    or
                    result.stdout.strip()
                    or
                    "erro desconhecido"
                )

                self._stop_unlocked()

                raise RuntimeError(
                    "Falha ao criar fonte PipeWire "
                    f"em {rate} Hz: {error}"
                )

            self.module_id = (
                result.stdout.strip()
            )

            self.sample_rate = rate

            if self.set_default:
                self._run(
                    "pactl",
                    "set-default-source",
                    self.source_name,
                    check=False,
                )

            print(
                "[Glauco Phone Mic] "
                f"PipeWire={rate} Hz mono",
                flush=True,
            )


    def write(
        self,
        payload: bytes,
    ) -> None:

        if not payload:
            return

        with self.lock:
            fd = self.fd

            if fd is None:
                self.dropped_bytes += len(
                    payload
                )
                return

            view = memoryview(
                payload
            )

            while view:
                try:
                    count = os.write(
                        fd,
                        view,
                    )

                except (
                    BlockingIOError,
                    OSError,
                ):
                    self.dropped_bytes += len(
                        view
                    )
                    return

                if count <= 0:
                    self.dropped_bytes += len(
                        view
                    )
                    return

                self.written_bytes += count

                view = view[
                    count:
                ]


    def stop(self) -> None:
        with self.lock:
            self._stop_unlocked()


    def status(self) -> dict:
        with self.lock:
            return {
                "enabled":
                    self.fd is not None,

                "backend":
                    "pipewire-pulse-module-pipe-source",

                "sourceName":
                    self.source_name,

                "description":
                    self.description,

                "fifo":
                    str(self.fifo_path),

                "sampleRate":
                    self.sample_rate,

                "writtenBytes":
                    self.written_bytes,

                "droppedBytes":
                    self.dropped_bytes,

                "defaultSource":
                    self.set_default,
            }


def create_system_microphone():
    enabled = _flag(
        "PHONE_MIC_SYSTEM_SOURCE",
        True,
    )

    if (
        enabled
        and
        sys.platform.startswith("linux")
        and
        shutil.which("pactl")
    ):
        return PipeWireVirtualMicrophone()

    return NullVirtualMicrophone()
