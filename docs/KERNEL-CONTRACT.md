# Kernel and runtime contract

Yoga Book Camera is a userspace image-processing and integration package. It
does not carry kernel drivers, and installing it on an unmodified kernel does
not create a working YB1-X91L camera stack. This document defines the kernel
capabilities the package consumes and the boundary between transport evidence
and physical image acceptance.

## Required kernel capabilities

The stable contract is expressed as capabilities rather than commit IDs so it
survives rebases and upstream integration:

| Capability | Runtime requirement |
| --- | --- |
| Yoga Book ACPI matching | Bind `OVTI2740` to OV2740 and `INT3477` to OV8858. |
| Firmware endpoint completion | Describe the front two-lane and rear four-lane CSI-2 links and associate the rear WV517S actuator. |
| Front transport geometry | Select the OV2740 288 MHz link frequency and expose its 1932x1092 BGGR10 transport as 1920x1080 using 12x12 padding. |
| Front color controls | Expose digital gain plus standard red- and blue-balance controls, with group-held channel updates. |
| Rear clock and gain setup | Apply the 19.2 MHz Cherry Trail mode overrides and expose the OV8858 1x-to-4x per-channel digital-gain range. |
| Opt-in raw capture | Provide AtomISP's `allow_raw_output` module parameter, disabled by default, and enumerate only the raw format matching the active sensor media-bus code. |
| Rear focus | Expose the WV517S standard absolute-focus control for full-resolution rear stills. This is not required for front preview. |

The corresponding Linux changes are maintained on the
`Yoga-Book_YB1-X91L` branch of `Yoga-Book-Linux-Kernel`. The upstream-oriented
media series contains the ACPI matching, endpoint, AtomISP, OV2740 and OV8858
changes. The WV517S actuator remains separate until its register programming
has a publishable provenance reference and appropriate maintainer coverage.

## Runtime policy

Raw output is deliberately default-off in the kernel. The package's early
system service checks the DMI product name for `YB1-X91L`, verifies that the
kernel gate exists, and only then enables:

```text
/sys/module/atomisp/parameters/allow_raw_output
```

The package expects an AtomISP media graph containing entities named
`ov2740`, `ov8858` and `Atom ISP`. Runtime code resolves sensor subdevices by
media-entity name; it does not depend on a fixed subdevice number. The current
physical capture and media-controller defaults are `/dev/video0` and
`/dev/media0`.

Only one physical sensor can stream at a time. The service converts the active
BGGR10 stream and keeps two V4L2 loopback outputs available:

- `/dev/video10`: `Front Camera`;
- `/dev/video11`: `Rear Camera`.

Both application-visible nodes are 1280x720 YUYV. Opening one endpoint selects
the corresponding physical sensor after a debounce interval; they are not two
simultaneous AtomISP pipelines.

## Provenance and copyright boundary

The kernel files retain their existing upstream SPDX identifiers and copyright
notices. The Yoga Book OV8858 manual-white-balance implementation follows the
GPL-2.0 upstream Intel OV5670 implementation, so `drivers/media/i2c/ov8858.c`
also retains the exact `Copyright (c) 2017 Intel Corporation.` notice.

The Yoga Book mode and link values are documented hardware-configuration facts
validated against the device and physical captures. No Lenovo proprietary
driver source is distributed. The inspected Lenovo/Intel AIQB tuning file is
also not distributed or activated by this package; only its public metadata
and checksum are tracked. The WV517S implementation must not be represented as
upstream-ready until its register provenance is resolved.

## Verification boundary

After installing a compatible kernel and this package, collect at least:

```bash
uname -r
cat /sys/module/atomisp/parameters/allow_raw_output
media-ctl -p -d /dev/media0
v4l2-ctl -d /dev/video10 --all
v4l2-ctl -d /dev/video11 --all
systemctl --no-pager status yogabook-camera.service
yogabook_camera_control.py status
```

Then perform bounded front and rear opens and check the current boot journal
for AtomISP, CSS, sensor or service errors. These checks establish kernel and
userspace transport only. Natural color, exposure, focus, detail, browser
switching and meeting behavior remain the physical gates in
[ACCEPTANCE.md](ACCEPTANCE.md).
