#!/usr/bin/env python
"""
Star removal with Seti Astro's Dark Star network (MIT), run locally via onnxruntime.

Reproduces the Cosmic Clarity pre/post-processing:
  * 5 px median border, MTF stretch to median 0.25 if the image is linear (median < 0.125)
  * 256 px tiles, 32 px overlap, each tile min-max normalised and its median lifted to 0.25 before
    the model (inverse applied after) - measured to give the smoothest result on faint sky
  * soft-ramp stitching, inverse stretch, border removed
  * stars-only layer by "unscreen":  stars = (orig - starless) / (1 - starless)
  * runs the network per channel (mono mode) to avoid its colour bias; bright extended cores
    (galaxy nuclei) are protected because the network mistakes them for stars

Writes <out>_starless.fits and <out>_stars.fits (same dtype range as input, 3xHxW float32).
Recombine with a screen blend:  orig = 1 - (1 - starless) * (1 - stars)   (stretch.py --stars does this).

Usage:
  destar.py IN.fits --out PREFIX [--model tools/darkstar/darkstar_v2.1c.onnx] [--strength 1.0] [--cpu]
"""
import argparse, os, sys, time
from pathlib import Path
import numpy as np
import sep
from astropy.io import fits
from scipy.ndimage import gaussian_filter

TILE, OVERLAP, BORDER = 256, 32, 5


def _mtf(x, m):
    """Midtone transfer function with midtone m.  Its own inverse under m -> 1-m:  _mtf(_mtf(x, m), 1-m) == x."""
    return (m - 1) * x / ((2 * m - 1) * x - m)


def mtf_to_median(img, target=0.25):
    """Linked MTF stretch (Cosmic Clarity 'stretch_image'): subtract the minimum, then one MTF whose midtone m
    maps the mean channel median to `target`.  Returns (stretched, orig_min, orig_median, m)."""
    mn = float(img.min())
    x = img - mn
    med = float(np.mean(np.median(x, axis=(0, 1))))
    m = float(_mtf(med, target))                   # the midtone that sends med -> target
    return np.clip(_mtf(x, m), 0, 1), mn, med, m


def mtf_undo(img, m, orig_min):
    """Exact inverse of mtf_to_median: x = _mtf(y, 1 - m) + orig_min.  (The old version re-estimated the
    midtone from the *starless* image's median, so the inverse did not match the forward transform.)"""
    return np.clip(np.clip(_mtf(img, 1.0 - m), 0, 1) + orig_min, 0, 1)


def blend_weights(size, overlap):
    ramp = np.linspace(0, 1, overlap, dtype=np.float32)
    v = np.concatenate([ramp, np.ones(size - 2 * overlap, np.float32), ramp[::-1]])
    return np.outer(v, v)


