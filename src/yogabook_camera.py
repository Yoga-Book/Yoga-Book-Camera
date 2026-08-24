#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Raw Bayer userspace ISP for Lenovo Yoga Book YB1-X91L cameras."""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst


DEFAULT_CONFIG_PATHS = (
    Path("/etc/yogabook-camera/cameras.json"),
    Path(__file__).resolve().parents[1] / "config" / "cameras.json",
)

FRONT_FRAGMENT_SHADER = r"""
precision mediump float;
varying vec2 v_texcoord;
uniform sampler2D tex;
void main() {
    vec4 pixel = texture2D(tex, v_texcoord);
    vec2 texel = vec2(1.0 / 1280.0, 1.0 / 720.0);
    vec3 neighbours =
        texture2D(tex, v_texcoord + vec2(texel.x, 0.0)).rgb +
        texture2D(tex, v_texcoord - vec2(texel.x, 0.0)).rgb +
        texture2D(tex, v_texcoord + vec2(0.0, texel.y)).rgb +
        texture2D(tex, v_texcoord - vec2(0.0, texel.y)).rgb;
    vec3 c = pixel.rgb + 0.16 * (pixel.rgb - 0.25 * neighbours);

    /* Correct the measured OV2740 left-edge color shading. */
    float left = clamp((0.55 - v_texcoord.x) / 0.55, 0.0, 1.0);
    float right = clamp((v_texcoord.x - 0.55) / 0.45, 0.0, 1.0);
    c *= vec3(1.0 + 0.17 * left + 0.15 * right,
              1.0 + 0.10 * left,
              1.0 - 0.12 * right);

    /* Preserve neutral tones while removing excess blue from warm colors. */
    c.b -= 0.70 * max(c.r - c.g, 0.0);

    gl_FragColor = vec4(clamp(c, 0.0, 1.0), pixel.a);
}
"""

IDENTITY_FRAGMENT_SHADER = r"""
precision mediump float;
varying vec2 v_texcoord;
uniform sampler2D tex;
void main() {
    gl_FragColor = texture2D(tex, v_texcoord);
}
"""


def notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = f"\0{address[1:]}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        notifier.connect(address)
        notifier.sendall(message.encode())


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expose a corrected Yoga Book camera through V4L2 loopback"
    )
    parser.add_argument("--camera", choices=("auto", "front", "rear"), default="auto")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--capture-device", default="/dev/video0")
    parser.add_argument("--output-device", default="/dev/video10")
    parser.add_argument("--media-device", default="/dev/media0")
    parser.add_argument("--no-auto", action="store_true", help="disable adaptive tone and white balance")
    parser.add_argument("--print-pipeline", action="store_true")
    return parser.parse_args()


def load_config(path: Path | None, camera: str) -> dict[str, Any]:
    candidates = (path,) if path else DEFAULT_CONFIG_PATHS
    for candidate in candidates:
        if candidate and candidate.is_file():
            with candidate.open(encoding="utf-8") as stream:
                data = json.load(stream)
            try:
                return data[camera]
            except KeyError as error:
                raise SystemExit(f"camera profile {camera!r} is missing from {candidate}") from error
    rendered = ", ".join(str(item) for item in candidates if item)
    raise SystemExit(f"camera configuration not found; checked: {rendered}")


def resolve_camera(camera: str) -> str:
    if camera != "auto":
        return camera
    selection = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    selection = selection / "yogabook-camera" / "active-camera"
    try:
        selected = selection.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "front"
    if selected not in ("front", "rear"):
        raise SystemExit(f"invalid camera selection in {selection}: {selected!r}")
    return selected


