#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for camera behavior that does not require physical hardware."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import select
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from yogabook_camera import (
    CameraActivityMonitor,
    CameraClientMonitor,
    CameraPipeline,
    CameraSelectionMonitor,
    command_when_available,
    configure_hardware,
    discover_sensor_subdevice,
    ensure_safe_start_temperature,
    externally_open_cameras,
    hottest_temperature_celsius,
    is_white_balance_candidate,
    lock_loopback_format,
    load_config,
    prepare_loopback_format,
    resolve_camera,
)
from yogabook_camera_capture import RearStillCapture
from yogabook_camera_control import read_camera_state


class CameraConfigurationTests(unittest.TestCase):
    def test_auto_selection_defaults_to_front(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
                self.assertEqual(resolve_camera("auto"), "front")

    def test_auto_selection_reads_persisted_camera(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selection = Path(temporary) / "yogabook-camera" / "active-camera"
            selection.parent.mkdir(parents=True)
            selection.write_text("rear\n", encoding="utf-8")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
                self.assertEqual(resolve_camera("auto"), "rear")

    def test_auto_selection_rejects_invalid_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selection = Path(temporary) / "yogabook-camera" / "active-camera"
            selection.parent.mkdir(parents=True)
            selection.write_text("external\n", encoding="utf-8")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
                with self.assertRaises(SystemExit):
                    resolve_camera("auto")

    def test_explicit_selection_does_not_access_user_configuration(self) -> None:
        self.assertEqual(resolve_camera("front"), "front")
        self.assertEqual(resolve_camera("rear"), "rear")

    def test_both_profiles_load(self) -> None:
        config = Path(__file__).resolve().parents[1] / "config" / "cameras.json"
        self.assertEqual(load_config(config, "front")["sensor"], "ov2740")
        self.assertEqual(load_config(config, "rear")["sensor"], "ov8858")

    def test_external_camera_open_is_detected_by_descriptor_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            descriptors = proc_root / "123" / "fd"
            descriptors.mkdir(parents=True)
            (descriptors / "5").symlink_to("/dev/null")
            self.assertEqual(
                externally_open_cameras(
                    {"front": "/dev/null"},
                    proc_root=proc_root,
                    own_pid=999,
                ),
                {"front"},
            )

    @patch("yogabook_camera.time.sleep")
    @patch("yogabook_camera.command")
    def test_v4l2_busy_handoff_is_retried(
        self,
        mocked_command: Mock,
        mocked_sleep: Mock,
    ) -> None:
        mocked_command.side_effect = (
            SimpleNamespace(returncode=1, stdout="", stderr="Device or resource busy"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        result = command_when_available("v4l2-ctl", "-d", "/dev/video0", "--set-input=1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(mocked_command.call_count, 2)
        mocked_sleep.assert_called_once_with(0.1)

    @patch("yogabook_camera.time.sleep")
    @patch("yogabook_camera.command")
    def test_v4l2_nonbusy_failure_is_not_retried(
        self,
        mocked_command: Mock,
        mocked_sleep: Mock,
    ) -> None:
        mocked_command.return_value = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Invalid argument",
        )
        with self.assertRaisesRegex(RuntimeError, "Invalid argument"):
            command_when_available("v4l2-ctl", "-d", "/dev/video0", "--set-input=1")
        mocked_command.assert_called_once()
        mocked_sleep.assert_not_called()

    @patch("yogabook_camera.command")
    def test_media_entity_lookup_is_exact(self, mocked_command: Mock) -> None:
        mocked_command.return_value = SimpleNamespace(
            stdout="""\
- entity 1: ATOM ISP CSI2-port0 (2 pads, 2 links, 0 routes)
            device node name /dev/v4l-subdev0
- entity 10: Atom ISP (2 pads, 4 links, 0 routes)
            device node name /dev/v4l-subdev3
"""
        )
        self.assertEqual(
            discover_sensor_subdevice("/dev/media0", "Atom ISP"),
            "/dev/v4l-subdev3",
        )

    @patch("yogabook_camera.command")
    def test_sensor_lookup_allows_i2c_bus_suffix(self, mocked_command: Mock) -> None:
        mocked_command.return_value = SimpleNamespace(
            stdout="""\
- entity 15: ov2740 2-0010 (1 pad, 1 link, 0 routes)
            device node name /dev/v4l-subdev5
"""
        )
        self.assertEqual(
            discover_sensor_subdevice("/dev/media0", "ov2740"),
            "/dev/v4l-subdev5",
        )

    @patch("yogabook_camera.discover_sensor_subdevice")
    @patch("yogabook_camera.command")
    def test_hardware_configuration_sets_raw_firmware_run_mode(
        self,
        mocked_command: Mock,
        mocked_discovery: Mock,
    ) -> None:
        mocked_discovery.side_effect = ("/dev/v4l-subdev5", "/dev/v4l-subdev3")
        mocked_command.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        profile = {
            "input": 0,
            "sensor": "ov2740",
            "sensor_controls": {"exposure": 100},
        }
        self.assertEqual(
            configure_hardware("/dev/video0", "/dev/media0", profile),
            "/dev/v4l-subdev5",
        )
        mocked_command.assert_any_call(
            "v4l2-ctl",
            "-d",
            "/dev/v4l-subdev3",
            "--set-ctrl=atomisp_run_mode=2",
        )

    @patch("yogabook_camera.command")
    def test_loopback_format_is_locked_for_late_browser_open(
        self,
        mocked_command: Mock,
    ) -> None:
        lock_loopback_format("/dev/video10")
        mocked_command.assert_called_once_with(
            "v4l2-ctl",
            "-d",
            "/dev/video10",
            "--set-ctrl=keep_format=1,sustain_framerate=1,timeout=1000",
        )

    @patch("yogabook_camera.command_when_available")
    def test_loopback_producer_format_is_established_before_pipeline_start(
        self,
        mocked_command: Mock,
    ) -> None:
        prepare_loopback_format("/dev/video10", 1280, 720)
        self.assertEqual(
            mocked_command.call_args_list,
            [
                unittest.mock.call(
                    "v4l2-ctl",
                    "-d",
                    "/dev/video10",
                    "--set-ctrl=keep_format=0",
                ),
                unittest.mock.call(
                    "v4l2-ctl",
                    "-d",
                    "/dev/video10",
                    "--set-fmt-video-out=width=1280,height=720,"
                    "pixelformat=YUYV,field=none,colorspace=rec709,xfer=srgb,"
                    "ycbcr=601,quantization=lim-range",
                ),
            ],
        )

    def test_pipeline_error_requests_failed_service_exit(self) -> None:
        pipeline = CameraPipeline.__new__(CameraPipeline)
        pipeline.pipeline_failed = False
        pipeline.loop = Mock()
        message = Mock()
        message.type = Gst.MessageType.ERROR
        message.parse_error.return_value = (RuntimeError("format mismatch"), "debug")

        with redirect_stderr(io.StringIO()):
            pipeline._message(Mock(), message)

        self.assertTrue(pipeline.pipeline_failed)
        pipeline.loop.quit.assert_called_once_with()

    def test_non_clipped_pixel_is_used_for_white_balance(self) -> None:
        self.assertTrue(is_white_balance_candidate(188.5))

    def test_dark_clipped_pixel_is_excluded_from_white_balance(self) -> None:
        self.assertFalse(is_white_balance_candidate(23.9))

    def test_bright_clipped_pixel_is_excluded_from_white_balance(self) -> None:
        self.assertFalse(is_white_balance_candidate(235.1))


class CameraControlStateTests(unittest.TestCase):
    def test_state_read_strips_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "active-camera"
            state.write_text("rear\n", encoding="utf-8")
            self.assertEqual(read_camera_state(state, "front"), "rear")

    def test_missing_state_returns_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "active-camera"
            self.assertEqual(read_camera_state(state, "front"), "front")

    def test_state_read_does_not_hide_other_io_errors(self) -> None:
        with patch(
            "yogabook_camera_control.Path.read_text",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaisesRegex(PermissionError, "denied"):
                read_camera_state(Path("active-camera"), "front")


class CameraSelectionMonitorTests(unittest.TestCase):
    def test_short_browser_probe_does_not_switch(self) -> None:
        monitor = CameraSelectionMonitor(1.0)
        self.assertIsNone(monitor.update({"rear"}, "front", 10.0))
        self.assertIsNone(monitor.update(set(), "front", 10.5))
        self.assertIsNone(monitor.update({"rear"}, "front", 11.0))

    def test_sustained_single_endpoint_switches(self) -> None:
        monitor = CameraSelectionMonitor(1.0)
        self.assertIsNone(monitor.update({"rear"}, "front", 20.0))
        self.assertEqual(monitor.update({"rear"}, "front", 21.0), "rear")

    def test_two_open_endpoints_are_ambiguous(self) -> None:
        monitor = CameraSelectionMonitor(1.0)
        self.assertIsNone(monitor.update({"front", "rear"}, "front", 30.0))
        self.assertIsNone(monitor.update({"front", "rear"}, "front", 31.5))

    def test_new_endpoint_wins_during_browser_handoff_overlap(self) -> None:
        monitor = CameraSelectionMonitor(1.0)
        self.assertIsNone(monitor.update({"rear"}, "rear", 30.0))
        self.assertIsNone(monitor.update({"front", "rear"}, "rear", 31.0))
        self.assertEqual(
            monitor.update({"front", "rear"}, "rear", 32.0),
            "front",
        )

    def test_active_endpoint_never_requests_a_switch(self) -> None:
        monitor = CameraSelectionMonitor(1.0)
        self.assertIsNone(monitor.update({"front"}, "front", 40.0))
        self.assertIsNone(monitor.update({"front"}, "front", 42.0))


class CameraActivityMonitorTests(unittest.TestCase):
    def monitor(self) -> CameraActivityMonitor:
        return CameraActivityMonitor(
            idle_delay=3.0,
            max_temperature=85.0,
            resume_temperature=75.0,
        )

    def test_streaming_stops_after_idle_delay(self) -> None:
        monitor = self.monitor()
        self.assertEqual(
            monitor.update(opened=set(), streaming=True, temperature=60.0, now=10.0),
            (None, None),
        )
        self.assertEqual(
            monitor.update(opened=set(), streaming=True, temperature=60.0, now=13.0),
            (False, "no camera clients"),
        )

    def test_open_client_resumes_idle_camera(self) -> None:
        monitor = self.monitor()
        self.assertEqual(
            monitor.update(opened={"front"}, streaming=False, temperature=60.0, now=20.0),
            (True, "camera client opened"),
        )

    def test_thermal_limit_stops_streaming_immediately(self) -> None:
        monitor = self.monitor()
        self.assertEqual(
            monitor.update(opened={"front"}, streaming=True, temperature=85.0, now=30.0),
            (False, "thermal limit"),
        )

    def test_thermal_hysteresis_prevents_rapid_restart(self) -> None:
        monitor = self.monitor()
        monitor.update(opened={"front"}, streaming=True, temperature=86.0, now=40.0)
        self.assertEqual(
            monitor.update(opened={"front"}, streaming=False, temperature=80.0, now=41.0),
            (None, None),
        )
        self.assertEqual(
            monitor.update(opened={"front"}, streaming=False, temperature=75.0, now=42.0),
            (True, "camera client opened"),
        )

    def test_invalid_temperature_hysteresis_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be below"):
            CameraActivityMonitor(3.0, 80.0, 80.0)


class CameraClientMonitorTests(unittest.TestCase):
    def test_open_and_close_events_track_external_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = Path(temporary) / "camera"
            endpoint.touch()
            monitor = CameraClientMonitor({"front": str(endpoint)})
            try:
                with endpoint.open(encoding="utf-8"):
                    self.assertTrue(select.select([monitor.descriptor], [], [], 1.0)[0])
                    self.assertFalse(monitor.consume())
                    self.assertEqual(monitor.opened, {"front"})
                self.assertTrue(select.select([monitor.descriptor], [], [], 1.0)[0])
                self.assertFalse(monitor.consume())
                self.assertEqual(monitor.opened, set())
            finally:
                monitor.close()


class ThermalReadingTests(unittest.TestCase):
    def test_invalid_firmware_zones_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            readings = (0, -273150, 63250, 79000, 160000)
            for index, reading in enumerate(readings):
                zone = root / f"thermal_zone{index}"
                zone.mkdir()
                (zone / "temp").write_text(f"{reading}\n", encoding="ascii")
            self.assertEqual(hottest_temperature_celsius(root), 79.0)

    def test_no_valid_temperature_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(hottest_temperature_celsius(Path(temporary)))

    def test_hot_start_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "refusing to start"):
            ensure_safe_start_temperature(85.0, 85.0)

    def test_cool_or_unknown_start_is_allowed(self) -> None:
        ensure_safe_start_temperature(84.9, 85.0)
        ensure_safe_start_temperature(None, 85.0)


class RearStillCaptureTests(unittest.TestCase):
    def capture(self, *, focus_position: int | None, no_autofocus: bool) -> RearStillCapture:
        capture = RearStillCapture.__new__(RearStillCapture)
        capture.arguments = Mock(
            focus_position=focus_position,
            no_autofocus=no_autofocus,
        )
        capture._capture_at_focus = Mock(return_value="frame")
        capture.autofocus = Mock(return_value=576)
        return capture

    def test_fixed_focus_uses_one_fresh_capture(self) -> None:
        capture = self.capture(focus_position=448, no_autofocus=False)
        self.assertEqual(capture.capture_frame(), "frame")
        capture.autofocus.assert_not_called()
        capture._capture_at_focus.assert_called_once_with(448)

    def test_no_autofocus_uses_safe_default(self) -> None:
        capture = self.capture(focus_position=None, no_autofocus=True)
        self.assertEqual(capture.capture_frame(), "frame")
        capture.autofocus.assert_not_called()
        capture._capture_at_focus.assert_called_once_with(300)

    def test_autofocus_captures_final_selected_position(self) -> None:
        capture = self.capture(focus_position=None, no_autofocus=False)
        self.assertEqual(capture.capture_frame(), "frame")
        capture.autofocus.assert_called_once_with()
        capture._capture_at_focus.assert_called_once_with(576)


if __name__ == "__main__":
    unittest.main()
