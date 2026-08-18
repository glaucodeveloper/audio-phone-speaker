from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_ID = "glauco.phone.audiospeaker"
PORTS = (5001, 5002)


class SetupError(RuntimeError):
    pass


def command_text(command: list[str]) -> str:
    return " ".join(str(part) for part in command)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    print("+", command_text(command))

    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise SetupError(
            f"command timed out after {timeout}s: "
            f"{command_text(command)}"
        ) from error

    if check and result.returncode != 0:
        raise SetupError(
            f"command failed with exit code "
            f"{result.returncode}: "
            f"{command_text(command)}"
        )

    return result


def find_program(name: str) -> str:
    value = shutil.which(name)
    if value:
        return value

    if os.name == "nt":
        cmd = shutil.which(name + ".CMD")
        if cmd:
            return cmd

    raise SetupError(f"{name} was not found in PATH")


def find_adb() -> str:
    adb = shutil.which("adb")
    if adb:
        return adb

    if os.name == "nt":
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

    raise SetupError(
        "adb was not found. Install Android SDK platform-tools."
    )


def adb_devices(adb: str) -> list[str]:
    try:
        result = subprocess.run(
            [adb, "devices"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return []

    devices = []

    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    return devices


def hard_reset_adb(adb: str) -> None:
    print("Resetting adb daemon...")

    if os.name == "nt":
        subprocess.run(
            [
                "taskkill",
                "/F",
                "/T",
                "/IM",
                "adb.exe",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        # Avoid `adb kill-server`: a wedged daemon can make the command
        # itself hang. Kill the stale process first and start a new daemon.
        if shutil.which("pkill"):
            subprocess.run(
                ["pkill", "-9", "-x", "adb"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    time.sleep(0.6)

    run(
        [adb, "start-server"],
        timeout=15,
        check=False,
    )


def ensure_adb_device(
    adb: str,
    timeout: int = 30,
) -> str:
    devices = adb_devices(adb)

    if devices:
        print("ADB device ready:", devices[0])
        return devices[0]

    hard_reset_adb(adb)

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        devices = adb_devices(adb)

        if devices:
            print("ADB device recovered:", devices[0])
            return devices[0]

        time.sleep(1)

    try:
        detail = subprocess.run(
            [adb, "devices", "-l"],
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout.strip()
    except Exception as error:
        detail = repr(error)

    raise SetupError(
        "ADB did not expose an authorized device.\n"
        "Current `adb devices -l`:\n"
        + detail
    )


def install_python_dependencies(
    repo: Path,
) -> None:
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(repo / "requirements-audio-sender.txt"),
        ],
        cwd=repo,
        timeout=180,
    )

    run(
        [
            sys.executable,
            "-m",
            "py_compile",
            "audio_sender.py",
            "phone_microphone_bridge.py",
            "virtual_mic_sink_v15.py",
            "virtual_mic_sink_linux.py",
        ],
        cwd=repo,
        timeout=30,
    )


def build_android(
    repo: Path,
) -> Path:
    npm = find_program("npm")
    npx = find_program("npx")

    run(
        [npm, "install"],
        cwd=repo,
        timeout=180,
    )

    run(
        [npm, "run", "build"],
        cwd=repo,
        timeout=120,
    )

    run(
        [npx, "cap", "sync", "android"],
        cwd=repo,
        timeout=120,
    )

    android_dir = repo / "android"

    gradle = (
        android_dir / "gradlew.bat"
        if os.name == "nt"
        else android_dir / "gradlew"
    )

    if not gradle.exists():
        raise SetupError(
            f"Gradle wrapper not found: {gradle}"
        )

    if os.name != "nt":
        gradle.chmod(
            gradle.stat().st_mode | 0o111
        )

    run(
        [str(gradle), "assembleDebug"],
        cwd=android_dir,
        timeout=300,
    )

    apk = (
        android_dir
        / "app"
        / "build"
        / "outputs"
        / "apk"
        / "debug"
        / "app-debug.apk"
    )

    if not apk.exists():
        raise SetupError(
            f"APK was not generated: {apk}"
        )

    return apk


def install_android_app(
    adb: str,
    apk: Path,
) -> str:
    serial = ensure_adb_device(adb)

    run(
        [
            adb,
            "-s",
            serial,
            "shell",
            "am",
            "force-stop",
            APP_ID,
        ],
        timeout=10,
        check=False,
    )

    command = [
        adb,
        "-s",
        serial,
        "install",
        "--no-streaming",
        "-r",
        str(apk),
    ]

    try:
        result = run(
            command,
            timeout=75,
            check=False,
        )
    except SetupError:
        hard_reset_adb(adb)
        serial = ensure_adb_device(adb)
        command[2] = serial
        result = run(
            command,
            timeout=90,
            check=False,
        )

    if result.returncode != 0:
        raise SetupError(
            "APK installation failed."
        )

    return serial


def configure_reverse(
    adb: str,
    serial: str,
) -> None:
    for port in PORTS:
        run(
            [
                adb,
                "-s",
                serial,
                "reverse",
                f"tcp:{port}",
                f"tcp:{port}",
            ],
            timeout=10,
        )


def linux_preflight() -> None:
    if not sys.platform.startswith("linux"):
        return

    if not shutil.which("pactl"):
        print(
            "WARNING: pactl was not found. "
            "Install pipewire-pulse or PulseAudio utilities "
            "before using the phone as a Linux microphone."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install dependencies, build the Capacitor Android app, "
            "install the APK and configure ADB reverse."
        )
    )

    default_repo = (
        Path(__file__).resolve().parent.parent
    )

    parser.add_argument(
        "--repo",
        type=Path,
        default=default_repo,
    )
    parser.add_argument(
        "--skip-python-deps",
        action="store_true",
    )
    parser.add_argument(
        "--skip-android-build",
        action="store_true",
    )
    parser.add_argument(
        "--skip-apk-install",
        action="store_true",
    )

    args = parser.parse_args()
    repo = args.repo.resolve()

    if not (
        repo / "audio_sender.py"
    ).exists():
        raise SetupError(
            f"invalid repository path: {repo}"
        )

    linux_preflight()

    if not args.skip_python_deps:
        install_python_dependencies(
            repo
        )

    apk = (
        repo
        / "android"
        / "app"
        / "build"
        / "outputs"
        / "apk"
        / "debug"
        / "app-debug.apk"
    )

    if not args.skip_android_build:
        apk = build_android(repo)

    if not args.skip_apk_install:
        if not apk.exists():
            raise SetupError(
                "APK does not exist. Build Android first "
                "or remove --skip-android-build."
            )

        adb = find_adb()
        serial = install_android_app(
            adb,
            apk,
        )
        configure_reverse(
            adb,
            serial,
        )

    print()
    print(
        "audio-phone-speaker setup complete."
    )
    print(
        "Run the bridge:"
    )

    if os.name == "nt":
        print(
            r"  python .\audio_sender.py"
        )
    else:
        print(
            "  python ./audio_sender.py"
        )

    print(
        "Open Audio Phone Speaker manually on Android. "
        "The native bridges reconnect automatically."
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SetupError as error:
        print(
            "ERROR:",
            error,
            file=sys.stderr,
        )
        raise SystemExit(1)
