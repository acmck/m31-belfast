# Review: Seestar S30 Pro post-processing pipeline

Reviewed 2026-09-09 against commit `857033f` on `main`, using the M31 session of 2026-09-07/08
(479 x 10 s subs, 427 stacked). Everything under "measured" below was run on this data during the
review; commands and outputs are summarised in the appendix. Everything under "literature" comes from
the sources cited inline. Nothing in the repository was changed.

## Summary

The pipeline is a competent, well-instrumented free-tools workflow. Registration, Bayer drizzle at
1x, winsorized rejection, the QA instrumentation and the star-removal stage are all at or above what
the free-tools community does. Two default settings are actively costing quality on this data, and
both were chosen on plausible reasoning that the measurements do not support:

1. **`-weight=wfwhm` in the Siril stack costs 17 % in sky noise** (galaxy signal-to-noise 6.4 with
   it, 7.9 without) for a 9 % smaller star FWHM (14.05" vs 15.35"). Combined with the QA rejection of
   52 frames, the pipeline gives up about 30 % of the achievable signal-to-noise ratio (SNR): the same
   frames stacked unweighted and unfiltered reach SNR 8.3. That is the equivalent of 40 extra minutes
   of exposure, thrown away for a resolution gain that is at the limit of visibility at 4 px FWHM.
2. **GraXpert's AI background extraction removes about half of M31's outer halo** (3.3 ADU above sky
   at 37' along the minor axis before, 1.7 ADU after). A first-order polynomial in Siril flattens the
   background just as well (corner-to-corner range 1.2 -> 0.4 ADU vs GraXpert's 0.3) and keeps the halo.

Two documented findings in the README are wrong: GraXpert denoise strength is an exact linear blend
(residual noise scales 0.50 / 0.80 / 1.00 with the setting), not a no-op; and the field rotation was
19.4 degrees, not 25 or 40. One code path is broken: `stack.sh --no-drizzle` registers the raw Bayer
mosaic as a monochrome image and produces a single-channel stack.

The single change that would move the result furthest is on the capture side and costs nothing:
enable the scope's periodic autofocus. FWHM grew from 12.9" to 17.3" over the session while the
focuser never moved. No processing choice recovers that.

---

## Step 1: The reference

### Is there a gold standard?

No. There is a well-agreed *paid* workflow for OSC deep-sky data in general (PixInsight WBPP with
PSF Signal Weight and ESD/linear-fit rejection, then BlurXTerminator, SPCC, NoiseXTerminator,
StarXTerminator, GHS), and a well-agreed *free* workflow (Siril register/stack, GraXpert, Siril or
StarNet for stars, GHS). Neither community has converged on settings for the specific case of a
30 mm smart telescope producing hundreds of 10 s sky-limited frames with field rotation and no
calibration masters. Siril's own Seestar script uses the plainest possible settings (`rej 3 3`,
no weighting, no drizzle) [1]; the Naztronomy smart-telescope script exposes weighting and drizzle as
user options without recommending values [2]; Seestar users on Cloudy Nights and the ZWO forum mostly
argue about exposure length and EQ mode, not stacking parameters. Where the two communities agree:

- **CFA (Bayer) drizzle at scale 1, pixfrac 1** for OSC data that is not undersampled. PixInsight's
  Juan Conejero: "CFA drizzle is not optional... If your data justifies it (both because it is
  subsampled and because you have enough frames), you can perform drizzle with a scale of 2.
  Otherwise select a scale of 1" [3]. Siril's docs say the same and add that lanczos kernels are
  only for scale = pixfrac = 1 [4]. Undersampled means FWHM under about 2 px [5]; this data is
  3.5 to 4.5 px.
- **Rejection for large frame counts**: Generalized ESD or linear fit for more than 50 frames;
  winsorized is described both as "for limited frame counts" and "works well with large sets"
  depending on the author [6][7]. PixInsight's RickS: integrate once with no rejection, note the
  noise, then tighten rejection only until you are within a couple of percent of that [8].
- **Weighting**: inverse-variance (noise) weighting is theoretically SNR-optimal; PixInsight's PSF
  Signal Weight is a hybrid that also rewards resolution and is the recommended default there [9].
  Nobody recommends FWHM-only weighting for faint extended targets.
- **Frame culling**: remove only unrecoverable frames, let weighting handle the rest [10]. No
  source provides a controlled test of "drop the worst 10 % by FWHM".
- **Order of operations**: gradient removal on the linear stack before colour calibration, colour
  calibration before any non-linear step, deconvolution (if any) on linear data with stars, noise
  reduction on linear data, star removal on linear data, then stretch.
- **Recombination**: screen (`1-(1-a)(1-b)`) in the stretched domain, or plain addition in the
  linear domain; Siril's own Star Recomposition tool uses screen [11].

### The reference I would defend for this class of data

**Free tools (Siril 1.4 + GraXpert + Dark Star):**

