from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

HOST = "127.0.0.1"
MIC_PORT = 5002
CONTROL_PORT = 5003
TYPE_CONTROL = 1
TYPE_PCM = 2

SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2
MIC_CHUNK_FRAMES = 960

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

if IS_WINDOWS:
    VIRTUAL_MIC_DEVICE = os.environ.get(
        "PHONE_MIC_VIRTUAL_DEVICE",
        os.environ.get(
            "PHONE_MIC_PLAYBACK_DEVICE",
            "CABLE Input",
        ),
    ).strip()
    VIRTUAL_MIC_SOURCE_NAME = ""
    VIRTUAL_MIC_DESCRIPTION = ""
else:
    VIRTUAL_MIC_DEVICE = ""
    VIRTUAL_MIC_SOURCE_NAME = os.environ.get(
        "PHONE_MIC_SOURCE_NAME",
        "audio_phone_speaker_mic",
    ).strip()
    VIRTUAL_MIC_DESCRIPTION = os.environ.get(
        "PHONE_MIC_DESCRIPTION",
        "Audio Phone Microphone",
    ).strip()

VIRTUAL_MIC_ENABLED = os.environ.get(
    "PHONE_MIC_VIRTUAL_ENABLED",
    "1",
).lower() in {"1", "true", "yes", "on"}

MIC_RECORDING_EVENT = threading.Event()


class VirtualMicrophoneSink:
    """Phone mic -> isolated platform audio process.

    Windows:
      stdin PCM -> PyAudioWPatch/WASAPI -> CABLE Input -> CABLE Output.

    Linux:
      stdin PCM -> pactl module-pipe-source -> PipeWire/Pulse source.
    """

    def __init__(self, device_hint: str):
        self.device_hint = device_hint
        self.queue = queue.Queue(maxsize=40)
        self.stop_event = threading.Event()
        self.thread = None
        self.process = None
        self.ready = threading.Event()
        self.parent_dropped = 0

        if IS_WINDOWS:
            self.device_name = (
                device_hint
                or "CABLE Input"
            )
            self.browser_device = (
                "CABLE Output "
                "(VB-Audio Virtual Cable)"
            )
            self.backend = "windows-wasapi-process"
        elif IS_LINUX:
            self.device_name = (
                VIRTUAL_MIC_SOURCE_NAME
                or "audio_phone_speaker_mic"
            )
            self.browser_device = (
                VIRTUAL_MIC_DESCRIPTION
                or self.device_name
            )
            self.backend = "linux-pipewire-pulse-source"
        else:
            self.device_name = ""
            self.browser_device = ""
            self.backend = "unsupported"

    def start(self) -> None:
        if not VIRTUAL_MIC_ENABLED:
            print(
                "Virtual microphone routing disabled."
            )
            return

        if not (IS_WINDOWS or IS_LINUX):
            print(
                "Virtual microphone unsupported on:",
                sys.platform,
            )
            return

        if (
            self.thread is not None
            and self.thread.is_alive()
        ):
            return

        self.thread = threading.Thread(
            target=self._run,
            name="phone-mic-virtual-source",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

        process = self.process
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass

    def put(self, payload: bytes) -> None:
        if (
            not VIRTUAL_MIC_ENABLED
            or not payload
        ):
            return

        try:
            self.queue.put_nowait(
                bytes(payload)
            )
            return
        except queue.Full:
            pass

        # The helper owns the audio clock. If it ever stalls hard, drop the
        # oldest parent-side packet so latency remains bounded.
        try:
            self.queue.get_nowait()
            self.parent_dropped += 1
        except queue.Empty:
            pass

        try:
            self.queue.put_nowait(
                bytes(payload)
            )
        except queue.Full:
            self.parent_dropped += 1

    def _helper_command(self) -> list[str]:
        if IS_WINDOWS:
            helper = Path(__file__).with_name(
                "virtual_mic_sink_v15.py"
            )
            return [
                sys.executable,
                "-u",
                str(helper),
                "--device",
                self.device_name,
            ]

        helper = Path(__file__).with_name(
            "virtual_mic_sink_linux.py"
        )

        command = [
            sys.executable,
            "-u",
            str(helper),
            "--source-name",
            self.device_name,
            "--description",
            self.browser_device,
        ]

        set_default = os.environ.get(
            "PHONE_MIC_SET_DEFAULT",
            "1",
        ).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        if set_default:
            command.append(
                "--set-default"
            )

        return command

    def _run(self) -> None:
        while not self.stop_event.is_set():
            process = None

            try:
                command = self._helper_command()
                helper = Path(command[2])

                if not helper.exists():
                    raise RuntimeError(
                        f"virtual mic helper not found: "
                        f"{helper}"
                    )

                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=None,
                    stderr=None,
                    bufsize=0,
                )

                self.process = process
                self.ready.set()

                if IS_WINDOWS:
                    print(
                        "Phone -> PC virtual mic:",
                        "separate WASAPI process -> "
                        f"{self.device_name}",
                    )
                    print(
                        "Browser microphone:",
                        self.browser_device,
                    )
                else:
                    print(
                        "Phone -> PC virtual mic:",
                        "PipeWire/Pulse source -> "
                        f"{self.device_name}",
                    )
                    print(
                        "Browser microphone:",
                        self.browser_device,
                    )

                while (
                    not self.stop_event.is_set()
                    and process.poll() is None
                ):
                    try:
                        payload = self.queue.get(
                            timeout=0.2
                        )
                    except queue.Empty:
                        continue

                    if process.stdin is None:
                        break

                    process.stdin.write(payload)

            except (
                BrokenPipeError,
                OSError,
                RuntimeError,
            ) as error:
                if not self.stop_event.is_set():
                    print(
                        "Virtual microphone "
                        "process restart:",
                        repr(error),
                    )
                    time.sleep(0.5)

            finally:
                self.ready.clear()

                if process is not None:
                    try:
                        if (
                            process.stdin
                            is not None
                        ):
                            process.stdin.close()
                    except Exception:
                        pass

                    try:
                        if (
                            process.poll()
                            is None
                        ):
                            process.terminate()
                            process.wait(
                                timeout=2
                            )
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass

                self.process = None


