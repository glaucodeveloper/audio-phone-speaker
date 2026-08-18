from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

SAMPLE_RATE = 48000
OUT_CHANNELS = 2
SAMPLE_WIDTH = 2

CALLBACK_FRAMES = 480       # 10 ms
PRIME_MS = 80
TARGET_MS = 100
REPRIME_MS = 60
MAX_MS = 300

# Smooth drift correction: +/- 2 input frames per 10 ms callback (~0.4%).
DRIFT_FRAMES = 2
DRIFT_HIGH_MS = 125
DRIFT_LOW_MS = 75


def find_output_device(p: pyaudio.PyAudio, hint: str) -> dict:
    wanted = hint.casefold().strip()
    candidates = []

    for index in range(p.get_device_count()):
        info = dict(p.get_device_info_by_index(index))
        if int(info.get("maxOutputChannels", 0)) <= 0:
            continue

        name = str(info.get("name", ""))
        lowered = name.casefold()
        if wanted not in lowered:
            continue

        if "16ch" in lowered and "16ch" not in wanted:
            continue

        try:
            host = p.get_host_api_info_by_index(
                int(info.get("hostApi", 0))
            )
            host_name = str(host.get("name", ""))
        except Exception:
            host_name = ""

        score = 0
        if lowered == wanted:
            score += 100
        if "wasapi" in host_name.casefold():
            score += 50
        if "vb-audio virtual cable" in lowered:
            score += 20

        info["_host_name"] = host_name
        info["_score"] = score
        candidates.append(info)

    if not candidates:
        available = []
        for index in range(p.get_device_count()):
            info = p.get_device_info_by_index(index)
            if int(info.get("maxOutputChannels", 0)) > 0:
                available.append(str(info.get("name", "")))
        raise RuntimeError(
            f'Output device "{hint}" not found. Available: '
            + ", ".join(available)
        )

    candidates.sort(key=lambda d: d["_score"], reverse=True)
    return candidates[0]