def run_model(session, img_hwc, batch=8, progress=True, norm="mtf", bg_match=False):
    """Tile -> normalise -> infer -> stitch. img_hwc float32 in 0..1, shape (H, W, 3)."""
    H, W, _ = img_hwc.shape
    step = TILE - OVERLAP
    # pad so tiles cover the image fully (reflect keeps statistics sane at the edges)
    ph = (-(H - TILE) % step) if H > TILE else TILE - H
    pw = (-(W - TILE) % step) if W > TILE else TILE - W
    pad = np.pad(img_hwc, ((0, ph), (0, pw), (0, 0)), mode="reflect")
    PH, PW, _ = pad.shape
    ys = list(range(0, PH - TILE + 1, step)); xs = list(range(0, PW - TILE + 1, step))
    coords = [(y, x) for y in ys for x in xs]
    out = np.zeros_like(pad); wsum = np.zeros(pad.shape[:2], np.float32)
    w = blend_weights(TILE, OVERLAP)
    name = session.get_inputs()[0].name
    t0 = time.time()
    for b in range(0, len(coords), batch):
        cs = coords[b:b + batch]
        tiles = np.stack([pad[y:y + TILE, x:x + TILE] for y, x in cs])          # (B,T,T,3)
        if norm == "none":
            mn = np.zeros((len(cs), 1, 1, 1), np.float32); mx = np.ones((len(cs), 1, 1, 1), np.float32)
        else:   # Cosmic Clarity's per-tile min-max
            mn = tiles.min(axis=(1, 2, 3), keepdims=True); mx = tiles.max(axis=(1, 2, 3), keepdims=True)
        n = (tiles - mn) / (mx - mn + 1e-8)
        if norm == "mtf":
            # lift each tile's median to 0.25 (the level the network was trained around) and undo after
            med = np.clip(np.median(n, axis=(1, 2, 3), keepdims=True), 1e-3, 0.999)
            m = _mtf(med, 0.25)
            n = _mtf(n, m)
        pred = session.run(None, {name: np.ascontiguousarray(n.transpose(0, 3, 1, 2), dtype=np.float32)})[0]
        pred = np.clip(pred.transpose(0, 2, 3, 1), 0, 1)
        if norm == "mtf":
            pred = np.clip(_mtf(pred, 1 - m), 0, 1)
        if bg_match:
            # The network shows a per-channel offset on faint, normalised sky (G down, B up), which
            # stitches into coloured tile blocks.  A starless tile's background must equal the input's
            # background, so re-anchor each channel's median to the input's.
            pred += np.median(n, axis=(1, 2), keepdims=True) - np.median(pred, axis=(1, 2), keepdims=True)
            pred = np.clip(pred, 0, 1)
        pred = pred * (mx - mn + 1e-8) + mn
        for k, (y, x) in enumerate(cs):
            out[y:y + TILE, x:x + TILE] += pred[k] * w[..., None]
            wsum[y:y + TILE, x:x + TILE] += w
        if progress and (b // batch) % 5 == 0:
            print(f"  tiles {min(b + batch, len(coords))}/{len(coords)}  {time.time() - t0:.0f}s", flush=True)
    out /= np.maximum(wsum, 1e-6)[..., None]
    return out[:H, :W]


PROTECT_SIGMA = 15.0      # final smoothing of the protection mask (px); 6 left a dark ring around M32
PROTECT_RAMP = 0.20       # luminance ramp width above `protect` over which the mask goes 0 -> 1 (was 0.12)
PROTECT_FLUX_RAMP = 0.06  # removed-flux ramp width above 0.02 (was 0.04)
# Relative protection (default).  Levels are fractions of the nucleus's own 12 px-smoothed brightness above
# the sky, measured in the LINEAR input, so they mark the same isophote of the galaxy whatever the sky
# brightness.  Calibrated on M31 2026-09-07 to reproduce the stretched-mode mask that cured the ring there
# (raw mask 0 at r~160 px, 1 at r~80 px); on the moonlit 2026-09-25 data the stretched mode collapsed to a
# peak of 0.17 and the network hollowed the nucleus, leaving a ring.
PROTECT_REL_LO = 0.03     # protection starts where the galaxy is 3 % of the nucleus brightness above sky
PROTECT_REL_RAMP = 0.06   # ...and is full at 9 %
NUCLEUS_SIGMA = 40.0      # heavy smoothing used only to locate the nucleus (stars wash out, the core does not)
NUCLEUS_MIN_SNR = 20.0    # skip protection if no extended peak stands this far above the smoothed sky noise


def relative_protect_mask(lin_hwc, lo=PROTECT_REL_LO, ramp=PROTECT_REL_RAMP, sigma=PROTECT_SIGMA):
    """Protection mask (H, W) in 0..1 from the linear image, independent of sky brightness and stretch.
    Returns (mask, info) where info describes the nucleus found (or why protection was skipped)."""
    from scipy.ndimage import gaussian_filter
    lum = lin_hwc.mean(-1).astype(np.float64)
    s12 = gaussian_filter(lum, 12)
    sky = float(np.median(s12))
    noise = 1.4826 * float(np.median(np.abs(s12 - sky))) + 1e-12
    s40 = gaussian_filter(lum, NUCLEUS_SIGMA)
    iy, ix = np.unravel_index(int(np.argmax(s40)), s40.shape)
    norm = float(s12[iy, ix]) - sky
    if norm < NUCLEUS_MIN_SNR * noise:
        return np.zeros(lum.shape, np.float32), f"no extended core (peak {norm / noise:.1f}x smoothed sky noise < {NUCLEUS_MIN_SNR:g}); protection off"
    m = np.clip(((s12 - sky) / norm - lo) / ramp, 0, 1)
    m = gaussian_filter(m, sigma).astype(np.float32)
    return m, f"nucleus at (row {iy}, col {ix}), {norm / noise:.0f}x smoothed sky noise; mask >0.5 over {int((m > 0.5).sum())} px"


def destar(rgb_chw, session, strength=1.0, norm="mtf", bg_match=False, bgr=True, protect=0.45, mode="mono",
           protect_sigma=PROTECT_SIGMA, protect_ramp=PROTECT_RAMP, protect_flux_ramp=PROTECT_FLUX_RAMP,
           require_removed=False, protect_mode="relative"):
    """rgb_chw: (3,H,W) float32 in 0..1. Returns (starless, stars) both (3,H,W).
    mode:    "mono" runs the network on each channel separately as a grey image (3 passes; no
             cross-channel colour bias, measured residual < 2.5 % per channel) - default.
             "colour" is the single RGB pass (faster, but leaves magenta/cyan colour errors).
    bgr:     colour mode only: feed channels reversed (measured lower residual than RGB)
    protect: bright extended cores (galaxy nuclei) are mistaken for stars; where the 12 px-smoothed
             stretched luminance exceeds this level the original is blended back (0 disables).
             protect_sigma / protect_ramp / protect_flux_ramp soften the mask edge (see module constants)."""
    img = np.moveaxis(rgb_chw, 0, -1).astype(np.float32)
    med_all = float(np.median(img))
    bordered = np.pad(img, ((BORDER, BORDER), (BORDER, BORDER), (0, 0)), mode="constant", constant_values=med_all)
    linear = np.median(bordered) < 0.125
    if linear:
        work, mn0, med0, m0 = mtf_to_median(bordered)
    else:
        work = bordered
    if mode == "mono":
        chans = []
        for c in range(3):
            print(f"  channel {'RGB'[c]}", flush=True)
            mono = np.repeat(work[..., c:c + 1], 3, axis=-1)
            chans.append(run_model(session, np.ascontiguousarray(mono), norm=norm, bg_match=bg_match, progress=False)[..., 0])
        starless = np.stack(chans, -1)
    else:
        if bgr:
            work = work[..., ::-1]
        starless = run_model(session, np.ascontiguousarray(work), norm=norm, bg_match=bg_match)
        if bgr:
            work = work[..., ::-1]; starless = starless[..., ::-1]
    if protect > 0:
        # Galaxy nuclei and small companions (M32) get mistaken for stars.  Restore the original wherever
        # the target is bright over a large area (a 12 px-smoothed luminance above `protect`).
        #
        # An earlier version additionally required a 12 px-smoothed "removed flux" above a floor, on the
        # theory that only pixels the network actually changed need protecting.  On M31's nucleus this
        # made things WORSE: the network's own removal (in the stretched domain used here) falls from
        # about 0.3-0.4 at the core to under 0.02 by ~65 px radius, while the extended brightness itself
        # stays above a typical protect threshold out to ~90 px.  Gating on both meant protection was cut
        # off at the tighter (removed-flux) radius, leaving an unprotected annulus between roughly 65 and
        # 90 px where the network's removal was small but nonzero -- visible as a dark ring/"moat" around
        # the nucleus, worst right where protection ended.  Brightness alone gives a clean, monotonic mask
        # radius with no such gap; measured to remove the ring completely at the default threshold with no
        # loss of star removal beyond it (checked down to protect=0.30 before stars near the core start
        # reappearing).  protect_flux_ramp is kept as an argument for anyone who wants the old AND-gated
        # behaviour back, via the (undocumented) --require-removed flag.
        #
        # That absolute stretched threshold still depends on the sky: the stretch puts the sky at 0.25, so a
        # bright (moonlit) sky compresses the nucleus.  M31 2026-09-07 reached 0.93 and was fully protected;
        # 2026-09-25 under a near-full moon reached only 0.54, the mask peaked at 0.17, the network removed the
        # nucleus as a star and the edge of the hole showed as a ring.  protect_mode="relative" (default)
        # measures brightness above sky as a fraction of the nucleus in the linear input instead.
        from scipy.ndimage import gaussian_filter
        if protect_mode == "relative":
            m, info = relative_protect_mask(bordered, sigma=protect_sigma)
            print(f"  core protection (relative): {info}", flush=True)
            m = m[..., None]
        else:
            lum = work.mean(-1)
            big = gaussian_filter(lum, 12)
            m = np.clip((big - protect) / protect_ramp, 0, 1)
            if require_removed:
                removed = gaussian_filter(np.clip(lum - starless.mean(-1), 0, None), 12)
                m = m * np.clip((removed - 0.02) / protect_flux_ramp, 0, 1)
            m = gaussian_filter(m, protect_sigma)[..., None]                 # wide soft edge, no ring
        starless = starless * (1 - m) + work * m
    if linear:
        starless = mtf_undo(starless, m0, mn0)
    starless = starless[BORDER:BORDER + img.shape[0], BORDER:BORDER + img.shape[1]]
    # A starless image can never be brighter than the original.  With the exact MTF inverse this clamp
    # should be (nearly) a no-op; report how much it still touches so a regression is visible.
    diff = starless - img; over = diff > 0
    print(f"  np.minimum clamp: {int(over.sum())} of {over.size} values ({100 * over.mean():.2f} %), "
          f"median signed (starless - orig) {float(np.median(diff)):+.2e} (a systematic offset means the inverse "
          f"stretch is off; scatter around 0 is just sky noise vs the network's smooth sky), "
          f"max excess {float(diff.max()):.2e}", flush=True)
    starless = np.minimum(starless, img)
    if strength < 1.0:
        starless = img * (1 - strength) + starless * strength
    stars = np.clip((img - starless) / np.clip(1 - starless, 1e-4, None), 0, 1)   # unscreen
    return np.moveaxis(starless, -1, 0), np.moveaxis(stars, -1, 0)


def remove_residual_points(starless_chw, k_sigma=10.0, max_radius_px=4.0, max_npix=200, smooth_sigma=20, margin=3):
    """Mop up point sources the network left behind.  Common near a bright, high-surface-brightness
    core: individual 256 px tiles there are dominated by the smooth bright galaxy gradient, so a star's
    contrast against its immediate surroundings is much lower than the same star would show against
    plain sky, and the network under-removes it.

    Detection: subtract a heavily-smoothed (sigma=`smooth_sigma`) copy of the image, which tracks even a
    steep galaxy nucleus about as well as a real background, and look for compact excess above that in
    the residual.  A box-grid background (as used elsewhere in this file) is the wrong tool here: its
    mesh is coarse relative to a nucleus's own falloff, so a star's contrast against a coarse local
    background is barely better than against raw sky, which is the whole problem being fixed.
    The compactness cut (`max_radius_px`, `max_npix`) matters more than usual: the nucleus's own peak is
    not perfectly captured by a single Gaussian smoothing either, and shows up as a large, low-level
    residual blob at the exact centre - real leftover stars are tiny (a few px) by comparison, so a tight
    size cap keeps the nucleus itself untouched while still catching genuinely point-like residuals.

    Fills each detection from the surrounding smooth background via a masked Gaussian blur (the same
    masked-diffusion technique used for QA's star masking and the synthetic flat) - so a filled spot
    picks up the true local galaxy brightness and gradient, not a flat patch or the network's own value.
    """
    img = np.moveaxis(starless_chw, 0, -1).astype(np.float32)
    lum = np.ascontiguousarray(img.mean(-1))
    smooth = gaussian_filter(lum, smooth_sigma)
    resid = lum - smooth
    sig = 1.4826 * np.median(np.abs(resid - np.median(resid)))
    sep.set_extract_pixstack(5_000_000)   # default 300k overflowed on a large multi-night canvas
    objs = sep.extract(np.ascontiguousarray(resid), thresh=k_sigma, err=max(sig, 1e-12), minarea=3,
                        deblend_nthresh=32, deblend_cont=0.001, clean=True)
    if len(objs) == 0:
        return starless_chw, 0
    # keep compact, round, small sources (a real leftover star, a few px across); reject anything as
    # large as the nucleus's own smoothing residual, and anything elongated (dust-lane structure)
    a, b = objs["a"], objs["b"]
    compact = (a < max_radius_px) & (objs["npix"] < max_npix) & (b / np.maximum(a, 1e-6) > 0.5) & (objs["peak"] > 0)
    obj = objs[compact]
    if len(obj) == 0:
        return starless_chw, 0

    mask = np.zeros(lum.shape, bool)
    yy, xx = np.mgrid[0:lum.shape[0], 0:lum.shape[1]]
    for o in obj:
        rad = max(3.0, 2.2 * max(o["a"], o["b"])) + margin
        mask |= (yy - o["y"]) ** 2 + (xx - o["x"]) ** 2 <= rad ** 2

    # masked-diffusion fill: blur the image with holes zeroed, blur a matching weight map, divide.
    # Two passes at different scales bridges holes larger than a single blur radius cleanly.
    filled = img.copy()
    holes = mask.copy()
    for sigma in (6, 16, 36):
        if not holes.any():
            break
        w = (~holes).astype(np.float32)
        for c in range(img.shape[-1]):
            num = gaussian_filter(np.where(holes, 0.0, filled[..., c]), sigma)
            den = gaussian_filter(w, sigma)
            est = np.divide(num, den, out=np.zeros_like(num), where=den > 1e-6)
            filled[..., c] = np.where(holes, est, filled[..., c])
        holes &= (gaussian_filter((~holes).astype(np.float32), sigma) < 0.3)  # only re-fill still-empty core
    return np.moveaxis(filled, -1, 0), len(obj)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp"); ap.add_argument("--out", required=True, help="output prefix")
    ap.add_argument("--model", default=str(Path(__file__).resolve().parent.parent / "tools/darkstar/darkstar_v2.1c.onnx"))
    ap.add_argument("--strength", type=float, default=1.0, help="1 = full removal; <1 blends some of the original back")
    ap.add_argument("--cpu", action="store_true", help="force the CPU provider (CoreML is used when available)")
    ap.add_argument("--norm", choices=["mtf", "minmax", "none"], default="mtf", help="per-tile normalisation: min-max then median->0.25 (default), plain min-max (Cosmic Clarity), or none")
    ap.add_argument("--bg-match", action="store_true", help="re-anchor each tile's output median to its input (only useful in colour mode; causes blocks in mono mode)")
    ap.add_argument("--mode", choices=["mono", "colour"], default="mono", help="per-channel mono passes (default, cleanest) or one colour pass")
    ap.add_argument("--rgb-order", action="store_true", help="colour mode: feed channels as RGB instead of the (better-measured) BGR order")
    ap.add_argument("--require-removed", action="store_true",
                    help="also require measurable removed flux before protecting (old behaviour; leaves a ring around M31's nucleus, see comment in destar())")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="skip the residual-star cleanup pass (stars the network missed near a bright core)")
    ap.add_argument("--cleanup-sigma", type=float, default=10.0, help="detection threshold for residual stars, in background sigma")
    ap.add_argument("--protect", type=float, default=0.45, help="restore bright, extended structures the network removed (galaxy cores); 0 = off. In --protect-mode stretched this is the threshold on 12 px-smoothed stretched luminance")
    ap.add_argument("--protect-mode", choices=["relative", "stretched"], default="relative",
                    help="relative (default): protect where the linear image is >3-9 %% of the nucleus brightness above sky - independent of sky brightness; stretched: the old absolute threshold --protect on the stretched image (fails under moonlight)")
    a = ap.parse_args()

    import onnxruntime as ort
    prov = ["CPUExecutionProvider"] if a.cpu else [p for p in ("CoreMLExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
    so = ort.SessionOptions(); so.log_severity_level = 3
    sess = ort.InferenceSession(a.model, so, providers=prov)
    print(f"Dark Star model {os.path.basename(a.model)} on {sess.get_providers()[0]}")

    with fits.open(a.inp) as h:
        d = h[0].data.astype(np.float32); hdr = h[0].header.copy()
    if d.ndim == 2:
        d = np.stack([d, d, d])
    scale = 65535.0 if d.max() > 1.5 else 1.0
    d /= scale
    t0 = time.time()
    starless, stars = destar(d, sess, a.strength, a.norm, a.bg_match, not a.rgb_order, a.protect, a.mode,
                              require_removed=a.require_removed, protect_mode=a.protect_mode)
    if not a.no_cleanup:
        starless, n = remove_residual_points(starless, k_sigma=a.cleanup_sigma)
        print(f"  residual-star cleanup: {n} leftover point sources filled")
        stars = np.clip((d - starless) / np.clip(1 - starless, 1e-4, None), 0, 1)
    print(f"done in {time.time() - t0:.0f}s; starless median {np.median(starless):.5f}, stars max {stars.max():.3f}, star flux fraction {stars.sum() / max(d.sum(), 1e-9):.3%}")
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    hdr["HISTORY"] = f"destar.py Dark Star {os.path.basename(a.model)} strength={a.strength}"
    fits.PrimaryHDU((starless * scale).astype(np.float32), header=hdr).writeto(f"{out}_starless.fits", overwrite=True)
    fits.PrimaryHDU((stars * scale).astype(np.float32), header=hdr).writeto(f"{out}_stars.fits", overwrite=True)
    print(f"wrote {out}_starless.fits and {out}_stars.fits")


if __name__ == "__main__":
    main()
