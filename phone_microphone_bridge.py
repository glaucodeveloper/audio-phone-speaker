from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import threading
import wave
from datetime import datetime
from pathlib import Path
from phone_virtual_microphone import create_system_microphone

HOST = "127.0.0.1"
MIC_PORT = 5002
CONTROL_PORT = 5003
TYPE_CONTROL = 1
TYPE_PCM = 2
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
MIC_RECORDING_EVENT = threading.Event()


class PhoneMicrophoneBridge:
    def __init__(self, data_root: str | Path | None = None):
        default_root = Path.home() / ".local" / "share" / "glaucoplastic" / "phone-microphone"
        self.data_root = Path(data_root or os.environ.get("PHONE_MIC_DATA_ROOT", default_root))
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.phone_server: asyncio.AbstractServer | None = None
        self.control_server: asyncio.AbstractServer | None = None
        self.phone_writer: asyncio.StreamWriter | None = None
        self.phone_write_lock = asyncio.Lock()
        self.record_lock = asyncio.Lock()
        self.wave_file: wave.Wave_write | None = None
        self.recording_path: Path | None = None
        self.recording = False
        self.last_status: dict = {}
        self.virtual_microphone = create_system_microphone()
        self.system_microphone_always_on = False

    async def start(self) -> None:
        if self.virtual_microphone.enabled:
            try:
                await asyncio.to_thread(self.virtual_microphone.start)
                self.system_microphone_always_on = bool(
                    self.virtual_microphone.status().get("enabled", False)
                )
            except Exception as error:
                self.system_microphone_always_on = False
                print("System microphone backend failed:", repr(error))
        self.phone_server = await asyncio.start_server(self._handle_phone, HOST, MIC_PORT)
        self.control_server = await asyncio.start_server(self._handle_http, HOST, CONTROL_PORT)
        print(f"Phone microphone transport: tcp://{HOST}:{MIC_PORT}")
        print(f"Phone microphone control:   http://{HOST}:{CONTROL_PORT}")

    async def stop(self) -> None:
        if self.phone_writer is not None and self.system_microphone_always_on:
            try:
                await self._send_phone_control({"type": "stopMic"})
            except Exception:
                pass
        async with self.record_lock:
            await self._finish_recording(send_stop=False)
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
        await asyncio.to_thread(self.virtual_microphone.stop)

    async def _handle_phone(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        previous = self.phone_writer
        self.phone_writer = writer
        if previous is not None and previous is not writer:
            previous.close()
        peer = writer.get_extra_info("peername")
        print("Phone microphone connected:", peer)
        try:
            if self.system_microphone_always_on:
                await self._send_phone_control({"type": "startMic"})
            while True:
                length = struct.unpack(">I", await reader.readexactly(4))[0]
                if length < 1 or length > 4 * 1024 * 1024:
                    raise ValueError(f"invalid phone frame length: {length}")
                frame = await reader.readexactly(length)
                frame_type, payload = frame[0], frame[1:]
                if frame_type == TYPE_CONTROL:
                    try:
                        self.last_status = json.loads(payload.decode("utf-8"))
                    except Exception:
                        self.last_status = {"type": "invalidStatus"}
                    print("Phone microphone status:", self.last_status)
                elif frame_type == TYPE_PCM:
                    self.virtual_microphone.write(payload)
                    if self.recording and self.wave_file is not None:
                        self.wave_file.writeframesraw(payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as error:
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
            raise RuntimeError("O serviço de microfone do celular ainda não está conectado.")
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        frame = struct.pack(">I", len(encoded) + 1) + bytes([TYPE_CONTROL]) + encoded
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
                return {"ok": True, "recording": True, "path": str(self.recording_path or "")}
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
                if not self.system_microphone_always_on:
                    await self._send_phone_control({"type": "startMic"})
            except Exception:
                self.recording = False
                MIC_RECORDING_EVENT.clear()
                output.close()
                self.wave_file = None
                self.recording_path = None
                raise
            return {"ok": True, "recording": True, "path": str(path)}

    async def _finish_recording(self, send_stop: bool = True) -> dict:
        if not self.recording:
            return {"ok": False, "error": "Nenhuma gravação está ativa."}
        if send_stop and self.phone_writer is not None and not self.system_microphone_always_on:
            try:
                await self._send_phone_control({"type": "stopMic"})
            except Exception:
                pass
            await asyncio.sleep(0.18)
        self.recording = False
        MIC_RECORDING_EVENT.clear()
        output = self.wave_file
        path = self.recording_path
        self.wave_file = None
        self.recording_path = None
        if output is not None:
            output.close()
        if path is None or not path.exists():
            return {"ok": False, "error": "O áudio do celular não foi recebido."}
        data = path.read_bytes()
        keep = os.environ.get("PHONE_MIC_KEEP_AUDIO", "0").lower() in {"1", "true", "yes", "on"}
        response = {
            "ok": True,
            "recording": False,
            "mimeType": "audio/wav",
            "data": base64.b64encode(data).decode("ascii"),
            "path": str(path),
            "bytes": len(data),
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
            "systemMicrophone": self.virtual_microphone.status(),
        }

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            first = raw.split(b"\r\n", 1)[0].decode("latin1")
            method, target, _ = first.split(" ", 2)
            if method == "OPTIONS":
                await self._http_response(writer, 204, {})
                return
            if method not in {"GET", "POST"}:
                await self._http_response(writer, 405, {"ok": False, "error": "Method not allowed"})
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
                    await self._http_response(writer, 404, {"ok": False, "error": "Not found"})
                    return
                await self._http_response(writer, 200, payload)
            except Exception as error:
                await self._http_response(writer, 503, {"ok": False, "error": str(error)})
        except Exception as error:
            try:
                await self._http_response(writer, 400, {"ok": False, "error": str(error)})
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _http_response(self, writer: asyncio.StreamWriter, status: int, payload: dict) -> None:
        reasons = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}
        body = b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
        writer.write("\r\n".join(headers).encode("latin1") + body)
        await writer.drain()
