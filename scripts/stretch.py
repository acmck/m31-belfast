#!/usr/bin/env python
"""
Finish a linear, colour-calibrated, background-extracted RGB stack:
  auto-crop noisy edges (block noise map) -> [HOO palette] -> neutral sky background -> SCNR green removal ->
  linked auto-stretch (midtone transfer function, Siril-style) -> arcsinh colour boost -> saturation ->
  grey sky (sky chroma low-pass + desaturation) -> sky luminance smoothing -> highlight knee ->
  star-layer screen recombination -> 16-bit TIFF, PNG and JPEG, plus a preview strip of 3 strengths.

Usage:
  stretch.py IN.fit(s) --out PREFIX [--stars STARS.fits --star-reduce 0.7] [--crop auto|0.04] [--target-bg 0.10]
             [--shadow-sigma 2.8] [--asinh 3] [--saturation 1.15] [--scnr 0.8] [--no-scnr]
             [--bg-chroma-blur 12] [--bg-desat 0.4] [--bg-smooth 1] [--knee 0.7]
             [--palette rgb|hoo --hoo-green-mix 0.2]

Crop.  `--crop auto` (default) measures a local-noise map on the linear input luminance: high-pass (image minus a
9 px median filter), robust std (1.4826 MAD) per 64 px block, central noise = median of the middle 50 % of blocks.
Edges are trimmed one block row/col at a time (the edge with the most blocks above --crop-noise-ratio x central first) until every border has < 5 % such blocks, plus
one block of margin; clamped to 1..15 % per edge.  Registered stacks with field rotation have ~2x noise in the corners
the reference frame did not cover, and a fixed 4 % misses them.  A number instead of `auto` is a fixed fraction.

Highlight knee.  Values above --knee k are mapped y = k + (1-k) tanh((x-k)/(1-k)), so nothing reaches 255 (max 0.93
for k = 0.7).  It is applied per channel to the starless (or whole) image *before* the star layer is screened back,
so star cores may still reach white while the galaxy core is soft-limited.  Note the MTF itself compresses the top
end (on the M31 stack linear 0.011 -> 0.96, 0.038 -> 0.99): the knee prevents hard clipping and takes the core off
paper-white, but it cannot re-open an already compressed core - that needs highlight protection inside the stretch
or local (HDR) contrast.

Palette.  `--palette hoo` for the dual-band (Ha + OIII) light-pollution filter: R is read as Ha, mean(G, B) as OIII.
OIII is linearly matched to Ha on signal pixels (fit OIII ~ a*Ha + b on pixels 3-200 MAD above sky, Siril's
linear_match idea), then R = Ha, G = mix*Ha + (1-mix)*OIII, B = OIII; SCNR is skipped and the sky is neutralised
after composing.  UNTESTED ON REAL LP-FILTER DATA - only exercised on the broadband M31 stack, where it is false colour.
"""
import argparse
from pathlib import Path
import numpy as np
from astropy.io import fits
from PIL import Image


def load_rgb(path):
    d = fits.getdata(path).astype(np.float32)
    if d.ndim == 2:
        d = np.stack([d, d, d])
    if d.shape[0] != 3 and d.shape[-1] == 3:
        d = np.moveaxis(d, -1, 0)
    if d.max() > 1.5:                      # 16-bit integer data
        d = d / 65535.0
    return d  # (3, H, W) in 0..1


def mtf(x, m):
    """Midtone transfer function (PixInsight/Siril)."""
    return (m - 1) * x / ((2 * m - 1) * x - m)


# ----------------------------------------------------------------------------------------------------- crop
def block_noise_map(lum, block=64, hp_size=9):
    """Robust local noise (1.4826 MAD of a median high-pass) per ~block x block cell.
    Cells are anchored so both edges of each axis are covered (array_split, cells within a pixel of `block`)."""
    from scipy.ndimage import median_filter
    hp = lum - median_filter(lum, size=hp_size)
    ys = np.array_split(np.arange(lum.shape[0]), max(1, round(lum.shape[0] / block)))
    xs = np.array_split(np.arange(lum.shape[1]), max(1, round(lum.shape[1] / block)))
    out = np.zeros((len(ys), len(xs)), np.float32)
    for i, yy in enumerate(ys):
        rows = hp[yy[0]:yy[-1] + 1]
        for j, xx in enumerate(xs):
            b = rows[:, xx[0]:xx[-1] + 1]
            out[i, j] = 1.4826 * np.median(np.abs(b - np.median(b)))
    return out, [len(y) for y in ys], [len(x) for x in xs]