def command(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        rendered = " ".join(arguments)
        if detail:
            rendered = f"{rendered}: {detail}"
        raise RuntimeError(rendered) from error


def command_when_available(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Retry only the short V4L2 close/open handoff race."""
    for attempt in range(50):
        result = command(*arguments, check=False)
        if result.returncode == 0:
            return result
        detail = (result.stderr or result.stdout or "").strip()
        if "Device or resource busy" not in detail or attempt == 49:
            rendered = " ".join(arguments)
            if detail:
                rendered = f"{rendered}: {detail}"
            raise RuntimeError(rendered)
        time.sleep(0.1)
    raise AssertionError("unreachable V4L2 retry loop")


def discover_sensor_subdevice(media_device: str, sensor: str) -> str:
    topology = command("media-ctl", "-d", media_device, "-p").stdout
    entity = False
    for line in topology.splitlines():
        if line.startswith("- entity "):
            match = re.match(r"- entity \d+: (.+?) \(\d+ pads?[,)]", line)
            candidate = match.group(1).casefold() if match else ""
            expected = sensor.casefold()
            entity = candidate == expected or (
                " " not in expected and candidate.startswith(f"{expected} ")
            )
        elif entity:
            match = re.search(r"device node name (/dev/v4l-subdev\d+)", line)
            if match:
                return match.group(1)
    raise RuntimeError(f"cannot find {sensor} sensor subdevice in {media_device}")


def configure_hardware(
    capture_device: str,
    media_device: str,
    profile: dict[str, Any],
) -> str:
    command_when_available(
        "v4l2-ctl",
        "-d",
        capture_device,
        f"--set-input={profile['input']}",
    )
    sensor_device = discover_sensor_subdevice(media_device, str(profile["sensor"]))
    controls = profile.get("sensor_controls", {})
    if controls:
        rendered = ",".join(f"{name}={value}" for name, value in controls.items())
        command("v4l2-ctl", "-d", sensor_device, f"--set-ctrl={rendered}")
    # AtomISP's CSS firmware has no raw-output binary for the default Preview
    # run mode. Explicit Still capture mode is required after every driver
    # probe; relying on a value left by an earlier application breaks reboot.
    isp_device = discover_sensor_subdevice(media_device, "Atom ISP")
    command(
        "v4l2-ctl",
        "-d",
        isp_device,
        "--set-ctrl=atomisp_run_mode=2",
    )
    return sensor_device


def lock_loopback_format(output_device: str) -> None:
    """Prevent a late browser open from reconfiguring and stalling the writer."""
    command(
        "v4l2-ctl",
        "-d",
        output_device,
        "--set-ctrl=keep_format=1,sustain_framerate=1,timeout=1000",
    )


def is_white_balance_candidate(level: float) -> bool:
    """Exclude only clipped pixels from whole-frame white-balance feedback."""
    return 24 <= level <= 235


def normalized_bayer_buffer(
    source: Gst.Buffer,
    *,
    width: int,
    height: int,
    source_stride: int,
) -> Gst.Buffer | None:
    """Copy a padded AtomISP Bayer frame into GStreamer's tight layout."""
    tight_stride = width * 2
    payload_size = tight_stride * height
    row_bytes = source_stride * height
    size = source.get_size()
    if size < row_bytes:
        print(
            f"ERROR: short raw frame: {size} bytes, expected at least {row_bytes}",
            file=sys.stderr,
        )
        return None
    if source_stride == tight_stride:
        return source.copy_region(
            Gst.BufferCopyFlags.MEMORY
            | Gst.BufferCopyFlags.TIMESTAMPS
            | Gst.BufferCopyFlags.META,
            0,
            payload_size,
        )

    payload = source.extract_dup(0, row_bytes)
    tight_payload = b"".join(
        payload[row * source_stride : row * source_stride + tight_stride]
        for row in range(height)
    )
    target = Gst.Buffer.new_allocate(None, payload_size, None)
    written = target.fill(0, tight_payload)
    if written != payload_size:
        raise RuntimeError(f"short Bayer copy: {written} of {payload_size} bytes")
    target.pts = source.pts
    target.dts = source.dts
    target.duration = source.duration
    target.offset = source.offset
    target.offset_end = source.offset_end
    return target


class CameraPipeline:
    def __init__(
        self,
        arguments: argparse.Namespace,
        profile: dict[str, Any],
        sensor_device: str,
    ):
        self.arguments = arguments
        self.profile = profile
        self.loop = GLib.MainLoop()
        self.frames = 0
        self.statistics_frames = 0
        self.last_report = 0
        self.still_pending = False
        self.ready_reported = False
        self.sensor_device = sensor_device
        self.sensor_controls = {
            name: int(value) for name, value in profile.get("sensor_controls", {}).items()
        }
        self.control_ranges = profile.get("control_ranges", {})

        width = int(profile["width"])
        height = int(profile["height"])
        self.source_stride = int(profile["bytes_per_line"])
        processing = profile["processing"]
        output_width = int(profile["output_width"])
        output_height = int(profile["output_height"])
        caps = (
            f"video/x-bayer,format={profile['bayer_format']},"
            f"width={width},height={height},framerate=30/1"
        )

        self.capture_description = (
            f"v4l2src device={arguments.capture_device} io-mode=2 do-timestamp=true ! "
            f"{caps} ! appsink name=capture emit-signals=true sync=false "
            "max-buffers=2 drop=true"
        )
        self.process_description = (
            f"appsrc name=source is-live=true format=time caps=\"{caps}\" "
            "max-buffers=2 leaky-type=downstream ! "
            "bayer2rgb ! videoconvert n-threads=4 ! "
            "videoscale method=bilinear n-threads=4 ! "
            f"video/x-raw,width={output_width},height={output_height},framerate=30/1 ! "
            f"videobalance contrast={processing['contrast']} brightness=0.0 "
            f"saturation={processing['saturation']} ! "
            f"gamma name=tone gamma={processing['gamma']} ! "
            "videoconvert n-threads=4 ! "
            f"video/x-raw,format=RGBA,width={output_width},height={output_height},framerate=30/1 ! "
            f"frei0r-filter-coloradj-rgb name=whitebalance action=1.0 "
            f"r={processing['rgb_red']} g={processing['rgb_green']} "
            f"b={processing['rgb_blue']} keep-luma=false ! "
            f"frei0r-filter-hqdn3d spatial={processing['denoise_spatial']} "
            f"temporal={processing['denoise_temporal']} ! "
            "glupload ! glshader name=correction ! gldownload ! tee name=processed "
            "processed. ! queue max-size-buffers=2 leaky=downstream ! "
            "videoconvert n-threads=4 ! "
            f"video/x-raw,format=YUY2,width={output_width},height={output_height},framerate=30/1 ! "
            f"v4l2sink device={arguments.output_device} sync=false "
            "processed. ! queue max-size-buffers=1 leaky=downstream ! "
            "videorate drop-only=true max-rate=2 ! "
            "videoscale method=nearest-neighbour ! "
            "video/x-raw,format=RGBA,width=64,height=36,framerate=2/1 ! "
            "appsink name=statistics emit-signals=true sync=false max-buffers=1 drop=true"
        )
        if arguments.print_pipeline:
            print(self.capture_description)
            print(self.process_description)

        self.process = Gst.parse_launch(self.process_description)
        self.capture: Gst.Element | None = None
        self.appsink: Gst.Element | None = None
        self.capture_bus: Gst.Bus | None = None
        self.capture_bus_handler: int | None = None
        self.sample_handler: int | None = None
        self.eos_handler: int | None = None
        self.appsrc = self.process.get_by_name("source")
        self.statistics = self.process.get_by_name("statistics")
        self.tone = self.process.get_by_name("tone")
        self.whitebalance = self.process.get_by_name("whitebalance")
        shader = FRONT_FRAGMENT_SHADER if arguments.camera == "front" else IDENTITY_FRAGMENT_SHADER
        self.process.get_by_name("correction").set_property("fragment", shader)
        self.gamma = float(processing["gamma"])
        self.rgb_red = float(processing["rgb_red"])
        self.rgb_blue = float(processing["rgb_blue"])

        self.statistics.connect("new-sample", self._new_statistics_sample)
        process_bus = self.process.get_bus()
        process_bus.add_signal_watch()
        process_bus.connect("message", self._message)
        self._create_capture()

    def _create_capture(self) -> None:
        if self.capture is not None:
            raise RuntimeError("physical capture pipeline already exists")
        self.capture = Gst.parse_launch(self.capture_description)
        self.appsink = self.capture.get_by_name("capture")
        self.sample_handler = self.appsink.connect("new-sample", self._new_raw_sample)
        self.eos_handler = self.appsink.connect(
            "eos", lambda _sink: self.appsrc.emit("end-of-stream")
        )
        self.capture_bus = self.capture.get_bus()
        self.capture_bus.add_signal_watch()
        self.capture_bus_handler = self.capture_bus.connect("message", self._message)

    def _destroy_capture(self) -> None:
        capture = self.capture
        appsink = self.appsink
        if capture is None:
            return
        # Disconnect EOS before stopping. A temporary physical-source pause
        # must not terminate the independent browser-facing output pipeline.
        if appsink is not None and self.sample_handler is not None:
            appsink.disconnect(self.sample_handler)
        if appsink is not None and self.eos_handler is not None:
            appsink.disconnect(self.eos_handler)
        self.sample_handler = None
        self.eos_handler = None
        capture.set_state(Gst.State.NULL)
        state_result, current, _ = capture.get_state(10 * Gst.SECOND)
        if state_result == Gst.StateChangeReturn.FAILURE or current != Gst.State.NULL:
            raise RuntimeError(f"physical capture did not stop cleanly: {current.value_nick}")
        if self.capture_bus is not None:
            if self.capture_bus_handler is not None:
                self.capture_bus.disconnect(self.capture_bus_handler)
            self.capture_bus.remove_signal_watch()
        self.capture_bus_handler = None
        self.capture_bus = None
        self.appsink = None
        self.capture = None
        del appsink
        del capture
        gc.collect()

    def _tight_buffer(self, source: Gst.Buffer) -> Gst.Buffer | None:
        return normalized_bayer_buffer(
            source,
            width=int(self.profile["width"]),
            height=int(self.profile["height"]),
            source_stride=self.source_stride,
        )

    def _new_raw_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        output = self._tight_buffer(sample.get_buffer())
        if output is None:
            return Gst.FlowReturn.ERROR
        self.frames += 1
        return self.appsrc.emit("push-buffer", output)

    def _new_statistics_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None or self.arguments.no_auto:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        mapped, info = buffer.map(Gst.MapFlags.READ)
        if not mapped:
            return Gst.FlowReturn.OK
        pixels = memoryview(info.data)
        if self.statistics_frames == 0:
            lock_loopback_format(self.arguments.output_device)
            print(
                f"CAMERA_STATS_CAPS caps={sample.get_caps().to_string()} "
                f"bytes={len(pixels)} first={list(pixels[:16])}",
                flush=True,
            )
            if not self.ready_reported:
                notify_systemd("READY=1\nSTATUS=Corrected camera frames are available")
                self.ready_reported = True
        luminance: list[float] = []
        valid_r = valid_g = valid_b = 0.0
        valid_count = 0
        for offset in range(0, min(len(pixels), 64 * 36 * 4), 4):
            red, green, blue = pixels[offset], pixels[offset + 1], pixels[offset + 2]
            level = (54 * red + 183 * green + 19 * blue) / 256.0
            luminance.append(level)
            if is_white_balance_candidate(level):
                valid_r += red
                valid_g += green
                valid_b += blue
                valid_count += 1
        buffer.unmap(info)
        if not luminance:
            return Gst.FlowReturn.OK
        self.statistics_frames += 1
        if self.statistics_frames % 2:
            return Gst.FlowReturn.OK

        luminance.sort()
        percentile = luminance[int(len(luminance) * 0.70)] / 255.0
        target_luma = float(self.profile["processing"]["target_luma"])
        sensor_changed = self._adjust_sensor_exposure(percentile, target_luma)
        if not sensor_changed:
            if percentile < target_luma - 0.035:
                self.gamma = min(2.4, self.gamma + 0.04)
            elif percentile > target_luma + 0.035:
                self.gamma = max(1.0, self.gamma - 0.04)
        self.tone.set_property("gamma", self.gamma)

        red_green = blue_green = 0.0
        if percentile < 0.90 and valid_count >= len(luminance) // 4 and valid_g > 0:
            red_green = valid_r / valid_g
            blue_green = valid_b / valid_g
            target_rg = float(self.profile["processing"]["target_red_green"])
            target_bg = float(self.profile["processing"]["target_blue_green"])
            self.rgb_red = min(0.90, max(0.40, self.rgb_red + 0.030 * (target_rg - red_green)))
            self.rgb_blue = min(1.0, max(0.40, self.rgb_blue + 0.030 * (target_bg - blue_green)))
            self.whitebalance.set_property("r", self.rgb_red)
            self.whitebalance.set_property("b", self.rgb_blue)

        if self.statistics_frames - self.last_report >= 25:
            print(
                f"CAMERA_STATS frames={self.frames} p70={percentile:.3f} "
                f"gamma={self.gamma:.3f} rg={red_green:.3f} bg={blue_green:.3f} "
                f"rgb_r={self.rgb_red:.3f} rgb_b={self.rgb_blue:.3f}",
                flush=True,
            )
            self.last_report = self.statistics_frames
        return Gst.FlowReturn.OK

    def _set_sensor_control(self, name: str, value: int) -> None:
        command(
            "v4l2-ctl",
            "-d",
            self.sensor_device,
            f"--set-ctrl={name}={value}",
        )
        self.sensor_controls[name] = value
        print(f"CAMERA_AE {name}={value}", flush=True)

    def _adjust_sensor_exposure(self, measured: float, target: float) -> bool:
        if measured > target + 0.06:
            order = ("digital_gain", "analogue_gain", "exposure")
            scale = 0.82
            select = max
            bound_index = 0
        elif measured < target - 0.06:
            order = ("exposure", "analogue_gain", "digital_gain")
            scale = 1.18
            select = min
            bound_index = 1
        else:
            return False

        for name in order:
            if name not in self.sensor_controls or name not in self.control_ranges:
                continue
            lower, upper = (int(value) for value in self.control_ranges[name])
            current = self.sensor_controls[name]
            bound = (lower, upper)[bound_index]
            if current == bound:
                continue
            candidate = int(round(current * scale))
            if measured > target:
                candidate = select(lower, candidate)
                if candidate >= current:
                    candidate = lower
            else:
                candidate = select(upper, candidate)
                if candidate <= current:
                    candidate = upper
            self._set_sensor_control(name, candidate)
            return True
        return False

    def _message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"ERROR: {error}\n{debug or ''}", file=sys.stderr, flush=True)
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS and message.src == self.process:
            self.loop.quit()

    def run(self) -> int:
        self.process.set_state(Gst.State.PLAYING)
        if self.capture is None:
            raise RuntimeError("physical capture pipeline is missing")
        self.capture.set_state(Gst.State.PLAYING)
        try:
            self.loop.run()
        finally:
            self._destroy_capture()
            self.appsrc.emit("end-of-stream")
            self.process.set_state(Gst.State.NULL)
        print(f"CAMERA_STOP frames={self.frames}", flush=True)
        return 0

    def stop(self, _signal: int, _frame: object) -> None:
        self.loop.quit()

    def request_still(self, _signal: int, _frame: object) -> None:
        if not self.still_pending:
            self.still_pending = True
            GLib.idle_add(self._handle_still_request)

    @staticmethod
    def _still_paths() -> tuple[Path, Path]:
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        directory = runtime / "yogabook-camera"
        return directory / "still-request.json", directory / "still-result.json"

    def _handle_still_request(self) -> bool:
        request_path, result_path = self._still_paths()
        request_id = "unknown"
        result: dict[str, Any]
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request_id = str(request["id"])
            self._destroy_capture()

            from yogabook_camera_capture import capture_to_path

            still_arguments = argparse.Namespace(
                output=Path(request["output"]),
                config=self.arguments.config,
                capture_device=self.arguments.capture_device,
                media_device=self.arguments.media_device,
                focus_position=request.get("focus_position"),
                no_autofocus=bool(request.get("no_autofocus", False)),
            )
            rear_profile = load_config(self.arguments.config, "rear")
            capture_to_path(still_arguments, rear_profile)
            result = {
                "id": request_id,
                "ok": True,
                "path": str(still_arguments.output.expanduser().resolve()),
            }
        except Exception as error:
            print(f"ERROR: rear still request failed: {error}", file=sys.stderr, flush=True)
            result = {"id": request_id, "ok": False, "error": str(error)}
        finally:
            try:
                self._destroy_capture()
                self.sensor_device = configure_hardware(
                    self.arguments.capture_device,
                    self.arguments.media_device,
                    self.profile,
                )
                self.sensor_controls = {
                    name: int(value)
                    for name, value in self.profile.get("sensor_controls", {}).items()
                }
                self._create_capture()
                if self.capture is None:
                    raise RuntimeError("physical capture pipeline was not recreated")
                self.capture.set_state(Gst.State.PLAYING)
            except Exception as error:
                print(f"ERROR: cannot restore camera preview: {error}", file=sys.stderr, flush=True)
                result = {"id": request_id, "ok": False, "error": f"preview restore failed: {error}"}
            result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = result_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(result_path)
            self.still_pending = False
        return GLib.SOURCE_REMOVE


def main() -> int:
    arguments = parse_arguments()
    arguments.camera = resolve_camera(arguments.camera)
    profile = load_config(arguments.config, arguments.camera)
    os.environ.setdefault("GST_GL_PLATFORM", "egl")
    os.environ.setdefault("GST_GL_WINDOW", "surfaceless")
    os.environ.setdefault("GST_GL_API", "gles2")
    Gst.init(None)
    sensor_device = configure_hardware(arguments.capture_device, arguments.media_device, profile)
    pipeline = CameraPipeline(arguments, profile, sensor_device)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, pipeline.stop)
    signal.signal(signal.SIGUSR2, pipeline.request_still)
    return pipeline.run()


if __name__ == "__main__":
    raise SystemExit(main())
