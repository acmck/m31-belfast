# Seestar S30 Pro post-processing pipeline

Pull raw sub-frames off a ZWO Seestar over the network, QA them, remove banding
where it is actually measurable, register and stack with Siril (Bayer drizzle),
then plate-solve, colour-calibrate, extract the background, denoise and stretch.

## Layout

```
run_pipeline.sh            one-shot driver: qa -> deband -> stack -> post
scripts/
  pull_frames.sh           mount smb://seestar.local/EMMC Images (read-only) and copy one target/night
  qa_frames.py             per-frame metrics, robust outlier rejection, HTML report, hard-links kept frames
  deband.py                measure-first CFA-aware row/column banding removal (skips itself when banding is negligible)
  stack.sh                 Siril 1.4 headless: convert -> 2-pass register -> Bayer drizzle -> winsorized stack
  post_process.sh          ASTAP plate solve -> Siril SPCC/PCC -> GraXpert BGE -> GraXpert denoise -> stretch.py
  destar.py                star removal: Seti Astro Dark Star network (MIT) run locally via onnxruntime -> starless + stars layers
  stretch.py               crop, SCNR, linked MTF autostretch, arcsinh colour, saturation, star-layer recombination -> TIFF/PNG/JPG + preview strip
tools/
  darkstar/darkstar_v2.1c.onnx   Dark Star v2.1c star-removal model (15 MB, from github.com/setiastro/cosmicclarity releases)
  astap/cli/astap_cli      ASTAP command-line solver (arm64)
  astap/db/                D50 star database (wide fields, down to mag ~13)
  downloads/               the installers that were fetched
data/<Target>_<date>/      one session per target/night (see below)
logs/
.venv/                     Python 3.12: numpy, scipy, astropy, photutils, sep, opencv, scikit-image, matplotlib
```

