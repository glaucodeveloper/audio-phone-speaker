from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2


def run_pactl(
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pactl", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def unload_stale_modules(
    source_name: str,
) -> None:
    result = run_pactl(
        "list",
        "modules",
        "short",
        check=False,
    )

    if result.returncode != 0:
        return

    marker = f"source_name={source_name}"

    for line in result.stdout.splitlines():
        if marker not in line:
            continue

        module_id = (
            line.split("\t", 1)[0]
            .split(None, 1)[0]
        )

        if module_id:
            run_pactl(
                "unload-module",
                module_id,
                check=False,
            )


def safe_fifo_name(
    source_name: str,
) -> str:
    cleaned = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "-",
        source_name,
    ).strip("-")
    return cleaned or "audio-phone-speaker-mic"


def write_all(
    fd: int,
    data: bytes,
) -> None:
    view = memoryview(data)

    while view:
        written = os.write(fd, view)

        if written <= 0:
            raise BrokenPipeError(
                "virtual microphone FIFO stopped accepting audio"
            )

        view = view[written:]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Expose 48 kHz mono PCM from stdin as a "
            "PipeWire/PulseAudio microphone source."
        )
    )
    parser.add_argument(
        "--source-name",
        default="audio_phone_speaker_mic",
    )
    parser.add_argument(
        "--description",
        default="Audio Phone Microphone",
    )
    parser.add_argument(
        "--set-default",
        action="store_true",
    )
    args = parser.parse_args()

    if not shutil.which("pactl"):
        raise SystemExit(
            "pactl was not found. Install pipewire-pulse "
            "or PulseAudio utilities."
        )

    source_name = args.source_name.strip()
    description = args.description.strip()

    runtime_dir = Path(
        os.environ.get(
            "XDG_RUNTIME_DIR",
            f"/tmp/audio-phone-speaker-{os.getuid()}",
        )
    ) / "audio-phone-speaker"

    runtime_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    fifo_path = runtime_dir / (
        safe_fifo_name(source_name)
        + ".pcm"
    )

    module_id: str | None = None
    fifo_fd: int | None = None

    try:
        unload_stale_modules(
            source_name
        )

        try:
            fifo_path.unlink()
        except FileNotFoundError:
            pass

        os.mkfifo(
            fifo_path,
            0o600,
        )

        # Open read/write so startup cannot deadlock while pactl attaches the
        # module. Writes remain blocking and therefore follow the audio-server
        # clock instead of dropping chunks.
        fifo_fd = os.open(
            fifo_path,
            os.O_RDWR,
        )

        description_arg = (
            "source_properties="
            f'device.description="{description}"'
        )

        result = run_pactl(
            "load-module",
            "module-pipe-source",
            f"file={fifo_path}",
            f"source_name={source_name}",
            "format=s16le",
            f"rate={SAMPLE_RATE}",
            f"channels={CHANNELS}",
            "channel_map=mono",
            description_arg,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(
                "failed to create PipeWire/Pulse source: "
                + (
                    result.stderr.strip()
                    or result.stdout.strip()
                )
            )

        module_id = (
            result.stdout.strip()
        )

        if args.set_default:
            run_pactl(
                "set-default-source",
                source_name,
                check=False,
            )

        print(
            "Virtual mic process:",
            f"{description} ({source_name})",
            f"@ {SAMPLE_RATE} Hz mono",
            flush=True,
        )
        print(
            "Virtual mic backend:",
            "PipeWire/Pulse module-pipe-source",
            flush=True,
        )

        source = sys.stdin.buffer

        while True:
            if hasattr(source, "read1"):
                data = source.read1(
                    8192
                )
            else:
                data = os.read(
                    source.fileno(),
                    8192,
                )

            if not data:
                break

            # Android sends PCM16 mono 48 kHz already, so no resampling is
            # needed on Linux.
            usable = (
                len(data)
                - (len(data) % SAMPLE_WIDTH)
            )

            if usable:
                write_all(
                    fifo_fd,
                    data[:usable],
                )

        return 0

    finally:
        if fifo_fd is not None:
            try:
                os.close(fifo_fd)
            except OSError:
                pass

        if module_id:
            run_pactl(
                "unload-module",
                module_id,
                check=False,
            )

        try:
            fifo_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