| step | setting | what it buys | evidence |
|---|---|---|---|
| calibration | none, unless the scope's Flat Shoot is used; then a master flat via `-flat=` in drizzle | corners 17-22 % dark and 8 % colour-shifted without it | measured (this review) |
| frame selection | reject only unreadable, cloud, roundness < 0.75, FWHM > 1.5x median | +12 % SNR vs 11 % rejection, 3 % FWHM cost | measured |
| registration | `register -2pass`, reference chosen mid-way through the rotation, `-framing=min` or `max` + `-maximize -feather` | symmetric corner coverage | measured: 3 of 4 corners 2x noisier now |
| drizzle | CFA drizzle, scale 1, pixfrac 1, square | avoids demosaic interpolation; no SNR difference vs debayer at matched scale | measured + [3][4] |
| stacking | `rej w 3 3` or `rej g 0.3 0.05`, `-norm=addscale -fastnorm`, **no weighting** (or `nbstars` if stars matter), `-32b` | rejection costs < 1 % noise; wfwhm costs 17 % | measured |
| plate solve | ASTAP with SIP distortion, then Siril | PCC/SPCC warn about linear-only solutions | Siril log |
| gradient | Siril `subsky 1` (or 2) for frame-filling targets; GraXpert AI only for small targets | AI model eats 50 % of the M31 halo | measured |
| colour | Siril SPCC with "Sony IMX585" + "UV/IR Block" when it stops crashing headless; PCC until then | SPCC uses the real sensor curves | [12], measured crash |
| denoise | GraXpert AI on the linear image, strength to taste (it is a linear blend) | 3.4x lower sky noise at 0.8, no measurable loss of structure | measured |
| deconvolution | none | 1 % FWHM gain, 5x deeper dark rings | measured |
| stars | Dark Star mono-per-channel (as implemented), screen recombination | clean sky, no tile blocks | measured |
| stretch | GHS (`autoghs` or `ght`) with highlight protection, or the current MTF chain with a soft shoulder above ~0.7 | nucleus currently clipped over a 2.1' disc | measured |

**Paid tools (PixInsight):** WBPP with CFA drizzle 1x, PSF Signal Weight, ESD rejection, local
normalisation; then GraXpert or DBE for the gradient (DBE with manual samples for M31),
BlurXTerminator "Correct Only" then a light sharpen, SPCC with the Sony IMX585 curve,
NoiseXTerminator, StarXTerminator, GHS. The measurable difference from the free chain is
BlurXTerminator; everything else has a free equivalent within a few percent.

---

## Step 2: Stage-by-stage comparison

### 2.1 Calibration: falls short (but less than expected)

*Measured:*
- The frames are not bias-subtracted. The header's `BIAS = 1107` documents the pedestal; pixel
  values go below it (minimum 838 in a typical frame, 2260 pixels under 1107). The data use the full
  16-bit code space with no missing codes, so they are not a 12-bit ADC value multiplied by 16.
- Photon transfer on consecutive frame pairs (green channel, 3000 tiles across sky and galaxy):
  conversion factor 11.1 ADU per electron, read noise 2.6 e- (about 30 ADU), sky 20 e- per pixel per
  10 s in green, 14 e- in red and blue. **Read noise is 26-30 % of the per-pixel variance in green and
  red and more in blue.** The 10 s sub is sky-limited but not by a wide margin.
- This agrees with ZWO's ASI585 manual: 2.4 e- read noise at 15 dB, and high-conversion-gain mode
  only switches on at gain 252 [13]. The S30 Pro's fixed gain 200 sits in the low-gain mode.
- Per-pixel median of 120 frames: sky illumination in the corners is 0.78 (red), 0.83 (green),
  0.85 (blue) of the centre, identical in all four corners to 0.5 %, so this is lens vignetting,
  not a sky gradient. The red/blue ratio changes 8 % from centre to corner. No dust shadows in the
  high-passed median. No amp glow (edge and corner ratios are symmetric).
- Persistent hot pixels: 197 candidates in the green plane of the median stack, of which 3 coincide
  with single-frame outliers. The rest are stars. There are effectively no fixed hot pixels, which is
  consistent with the scope applying a hot-pixel map before saving (grelf.net reaches the same
  conclusion from the file contents [14]; a ZWO forum user with shell access found only a small
  `hpc.bin` and an unchanged dark library on the S30 Pro [15]).

*Literature:* Siril's tutorial states Seestar data cannot be calibrated because only lights are
saved [16]. ZWO's Flat Shoot feature (Settings > Advanced > Image Calibration) takes a sky or tablet
flat and applies it on the scope [17]; whether the saved subs already have it applied is not
documented, and this data shows 17-22 % falloff, so either it was not used or it is not applied to subs.

*Verdict:* Darks and bias would buy nothing (no hot pixels, no glow, no pedestal problem). A flat
would buy correct corner brightness and correct star colours in the corners, which matters for
photometric colour calibration (PCC excludes 2195 of 4227 stars as "not in area" partly because of
the corner falloff) and for any future mosaic or off-centre target. A synthetic flat fitted to the
per-frame median (excluding the galaxy) would do the same job without new captures.

### 2.2 Frame selection and weighting: falls short

*Measured on the 427 kept frames (same registered, drizzled sequence, only the `stack` line varied):*

