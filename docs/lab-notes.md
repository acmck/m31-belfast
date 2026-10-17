# Lab notes

What was measured on the M31 data, and what it changed. Distilled from working notes kept between
sessions, from September to October 2026. The independent review is in [REVIEW.md](REVIEW.md); this file
picks up from there. Setup: ZWO Seestar S30 Pro (IMX585, gain 200, 10 s subs, alt-az), Bortle 6 back
garden in Belfast, arm64 Mac.

## Nights

| session | what it was like |
|---|---|
| `M31_2026-09-07` | 479 subs saved, 467 kept; dark sky; the scope dropped 22 % of exposures; 16" stars |
| `M31_2026-09-25` | night of 25/26, crosses midnight (346 subs dated 09-25, 903 dated 09-26); near-full moon, sky 3.4x brighter; 11" stars |
| `M31_2026-10-16` | 384 x 10 s, 20:45-22:23; stayed at 10 s all night; the sharpest night |
| `M31_combined_2026-10-16` | Sep 7 + Sep 25/26 frames, 995 stacked, 166 min |

Folders are named by the evening the night started, so a night that crosses midnight stays in one folder.

## The September review, in brief

Measured on Sep 7 (details and sources in REVIEW.md):

- `stack -weight=wfwhm` cost 17 % in sky noise (galaxy SNR 6.4 with it, 7.9 without) for 9 % smaller
  FWHM. QA's trend-relative FWHM/roundness culling cost a further 12 % SNR for 3 % FWHM. Together about
  30 % of the achievable SNR. All 478 frames with `-weight=nbstars`: SNR 8.1, FWHM 15.22"; unweighted 8.3 / 15.57".
- GraXpert's AI background extraction removed about 50 % of M31's outer halo (3.3 -> 1.7 ADU at 37' on
  the minor axis). Siril `subsky 1` keeps it and flattens the sky as well. The residual gradient in the
  normalized stack is only 0.7 % of sky.
- Field rotation over the night was 19.4 deg. With the reference at the start of the session, three of
  four corners were 2x noisier within about 130 px.
- Photon transfer (green): 11.1 ADU/e-, read noise 2.6 e-, sky 20 e-/px per 10 s. Read noise is 26-30 %
  of the variance, so 20 s subs would gain about 8 % (G) to 18 % (B) SNR per hour. Gain 200 is below the
  IMX585's high-conversion-gain switch.
- Vignetting looked like corners at 0.78 (R) / 0.83 (G) / 0.85 (B) of the centre. That turned out to be
  the galaxy's own light in the "centre" box: with M31 masked out, the sky is flat to 1-2 %.
- 2x drizzle: +3 % FWHM only. Siril RL deconvolution: about 1 % FWHM, deep rings. Not worth it.
- The stretch clipped the nucleus to a 2.1' white disc although the linear peak sat 3.6x above the clip
  level (now a soft highlight knee).
- The focuser never moved all night while FWHM went from about 13" to 17" (15-minute medians of QA-passed frames; re-measured 2026-10-17). Autofocus is the biggest
  capture-side lever.

## Noise

- A darker stretch (sky at 0.10) plus sky-only luminance smoothing gave a cleaner 8-bit sky than the
  Seestar's own JPEG: 13 % against 15 % sky noise.
- GraXpert's denoise strength is an exact linear blend, `out = s * denoised + (1 - s) * input`, confirmed
  in its source and by measurement: residual noise at 0.5 / 0.8 / 1.0 scales 0.50 / 0.80 / 1.00. At 0.8
  green sky noise drops 3.4x; at 1.0, 7.5x. No measurable loss of galaxy structure at either. (An earlier
  README said strength was a no-op; that came from a flawed comparison.)
- After PCC the blue channel carries 2.8x the noise of green (PCC factors R 0.446, G 0.424, B 1.0).
- GraXpert's CLI needs absolute paths.

## Star removal

