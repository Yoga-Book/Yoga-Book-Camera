#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for camera behavior that does not require physical hardware."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from yogabook_camera import (
    command_when_available,
    configure_hardware,
    discover_sensor_subdevice,
    is_white_balance_candidate,
    lock_loopback_format,
    load_config,
    resolve_camera,
)
from yogabook_camera_capture import RearStillCapture


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

    def test_non_clipped_pixel_is_used_for_white_balance(self) -> None:
        self.assertTrue(is_white_balance_candidate(188.5))

    def test_dark_clipped_pixel_is_excluded_from_white_balance(self) -> None:
        self.assertFalse(is_white_balance_candidate(23.9))

    def test_bright_clipped_pixel_is_excluded_from_white_balance(self) -> None:
        self.assertFalse(is_white_balance_candidate(235.1))


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
