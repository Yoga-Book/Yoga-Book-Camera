# Architecture

Good camera output crosses several independently owned layers:

```text
OV2740 sensor
  -> CSI-2 transport and Yoga Book firmware properties       kernel
  -> AtomISP2401 CSS image-processing pipeline               kernel
  -> stride normalization and Bayer demosaic                 this project
  -> exposure, white balance and color/tone policy           this project
  -> V4L2/PipeWire camera exposure                            this project
  -> transport and physical acceptance                       Validator
```

## Design constraints

1. Use the upstream kernel sensor and AtomISP drivers as the base. The older
   `atomisp-6.10-dkms` project is useful for historical comparison, not as a
   replacement for the Yoga Book's newer customized kernel.
2. Treat Lenovo's AIQB tuning as user-supplied until redistribution rights are
   known. Store only its public metadata and checksum in Git.
3. Do not pretend that raw sensor controls replace ISP tuning. Sensor exposure
   and gains may support bring-up, but lens shading, color and denoising belong
   to the image pipeline.
4. Audit any legacy AtomISP userspace ABI command before enabling it. Never
   expose the complete private ioctl surface merely to run an old binary.
5. Keep application integration standard: the final front camera must appear
   through PipeWire/V4L2 without application-specific Meet workarounds.

## Incompatible Intel camera generations

| Component | Hardware role | Yoga Book relevance |
| --- | --- | --- |
| AtomISP2401 | Cherry Trail CSI and ISP/CSS pipeline | The actual YB1-X91L hardware |
| `intel_atomisp2_pm` | Legacy dummy PCI power-gating driver | Obsolete when the real AtomISP driver is bound |
| IPU6 | Modern Intel ISYS/PSYS camera architecture | Different hardware, firmware and userspace ABI |

IPU6 camera engines cannot run on PCI device `8086:22b8`. Their userspace
architecture may inform future design, but their firmware, processing
algorithms and camera HAL are not binaries or drivers for AtomISP2401.

The historical `intel_atomisp2_pm.c` driver supports `8086:22b8`, but only to
disable CSI ports and power-gate the IUNIT for D3 suspend. The active upstream
AtomISP driver already includes the equivalent ISPSSPM0 power sequence. The
dummy driver performs no capture, 3A, tuning or image processing.

## Delivery stages

1. Preserve the proven OV2740 transport changes in the kernel repository.
2. Normalize AtomISP buffer padding in userspace. OV2740 has 2,048 bytes of
   tail padding; OV8858 1616x1208 additionally uses a 3,328-byte row stride.
3. Demosaic BGGR10 with GStreamer's Bayer converter and scale meeting output
   to 1280x720.
4. Drive standard sensor exposure/gain controls from downscaled output
   statistics. Keep sensor RGB gains at unity and own white balance in one
   userspace stage so exposure changes cannot silently alter color.
5. Apply color, tone, denoising and spatial shading correction before writing
   YUYV to an exclusive-capabilities V4L2 loopback node.
6. Promote the Debian package only after Validator automation and physical
   front/rear meeting tests pass.

## Runtime data flow

```text
/dev/video0 BGGR10
  -> padding/stride normalization
  -> bayer2rgb
  -> 1280x720 RGBA
  -> color balance + hqdn3d + GLSL shading matrix
  -> YUYV /dev/video10
  -> PipeWire / browser

                     -> 64x36, 2 fps statistics
                     -> sensor AE + userspace AWB feedback
```

The physical AtomISP video, media and subdevice nodes deliberately do not
receive logind `uaccess` ACLs. The early system processor runs as desktop UID
1000 with supplementary `video` and `render` groups, while ordinary browser
processes of that UID cannot open `/dev/video0`. Only `/dev/video10` retains
normal desktop access. The service notifies systemd after its first processed
frame, locks the established loopback format and is ordered before the display
manager. The format lock prevents Chromium from reconfiguring the capture side
and stalling an already-running producer during its first open. The loopback
uses capture-only advertisement while the processor owns its output side;
Chromium filters loopback devices that also advertise video output.
Before a planned processor stop, systemd clears `keep_format`; otherwise the
loopback can retain capture-only state after the writer exits and reject the
next writer even when no browser has the node open.

The front and rear cameras share one AtomISP capture node. Sensor selection is
therefore serialized; it is not safe to run two physical capture producers at
once.

Full-resolution rear stills are requested through the running system service.
The service pauses only its AtomISP source pipeline while keeping the
V4L2-loopback output pipeline open, captures and encodes the rear frame, then
restores the selected preview. This prevents browsers from interpreting a
temporary loopback disappearance as permission to open the private raw
AtomISP node.

GStreamer can report the physical pipeline at NULL just before the last V4L2
file reference is released. Sensor switching therefore retries only
`Device or resource busy` for a bounded five seconds; unsupported formats,
missing devices and all other errors remain immediate failures.

AtomISP rejects system suspend while a raw stream is active. A DMI-scoped
system sleep unit therefore records each active per-user camera service,
stops it before `sleep.target`, and restarts only the recorded instances after
resume. The loopback and physical pipeline are reconstructed by the normal
service startup path.

AtomISP also defaults its ISP subdevice to `Preview` after every probe. The
bundled CSS firmware cannot construct a BGGR10 output pipeline in that mode.
Before every front or rear configuration, the runtime resolves the exact
`Atom ISP` media entity and selects `Still capture`; this is explicit boot
state, not a value inherited from earlier camera applications.
