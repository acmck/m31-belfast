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
from astropy.io import fits

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


def destar(rgb_chw, session, strength=1.0, norm="mtf", bg_match=False, bgr=True, protect=0.45, mode="mono",
           protect_sigma=PROTECT_SIGMA, protect_ramp=PROTECT_RAMP, protect_flux_ramp=PROTECT_FLUX_RAMP):
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
        # the network removed something that is both bright over a large area and large in extent:
        # a 12 px-smoothed luminance above `protect` AND a 12 px-smoothed removed-flux above 0.02.
        from scipy.ndimage import gaussian_filter
        lum = work.mean(-1)
        big = gaussian_filter(lum, 12)
        removed = gaussian_filter(np.clip(lum - starless.mean(-1), 0, None), 12)
        m = np.clip((big - protect) / protect_ramp, 0, 1) * np.clip((removed - 0.02) / protect_flux_ramp, 0, 1)
        m = gaussian_filter(m, protect_sigma)[..., None]                     # wide soft edge, no ring
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
    ap.add_argument("--protect", type=float, default=0.45, help="restore bright, extended structures the network removed (galaxy cores); threshold on 12 px-smoothed stretched luminance, 0 = off")
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
    starless, stars = destar(d, sess, a.strength, a.norm, a.bg_match, not a.rgb_order, a.protect, a.mode)
    print(f"done in {time.time() - t0:.0f}s; starless median {np.median(starless):.5f}, stars max {stars.max():.3f}, star flux fraction {stars.sum() / max(d.sum(), 1e-9):.3%}")
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    hdr["HISTORY"] = f"destar.py Dark Star {os.path.basename(a.model)} strength={a.strength}"
    fits.PrimaryHDU((starless * scale).astype(np.float32), header=hdr).writeto(f"{out}_starless.fits", overwrite=True)
    fits.PrimaryHDU((stars * scale).astype(np.float32), header=hdr).writeto(f"{out}_stars.fits", overwrite=True)
    print(f"wrote {out}_starless.fits and {out}_stars.fits")


if __name__ == "__main__":
    main()