| weighting | rejection | sky noise (% of sky) | faint-galaxy SNR | star FWHM | stars found |
|---|---|---|---|---|---|
| **wfwhm (pipeline)** | winsorized 3/3 | 1.633 | 6.4 | 14.05" | 1930 |
| none | winsorized 3/3 | 1.357 | 7.9 | 15.35" | 1916 |
| noise | winsorized 3/3 | 1.360 | 7.9 | 15.31" | 1925 |
| nbstars | winsorized 3/3 | 1.484 | 7.1 | 14.63" | 1953 |
| wfwhm | linear fit 3/3 | 1.881 | 5.8 | 14.76" | 1532 |
| wfwhm | GESD 0.3/0.05 | 1.633 | 6.4 | 13.81" | 1950 |
| wfwhm | none | 1.622 | 6.4 | 13.72" | 1971 |

*Measured on all 479 frames (Siril itself excluded one, 478 stacked):*

| selection | weighting | sky noise | SNR | FWHM |
|---|---|---|---|---|
| QA 427 (pipeline) | wfwhm | 1.633 | 6.4 | 14.05" |
| all 478 | wfwhm | 1.449 | 7.2 | 14.49" |
| all 478, Siril `-filter-round=0.85` (397 used) | wfwhm | 1.650 | 6.3 | 14.50" |
| all 478, Siril `-filter-wfwhm=90%` | wfwhm | 1.784 | 5.7 | 13.69" |
| all 478 | nbstars | 1.312 | 8.1 | 15.22" |
| **all 478** | **none** | **1.280** | **8.3** | 15.57" |

Reading:
- Per-frame noise varies only 40.1 to 43.4 ADU (± 4 %) and transparency ± 10 % across the night,
  so noise weighting is indistinguishable from no weighting. There is nothing for it to do on a
  single stable night. It will matter when two nights with different moon or haze are combined.
- Seeing degraded steadily from 12.9" to 17.3" (focuser fixed at step 1259, sensor 21 -> 23 C).
  wFWHM weighting therefore concentrates the weight on the first hour: the effective frame count
  drops from 427 to about 295 (the ratio of the noise figures squared). That is the 17 % noise cost.
- Rejecting the 52 QA frames costs a further 12 % in SNR and buys 3 % in FWHM. Siril's own roundness
  filter at 0.85 (which removes 81 frames) buys no FWHM at all, so elongation in these frames is not
  what limits the stacked star size. The trend-relative FWHM and roundness tests are the ones doing
  the damage; the absolute caps (roundness < 0.75, FWHM > 1.3x median, cloud tests) are sound and
  removed only 2 frames.
- Winsorized rejection costs 0.7 % in noise (0.1-0.4 % of pixels rejected) and slightly rounds the
  brightest star cores (FWHM 14.05" vs 13.72" unrejected). GESD is equivalent in noise and slightly
  better in FWHM. Linear fit is clearly worse here: it rejects more, and 400 fewer stars are detected
  in its output because it clips the cores of stars whose brightness varies with seeing.

*Verdict:* the pipeline trades 30 % of its SNR for 11 % smaller stars. For a galaxy whose outer disc
is the hard part, that is the wrong trade. Weighting by `nbstars` is the best compromise measured:
on all frames it reaches SNR 8.1 (+27 %) with FWHM 15.22" (+8 %), keeping most of the depth of the
unweighted stack and a little of the sharpness of the weighted one.

### 2.3 Registration and stacking: matches, with one coverage problem

*Measured:*
- Field rotation across the kept frames: -18.8 to +0.7 degrees relative to the reference, 19.4
  degrees total (the README says 25 in one place and 40 in another). The 2-pass registration chose
  frame 23 as reference because the seeing was best at the start, which puts the reference at one
  end of the rotation range. Result: within about 130 px of three corners the stack noise is 2.0-2.1x
  the central value; the fourth corner (top-left) is unaffected. The 4 % crop (86 x 153 px) does not
  quite cover it, which is why the bottom corners of `final.jpg` show purple mottling.
- 100-frame drizzle comparison, noise measured after binning everything to the native 3.67"/px grid:

| method | galaxy SNR (native) | galaxy SNR (4x4 binned) | FWHM |
|---|---|---|---|
| Bayer drizzle 1x, pixfrac 1.0 (pipeline) | 3.5 | 10.0 | 14.82" |
| Bayer drizzle 1x, pixfrac 0.7 | 3.1 | 9.6 | 14.71" |
| Bayer drizzle 2x, pixfrac 0.8 | 3.2 | 9.6 | 14.35" |
| debayer + Lanczos-4 registration | 4.1 | 10.1 | 14.49" |

  At a matched spatial scale all four are the same to within noise. 2x drizzle buys 3 % in FWHM for
  4x the data volume. Pixfrac below 1 at scale 1 just adds noise. Plain debayering is not worse in
  SNR; its per-pixel figure is higher only because interpolation correlates neighbouring pixels.
- `stack.sh --no-drizzle` produces a **1-layer image**: `convert` without `-debayer` leaves the CFA
  mosaic, and `seqapplyreg -interp=lanczos4` then registers it as monochrome (checkerboard contrast
  2 % in the result). The path needs `convert light -debayer`.
- Stacked-sky lag-1 pixel autocorrelation is 0.39 (square drizzle kernel plus Bayer interpolation);
  no concentric or rotating pattern is visible in the high-passed background.

