# Yoga Book Camera

Yoga Book Camera is the userspace camera-integration project for the Lenovo
Yoga Book YB1-X91L. Its primary acceptance target is a usable front-facing
OV2740 camera for video meetings. The rear OV8858 camera is a separate 8 MP
sensor and does not substitute for the front camera.

## Current status

The project now contains an experimental raw Bayer userspace ISP and Debian
package. On the physical YB1-X91L it has proven:

- continuous front OV2740 BGGR10 capture and 1280x720 browser output;
- continuous rear OV8858 BGGR10 capture with per-row stride removal;
- automatic sensor exposure/gain and userspace white balance convergence;
- GPU color-shading correction and temporal/spatial denoising;
- a standard V4L2 loopback endpoint usable by Chromium applications.

It is not yet a stable release. Front-camera color and noise still need final
physical tuning against human skin in several lighting conditions. The rear
camera transport is proven, but its image profile still requires a physically
uncovered, lit-scene calibration. See [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).

See [docs/STATUS.md](docs/STATUS.md) for established evidence,
[docs/CALIBRATION.md](docs/CALIBRATION.md) for the calibration method and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for subsystem ownership. The
inspected official package is documented in
[docs/WINDOWS-DRIVER-EVIDENCE.md](docs/WINDOWS-DRIVER-EVIDENCE.md).

## Repository scope

This repository will own:

- a safe userspace AIQ/3A or equivalent image-control implementation;
- user-supplied Lenovo tuning inspection and staging tools;
- integration with V4L2, PipeWire and meeting applications;
- camera-specific packaging and operational documentation.

It deliberately does not own:

- OV2740, OV8858 or AtomISP kernel changes, which remain in
  `Yoga-Book-Linux-Kernel`;
- general hardware acceptance, which remains in `Yoga-Book-Validator`;
- redistribution of Lenovo or Intel proprietary tuning/software without a
  verified license;
- an older AtomISP DKMS replacement for the newer in-tree kernel driver.

## Inspecting Lenovo tuning

The tuning file is not included. After obtaining it from Lenovo's official
Yoga Book Windows driver package, inspect it locally:

```bash
tools/inspect-aiqb.sh /path/to/OV2740_CJAE533_CHT.cpf
```

The command validates the AIQB signature, Yoga Book OV2740 description, exact
size and the hash of the physically inspected Lenovo artifact. To keep an
ignored private working copy for research:

```bash
tools/stage-lenovo-tuning.sh /path/to/OV2740_CJAE533_CHT.cpf
```

Staging does not install or activate the file because there is not yet a safe
runtime consumer.

## Development

```bash
sudo apt install binutils coreutils debhelper make shellcheck
make test
make package
```

## Runtime

The package deliberately enables AtomISP raw output only on a DMI-matched
YB1-X91L and exposes one `Yoga Book Camera` endpoint. The front camera is the
default. Switch sensors with:

```bash
yogabook_camera_control.py select front
yogabook_camera_control.py select rear
yogabook_camera_control.py status
yogabook_camera_control.py capture "$HOME/Pictures/yogabook-rear.jpg"
```

Only one sensor can stream through AtomISP2401 at a time. No release should
claim complete camera support until every physical gate in the acceptance
document passes.
