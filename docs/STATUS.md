# Camera status

## Established hardware

The YB1-X91L exposes two sensors through AtomISP2401:

| Input | Sensor | Native mode | Intended use |
| --- | --- | --- | --- |
| 0 | OV2740 | 1932x1092, cropped to 1920x1080 | Front-facing meetings |
| 1 | OV8858 | 3264x2448 | Rear-facing 8 MP capture |

The 8 MP specification refers to the rear OV8858. The front OV2740 is roughly
2 MP, but a correctly processed 1080p image should still be suitable for video
meetings.

## Proven transport

- OV2740 uses a 576 Mbps two-lane link (288 MHz link frequency).
- The vendor mode produces BGGR10 and requires 12x12 input padding to expose
  1920x1080.
- AtomISP normal-dequeue streaming has produced complete 1920x1080 YU12 frames
  at approximately 30 fps without kernel errors.
- AtomISP's poll path is unsupported; a `--stream-poll` timeout is not evidence
  of transport failure.

The Yoga Book-specific kernel work remains in `Yoga-Book-Linux-Kernel` and is
not duplicated here.

## Userspace ISP evidence

The repository implementation has run continuously on the physical tablet:

- OV2740: 1920x1080 BGGR10 input to 1280x720 YUYV loopback output;
- OV8858: 1616x1208 BGGR10 input with 3,328-byte row-stride normalization to
  1280x720 YUYV loopback output;
- sensor-domain auto exposure converged from a clipped bright-room start to
  70th-percentile luma near 0.55;
- grey-world AWB converged to the warm-neutral reference ratios
  `R/G=1.115`, `B/G=0.875`;
- both continuous raw tests completed without AtomISP/CSS/kernel errors;
- front processing used about 1.3 Atom cores after reducing the statistics
  branch to 2 fps; rear processing used about 1.1 cores in a bounded run.
- full-resolution 3248x2432 rear BGGR10 capture, one-shot focus movement and
  JPEG encoding have completed on the physical tablet;
- repeated fixed-focus stills stabilized near 173 MB service memory rather
  than growing once per capture.
- cold driver initialization requires `atomisp_run_mode=2` before BGGR10
  format negotiation; this is now set through the exact `Atom ISP` media
  entity rather than relying on state left by earlier diagnostic programs.
- Chrome fallback to the private green AtomISP YUV node is prevented by a
  device ACL boundary and an early system service that becomes ready only
  after the corrected loopback receives frames.
- The loopback format is locked after its first processed frame; a physical
  Chrome trace then sustained 66 successful dequeues over two seconds without
  the earlier stream-off/reopen loop or producer stall.
- Chromium requires capture-only loopback advertisement to enumerate the
  endpoint. The two-buffer limit also supports GStreamer consumers; the format
  lock prevents Chromium's open from stalling the producer. Package upgrades
  leave the active producer running so a browser cannot strand the output side.
- A direct 1280x720 frame exposed two-dimensional color shading hidden by the
  meeting crop. A clamped quadratic red/blue gain surface was physically
  rejected because it produced orange skin and inconsistent wall color. The
  A subsequent neutral-only feedback experiment was also rejected because
  sparse samples drove the scene orange. The front profile now uses the
  previously stable whole-frame non-clipped white-balance feedback and retains
  conservative post-denoise edge enhancement; final physical acceptance
  remains required.

This evidence proves runtime architecture and transport, not final image
acceptance. The front image still has visible noise and residual spatial color
variation. The rear lens was physically covered or obstructed during the first
continuous test and therefore has no valid scene-quality result yet.

Lenovo's inspected `OV2740_CJAE533_CHT.cpf` is a 33,720-byte Intel AIQB file
described internally as OV2740 V11 tuning dated 2015-02-02. Compatibility with
the current upstream AtomISP CSS ABI and a legally redistributable AIQ runtime
has not yet been established.

The inspected Lenovo package contains the CPF, an OV2740 Windows kernel driver
and an ISP2401 Windows resource-manager driver. It does not contain an AIQ
userspace DLL, so those files alone cannot be transplanted into Linux as the
missing 3A service.

## Safety boundary

AtomISP private ioctls remain disabled. A previous narrow white-balance ioctl
experiment reached an invalid ISP parameter path and divided by zero while
starting the CSS stream. Reopening private controls requires per-command ABI
analysis, bounded validation and a recoverable one-shot boot.

Raw output remains behind the kernel's default-off `allow_raw_output` gate.
Only this DMI-scoped package deliberately enables it; AtomISP private ISP
ioctls remain closed.