*Verdict:* Bayer drizzle 1x / pixfrac 1 / square is exactly what both communities recommend for
well-sampled OSC data. Keep it. The corner coverage should be fixed by choosing the reference at
the middle of the session or using `-framing=min`, and the crop should follow the measured noise
rather than a fixed 4 %.

### 2.4 Banding and fixed-pattern noise: matches

*Measured:* the deband estimator's own numbers (rows 0.14 ADU fixed pattern against a 0.20 ADU
noise floor: skip; columns 0.50 ADU at 3.3x its floor: applied) are consistent with an independent
check on the raw frames. Note that the README's deband section says "nothing was corrected" while
`deband_summary.json` and the results table show the column pattern *was* subtracted from all 427
frames, and `run_pipeline.sh` runs `deband.py` with the default `--axis rows`, so a fresh run would
not apply it. The measurement method (covariance of two independent edge estimates, high-passed, with
stars masked) is sound and appropriately conservative. Nothing was missed that matters: no
persistent hot pixels, no glow, no dust, and the 19 degrees of rotation plus the scope's dithering
smear any residual 0.5 ADU pattern into the noise. Walking noise is a risk in EQ mode with no
rotation, not here.

### 2.5 Colour: matches PCC practice; SPCC is one Siril bug away

*Measured:*
- PCC factors: R 0.446, G 0.424, B 1.000. Blue is scaled up 2.4x relative to green, so after
  calibration the blue channel carries **2.8x** the noise of green (5.26 vs 1.87 ADU), not the 2x in
  the README. This is the camera's weak blue response plus a red-heavy Belfast sky; it is inherent.
- Siril warns "imprecise solution, consider correcting the image gradient first" because PCC runs
  before background extraction. Running GraXpert first and then PCC gives identical factors to three
  decimals and the same warning, so the ordering is not costing anything on this data; the warning is
  driven by the blue-channel scatter (deviation 9.0). Reordering is still correct practice.
- Both PCC and SPCC warn that the ASTAP solution has no distortion terms. ASTAP can write SIP
  polynomials; Siril uses them near the corners.
- Headless SPCC: the crash is two problems. First, the SPCC sensor/filter database was never
  downloaded (the GUI syncs it; the CLI does not). I cloned
  `https://gitlab.com/free-astro/siril-spcc-database` into
  `~/Library/Application Support/org.siril.Siril/siril/siril-spcc-database`, after which
  `spcc "-oscsensor=Sony IMX585" "-oscfilter=UV/IR Block"` is accepted, fetches Gaia DR3 and runs
  aperture photometry on 4485 stars, then **segfaults during the fit in Siril 1.4.4**, both from
  `siril-cli` and from the GUI binary driven by a script. That is a Siril bug (the class of GTK-in-
  command crashes 1.4.2 partly fixed [18]). The database has the curves the S30 Pro needs: a "Sony
  IMX585" OSC sensor (from the Player One Uranus-C QE plot, "covers all cameras using this chip"), a
  generic "UV/IR Block" filter for the IR-cut position, and a "ZWO Seestar LP" filter for the
  dual-band position [12]. There is no S30 Pro-specific entry; the Naztronomy script maps the
  S30 Pro to the IMX585 entry.
- The blue noise is handled downstream by `--bg-desat 0.7` and the sky-only smoothing. In the final
  8-bit image the corner sky still shows colour blotches at the 10-30 px scale with amplitude about
  half the luminance blotches (R-B spread 2.1-2.7, luminance 4.6-6.9 in 8-bit units). Desaturation is a
  per-pixel operation; a chroma low-pass at 10-20 px in sky regions would remove the blotches
  without touching luminance.

*Verdict:* PCC is fine. SPCC is worth having the day it runs headless, or via the Siril GUI once per
sensor to record the factors; the colour gain over PCC is real but modest for a broadband galaxy.

### 2.6 Gradient removal and denoising: BGE falls short on this target, denoise matches

*Measured, M31 surface brightness above the far sky (ADU, 16-bit stack units) after each background model:*

| model | minor axis 24' | 30' | 37' | 43' | major axis 18' | 24' | corner-to-corner sky range |
|---|---|---|---|---|---|---|---|
| none (02_cc) | 8.7 | 6.1 | 3.3 | 1.9 | 3.8 | 2.0 | 1.2 |
| GraXpert AI, smoothing 0.5 (pipeline) | 7.5 | 4.6 | 1.7 | 0.7 | 2.9 | 1.2 | 0.3 |
| GraXpert AI, smoothing 1.0 | 7.7 | 5.0 | 2.1 | 0.9 | 3.1 | 1.4 | 1.1 |
| GraXpert AI, smoothing 0.0 | 7.4 | 4.4 | 1.6 | 0.8 | 2.8 | 1.1 | 0.4 |
| Siril `subsky 1` | 8.8 | 6.1 | 3.3 | 2.0 | 3.8 | 2.0 | 0.4 |
| Siril `subsky 2` | 8.8 | 6.1 | 3.3 | 2.0 | 3.9 | 2.1 | 0.5 |
| Siril `subsky -rbf` | 8.5 | 5.8 | 3.0 | 1.6 | 3.6 | 1.8 | 0.6 |

