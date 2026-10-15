from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

from phone_virtual_microphone import (
    create_system_microphone,
)


HOST = os.environ.get(
    "PHONE_MIC_HOST",
    "127.0.0.1",
)

MIC_PORT = int(
    os.environ.get(
        "PHONE_MIC_PORT",
        "5002",
    )
)

CONTROL_PORT = int(
    os.environ.get(
        "PHONE_MIC_CONTROL_PORT",
        "5003",
    )
)

TYPE_CONTROL = 1
TYPE_PCM = 2

CHANNELS = 1
SAMPLE_WIDTH = 2

MIC_RECORDING_EVENT = (
    threading.Event()
)


class PhoneMicrophoneBridge:
    def __init__(
        self,
        data_root:
            str | Path | None = None,
    ):
        default_root = (
            Path.home()
            /
            ".local"
            /
            "share"
            /
            "glaucoplastic"
            /
            "phone-microphone"
        )

        self.data_root = Path(
            data_root
            or
            os.environ.get(
                "PHONE_MIC_DATA_ROOT",
                default_root,
            )
        )

        self.data_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.phone_server = None
        self.control_server = None

        self.phone_writer = None

        self.phone_write_lock = (
            asyncio.Lock()
        )

        self.record_lock = (
            asyncio.Lock()
        )

        self.wave_file = None
        self.recording_path = None
        self.recording = False

        self.last_status: dict = {}

        self.sample_rate = 0
        self.rate_source = ""
        self.rate_error = ""

        self.virtual_microphone = (
            create_system_microphone()
        )

        self.system_microphone_always_on = (
            self.virtual_microphone.enabled
        )


    async def start(self) -> None:
        self.phone_server = (
            await asyncio.start_server(
                self._handle_phone,
                HOST,
                MIC_PORT,
            )
        )

        self.control_server = (
            await asyncio.start_server(
                self._handle_http,
                HOST,
                CONTROL_PORT,
            )
        )

        print(
            "Phone microphone transport: "
            f"tcp://{HOST}:{MIC_PORT}",
            flush=True,
        )

        print(
            "Phone microphone control:   "
            f"http://{HOST}:{CONTROL_PORT}",
            flush=True,
        )


    async def stop(self) -> None:
        if (
            self.phone_writer
            is not None
            and
            self.system_microphone_always_on
        ):
            try:
                await self._send_phone_control(
                    {
                        "type":
                            "stopMic",
                    }
                )
            except Exception:
                pass

        async with self.record_lock:
            if self.recording:
                await self._finish_recording(
                    send_stop=False,
                )

        for server in (
            self.phone_server,
            self.control_server,
        ):
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

        await asyncio.to_thread(
            self.virtual_microphone.stop,
        )


    async def _configure_rate(
        self,
        status: dict,
    ) -> None:

        try:
            rate = int(
                status.get(
                    "sampleRate",
                    0,
                )
                or
                0
            )
        except (
            TypeError,
            ValueError,
        ):
            rate = 0

        if rate <= 0:
            return

        if (
            rate == self.sample_rate
            and
            self.virtual_microphone.status()
            .get("sampleRate")
            == rate
        ):
            return

        try:
            await asyncio.to_thread(
                self.virtual_microphone.ensure_rate,
                rate,
            )

        except Exception as error:
            self.rate_error = str(
                error
            )

            print(
                "[Glauco Phone Mic] "
                "rate negotiation failed:",
                self.rate_error,
                flush=True,
            )

            return

        self.sample_rate = rate

        self.rate_source = str(
            status.get(
                "rateSource",
                "",
            )
            or
            status.get(
                "selection",
                "",
            )
        )

        self.rate_error = ""

        print(
            "[Glauco Phone Mic] "
            f"negotiated {rate} Hz "
            f"({self.rate_source or 'device'})",
            flush=True,
        )


    async def _handle_phone(
        self,
        reader:
            asyncio.StreamReader,

        writer:
            asyncio.StreamWriter,
    ) -> None:

        previous = self.phone_writer

        self.phone_writer = writer

        if (
            previous is not None
            and
            previous is not writer
        ):
            previous.close()

        peer = writer.get_extra_info(
            "peername"
        )

        print(
            "Phone microphone connected:",
            peer,
            flush=True,
        )

        continuous_requested = False

        try:
            while True:
                length = struct.unpack(
                    ">I",
                    await reader.readexactly(
                        4
                    ),
                )[0]

                if not (
                    1
                    <= length
                    <= 4 * 1024 * 1024
                ):
                    raise ValueError(
                        "invalid phone frame "
                        f"length: {length}"
                    )

                frame = (
                    await reader.readexactly(
                        length
                    )
                )

                frame_type = frame[0]
                payload = frame[1:]


                if frame_type == TYPE_CONTROL:
                    try:
                        status = json.loads(
                            payload.decode(
                                "utf-8"
                            )
                        )

                    except Exception:
                        status = {
                            "type":
                                "invalidStatus"
                        }

                    self.last_status = status

                    await self._configure_rate(
                        status
                    )

                    print(
                        "Phone microphone status:",
                        status,
                        flush=True,
                    )


                    # Android envia hello primeiro.
                    # Só depois pedimos a captura.
                    if (
                        status.get("type")
                        == "hello"
                        and
                        self.system_microphone_always_on
                        and
                        not continuous_requested
                    ):
                        await self._send_phone_control(
                            {
                                "type":
                                    "startMic",
                            }
                        )

                        continuous_requested = True


                elif frame_type == TYPE_PCM:
                    if self.sample_rate > 0:
                        self.virtual_microphone.write(
                            payload
                        )

                    if (
                        self.recording
                        and
                        self.wave_file
                        is not None
                    ):
                        self.wave_file.writeframesraw(
                            payload
                        )

        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
        ) as error:

            print(
                "Phone microphone disconnected:",
                repr(error),
                flush=True,
            )

        finally:
            if self.phone_writer is writer:
                self.phone_writer = None

            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass


    async def _send_phone_control(
        self,
        payload: dict,
    ) -> None:

        writer = self.phone_writer

        if (
            writer is None
            or
            writer.is_closing()
        ):
            raise RuntimeError(
                "Celular não conectado."
            )

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode(
            "utf-8"
        )

        frame = (
            struct.pack(
                ">I",
                len(encoded) + 1,
            )
            +
            bytes(
                [TYPE_CONTROL]
            )
            +
            encoded
        )

        async with self.phone_write_lock:
            writer.write(
                frame
            )

            await writer.drain()


    async def _wait_for_rate(
        self,
        timeout:
            float = 4.0,
    ) -> int:

        deadline = (
            time.monotonic()
            +
            timeout
        )

        while (
            self.sample_rate <= 0
            and
            time.monotonic()
            < deadline
        ):
            await asyncio.sleep(
                0.05
            )

        if self.sample_rate <= 0:
            raise RuntimeError(
                "Android não negociou "
                "a frequência do microfone."
            )

        return self.sample_rate


    async def _begin_recording(
        self,
    ) -> dict:

        async with self.record_lock:
            if self.phone_writer is None:
                raise RuntimeError(
                    "Celular não conectado."
                )

            if self.recording:
                return {
                    "ok":
                        True,

                    "recording":
                        True,

                    "path":
                        str(
                            self.recording_path
                            or
                            ""
                        ),

                    "sampleRate":
                        self.sample_rate,
                }


            if (
                not
                self.system_microphone_always_on
            ):
                await self._send_phone_control(
                    {
                        "type":
                            "startMic",
                    }
                )


            rate = (
                await self._wait_for_rate()
            )

            stamp = datetime.now().strftime(
                "%Y%m%d-%H%M%S-%f"
            )

            path = (
                self.data_root
                /
                f"phone-mic-{stamp}.wav"
            )

            output = wave.open(
                str(path),
                "wb",
            )

            output.setnchannels(
                CHANNELS
            )

            output.setsampwidth(
                SAMPLE_WIDTH
            )

            output.setframerate(
                rate
            )

            self.wave_file = output
            self.recording_path = path
            self.recording = True

            MIC_RECORDING_EVENT.set()

            return {
                "ok":
                    True,

                "recording":
                    True,

                "path":
                    str(path),

                "sampleRate":
                    rate,
            }


    async def _finish_recording(
        self,
        send_stop:
            bool = True,
    ) -> dict:

        if not self.recording:
            return {
                "ok":
                    False,

                "error":
                    "Nenhuma gravação ativa.",
            }

        if (
            send_stop
            and
            self.phone_writer
            is not None
            and
            not
            self.system_microphone_always_on
        ):
            try:
                await self._send_phone_control(
                    {
                        "type":
                            "stopMic",
                    }
                )
            except Exception:
                pass

            await asyncio.sleep(
                0.18
            )


        self.recording = False

        MIC_RECORDING_EVENT.clear()

        output = self.wave_file
        path = self.recording_path

        self.wave_file = None
        self.recording_path = None

        if output is not None:
            output.close()

        if (
            path is None
            or
            not path.exists()
        ):
            return {
                "ok":
                    False,

                "error":
                    "Áudio não recebido.",
            }

        data = path.read_bytes()

        keep = (
            os.environ.get(
                "PHONE_MIC_KEEP_AUDIO",
                "0",
            )
            .lower()
            in
            {
                "1",
                "true",
                "yes",
                "on",
            }
        )

        response = {
            "ok":
                True,

            "recording":
                False,

            "mimeType":
                "audio/wav",

            "data":
                base64.b64encode(
                    data
                ).decode(
                    "ascii"
                ),

            "path":
                str(path),

            "bytes":
                len(data),

            "sampleRate":
                self.sample_rate,
        }

        if not keep:
            try:
                path.unlink()
            except OSError:
                pass

        return response


    async def _status(self) -> dict:
        return {
            "ok":
                True,

            "phoneConnected":
                self.phone_writer
                is not None,

            "recording":
                self.recording,

            "phoneStatus":
                self.last_status,

            "sampleRate":
                self.sample_rate,

            "rateSource":
                self.rate_source,

            "rateError":
                self.rate_error,

            "transportPort":
                MIC_PORT,

            "controlPort":
                CONTROL_PORT,

            "systemMicrophone":
                self.virtual_microphone.status(),
        }


    async def _handle_http(
        self,
        reader:
            asyncio.StreamReader,

        writer:
            asyncio.StreamWriter,
    ) -> None:

        try:
            raw = await asyncio.wait_for(
                reader.readuntil(
                    b"\r\n\r\n"
                ),
                timeout=5,
            )

            first = raw.split(
                b"\r\n",
                1,
            )[0].decode(
                "latin1"
            )

            method, target, _ = (
                first.split(
                    " ",
                    2,
                )
            )

            if method == "OPTIONS":
                await self._http_response(
                    writer,
                    204,
                    {},
                )
                return

            if method not in {
                "GET",
                "POST",
            }:
                await self._http_response(
                    writer,
                    405,
                    {
                        "ok":
                            False,

                        "error":
                            "Method not allowed",
                    },
                )

                return


            if target.startswith(
                "/record/start"
            ):
                payload = (
                    await self._begin_recording()
                )

            elif target.startswith(
                "/record/stop"
            ):
                async with self.record_lock:
                    payload = (
                        await self._finish_recording()
                    )

            elif target.startswith(
                "/status"
            ):
                payload = (
                    await self._status()
                )

            else:
                await self._http_response(
                    writer,
                    404,
                    {
                        "ok":
                            False,

                        "error":
                            "Not found",
                    },
                )
                return


            await self._http_response(
                writer,
                200,
                payload,
            )

        except Exception as error:
            try:
                await self._http_response(
                    writer,
                    503,
                    {
                        "ok":
                            False,

                        "error":
                            str(error),
                    },
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
        writer,
        status: int,
        payload: dict,
    ) -> None:

        reasons = {
            200:
                "OK",

            204:
                "No Content",

            404:
                "Not Found",

            405:
                "Method Not Allowed",

            503:
                "Service Unavailable",
        }

        body = (
            b""
            if status == 204
            else
            json.dumps(
                payload,
                ensure_ascii=False,
            ).encode(
                "utf-8"
            )
        )

        headers = [
            f"HTTP/1.1 {status} "
            f"{reasons.get(status, 'OK')}",

            "Content-Type: "
            "application/json; charset=utf-8",

            f"Content-Length: {len(body)}",

            "Access-Control-Allow-Origin: *",

            "Access-Control-Allow-Methods: "
            "GET, POST, OPTIONS",

            "Access-Control-Allow-Headers: "
            "Content-Type",

            "Cache-Control: no-store",
            "Connection: close",

            "",
            "",
        ]

        writer.write(
            "\r\n".join(
                headers
            ).encode(
                "latin1"
            )
            +
            body
        )

        await writer.drain()