Seti Astro's Dark Star v2.1c ONNX model (MIT), run with onnxruntime and CoreML. StarNet was ruled out (no
scriptable download, non-commercial license); GraXpert 3.0.2 has no star removal.

- A single colour pass with per-tile min-max normalization (Cosmic Clarity's recipe) leaves magenta/green
  tile blocks on faint sky. Per-tile median re-anchoring makes blocks worse in mono mode. Upscaling or two
  passes add nothing.
- Per-channel mono passes with per-tile MTF-to-0.25 normalization: < 2.5 % residual, no colour errors.
- The network hollows out the nucleus, so bright, large structures are restored from the original.
- **The moonlit ring (2026-10-16).** The old protection used an absolute threshold (0.45) on the stretched
  image. Under moonlight the nucleus stretched to 0.54 instead of 0.93, the mask peaked at 0.17 and left a
  dark ring around the core. The default is now relative: in the linear image, protect where brightness
  above sky is 3-9 % of the nucleus's (nucleus = peak of a 40 px-smoothed image; skipped if below 20x the
  smoothed sky noise). Calibrated so Sep 7's mask is unchanged.
- Judge ring fixes with a radial profile around the WCS position of the nucleus; the globally brightest
  spot can be an edge star.

## Stacking several nights

`scripts/combine_sessions.sh` links every night's QA-selected, debanded frames into one session and runs
`stack post` with `--weight noise --fastnorm`, normal (current) framing. Frames are stacked together,
never stacks of stacks.

- QA stays per night: pooled QA reads a moonlit night's brighter sky as a "background jump" and rejects it.
- Sep 7 + moonlit Sep 25/26 gave +14 % SNR at fixed M31 positions over Sep 7 alone; inverse-variance
  weighting predicted about 15 %.
- `--framing max --maximize` across nights framed at different rotations gives a ragged union canvas. It
  broke Dark Star's cleanup (a sep pixel-buffer overflow) and the nucleus finder.
- Siril 1.4.4's Bayer drizzle writes the `setref` frame (and occasionally another) mostly empty in one or
  two channels; with `-fastnorm` or noise weights the stack aborts with "Normalization failed".
  `stack.sh` runs `check_registered.py` between registration and stacking to unselect empty frames and move
  the reference. Single-night stacks made before 2026-10-16 likely include that broken frame.
- Field rotation depends on the hours imaged, so nights over different hours overlap fully only in the
  middle, and the combined crop clips M31's disc. Auto-crop now trims edges greedily until each border has
  < 5 % noisy blocks; the earlier per-edge median missed half-edge strips.
- Geometry: M31's main disc (+/-60' along the major axis) fits inside the uncropped 2160 px frame width;
  the faint outer disc (+/-80') does not. The phone app looks roomier because it shows the full frame with
  a shallow stretch. For the first combined result a hand-made crop was chosen (sides 22/100 px, top 256,
  bottom 243 of 2160x3840) because auto-crop clipped the disc.

## Colour

- Siril PCC with NOMAD downloads from VizieR, which returned HTTP 503 all evening on 2026-10-16. A
  workaround: copy an existing cone file in Siril's download cache to the file name Siril says it "cannot
  create"; a cone from a nearby field centre covers the frame. It has to be redone for every new field centre.
- The durable fix is Siril's local Gaia catalogues (astrometric plus the xp_sampled HEALPix chunks visible
  from Belfast), so PCC works offline with `-catalog=localgaia`.
- Headless SPCC segfaults in Siril 1.4.4 (with "Sony IMX585" + "UV/IR Block"), with remote and local Gaia
  alike. SPCC run from the Siril GUI works.
- PCC's white balance depends on the catalogue: NOMAD is too blue (the lavender cast), local Gaia too red.
  `cc_correct.py` scales each channel about its sky median by SPCC/PCC measured on the same stacks:
  local Gaia R 0.710 / 0.706 / 0.744, G 0.864 / 0.874 / 0.858 (three stacks); NOMAD R 1.517 / 1.523,
  G 1.380 / 1.401 (two). Oct 16 was an independent check: predicted SPCC R 0.732 G 0.583, measured 0.769 / 0.576.