Installed apps: `/Applications/Siril.app` (1.4.4), `/Applications/GraXpert.app` (3.0.2), Homebrew `bash` 5
(the shell scripts use `#!/usr/bin/env bash`; macOS's built-in bash 3.2 mis-parses part of `post_process.sh`).

## Session layout

```
data/M31_2026-09-07/
  lights/            raw CFA subs from the scope       (Light_M 31_10.0s_IRCUT_2026....fit)
  seestar_stack/     the scope's own live stack, for comparison
  qa/                frames.csv, summary.json, report.html, qa_timeseries.png, selected/rejected.txt
  lights_good/       hard links to frames that passed QA
  lights_db/         debanded (or linked unchanged) copies of lights_good -> Siril input
  process/           Siril working dir (registered frames are deleted after a successful stack)
  stack/result.fit   32-bit linear RGB stack + siril_stack.log
  final/             01_solved.fit 02_cc.fit 03_bge.fits 04_dn.fits 05_starless.fits 05_stars.fits
                     final.tif/.png/.jpg  final_starless.jpg  final_preview_strip.jpg
```

## Getting frames off the scope

1. Put the Seestar in **Station mode** (app: device settings -> Station Mode -> join home Wi-Fi).
   It then shows up as `seestar.local` with an SMB share called `EMMC Images`.
2. If a corporate VPN is up, check `route -n get seestar.local` reports `en0`; a split-tunnel route
   that covers the scope's IP will black-hole it (fix: disconnect, or `sudo route add -host <ip> -interface en0`).
3. `scripts/pull_frames.sh "M 31" 20260908` copies only the `.fit` subs for that date into
   `data/M31_20260908/lights`, verifies sizes, resumes if interrupted, and grabs the night's live stack.

USB also works in principle (the scope enumerates as a 112 GB exFAT "File-Stor Gadget") but on this
Mac the volume refused to mount and then dropped off the bus; Wi-Fi is the reliable path.

## Running

```bash
./run_pipeline.sh data/M31_2026-09-07                 # everything
./run_pipeline.sh data/M31_2026-09-07 qa              # just re-run QA
QA_ARGS="--fwhm-ratio 1.15 --round-min 0.8" ./run_pipeline.sh data/M31_2026-09-07 qa deband stack post
STACK_ARGS="--framing max --pixfrac 0.8" ./run_pipeline.sh data/M31_2026-09-07 stack post
POST_ARGS="--target-bg 0.12 --asinh 4 --rotate 90" ./run_pipeline.sh data/M31_2026-09-07 post
```

Each stage can also be run directly, e.g. `.venv/bin/python scripts/qa_frames.py --help`.

## What each stage does, and why

**QA (`qa_frames.py`).** On a 2x2 green superpixel image per frame: SEP background, star extraction,
FWHM from half-flux radius, roundness b/a, star count, background level, background gradient,
saturated pixels, a transparency proxy (median flux of the 50 brightest unsaturated stars) and a
noise-separated banding estimate. Seeing, focus and altitude drift over a session, so FWHM, roundness,
star count, background and transparency are judged against a rolling median over 41 frames (the local
trend) using MAD-scaled limits with sensible floors, plus absolute caps (FWHM > 1.3x session median,
roundness < 0.75). Rejection reasons: FWHM bloat, elongation from wind/tracking, low star count or
transparency from cloud, background jump from cloud/light, severe banding, unreadable file. `qa/report.html` has time-series plots, histograms,
thumbnails of everything rejected and the reason.

**Deband (`deband.py`).** Estimates the per-row (or per-column) offset from the two outer 25 % strips of
the frame independently, after masking stars and high-passing (running median over 31 rows) so the
target in the middle and real sky gradients are excluded. Real banding is common to both strips
(covariance); noise is not. It applies a correction only when the banding is measurable: a fixed pattern
averaged over all frames if that clears 3x its noise floor, and a per-frame correction for frames whose
banding exceeds both `--min-band-rms` (1 ADU) and the estimator's own noise. Frames that need nothing are
hard-linked unchanged. Subtracting a noisy estimate from a clean frame would add more stripe noise than
it removes, so "do nothing" is the correct default when the measurement says so. `--force` overrides.
Use `--axis both` to also check columns (CMOS column offsets); `deband_summary.json` records the numbers.

On the 2026-09-07 M31 data the measured row banding was 0.14 ADU against a 0.20 ADU noise floor (skipped),
and columns showed a 0.50 ADU fixed pattern at 3.3x its floor, which was subtracted from all 427 frames.
`run_pipeline.sh` runs deband with `--axis both` so this is reproduced. The Seestar's own stack showed no stripes either; banding in this camera is not
a problem at gain 200 with a sky-limited 10 s exposure.

**Stack (`stack.sh`).** Siril: `convert` -> `register -2pass` (analyses every frame, picks the best
reference by wFWHM/star count) -> `seqapplyreg -drizzle -scale=1 -pixfrac=1 -kernel=square`
(Bayer drizzle: the CFA data are mapped straight onto the output without a debayer interpolation step,
which the ~19 degrees of alt-az field rotation over the night dithers well) -> `stack rej w 3 3`
(winsorized sigma clipping, additive+scaling normalisation, wFWHM weighting, RGB equalisation, 32-bit).
The Seestar produces no darks/flats/bias, so there is no calibration step. `-framing=current` keeps
the reference frame's footprint; use `--framing max` for the full rotated union or `min` for the
overlap only.

**Post (`post_process.sh`).**
1. ASTAP plate-solves the stack with the D50 database and writes WCS into the header.
2. Siril `spcc` (spectrophotometric, Gaia DR3) is attempted first, then `pcc` (photometric, NOMAD).
   Both need internet. Headless SPCC currently crashes without sensor/filter names; PCC works.
3. GraXpert AI background extraction (subtraction), models download to `~/Library/Application Support/GraXpert` on first use.
4. GraXpert AI denoise.
5. `destar.py` (star removal, `--no-destar` to skip). Runs Seti Astro's Dark Star network with onnxruntime
   (CoreML on this Mac, CUDA on an NVIDIA PC, CPU otherwise; ~20 s for 8 MP). The image is MTF-stretched to
   a 0.25 median, cut into 256 px tiles with 32 px overlap, each tile min-max normalised and lifted to a
   0.25 median, run through the network **once per colour channel as a grey image**, stitched with soft
   ramps and un-stretched. Stars-only = unscreen(original, starless). Bright, large structures the network
   removed (the M31 nucleus, M32) are restored from the original. Writes `05_starless.fits` + `05_stars.fits`.
6. `stretch.py`: crop 4 % per edge (rotation-starved corners), equalise the per-channel sky level so the
   background is neutral grey, linked MTF autostretch to a 0.15 background, signal-weighted SCNR (green
   removal that leaves background noise alone, so no magenta cast), arcsinh colour boost, saturation.
   Finally the sky background's colour noise is desaturated (`--bg-desat 0.7`; after PCC the blue
   channel carries about twice the noise of red/green and reads as a cast otherwise).
   With `--stars`, that whole chain runs on the **starless** image (so the galaxy can be stretched and
   smoothed without bloating stars), the star layer gets the same midtone stretch scaled by
   `--star-reduce 0.7`, and the two are screen-blended: `1 - (1-starless)(1-stars)`. Also writes
   `final_starless.jpg`. Writes 16-bit TIFF, PNG, JPEG and a gentle/default/strong preview strip.

