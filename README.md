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
  stretch.py               crop, SCNR, linked MTF autostretch, arcsinh colour, saturation -> TIFF/PNG/JPG + preview strip
tools/
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
  final/             01_solved.fit 02_cc.fit 03_bge.fits 04_dn.fits final.tif/.png/.jpg final_preview_strip.jpg
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

On the 2026-09-07 M31 data the measured row banding was 0.2 ADU against a 3-4 ADU per-row noise floor,
with no fixed pattern, and columns showed a 0.5 ADU fixed pattern just under the significance cut, so
nothing was corrected. The Seestar's own stack showed no stripes either; banding in this camera is not
a problem at gain 200 with a sky-limited 10 s exposure.

**Stack (`stack.sh`).** Siril: `convert` -> `register -2pass` (analyses every frame, picks the best
reference by wFWHM/star count) -> `seqapplyreg -drizzle -scale=1 -pixfrac=1 -kernel=square`
(Bayer drizzle: the CFA data are mapped straight onto the output without a debayer interpolation step,
which the ~40 degrees of alt-az field rotation over the night dithers perfectly) -> `stack rej w 3 3`
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
5. `stretch.py`: crop 4 % per edge (rotation-starved corners), equalise the per-channel sky level so the
   background is neutral grey, linked MTF autostretch to a 0.15 background, signal-weighted SCNR (green
   removal that leaves background noise alone, so no magenta cast), arcsinh colour boost, saturation.
   Finally the sky background's colour noise is desaturated (`--bg-desat 0.7`; after PCC the blue
   channel carries about twice the noise of red/green and reads as a cast otherwise).
   Writes 16-bit TIFF, PNG, JPEG and a gentle/default/strong preview strip.

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
| Median FWHM / roundness / stars per sub | 16.6" / 0.90 / 488 |
| Banding | rows 0.14 ADU (skip); columns 0.50 ADU fixed pattern at 3.3x noise floor (subtracted) |
| Field rotation across the session | ~25 degrees (alt-az) |
| Siril register + Bayer drizzle + stack | 139 s |
| Whole pipeline wall clock on the M2-class Mac | ~8 min, of which GraXpert denoise ~4 min |

Focus note: FOCUSPOS stayed at 1259 all night while the sensor warmed from 21 to 23 C and FWHM
grew from 12.5" to 16.5". Enabling the scope's periodic autofocus (or refocusing mid-session)
should tighten the second half of a long run.