The GraXpert model image shows two lobes symmetric about the nucleus along the minor axis: it is
fitting the galaxy. The real residual gradient after normalised stacking of a rotating field is tiny
(1.2 ADU on a 180 ADU sky, 0.7 %), so a plane removes it. The AI model's failure is the classic
"target fills the frame" case, and smoothing does not fix it.

Denoise: GraXpert's strength is an exact linear blend, `out = strength * denoised + (1 - strength) * input`
(confirmed in its source [19] and measured: residual noise at 0.5 / 0.8 / 1.0 is 0.50 / 0.80 / 1.00 of
the full-strength residual). The README's "negligible difference from 0.5" is wrong; its test
compared a maximum pixel change against the full 16-bit range. At 0.8, green sky noise drops from
1.87 to 0.55 ADU (3.4x) and blue from 5.26 to 1.52. The residual (input minus denoised) has a
correlation of 0.007 with the smoothed galaxy signal and a 0.1-0.3 % difference in 2-8 px smoothed
structure, so no faint structure is being removed at these scales. The denoiser leaves stars alone
by design (pixels above median + 250 MAD are passed through). It is applied at the right point
(linear, after gradient removal, before star removal).

### 2.7 Deconvolution and sharpening: none, correctly

*Measured:* Siril `makepsf stars` (17 px kernel) then `rl`: 10 iterations changed FWHM from 14.05" to
14.00"; 25 iterations with total-variation regularisation reached 13.87" (1.3 %) while the darkest
annulus around the 200 brightest stars went from -2.2 to -11.1 sky sigma (rings). At 3.8 px FWHM the
PSF is barely resolved, so there is very little to deconvolve. BlurXTerminator's manual calls 3-4 px
"adequately sampled" and needs at least that; it would produce a cleaner result than RL but the
ceiling is the same. GraXpert 3.1's deconvolution models map a 4 px FWHM to the bottom of their input
range [19]. Cosmic Clarity's sharpener is a stretched-domain model with user-reported ringing [20].

*Verdict:* the biggest resolution lever is focus, not software: FWHM was 12.5-13" at the start of the
session and 17" at the end with the focuser never moving.

### 2.8 Star separation and stretch: matches on separation, falls short on highlights

*Measured / reviewed:*
- Star separation is clean: no tile blocks, neutral sky in the starless image, no obvious star
  residuals. One artefact: M32 is hollowed by the network, restored by the "protect" guard, and the
  guard leaves a dark ring around it in the starless layer and a ring in the star layer.
- Recombination: the star layer is made in the linear domain by "unscreen" (equivalent to
  subtraction at these levels), stretched with the same midtone as the starless image, scaled by 0.7
  and screened back. This is the standard StarXTerminator-style approach and what Siril's Star
  Recomposition does [11]. It is not photometrically exact (a faint star stretched alone comes out
  brighter than it would inside the full image, which the 0.7 factor partly cancels), but that is
  true of every screen recombination and is a creative control. Correct.
- The stretch clips the nucleus: a 35 px (2.1') disc is pure white in the output although the linear
  peak is only 0.038 of full scale and the clip level corresponds to 0.011, so the top 1.8 stops of
  the nucleus are discarded. The MTF-then-arcsinh-then-saturation chain has no highlight shoulder. GHS
  with highlight protection, or a soft knee above 0.7 in the existing chain, would keep the core.
- Sky-only smoothing (`--bg-smooth 1.0`) is defensible: it acts only where the noise-weighted mask
  says there is no signal, and the same effect is available at zero cost by raising GraXpert strength
  to 1.0 (green sky noise 0.25 ADU) and dropping the blur. Neither hides data that the stack contains.
- The README says the default target background is 0.15; the code default is 0.10.

### 2.9 Multi-session integration: correct plan, one setting to add

The README's plan (put both nights' lights in one session directory, run once) is what the Siril
developers recommend: never stack stacks, register all frames against one reference [21]. Add:
`-framing=max` with `stack -maximize` (and `-feather=`) so a different framing is kept rather than
cropped, `-fastnorm` for long sequences, and use `-weight=noise` (or none) rather than wfwhm, since
night-to-night sky brightness differences are exactly what noise weighting handles. If star
registration fails across very different framings, `seqplatesolve` then `seqapplyreg` registers on
the sky solution instead [22]. The QA stage already judges each night against its own trend.

### 2.10 Narrowband (the built-in LP filter)

The filter is Ha ~656/20 nm plus OIII ~500/30 nm [23]; frames arrive with `FILTER='LP'`. The
pipeline would need: (1) skip PCC/SPCC broadband, or use SPCC's narrowband mode
(`-narrowband -rwl=656.3 -rbw=20 -gwl=500.7 -gbw=30 -bwl=500.7 -bbw=30`) once SPCC runs;
(2) `seqextract_HaOIII` on the registered CFA frames or `extract_HaOIII` on the stack to get a
half-resolution Ha and full-resolution OIII; (3) match OIII to Ha statistically (`linear_match` or the
MAD/median PixelMath expression used by the eVscope scripts) and compose HOO as R = Ha,
G = B = OIII (or G = 0.8 Ha + 0.2 OIII) [24]; (4) for continuum subtraction, plate-solve the broadband
and LP stacks to a common grid, scale the broadband red by the median star flux ratio, subtract, and
clip. QA, deband and Bayer-drizzle stacking work unchanged. The `ZWO Seestar LP` SPCC entry exists
for the day SPCC works headless.