def auto_crop_amounts(lum, ratio=1.35, block=64, min_frac=0.01, max_frac=0.15):
    """Per-edge crop (top, bottom, left, right) in pixels from the block noise map: the run of block rows/cols from
    the edge whose median noise exceeds ratio x central, plus one block margin, clamped to min_frac..max_frac."""
    noise, hs, ws = block_noise_map(lum, block)
    ny, nx = noise.shape
    central = float(np.median(noise[ny // 4: ny - ny // 4, nx // 4: nx - nx // 4]))
    r = noise / (central + 1e-12)

    # Greedy shrink: repeatedly drop one block row/col from whichever edge has the largest fraction of noisy
    # blocks along it, until every border has < 5 % noisy blocks.  The earlier rule (median of each edge line)
    # missed low-coverage strips and wedges that run along only part of an edge - e.g. a multi-night stack
    # where one night was framed at a different rotation leaves a half-height strip on one side and a
    # triangular wedge in a corner, neither of which moves an edge's median.
    bad = r > ratio
    lim_y, lim_x = max(1, int(max_frac * ny)), max(1, int(max_frac * nx))
    t = b = l = rt = 0
    while True:
        sub = bad[t:ny - b, l:nx - rt]
        if sub.size == 0:
            break
        cand = {}
        if t < lim_y: cand["t"] = sub[0].mean()
        if b < lim_y: cand["b"] = sub[-1].mean()
        if l < lim_x: cand["l"] = sub[:, 0].mean()
        if rt < lim_x: cand["r"] = sub[:, -1].mean()
        if not cand:
            break
        k = max(cand, key=cand.get)
        if cand[k] < 0.05:
            break
        if k == "t": t += 1
        elif k == "b": b += 1
        elif k == "l": l += 1
        else: rt += 1
    px = lambda n, sizes: int(sum(sizes[:n + 1])) if n else 0   # n bad blocks + one block margin
    top, bottom, left, right = px(t, hs), px(b, hs[::-1]), px(l, ws), px(rt, ws[::-1])
    H, W = lum.shape
    clamp = lambda v, n: int(min(max(v, round(min_frac * n)), round(max_frac * n)))
    return clamp(top, H), clamp(bottom, H), clamp(left, W), clamp(right, W), central, r


# -------------------------------------------------------------------------------------------------- palette
def linear_match_fit(target, ref, lo=3.0, hi=200.0):
    """Fit target ~ a*ref + b on pixels lo..hi MAD above the reference sky (Siril linear_match), 3-sigma clipped."""
    med = np.median(ref); mad = 1.4826 * np.median(np.abs(ref - med))
    sel = (ref > med + lo * mad) & (ref < med + hi * mad)
    x, y = ref[sel].astype(np.float64), target[sel].astype(np.float64)
    if x.size < 100:
        return 1.0, float(np.median(target) - med)
    for _ in range(2):
        a, b = np.polyfit(x, y, 1)
        res = y - (a * x + b); s = 1.4826 * np.median(np.abs(res - np.median(res)))
        keep = np.abs(res) < 3 * s + 1e-12
        x, y = x[keep], y[keep]
    a, b = np.polyfit(x, y, 1)
    if not np.isfinite(a) or a < 1e-6:
        a = 1.0
    return float(a), float(b)


def compose_hoo(rgb, green_mix=0.2, fit=None, offset=True):
    """Dual-band palette: Ha = R, OIII = mean(G, B) linearly matched to Ha; R = Ha, G = mix*Ha + (1-mix)*OIII, B = OIII.
    `fit` reuses (a, b) from the starless image; offset=False applies only the scale (for the sky-free star layer)."""
    ha = rgb[0]; o3 = 0.5 * (rgb[1] + rgb[2])
    if fit is None:
        fit = linear_match_fit(o3, ha)
    a, b = fit
    o3 = (o3 - (b if offset else 0.0)) / a
    g = green_mix * ha + (1 - green_mix) * o3
    return np.clip(np.stack([ha, g, o3]), 0, None).astype(np.float32), fit


# -------------------------------------------------------------------------------------------------- stretch
def neutralize_background(rgb):
    """Shift each channel so its sky background sits at the same level (neutral grey sky)."""
    meds = []
    for c in rgb:
        med = np.median(c); mad = 1.4826 * np.median(np.abs(c - med))
        sky = c[np.abs(c - med) < 2.5 * mad]
        meds.append(np.median(sky) if sky.size else med)
    meds = np.array(meds, dtype=np.float32)
    return np.clip(rgb - (meds - meds.mean())[:, None, None], 0, None)


def autostretch(rgb, target_bg=0.10, shadow_sigma=2.8):
    """Linked-channel autostretch: same shadows clip and midtone for all channels."""
    lum = rgb.mean(axis=0)
    med = np.median(lum)
    mad = 1.4826 * np.median(np.abs(lum - med))
    c0 = max(0.0, med - shadow_sigma * mad)
    m = mtf(med - c0, target_bg)  # midtone so the background lands at target_bg
    out = np.clip((rgb - c0) / (1 - c0), 0, 1)
    return mtf(out, m)


def signal_weight(rgb, protect_sigma=3.0):
    """(luminance, w) with w = 0 on sky, 1 on signal: how far the pixel sits above the sky median in noise units."""
    lum = rgb.mean(axis=0)
    med = np.median(lum); mad = 1.4826 * np.median(np.abs(lum - med))
    return lum, np.clip((lum - med) / (protect_sigma * mad + 1e-9), 0, 1)


def scnr(rgb, amount=0.8, protect_sigma=3.0):
    """Average-neutral SCNR restricted to real signal.  On pure background noise, plain SCNR
    clips green wherever noise makes it the top channel and leaves a magenta cast; here the
    correction is weighted by how far the pixel sits above the sky (in noise units)."""
    r, g, b = rgb
    _, w = signal_weight(rgb, protect_sigma)
    m = 0.5 * (r + b)
    g2 = np.where(g > m, g - amount * w * (g - m), g)
    return np.stack([r, g2, b])


def asinh_color(rgb, beta=3.0):
    """Arcsinh stretch applied on luminance with colour ratios preserved (colour boost)."""
    if beta <= 0:
        return rgb
    lum = rgb.mean(axis=0) + 1e-6
    f = np.arcsinh(beta * lum) / np.arcsinh(beta) / lum
    return np.clip(rgb * f, 0, 1)


def blur_background_chroma(rgb, sigma=12.0, protect_sigma=3.0):
    """Sky chroma low-pass.  After photometric colour calibration the blue channel carries ~2-3x the noise of green,
    which per-pixel desaturation only dims into 10-30 px colour blotches.  Here the chroma (rgb - lum) of sky pixels
    is replaced by a gaussian-blurred chroma (sigma px), blended by (1-w); luminance is untouched.  The blur is a
    normalised convolution over sky pixels only, so galaxy/star colour does not bleed into the surrounding sky."""
    if sigma <= 0:
        return rgb
    from scipy.ndimage import gaussian_filter
    lum, w = signal_weight(rgb, protect_sigma)
    sky = 1 - w
    norm = gaussian_filter(sky, sigma) + 1e-6
    out = np.empty_like(rgb)
    for i in range(3):
        chroma = rgb[i] - lum
        cb = gaussian_filter(chroma * sky, sigma) / norm
        out[i] = lum + w * chroma + sky * cb
    return np.clip(out, 0, 1)


def desaturate_background(rgb, amount=0.4, protect_sigma=3.0):
    """Pull sky-background pixels toward grey.  After photometric colour calibration the blue channel
    carries ~2x the noise of red/green (the camera's weak blue response gets scaled up), which reads
    as a coloured cast in a stretched sky.  Signal (stars, galaxy) is protected by the same
    noise-unit weight used for SCNR, so object colour is untouched."""
    if amount <= 0:
        return rgb
    lum, w = signal_weight(rgb, protect_sigma)      # 0 = sky, 1 = signal
    k = amount * (1 - w)
    return np.clip(lum + (rgb - lum) * (1 - k), 0, 1)


def smooth_background(rgb, sigma=1.0, protect_sigma=3.0):
    """Masked luminance noise reduction: blur only where the pixel is sky (weight 0), leave stars and
    galaxy (weight 1) untouched.  Equivalent to Siril's masked noise reduction / a starless-background blur."""
    if sigma <= 0:
        return rgb
    from scipy.ndimage import gaussian_filter
    _, w = signal_weight(rgb, protect_sigma)
    out = np.empty_like(rgb)
    for i in range(3):
        out[i] = w * rgb[i] + (1 - w) * gaussian_filter(rgb[i], sigma)
    return out


def saturate(rgb, s=1.25):
    lum = rgb.mean(axis=0)
    return np.clip(lum + s * (rgb - lum), 0, 1)


def highlight_knee(rgb, k=0.7):
    """Soft highlight shoulder: above k, y = k + (1-k) tanh((x-k)/(1-k)); continuous in value and slope at k,
    asymptote 1 so nothing clips (x = 1 -> k + (1-k) tanh 1 = 0.93 for k = 0.7).  k <= 0 disables."""
    if k <= 0 or k >= 1:
        return rgb
    return np.where(rgb > k, k + (1 - k) * np.tanh((rgb - k) / (1 - k)), rgb)


def to_img(rgb):
    return np.moveaxis(np.clip(rgb, 0, 1), 0, -1)


def parse_crop(s):
    return "auto" if s.strip().lower() == "auto" else float(s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp", help="linear RGB FITS; with --stars this is the STARLESS image")
    ap.add_argument("--stars", help="stars-only FITS from destar.py; stretched separately and screen-blended back")
    ap.add_argument("--star-reduce", type=float, default=0.7, help="scale applied to the stretched star layer (1 = stars as bright as a plain stretch)")
    ap.add_argument("--out", required=True, help="output prefix (no extension)")
    ap.add_argument("--crop", type=parse_crop, default="auto", help="'auto' (default): trim edges whose block noise exceeds --crop-noise-ratio x central, 1..15 %% per edge; a number = fixed fraction per edge")
    ap.add_argument("--crop-noise-ratio", type=float, default=1.35, help="auto-crop threshold: edge block noise / central noise (default 1.35)")
    ap.add_argument("--target-bg", type=float, default=0.10, help="sky background level after the stretch (default 0.10)")
    ap.add_argument("--shadow-sigma", type=float, default=2.8, help="shadows clip point: median - N x MAD (default 2.8)")
    ap.add_argument("--asinh", type=float, default=3.0, help="arcsinh colour-boost strength, 0 = off (default 3)")
    ap.add_argument("--saturation", type=float, default=1.15, help="chroma gain (default 1.15)")
    ap.add_argument("--scnr", type=float, default=0.8, help="signal-weighted SCNR amount (default 0.8)")
    ap.add_argument("--no-scnr", action="store_true")
    ap.add_argument("--no-neutralize", action="store_true", help="skip per-channel background neutralisation")
    ap.add_argument("--bg-chroma-blur", type=float, default=12.0, help="sigma (px) of the sky-only chroma low-pass; 0 = off (default 12)")
    ap.add_argument("--bg-desat", type=float, default=0.4, help="desaturate sky-background colour noise (0 = off, 1 = fully grey sky; default 0.4)")
    ap.add_argument("--bg-smooth", type=float, default=1.0, help="sigma (px) of a sky-only luminance blur; 0 = off, 0.8-1.5 is gentle")
    ap.add_argument("--knee", type=float, default=0.7, help="highlight shoulder start (tanh soft clip above this level, max output 0.93 at 0.7); 0 = off")
    ap.add_argument("--palette", choices=["rgb", "hoo"], default="rgb", help="'hoo': dual-band Ha/OIII palette (R = Ha, G/B = OIII); untested on real LP data")
    ap.add_argument("--hoo-green-mix", type=float, default=0.2, help="HOO: G = mix*Ha + (1-mix)*OIII (default 0.2)")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270], help="rotate output (Seestar frames are portrait)")
    a = ap.parse_args()

    rgb = load_rgb(a.inp)
    stars = load_rgb(a.stars) if a.stars else None
    _, H, W = rgb.shape
    if a.crop == "auto":
        top, bottom, left, right, central, _ = auto_crop_amounts(rgb.mean(axis=0), a.crop_noise_ratio)
        print(f"auto-crop (block noise > {a.crop_noise_ratio} x central {central:.3g}): "
              f"top {top} px ({top / H:.1%}), bottom {bottom} px ({bottom / H:.1%}), "
              f"left {left} px ({left / W:.1%}), right {right} px ({right / W:.1%})")
    else:
        top = bottom = int(H * a.crop); left = right = int(W * a.crop)
        print(f"fixed crop {a.crop:.1%}: top/bottom {top} px, left/right {left} px")
    rgb = rgb[:, top:H - bottom, left:W - right]
    if stars is not None:
        stars = stars[:, top:H - bottom, left:W - right]
    if a.rotate:
        rgb = np.rot90(rgb, k=a.rotate // 90, axes=(1, 2))
        if stars is not None:
            stars = np.rot90(stars, k=a.rotate // 90, axes=(1, 2))
    use_scnr = not a.no_scnr
    if a.palette == "hoo":
        rgb, fit = compose_hoo(rgb, a.hoo_green_mix)
        print(f"HOO palette: OIII = mean(G,B) matched to Ha as OIII ~ {fit[0]:.4f} Ha + {fit[1]:.3g}; G = {a.hoo_green_mix} Ha + {1 - a.hoo_green_mix:.2f} OIII; SCNR off")
        if stars is not None:
            stars, _ = compose_hoo(stars, a.hoo_green_mix, fit=fit, offset=False)
        use_scnr = False
    lin = neutralize_background(rgb) if not a.no_neutralize else rgb.copy()

    def finish(target_bg, asinh, sat):
        # --- starless (or whole image): the full chain
        x = autostretch(lin, target_bg, a.shadow_sigma)
        if not a.no_neutralize:
            x = neutralize_background(x)
        if use_scnr:
            x = scnr(x, a.scnr)
        x = asinh_color(x, asinh)
        x = saturate(x, sat)
        x = blur_background_chroma(x, a.bg_chroma_blur)
        x = desaturate_background(x, a.bg_desat)
        x = smooth_background(x, a.bg_smooth)
        x = highlight_knee(x, a.knee)      # before the screen: galaxy core soft-limited, star cores may still reach white
        if stars is None:
            return x
        # --- stars: same midtone as the starless layer so they land where a plain stretch would put
        # them, then scaled down by --star-reduce, and screen-blended: 1 - (1-a)(1-b)
        lum = lin.mean(axis=0); med = np.median(lum); mad = 1.4826 * np.median(np.abs(lum - med))
        c0 = max(0.0, med - a.shadow_sigma * mad); m = mtf(med - c0, target_bg)
        s = mtf(np.clip(stars / (1 - c0), 0, 1), m) * a.star_reduce
        s = saturate(s, sat)
        return np.clip(1 - (1 - x) * (1 - s), 0, 1)

    final = finish(a.target_bg, a.asinh, a.saturation)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    if stars is not None:   # also export the starless rendering on its own
        _tmp = stars; stars = None
        sl = finish(a.target_bg, a.asinh, a.saturation); stars = _tmp
        Image.fromarray((to_img(sl) * 255).round().astype(np.uint8)).save(f"{out}_starless.jpg", quality=93, subsampling=0)
    img8 = (to_img(final) * 255).round().astype(np.uint8)
    img16 = (to_img(final) * 65535).round().astype(np.uint16)
    Image.fromarray(img8).save(f"{out}.png")
    Image.fromarray(img8).save(f"{out}.jpg", quality=93, subsampling=0)
    try:
        import tifffile
        tifffile.imwrite(f"{out}.tif", img16, photometric="rgb", compression="zlib")
    except ImportError:
        Image.fromarray(img8).save(f"{out}.tif")
    # preview strip: gentle / default / strong
    variants = [(a.target_bg * 0.7, max(a.asinh - 1.5, 0), 1.1), (a.target_bg, a.asinh, a.saturation), (a.target_bg * 1.35, a.asinh + 2, a.saturation + 0.15)]
    thumbs = []
    for tb, ah, s in variants:
        t = (to_img(finish(tb, ah, s)) * 255).astype(np.uint8)
        im = Image.fromarray(t); im.thumbnail((700, 700)); thumbs.append(im)
    wsum = sum(t.width for t in thumbs) + 10 * (len(thumbs) - 1)
    strip = Image.new("RGB", (wsum, max(t.height for t in thumbs)), (20, 20, 20))
    x = 0
    for t in thumbs:
        strip.paste(t, (x, 0)); x += t.width + 10
    strip.save(f"{out}_preview_strip.jpg", quality=88)
    print(f"wrote {out}.tif/.png/.jpg and {out}_preview_strip.jpg  (gentle | default | strong)")


if __name__ == "__main__":
    main()