class PhoneMicrophoneBridge:
    def __init__(self, data_root: str | Path | None = None):
        default_root = (
            Path.home()
            / ".local"
            / "share"
            / "audio-phone-speaker"
            / "phone-microphone"
        )

        self.data_root = Path(
            data_root
            or os.environ.get("PHONE_MIC_DATA_ROOT", default_root)
        )
        self.data_root.mkdir(parents=True, exist_ok=True)

        self.phone_server = None
        self.control_server = None
        self.phone_writer = None
        self.phone_write_lock = asyncio.Lock()
        self.record_lock = asyncio.Lock()

        self.wave_file = None
        self.recording_path = None
        self.recording = False
        self.last_status = {}

        self.virtual_mic = VirtualMicrophoneSink(VIRTUAL_MIC_DEVICE)

    async def start(self) -> None:
        self.virtual_mic.start()

        self.phone_server = await asyncio.start_server(
            self._handle_phone,
            HOST,
            MIC_PORT,
        )

        self.control_server = await asyncio.start_server(
            self._handle_http,
            HOST,
            CONTROL_PORT,
        )

        print(f"Phone microphone transport: tcp://{HOST}:{MIC_PORT}")
        print(f"Phone microphone control:   http://{HOST}:{CONTROL_PORT}")
        print(
            f"Phone microphone format:    {SAMPLE_RATE} Hz / mono / PCM s16le / 20 ms"
        )

    async def stop(self) -> None:
        if self.phone_writer is not None:
            try:
                await self._send_phone_control({"type": "stopMic"})
            except Exception:
                pass

        async with self.record_lock:
            if self.recording:
                await self._finish_recording(send_stop=False)

        self.virtual_mic.stop()

        for server in (self.phone_server, self.control_server):
            if server is not None:
                server.close()
                await server.wait_closed()

        if self.phone_writer is not None:
            self.phone_writer.close()
            try:
                await self.phone_writer.wait_closed()
            except Exception:
                pass
            self.phone_writer = None

    async def _handle_phone(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        previous = self.phone_writer
        self.phone_writer = writer

        if previous is not None and previous is not writer:
            previous.close()

        peer = writer.get_extra_info("peername")
        print("Phone microphone connected:", peer)

        try:
            while True:
                length = struct.unpack(
                    ">I",
                    await reader.readexactly(4),
                )[0]

                if length < 1 or length > 4 * 1024 * 1024:
                    raise ValueError(
                        f"invalid phone frame length: {length}"
                    )

                frame = await reader.readexactly(length)
                frame_type, payload = frame[0], frame[1:]

                if frame_type == TYPE_CONTROL:
                    try:
                        self.last_status = json.loads(
                            payload.decode("utf-8")
                        )
                    except Exception:
                        self.last_status = {"type": "invalidStatus"}

                    print(
                        "Phone microphone status:",
                        self.last_status,
                    )

                    if self.last_status.get("type") == "hello":
                        await self._send_phone_control(
                            {"type": "startMic"}
                        )

                elif frame_type == TYPE_PCM:
                    self.virtual_mic.put(payload)

                    if self.recording and self.wave_file is not None:
                        self.wave_file.writeframesraw(payload)

        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
        ) as error:
            print("Phone microphone disconnected:", repr(error))

        finally:
            if self.phone_writer is writer:
                self.phone_writer = None

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_phone_control(self, payload: dict) -> None:
        writer = self.phone_writer

        if writer is None or writer.is_closing():
            raise RuntimeError(
                "O serviço de microfone do celular ainda não está conectado."
            )

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

        frame = (
            struct.pack(">I", len(encoded) + 1)
            + bytes([TYPE_CONTROL])
            + encoded
        )

        async with self.phone_write_lock:
            writer.write(frame)
            await writer.drain()

    async def _begin_recording(self) -> dict:
        async with self.record_lock:
            if self.phone_writer is None:
                raise RuntimeError(
                    "Celular não conectado. Abra o app e confirme a permissão de microfone."
                )

            if self.recording:
                return {
                    "ok": True,
                    "recording": True,
                    "path": str(self.recording_path or ""),
                }

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.data_root / f"phone-mic-{stamp}.wav"

            output = wave.open(str(path), "wb")
            output.setnchannels(CHANNELS)
            output.setsampwidth(SAMPLE_WIDTH)
            output.setframerate(SAMPLE_RATE)

            self.wave_file = output
            self.recording_path = path
            self.recording = True
            MIC_RECORDING_EVENT.set()

            try:
                await self._send_phone_control({"type": "startMic"})
            except Exception:
                pass

            return {
                "ok": True,
                "recording": True,
                "path": str(path),
            }

    async def _finish_recording(
        self,
        send_stop: bool = True,
    ) -> dict:
        if not self.recording:
            return {
                "ok": False,
                "error": "Nenhuma gravação está ativa.",
            }

        # Do not stop Android AudioRecord here. The virtual microphone
        # must remain live for browsers and voice apps.
        self.recording = False
        MIC_RECORDING_EVENT.clear()

        output = self.wave_file
        path = self.recording_path
        self.wave_file = None
        self.recording_path = None

        if output is not None:
            output.close()

        if path is None or not path.exists():
            return {
                "ok": False,
                "error": "O áudio do celular não foi recebido.",
            }

        data = path.read_bytes()

        keep = os.environ.get(
            "PHONE_MIC_KEEP_AUDIO",
            "0",
        ).lower() in {"1", "true", "yes", "on"}

        response = {
            "ok": True,
            "recording": False,
            "mimeType": "audio/wav",
            "data": base64.b64encode(data).decode("ascii"),
            "path": str(path),
            "bytes": len(data),
            "sampleRate": SAMPLE_RATE,
        }

        if not keep:
            try:
                path.unlink()
            except OSError:
                pass

        return response

    async def _status(self) -> dict:
        return {
            "ok": True,
            "phoneConnected": self.phone_writer is not None,
            "recording": self.recording,
            "phoneStatus": self.last_status,
            "transportPort": MIC_PORT,
            "controlPort": CONTROL_PORT,
            "sampleRate": SAMPLE_RATE,
            "virtualMicEnabled": VIRTUAL_MIC_ENABLED,
            "virtualMicBackend": self.virtual_mic.backend,
            "virtualMicSink": self.virtual_mic.device_name,
            "virtualMicReady": self.virtual_mic.ready.is_set(),
            "parentDroppedPackets": self.virtual_mic.parent_dropped,
            "browserDevice": self.virtual_mic.browser_device,
            "platform": sys.platform,
        }

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=5,
            )

            first = raw.split(
                b"\r\n",
                1,
            )[0].decode("latin1")

            method, target, _ = first.split(" ", 2)

            if method == "OPTIONS":
                await self._http_response(writer, 204, {})
                return

            if method not in {"GET", "POST"}:
                await self._http_response(
                    writer,
                    405,
                    {"ok": False, "error": "Method not allowed"},
                )
                return

            try:
                if target.startswith("/record/start"):
                    payload = await self._begin_recording()

                elif target.startswith("/record/stop"):
                    async with self.record_lock:
                        payload = await self._finish_recording()

                elif target.startswith("/status"):
                    payload = await self._status()

                else:
                    await self._http_response(
                        writer,
                        404,
                        {"ok": False, "error": "Not found"},
                    )
                    return

                await self._http_response(
                    writer,
                    200,
                    payload,
                )

            except Exception as error:
                await self._http_response(
                    writer,
                    503,
                    {"ok": False, "error": str(error)},
                )

        except Exception as error:
            try:
                await self._http_response(
                    writer,
                    400,
                    {"ok": False, "error": str(error)},
                )
            except Exception:
                pass

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _http_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict,
    ) -> None:
        reasons = {
            200: "OK",
            204: "No Content",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }

        body = (
            b""
            if status == 204
            else json.dumps(
                payload,
                ensure_ascii=False,
            ).encode("utf-8")
        )

        headers = [
            f"HTTP/1.1 {status} {reasons.get(status, 'OK')}",
            "Content-Type: application/json; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, POST, OPTIONS",
            "Access-Control-Allow-Headers: Content-Type",
            "Cache-Control: no-store",
            "Connection: close",
            "",
            "",
        ]

        writer.write(
            "\r\n".join(headers).encode("latin1") + body
        )
        await writer.drain()