### 2.11 Other things a reviewer would flag

- **Pixel scale.** QA uses 3.74"/px from the header's 160 mm; the plate solve gives 3.665"/px
  (163 mm). All QA FWHM numbers are 2 % high. Trivial, but the README quotes them.
- **Deband default vs. documented run.** `run_pipeline.sh` runs `deband.py` with `--axis rows`; the
  recorded run used `both` (the summary has a `cols` block). A fresh run will not reproduce the
  published stack.
- **QA's FWHM estimate** is 2 x half-flux radius on a 2x2 binned image; binning biases it up by a
  few percent. Siril's registration FWHM (3.5-5.6 px) is the better number and is already in
  `r_light_.seq`; QA could read it instead of re-measuring.
- **destar.py's inverse stretch** re-estimates the midtone from the starless image rather than
  reusing the forward parameters (same as Cosmic Clarity). The error is small but it is why
  `np.minimum(starless, img)` is needed.
- **ASTAP SIP.** Add distortion terms so Siril stops warning and corner stars calibrate correctly.
- **Sub-exposure.** With read noise at 26-30 % of the variance (57 % in blue by the less reliable
  blue fit), 20 s subs would raise SNR per hour by about 8 % in green and 18 % in blue, plus 2 % from
  halving the 1 s per-frame overhead; 30 s about 11 % and 27 %. The cost is more wind rejections
  (ZWO's own figure: under 10 % at 30 s in a calm sky [25]) and twice the risk per dropped frame.
  The README's "no effect" claim is too strong.
- **EQ mode** removes rotation and allows 60 s subs, but it also removes the free dithering that
  rotation provides; users report walking noise in EQ stacks [26]. Not recommended for this user
  unless the alt-az drop rate on windy nights becomes the limit.
- **Scope's own stack.** The Seestar's live stack of 479 frames has the same faint-galaxy SNR as the
  pipeline's at the 8x8 binned scale (23.9 vs 21.3, it has 12 % more frames) and 15.8" FWHM against
  the pipeline's 13.6-14.0". The pipeline wins on resolution, colour calibration, gradient, and
  control, not on raw depth; with the weighting and selection changes above it would win on depth too.

---

## Step 3: Recommendations

Ordered by value for effort for this user. "Gain" figures are measured on this data unless marked.

### (a) Do now

| # | change | gain | effort | confidence |
|---|---|---|---|---|
| 1 | Turn on the scope's periodic autofocus (or refocus at mid-session). Focus never moved while FWHM went 12.9" -> 17.3". | Largest available resolution gain; likely 15-25 % smaller stars on a similar night (inferred from the first vs last hour). | none | high that focus drifted; medium on the size of the gain |
| 2 | Remove `-weight=wfwhm` from `stack.sh` (or make `nbstars` the default). | +23 % SNR at 9 % FWHM cost (+11 % / 4 % for nbstars) | one token | high (measured) |
| 3 | Drop the trend-relative FWHM and roundness rejection in QA; keep the absolute caps and cloud tests. | +12 % SNR (+30 % combined with #2) at 3 % FWHM cost | flags: `--fwhm-mad 99 --round-mad 99` or code | high (measured) |
| 4 | Use Siril `subsky 1` instead of GraXpert AI for the gradient when the target fills the frame (make it the default; GraXpert as option). | keeps the outer halo: 3.3 vs 1.7 ADU at 37' | small shell change | high (measured) |
| 5 | Fix corner coverage: run `setref` to a frame from the middle of the session after `register -2pass`, or use `-framing=min`; crop by measured noise. | removes 2x-noise corners and the mottled bottom corners | small | high (measured) |
| 6 | Correct the README: denoise strength is a linear blend; rotation 19.4 degrees; column deband was applied; target-bg 0.10; pixel scale 3.665. Make `deband --axis both` the default if the column fix is wanted. | correctness | doc | high |
| 7 | Fix `stack.sh --no-drizzle` (`convert light -debayer`). | path currently outputs mono | one token | high (measured) |
| 8 | Add a highlight shoulder to the stretch (or switch to `autoghs`/`ght` with HP), so the nucleus is not clipped to a 2.1' white disc. | cosmetic but visible in every image of a bright core | small | high |
| 9 | Low-pass the chroma (not luminance) in sky regions at 10-20 px, instead of or in addition to desaturating. | removes the colour blotches; cosmetic | small | medium |
| 10 | Move PCC after gradient removal and ask ASTAP for SIP terms. | no measured change in the factors; removes two Siril warnings and helps corner stars | trivial | high that it is correct, low that it changes this image |
| 11 | Try 20 s subs on a calm night and compare the drop rate. | +8 % (green) to +18 % (blue) SNR per hour, half the frame count | capture setting | medium (photon-transfer fit) |
| 12 | Take a flat with the scope's Flat Shoot, or fit a synthetic flat to the frame median, and pass it as `-flat=` to the drizzle. | corners 17-22 % brighter, 8 % colour shift removed; mostly matters for PCC and off-centre targets | medium | high on the vignetting, medium on the workflow |

### (b) When the second night arrives

- Link both nights' lights into one `lights/` (as the README says), register with `-2pass`,
  `-framing=max`, `stack ... -maximize -feather=50 -fastnorm`, no weighting or `-weight=noise`.
  Expect 1.4x lower noise for equal integration; more if the second night is sharper.
- Keep QA per night (it already trends per session) but with the loosened thresholds from (a)3.
- If the framing differs by more than the overlap tolerates, plate-solve registration
  (`seqplatesolve` + `seqapplyreg`) instead of star matching.
- Re-run the weighting comparison: with two nights of different sky, `noise` weighting should
  start to matter and `wfwhm` will again favour the sharper night.

### (c) Only worth it with paid tools or the GPU PC

- **BlurXTerminator** (PixInsight, paid): the one paid tool without a free equivalent. Expected gain
  at 3.8 px FWHM: modest star tightening and mild dust-lane contrast; "Correct Only" first for the
  elongated frames. Worth it if you end up in PixInsight anyway; not worth buying PixInsight for.
- **SPCC** in PixInsight or the Siril GUI: works today, gives calibrated colour from the IMX585
  curve. Free in Siril's GUI (manual step), blocked headless by the 1.4.4 crash.
- **GPU PC**: GraXpert denoise drops from 4 min to seconds and Dark Star from 20 s to 2 s. Speed
  only; no quality change. Useful if you start iterating on stretch settings.
- **NoiseXTerminator / StarXTerminator**: within a few percent of GraXpert / Dark Star on this data
  class by every comparison found; not worth paying for on their own.

### (d) Not worth it for this setup, despite being common practice

- **2x drizzle**: 3 % FWHM for 4x the data. The data are not undersampled.
- **Pixfrac below 1 at scale 1**: only adds noise.
- **Deconvolution** (Siril RL, Cosmic Clarity sharpen, GraXpert 3.1 deconvolution): 1 % FWHM, rings.
- **Linear-fit rejection**: worse noise and clipped star cores here. GESD is a fine alternative to
  winsorized but changes nothing measurable.
- **Noise weighting on a single night**: identical to no weighting; keep it for multi-night.
- **Darks and bias**: nothing to correct.
- **Chasing headless SPCC in 1.4.4**: it is a Siril crash, not a configuration problem. Re-test on
  the next Siril release with the database already in place.
- **Per-frame deband**: the code correctly skips it; leave it.
- **More aggressive frame culling** (Siril `-filter-wfwhm=90%`): 2.5 % FWHM for 9 % SNR.
- **EQ mode** for this target: loses the free dithering; only worth trying if the windy-night drop
  rate makes 20 s subs impossible in alt-az.

---

## Verdict

On a five-step scale where 1 is the scope's own JPEG, 2 is Siril's stock Seestar script, 3 is a
careful free workflow with sensible defaults, 4 is the best achievable free workflow with settings
verified on the data, and 5 is the paid PixInsight chain with BlurXTerminator, this pipeline sits at
a strong 3: its registration, drizzle, rejection, QA instrumentation and star handling are already at
level 4, but two defaults, wFWHM weighting with trend-relative frame rejection and AI background
extraction, pull the final image below what the same data would give with the plainest settings,
costing about 30 % of the achievable signal-to-noise and half of M31's outer halo. The single change
that moves the result furthest is to stop weighting and culling by star size on this kind of target;
the single change that moves the *data* furthest is to let the scope refocus during the session.

---

## Appendix: what was run

All experiments ran in a scratch directory on hard links to the session's frames; nothing under
`data/` or `scripts/` was modified. One change outside the repo: the Siril SPCC database was cloned
into `~/Library/Application Support/org.siril.Siril/siril/siril-spcc-database` (the GUI would do
this on its first online start).

- **Raw frame statistics**: astropy/numpy on `lights/`; pedestal, code histogram, per-CFA-plane
  sky and noise.
- **Photon transfer**: three consecutive frame pairs, 32 px tiles, plane removed per tile, stars
  masked, robust variance vs. mean fit per CFA plane. Green planes agree (K = 11.1 and 11.2 ADU/e-,
  RN = 2.6 e-); red and blue fits give different K (20 and 13), which I attribute to the narrow
  signal range, so only the green numbers are quoted as reliable.
- **Fixed pattern**: per-pixel median of every 4th frame (120 frames); 7 px median-filter residual
  for hot pixels; 16x16 binned corner/edge/centre ratios per plane.
- **Siril experiments** (Siril 1.4.4 CLI, scripts in the appendix directory of the scratchpad):
  A: 427 frames from `lights_db`, one `register -2pass` + `seqapplyreg -drizzle`, eight `stack`
  variants. B: all 479 frames from `lights`, five stack variants. C: 100 frames (nos. 100-199),
  1x pixfrac 1.0, 1x pixfrac 0.7, 2x pixfrac 0.8, and `convert -debayer` + Lanczos-4.
- **Stack metrics** (`measure_stack.py`): sky mask from a 65 px smoothed luminance within the
  central 70 %; noise = 1.4826 x MAD of a 9 px median-filtered residual on sky pixels; faint-galaxy
  signal = median of pixels 3-200 MAD above sky in the 8 px smoothed image; FWHM = 2 x half-flux
  radius of stars with peak > 30 sigma and < 0.5 max, via `sep`; 2x stacks binned 2x2 first.
- **Background models**: GraXpert 3.0.2 CLI at smoothing 0.0/0.5/1.0; Siril `subsky 1`, `subsky 2
  -tolerance=0.5`, `subsky -rbf -smooth=0.8 -samples=15 -tolerance=0.5`; profiles are medians of
  40 x 40 px boxes along the axes found from image moments, both directions averaged, relative to the
  sky-mask median.
- **Denoise**: GraXpert CLI at 0.5, 0.8 (pipeline output), 1.0 on `03_bge.fits`; residual statistics
  in sky and galaxy masks; correlation of residual with 8 px smoothed signal. The 0.2 run was
  started but not measured.
- **Deconvolution**: Siril `makepsf stars -sym`, `rl -iters=10 -alpha=3000` and
  `rl -iters=25 -alpha=1000 -tv` on `03_bge.fits`.
- **SPCC**: database cloned; `spcc` with full and minimal arguments via `siril-cli`, and via the
  GUI binary with `-s`; all three reach photometry then exit with signal 11.
- **Order test**: GraXpert BGE on `01_solved.fit` then `pcc`; factors identical to the pipeline's.
- **`--no-drizzle`**: `scripts/stack.sh` on a 12-frame scratch session; output is 1-layer.
- **Failures**: the first photon-transfer attempt (sky-only tiles, 64 px) fitted a negative read
  noise in green because the signal range was too narrow; the first BGE profile locked onto an edge
  artefact instead of the nucleus; the first SPCC attempt died on a Zenodo 503 before reaching the
  fit. All were repeated.

## Sources

1. Siril Seestar preprocessing script: https://gitlab.com/free-astro/siril-scripts/-/blob/main/preprocessing/Seestar_Preprocessing.ssf
2. Naztronomy smart-telescope script (same repository).
3. Conejero on CFA drizzle: https://pixinsight.com/forum/index.php?threads/bayer-drizzle-instead-of-de-bayering-with-osc.12996/
4. Siril drizzle documentation: https://siril.readthedocs.io/en/stable/preprocessing/drizzle.html
5. PixInsight forum, "What use is drizzle 1x in WBPP": https://pixinsight.com/forum/index.php?threads/what-use-is-drizzle-1x-in-wbpp.21223/
6. Siril stacking documentation: https://siril.readthedocs.io/en/latest/preprocessing/stacking.html
7. Utah Desert Remote on WBPP rejection: https://utahdesertremote.com/improve-your-astrophotography-with-weighted-batch-preprocessing/
8. PixInsight forum, winsorized sigma clipping values: https://pixinsight.com/forum/index.php?threads/winsorised-sigma-clipping-rejection-values.8069/
9. PixInsight ImageWeighting reference: https://pixinsight.com/doc/docs/ImageWeighting/ImageWeighting.html
10. Northern Lights guide, SubframeSelector: https://northernlights.guide/knowledge-base/subframe-selector-analysing-and-weighting-your-data/
11. Siril Star Recomposition: https://siril.readthedocs.io/en/stable/processing/stars/star-recomp.html
12. Siril SPCC database: https://gitlab.com/free-astro/siril-spcc-database
13. ZWO ASI585 manual: https://i.zwoastro.com/zwo-website/manuals/ASI585_Manual_EN_V1.0.pdf
14. grelf.net on Seestar files: https://www.grelf.net/seestar.html
15. ZWO forum, S30 Pro dark frames: https://bbs.zwoastro.com/d/26162-s30-pro-not-creating-dark-frames
16. Siril Seestar tutorial: https://siril.org/tutorials/seestar/
17. Seestar Flat Shoot: https://www.seestar.com/blogs/tutorial/use-flat-shoot-on-seestar
18. Siril 1.4.2 release notes: https://siril.org/download/2026-02-18-siril-1-4-2/
19. GraXpert source, `graxpert/denoising.py` and deconvolution: https://github.com/Steffenhir/GraXpert
20. Cosmic Clarity first impressions: https://www.astrobin.com/forum/c/astrophotography/deep-sky-processing-techniques/seti-astro-cosmic-clarity-sharpening-and-denoising-filters-first-impressions/
21. Siril FAQ on multiple sessions: https://siril.org/faq/
22. Siril 1.4 framing=max discussion: https://discuss.pixls.us/t/siril-1-4-question-on-framing-max-option/49976
23. Seestar LP filter guide: https://www.seestar.com/blogs/tutorial/seestar-light-pollution-filter-guide
24. HOO from OSC dual-band: https://www.stastrophotography.com/creating-a-hubble-palette-image-from-osc-dual-band-data/
25. ZWO S50 Pro review (30 s subs): https://www.seestar.com/blogs/review/seestar-s50-pro-vs-s50-how-much-better-is-it-under-bortle-8-skies
26. ZWO forum, dithering on S30 Pro: https://bbs.zwoastro.com/d/26133-dithering-about-s30-pro