## Tried and rejected (2026-10-16)

- **VeraLux HyperMetric Stretch** (auto log D 4.25, S30 sensor weights): M31 far too dark, because its
  output scaling maps the 99th percentile (stars, in this field) to 0.98. D 6.5, or "scientific" mode with
  an MTF sky of 0.10, did not fix it.
- **Per-frame `seqsubsky 1` before registration** on moonlit Sep 25/26 (529 frames): sky tilt 16.4 % ->
  1.7 %, but halo/outer-disc SNR fell 15-55 % because each frame's fit absorbs galaxy light. Kept as
  `stack.sh --seqsubsky`, off by default. Might still suit small targets with lots of empty sky.
- **petuchen/siril-s30pro-pipeline** (GPL-3.0): defaults are wFWHM weighting, GraXpert AI background
  extraction, framing max + maximize, debayer without drizzle, no frame QA, and multi-night by stacking
  masters. This data argued against each of those. Worth borrowing: local Gaia catalogues, SPCC inside a
  live Siril session.

## Getting frames off the scope

- Station mode puts the scope on the home Wi-Fi as `seestar.local`, guest SMB share `EMMC Images`, mounted
  read-only. Frames live under `MyWorks/<Target>_sub/Light_*.fit` (CFA GRBG, 2160x3840 uint16, gain 200),
  live stacks under `MyWorks/<Target>/`. The eMMC is 104 GB.
- USB mass storage enumerated as a 112 GB exFAT "File-Stor Gadget" but would not mount and dropped off the bus.
- The scope does not answer ping. Any split-tunnel route on the Mac that covers the home LAN black-holes it;
  check that `route -n get seestar.local` reports `en0` before pulling.
- Apple's openrsync lacks `--info` and its include filters missed file names with spaces, so
  `pull_frames.sh` is a `cp` loop with size verification that resumes.
- On 2026-10-16 the scope's Samba logged about 40 retried reads a minute during a long copy, stalled, then
  left Wi-Fi entirely while on mains power. Moving it for better coverage brought retries down to about 26 a
  minute. A dead soft mount makes every later copy fail at once with "size mismatch": `umount -f`, remount,
  rerun. Copy rate is about 10 frames a minute (16.6 MB each), about 7.6 a minute while the Mac is also
  downloading. Budget an hour per 600 frames.

## Capture plan

- Refocus during the session (Sep 7 drifted from about 13" to 17" with the focuser fixed; Oct 16 drifted only 0.2").
- Dew heater on in a Belfast autumn (temperature-dew point spread around 3 C).
- Pick the target from the app's catalogue so the framing repeats.
- Windy evenings at 10 s, calm late nights at 20 s (read noise is about 27 % of the 10 s variance).
- Image over similar hours each night so the field rotation lines up and combined stacks lose less at the edges.
- An EQ mount is coming. It will not widen the fixed ~2.2 x 3.9 deg field but removes rotation, so nearly
  the whole frame survives the crop; M31's disc (about 2.0 x 2.5 deg at PA 38) just fits north-up. Without
  rotation, enable dithering to avoid walking noise; then revisit 30-60 s subs. A mosaic is the only way to
  "zoom out".

## Dropped frames are not (measurably) wind

Checked 2026-10-17 with `scripts/make_charts_data.py`. Hourly gusts at Belfast city centre (ERA5) against the
scope's per-hour drop rate give r = 0.15 over 13 night-hours, which is no relationship. 16/17 Oct dropped 27 % in
10-13 mph gusts, and on 25/26 Sep drops rose to 51-59 % after 02:00 while the gusts fell. The cause is still
unknown. City-centre gusts may not reflect the garden; Wi-Fi, the app and tracking are other candidates.

## Clear nights

From 1 Sep to 16 Oct, 9 of 46 nights had mean cloud cover of 30 % or less between 21:00 and 03:00 UTC at Belfast
city centre. I imaged 3 of them.