class AdaptiveMicRenderer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buffer = bytearray()  # mono PCM16
        self.primed = False

        self.prime_frames = SAMPLE_RATE * PRIME_MS // 1000
        self.target_frames = SAMPLE_RATE * TARGET_MS // 1000
        self.reprime_frames = SAMPLE_RATE * REPRIME_MS // 1000
        self.max_frames = SAMPLE_RATE * MAX_MS // 1000
        self.high_frames = SAMPLE_RATE * DRIFT_HIGH_MS // 1000
        self.low_frames = SAMPLE_RATE * DRIFT_LOW_MS // 1000

        self.underflows = 0
        self.hard_dropped_frames = 0
        self.speedup_callbacks = 0
        self.slowdown_callbacks = 0
        self.last_rms = 0.0
        self.last_report = time.monotonic()

    def append(self, data: bytes) -> None:
        usable = len(data) - (len(data) % 2)
        if usable <= 0:
            return

        mono = np.frombuffer(data[:usable], dtype="<i2")
        if mono.size == 0:
            return

        f = mono.astype(np.float32) / 32768.0
        self.last_rms = float(np.sqrt(np.mean(f * f)))

        with self.lock:
            self.buffer.extend(data[:usable])

            frames = len(self.buffer) // 2
            if frames > self.max_frames:
                # This is only an emergency bound. Normal drift is corrected
                # gradually in the callback and should not hit this path.
                drop = frames - self.target_frames
                drop_bytes = drop * 2
                del self.buffer[:drop_bytes]
                self.hard_dropped_frames += drop

    def _resample_to_callback(
        self,
        mono: np.ndarray,
        out_frames: int,
    ) -> np.ndarray:
        if mono.size == out_frames:
            return mono

        if mono.size <= 1:
            return np.zeros(out_frames, dtype=np.int16)

        src_x = np.linspace(
            0.0,
            1.0,
            mono.size,
            endpoint=True,
            dtype=np.float32,
        )
        dst_x = np.linspace(
            0.0,
            1.0,
            out_frames,
            endpoint=True,
            dtype=np.float32,
        )
        rendered = np.interp(
            dst_x,
            src_x,
            mono.astype(np.float32),
        )
        return np.clip(rendered, -32768, 32767).astype("<i2")

    def callback(self, in_data, frame_count, time_info, status_flags):
        silence = b"\x00" * frame_count * OUT_CHANNELS * SAMPLE_WIDTH

        with self.lock:
            available_frames = len(self.buffer) // 2

            if not self.primed:
                threshold = (
                    self.prime_frames
                    if self.hard_dropped_frames == 0
                    else self.reprime_frames
                )
                if available_frames >= threshold:
                    self.primed = True
                else:
                    return (silence, pyaudio.paContinue)

            consume_frames = frame_count

            if available_frames > self.high_frames:
                consume_frames += DRIFT_FRAMES
                self.speedup_callbacks += 1
            elif available_frames < self.low_frames:
                consume_frames = max(1, frame_count - DRIFT_FRAMES)
                self.slowdown_callbacks += 1

            if available_frames < consume_frames:
                self.underflows += 1
                self.primed = False
                return (silence, pyaudio.paContinue)

            byte_count = consume_frames * 2
            raw = bytes(self.buffer[:byte_count])
            del self.buffer[:byte_count]

        mono = np.frombuffer(raw, dtype="<i2")
        mono = self._resample_to_callback(mono, frame_count)

        stereo = np.empty(frame_count * 2, dtype="<i2")
        stereo[0::2] = mono
        stereo[1::2] = mono

        return (stereo.tobytes(), pyaudio.paContinue)

    def report_if_due(self) -> None:
        now = time.monotonic()
        if now - self.last_report < 2.0:
            return

        with self.lock:
            buffer_ms = (
                (len(self.buffer) // 2)
                * 1000.0
                / SAMPLE_RATE
            )

        hard_drop_ms = (
            self.hard_dropped_frames
            * 1000.0
            / SAMPLE_RATE
        )

        print(
            "Virtual mic:",
            f"RMS={self.last_rms:.4f}",
            f"buffer={buffer_ms:.0f}ms",
            f"underflows={self.underflows}",
            f"hard_dropped={hard_drop_ms:.0f}ms",
            f"speedup={self.speedup_callbacks}",
            f"slowdown={self.slowdown_callbacks}",
            flush=True,
        )
        self.last_report = now


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="CABLE Input")
    args = parser.parse_args()

    renderer = AdaptiveMicRenderer()
    p = pyaudio.PyAudio()
    stream = None

    try:
        device = find_output_device(p, args.device)

        print(
            "Virtual mic process:",
            f'{device.get("name", "unknown")} @ {SAMPLE_RATE} Hz',
            f'[{device.get("_host_name", "")}]',
            flush=True,
        )
        print(
            "Virtual mic adaptive clock:",
            f"callback=10ms target={TARGET_MS}ms "
            f"range={DRIFT_LOW_MS}-{DRIFT_HIGH_MS}ms "
            f"max={MAX_MS}ms",
            flush=True,
        )

        stream = p.open(
            format=pyaudio.paInt16,
            channels=OUT_CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            output_device_index=int(device["index"]),
            frames_per_buffer=CALLBACK_FRAMES,
            stream_callback=renderer.callback,
            start=True,
        )

        source = sys.stdin.buffer

        while stream.is_active():
            # read1 returns currently available pipe data instead of waiting
            # for a large fixed block, keeping input latency low.
            if hasattr(source, "read1"):
                data = source.read1(8192)
            else:
                data = os.read(source.fileno(), 8192)

            if not data:
                break

            renderer.append(data)
            renderer.report_if_due()

        return 0

    finally:
        if stream is not None:
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        try:
            p.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
