# Yoga Book Camera

Yoga Book Camera is the userspace camera-integration project for the Lenovo
Yoga Book YB1-X91L. Its primary acceptance target is a usable front-facing
OV2740 camera for video meetings. The rear OV8858 camera is a separate 8 MP
sensor and does not substitute for the front camera.

## Current status

This project is experimental and does **not** yet provide a production camera
package. The kernel can transport complete 1920x1080 frames from the OV2740,
but the visible result remains dark, noisy and poorly corrected. That is not an
acceptable camera implementation.

The remaining problem is the image-processing pipeline: automatic exposure,
automatic white balance, lens shading, color correction, gamma and denoising.
Lenovo supplied sensor-specific Intel AIQ tuning in
`OV2740_CJAE533_CHT.cpf`, but no compatible open Linux AIQ/3A service has yet
been proven on this device.

See [docs/STATUS.md](docs/STATUS.md) for established evidence and
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
sudo apt install binutils coreutils make shellcheck
make test
```

No release or Debian package should claim to fix the camera until the front
camera passes real video-call testing with stable exposure, color and noise
under multiple lighting conditions.
