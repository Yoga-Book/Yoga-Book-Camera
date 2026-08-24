#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Select and inspect the active Yoga Book camera."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time


SERVICE = "yogabook-camera.service"


def service_command(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("systemctl", *arguments, SERVICE), check=check, text=True)


def service_pid() -> int:
    result = subprocess.run(
        ("systemctl", "show", "--property=MainPID", "--value", SERVICE),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise RuntimeError("camera service has no running main process")
    return pid


def selection_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "yogabook-camera" / "active-camera"


def runtime_camera_path() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "yogabook-camera" / "active-camera"


def select(camera: str) -> None:
    destination = selection_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(f"{camera}\n", encoding="utf-8")
    temporary.replace(destination)
    current_pid = service_pid()
    os.kill(current_pid, signal.SIGHUP)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            active = runtime_camera_path().read_text(encoding="utf-8").strip()
        except (RuntimeError, subprocess.CalledProcessError, ValueError):
            active = ""
        except FileNotFoundError:
            active = ""
        if (
            active == camera
            and service_command("is-active", "--quiet").returncode == 0
        ):
            print(f"Active Yoga Book camera: {camera}")
            return
        time.sleep(0.2)
    raise TimeoutError("camera service did not become ready after selection")


def status() -> None:
    try:
        camera = selection_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        camera = "front"
    print(f"Selected camera: {camera}")
    try:
        active = runtime_camera_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        active = "unavailable"
    print(f"Active camera: {active}")
    service_command("--no-pager", "status")


def capture(output: Path, focus_position: int | None, no_autofocus: bool) -> None:
    if service_command("is-active", "--quiet").returncode != 0:
        raise RuntimeError("camera system service is not active")

    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    directory = runtime / "yogabook-camera"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    request_path = directory / "still-request.json"
    result_path = directory / "still-result.json"
    request_id = secrets.token_hex(12)
    request = {
        "id": request_id,
        "output": str(output),
        "focus_position": focus_position,
        "no_autofocus": no_autofocus,
    }
    temporary = request_path.with_suffix(".tmp")
    result_path.unlink(missing_ok=True)
    temporary.write_text(json.dumps(request) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(request_path)
    os.kill(service_pid(), signal.SIGUSR2)

    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("id") == request_id:
                request_path.unlink(missing_ok=True)
                result_path.unlink(missing_ok=True)
                if not result.get("ok"):
                    raise RuntimeError(str(result.get("error", "rear still failed")))
                print(f"Rear still captured: {result['path']}")
                return
        if service_command("is-active", "--quiet").returncode != 0:
            raise RuntimeError("camera service stopped while capturing rear still")
        time.sleep(0.2)
    raise TimeoutError("timed out waiting for rear still capture")


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the Yoga Book camera service")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status")
    select_parser = subcommands.add_parser("select")
    select_parser.add_argument("camera", choices=("front", "rear"))
    capture_parser = subcommands.add_parser("capture")
    capture_parser.add_argument("output", type=Path)
    capture_parser.add_argument("--focus-position", type=int, choices=range(0, 1024))
    capture_parser.add_argument("--no-autofocus", action="store_true")
    arguments = parser.parse_args()
    if arguments.command == "select":
        select(arguments.camera)
    elif arguments.command == "capture":
        capture(arguments.output, arguments.focus_position, arguments.no_autofocus)
    else:
        status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