## Star removal: what was tested

Dark Star's ONNX model was chosen over StarNet (no scriptable download, non-commercial licence) and the
Cosmic Clarity app (its CLI is folder-based). Findings on the M31 data, measured as residual flux at star
positions in the stretched domain:

| variant | result |
|---|---|
| single colour pass, Cosmic Clarity's per-tile min-max | stars gone (2-5 % residual) but tiles stitch into magenta/green blocks on faint sky |
| colour pass, no tile normalisation | neutral colour but weak removal, coloured halo rings |
| colour pass, BGR channel order | best of the colour passes (<1.5 % residual) |
| 2x upscale, or two passes | no gain |
| **per-channel mono passes** (default) | <2.5 % residual, no colour errors, no blocks; 3x slower (20 s) |
| per-tile MTF to 0.25 median (default `--norm mtf`) | smoothest sky of the three normalisations |
| galaxy core | always hollowed; restored by the bright-and-large guard (`--protect 0.45`) |

`--star-reduce` is the creative knob: 1.0 puts stars back at plain-stretch brightness, 0.5 gives a
"deep-sky first" look. `--no-destar` on `post_process.sh` returns to the plain stretch.

## Noise: what actually moves the needle

Measured on the M31 run (sky noise as % of sky level in the 8-bit output, Seestar's own JPEG = 15 %):

| change | effect |
|---|---|
| `--target-bg` 0.15 -> 0.10 (the default; darker sky, like the Seestar's rendering) | large: the same noise is simply displayed smaller |
| `--bg-smooth 1.0` (sky-only luminance blur, stars/galaxy protected) | 18 % -> 11 % |
| `--bg-desat 0.7` (grey sky) | removes colour speckle, no effect on luminance noise |
| `--denoise-strength 0.8` instead of GraXpert's default 0.5 | real: strength is an exact linear blend `out = s*denoised + (1-s)*input`, so residual sky noise scales 0.5 / 0.8 / 1.0 (REVIEW.md 2.6). At 0.8 green sky noise drops 3.4x with no measurable loss of 2-8 px structure |
| more integration | the only thing that improves the *data*: noise falls as 1/sqrt(minutes). Two nights of 70 min = 1.4x cleaner |
| longer subs (20-30 s) | real but modest: read noise is 26-30 % of the per-pixel variance at 10 s (photon transfer, REVIEW.md 2.1), so 20 s subs gain ~8 % SNR/hour in green and ~18 % in blue, 30 s ~11 % / 27 %; cost is more wind rejections |

Stacking several nights: put all sessions' `lights/` into one session directory (or hard-link them) and run
the pipeline once; QA judges each night against its own local trend so mixed conditions are fine.

## Manual finishing (optional)

Open `final/04_dn.fits` (linear, colour-calibrated, flat background) in Siril or GraXpert's GUI for
interactive GHS stretching, star removal (StarNet/StarXTerminator are not installed), or in
PixInsight if you have it. `stack/result.fit` is the untouched linear stack.

## First run: M31, night of 2026-09-07/08

| | |
|---|---|
| Captured by the scope (from 11 s cadence) | ~617 exposures over 113 min |
| Saved by the scope (its live stacker dropped ~134, 22 %, windy night) | 479 x 10 s = 79.8 min |
| Rejected by QA (46 elongated, 41 bloated vs trend, 1 > 1.3x median, 1 few stars) | 52 |
| Stacked | 427 x 10 s = 71.2 min |
| Median FWHM / roundness / stars per sub | 16.3" / 0.90 / 488  (at the solved 3.665"/px) |
| Banding | rows 0.14 ADU (skip); columns 0.50 ADU fixed pattern at 3.3x noise floor (subtracted) |
| Field rotation across the session | 19.4 degrees (alt-az), -18.8 to +0.7 relative to the reference |
| Siril register + Bayer drizzle + stack | 139 s |
| Whole pipeline wall clock on the M2-class Mac | ~8 min, of which GraXpert denoise ~4 min |
| Sky noise in the 8-bit output (Seestar's own JPEG: 15 %) | 13 % with the default stretch, 11 % with `--target-bg 0.09` |

## Capture-side recommendations (from REVIEW.md; nothing in software recovers these)

1. **Enable the scope's periodic autofocus, or refocus mid-session.** FOCUSPOS stayed at 1259 all night
   while the sensor warmed from 21 to 23 C and FWHM grew from 12.9" to 17.3". This is the single largest
   available resolution gain (likely 15-25 % smaller stars on a similar night).
2. **Try 20 s subs on a calm night.** Read noise is 26-30 % of the per-pixel variance at 10 s; 20 s gains
   ~8 % (green) to ~18 % (blue) SNR per hour and halves the 1 s/frame overhead. Watch the drop rate.
3. **A flat is probably not needed for a frame-filling target like M31.** REVIEW.md quoted 17-22 %
   corner falloff from a raw box-median comparison; that number turned out to be the galaxy's own
   light inflating the "centre" reference, not lens vignetting. `scripts/synth_flat.py` masks the
   target out before measuring and finds under 1-2 % falloff on this data, which it reports as
   negligible and does not apply. Trust that verdict over a quick eyeball comparison; if you image
   a smaller, off-centre target where the whole frame is clean sky, re-run it and check the number.
4. **Stay in alt-az for this target.** EQ mode removes field rotation and allows 60 s subs but also removes
   the free dithering; users report walking noise in EQ stacks.
5. A second night of the same target stacks straight in: put both nights' lights in one session and run once
   (see "Stacking several nights" below).

## Review implementation (2026-09-09)

`REVIEW.md` is an independent review of this pipeline against the free/paid-tool state of the art,
run on the M31 session above. Its "do now" findings are implemented and are now the defaults:

| finding | change | file |
|---|---|---|
| `-weight=wfwhm` cost 17-23 % SNR for a 9 % star-size gain | default weighting is `nbstars`; `--weight none\|noise\|nbstars\|wfwhm` | `stack.sh` |
| registration reference sat at one end of the ~19 deg rotation, leaving 3 corners at 2x noise | reference reset to the mid-session frame by default; `--ref auto\|mid\|N` | `stack.sh` |
| `--no-drizzle` silently produced a 1-layer (mono) stack | fixed: debayers before registration in that path | `stack.sh` |
| trend-relative FWHM/roundness rejection cost 12 % SNR for a 3 % FWHM gain | off by default (absolute caps + cloud tests only); `--trend-reject` restores it | `qa_frames.py` |
| GraXpert's AI background model fits ~half of M31's halo away | Siril `subsky 1` is the default gradient step, run before colour calibration; `--bge graxpert` restores the old behaviour | `post_process.sh` |
| destar's inverse stretch was an approximation | exact analytic inverse; the safety clamp now reports how often it still fires | `destar.py` |
| destar's core-protection guard left a ring around M32 | mask smoothing widened (6 px -> 15 px) | `destar.py` |
| ASTAP solve had no distortion terms | `-sip` passed to ASTAP | `post_process.sh` |
| nucleus clipped to a flat white disc | soft `--knee` highlight shoulder on the final image | `stretch.py` |
| fixed 4 % crop missed the noisy rotated-in corners | `--crop auto` measures per-edge noise and crops to it | `stretch.py` |
| blue-channel noise showed as sky colour blotches | `--bg-chroma-blur` low-pass on sky-only chroma | `stretch.py` |
| Seestar has no calibration frames | `scripts/synth_flat.py` fits a vignetting flat from the lights' own sky, with a sanity check and a genuine finding below | new |
| no plan for the built-in dual-band (LP) filter | `--palette hoo` in `stretch.py`; `post_process.sh` auto-detects `FILTER=LP` and skips broadband colour calibration; untested on real narrowband data | `stretch.py`, `post_process.sh` |

**A finding beyond the review, found while building the flat:** `synth_flat.py`'s first version
reproduced the review's ~18-22 % corner falloff, but the fit was numerically unstable (its radial
model let the optical centre wander to nonsensical positions when fitting sky that has a
galaxy-shaped hole cut out of it, and an unconstrained 2-D polynomial did the same by putting its
peak in a corner). Pinning the optical centre at the sensor's geometric centre and adding a sanity
check (reject a fit whose peak isn't near the centre, or that makes any corner brighter than the
centre) turned up the real story: once M31's own light is masked out of the measurement, the sky
is flat to within 1-2 % everywhere. The review's 17-22 % figure was the galaxy's own extended light
inflating a naive "centre" box, not lens vignetting. The flat stage now measures this correctly,
reports the verdict, and `run_pipeline.sh` does not apply a flat when it is negligible.
