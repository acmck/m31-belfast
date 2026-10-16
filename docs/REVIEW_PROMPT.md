# Review request: Seestar S30 Pro post-processing pipeline

You are reviewing an astrophotography post-processing pipeline as a senior imager and software
reviewer. Your audience is one amateur: a Seestar S30 Pro owner imaging from a Bortle 6 back garden
in Belfast (lat 54.6 N), comfortable with scripts, not an astrophotography expert, with an arm64
Mac (12 cores, 24 GB) and access to a many-core Intel PC with 64 GB and an NVIDIA GPU. He wants a
modern, mostly hands-off workflow that produces the best image the data can support, and he wants
to understand *why* each step is there.

## What to review

Repository: `/Users/andrew/src/seestar` (git, branch `main`). Read `README.md` first, then every
file under `scripts/` and `run_pipeline.sh`. Do not skim: the README records measurements and
decisions, and several defaults were chosen against evidence that is described there.

Data for testing: `data/M31_2026-09-07/` holds 479 raw 10 s CFA sub-frames (`lights/`), the scope's
own live stack (`seestar_stack/`), and every intermediate product of one full run (`qa/`, `stack/`,
`final/`). You may run any stage; the whole pipeline takes about 8 minutes. Use
`.venv/bin/python` for Python. Tools installed: Siril 1.4.4, GraXpert 3.0.2, ASTAP CLI + D50
database, Dark Star ONNX model, Homebrew bash 5.

Pipeline stages, in order: pull frames over SMB -> per-frame QA with trend-relative rejection ->
measure-first banding correction -> Siril 2-pass registration + Bayer drizzle + winsorized
rejection stack -> ASTAP plate solve -> Siril photometric colour calibration -> GraXpert AI
background extraction -> GraXpert AI denoise -> Dark Star star separation -> stretch (neutral sky,
linked MTF, signal-weighted SCNR, arcsinh colour, saturation, sky-only smoothing, star-layer
screen recombination) -> TIFF/PNG/JPEG.

## The question

**How does this pipeline compare to the gold standard for this class of data, and what would
most improve the result for this user?**

"This class of data" means: a small, fixed-configuration smart telescope (30 mm aperture, 150 mm
focal length, IMX585-class OSC sensor, 2.9 um pixels, ~3.7"/px), alt-az tracking with field
rotation, short sub-exposures (10 s, gain 200), no user-supplied darks, flats or bias, hundreds of
frames per session, moderate light pollution, no guiding, no cooling.

### Step 1. Establish the reference

Research what experienced imagers currently consider best practice for OSC smart-telescope data.
Look at, at minimum: the PixInsight workflow (WBPP, BlurXTerminator, NoiseXTerminator,
StarXTerminator, SPCC, GHS), the free Siril + GraXpert workflow, Seestar-specific community
workflows (Cloudy Nights, the Seestar subreddit, ZWO's own guidance), and any published comparisons
of drizzle, rejection and weighting choices for large numbers of short subs. Cite what you use.

If no single gold standard exists for this class of data, say so plainly and then **define one**:
the sequence of steps and the specific settings you would defend as the best achievable today,
with a free-tools variant and a paid-tools variant. State what each step buys and what evidence
supports it.

### Step 2. Compare, stage by stage

For each stage of the current pipeline, say whether it matches, exceeds or falls short of the
reference, and why. Be specific about settings, not just tools. Areas I particularly want examined:

- **Calibration.** The Seestar supplies no darks or flats. Is that leaving quality on the table
  (amp glow, dust, vignetting, hot pixels), and is there a practical way for this user to take
  them? Does the scope apply any internal calibration already? Check the FITS headers.
- **Frame selection and weighting.** QA rejects ~11 % of frames on FWHM and roundness relative to a
  rolling trend, and Siril weights by wFWHM. Is rejection helping or hurting SNR here? Would a
  different weighting (noise, PSF-based, per-frame SNR) do better? Quantify on the data if you can.
- **Registration and stacking.** Bayer drizzle at 1x, pixfrac 1.0, winsorized 3/3 rejection,
  additive+scaling normalisation. Would 2x drizzle, a different kernel or pixfrac, or a different
  rejection be better for 400+ dithered frames at 3.7"/px? What about the ~25 degrees of field
  rotation and the uneven coverage at the corners?
- **Banding and fixed-pattern noise.** The pipeline measured row banding as negligible and found
  a 0.5 ADU fixed column pattern. Is the method sound? Is anything being missed (e.g. pattern that
  only shows after stacking, walking noise, dark-current structure)?
- **Colour.** Photometric colour calibration via PCC/NOMAD is used because headless SPCC crashes
  without a sensor database. Is SPCC worth fixing, and what sensor/filter curves would the S30 Pro
  need? Is the blue channel's ~2x higher noise being handled well?
- **Gradient removal and denoising.** GraXpert AI at default smoothing; denoise at 0.8 with
  measured negligible difference from 0.5. Are these being applied at the right point in the
  chain and at the right strength? Is the denoise removing real faint structure?
- **Deconvolution / sharpening.** There is none. BlurXTerminator is the paid standard; are there
  credible free alternatives (Cosmic Clarity sharpen, Siril deconvolution) and would they help
  at 3.7"/px with 12-16" FWHM stars?
- **Star separation and stretch.** Dark Star mono-per-channel with per-tile MTF normalisation;
  stars screened back at 0.7. Is the recombination correct (linear vs stretched, screen vs add)?
  Is the stretch itself (linked MTF to 0.10 background, arcsinh, saturation) the right tool, or
  should this be GHS / a different curve? Is the sky-only smoothing defensible or is it hiding
  data?
- **Multi-session integration.** Nothing yet handles combining nights. What is the right way to
  add a second night of M31 (or a different framing) to this stack?
- **Narrowband.** The scope has a built-in dual-band (Ha/OIII) filter. What would the pipeline
  need for those frames (colour calibration off, HOO palette, continuum subtraction with the
  broadband data)?
- **Anything I have not listed** that a strong reviewer would flag.

### Step 3. Recommend

Produce a prioritised list of improvements. For each: what to change, the expected gain
(quantified or honestly described as cosmetic), the effort, whether it needs the NVIDIA PC or paid
software, and how confident you are. Order by value-for-effort *for this user*, not by
sophistication. Mark anything you would *not* do despite it being common practice, and say why.

Separate the list into: (a) do now, (b) do when the second night of data arrives, (c) only worth it
with paid tools or the GPU PC, (d) not worth it for this setup.

## Ground rules

- Measure rather than assert where the data allows. You have the raw frames and every
  intermediate; a claim like "rejection is costing SNR" should come with a number from this data.
- If you run something, report exactly what you ran and what came out, including failures.
- Distinguish clearly between what the literature/community says and what you verified here.
- Do not rewrite the pipeline. This is a review. If a change is small and clearly correct you may
  propose the diff, but do not apply it.
- Plain language for the recommendations. Expand every acronym on first use. The reader knows
  what a galaxy is and how to run a script; he does not know what a wFWHM is.
- Finish with a one-paragraph verdict: where this pipeline sits relative to the gold standard on a
  scale you define, and the single change that would move it furthest.
