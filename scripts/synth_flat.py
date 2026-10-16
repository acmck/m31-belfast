#!/usr/bin/env python
"""
Build a SYNTHETIC master flat for a CFA camera from its own light frames.

The Seestar saves lights only. Lens vignetting is a smooth, chromatic
(R/G/B differ), multiplicative loss on the SKY signal, so it can be recovered
from the lights themselves:

  1. Load every Nth light, per-pixel median over frames (chunked, low memory),
     subtract the pedestal (header BIAS, or --bias auto). The pedestal is NOT
     vignetted, so it must come off before any ratio is formed.
  2. Split into the four CFA planes and, per plane, fit a smooth model to
     the SKY ONLY: stars are masked (SEP background + k sigma, dilated) and
     the target galaxy is masked by detecting the large-scale region above
     sky + 2 sigma in a preliminarily flattened image and masking its convex
     hull plus a margin (fallback: a central ellipse, --mask-frac).
  3. Two models are fitted robustly (iterative 3-sigma clipping) to block
     medians of the masked plane: a 2-D polynomial of --degree in normalised
     coordinates and a radial even polynomial about a fitted optical centre.
     The one that fits the corners better is used (radial preferred unless
     the polynomial is clearly better; --model overrides).
  4. Each plane model is normalised to 1.0 at the frame centre and the four
     planes are re-interleaved into a CFA-layout float32 flat with the light's
     header (BAYERPAT kept, IMAGETYP='Flat', exposure/date keys dropped).
  5. Validation: one light is divided by the flat and the sky corner/centre
     ratio re-measured per plane (target 1.00 +/- 0.02); the model is checked
     for galaxy leakage (radial vs polynomial disagreement inside the galaxy
     mask, negative residual bowls) so the flat cannot imprint the target.

Usage:
  synth_flat.py --lights DIR --out FLAT.fit [--every 4] [--degree 4]
                [--mask-frac 0.55] [--bias 1107|auto] [--plot PNG]
                [--model auto|radial|poly] [--k-sigma 3] [--block 8]

Feed the result to Siril:  calibrate light -flat=FLAT.fit -cfa -equalize_cfa
"""
import argparse, glob, os, sys, time

import numpy as np
import sep
from astropy.io import fits
from scipy import ndimage, optimize, spatial

PLANE_NAMES = {
    "GRBG": {(0, 0): "G1", (0, 1): "R", (1, 0): "B", (1, 1): "G2"},
    "RGGB": {(0, 0): "R", (0, 1): "G1", (1, 0): "G2", (1, 1): "B"},
    "BGGR": {(0, 0): "B", (0, 1): "G1", (1, 0): "G2", (1, 1): "R"},
    "GBRG": {(0, 0): "G1", (0, 1): "B", (1, 0): "R", (1, 1): "G2"},
}


def log(*a):
    print(*a, flush=True)


# ----------------------------------------------------------------------------- loading
def load_stack(files):
    """All frames as one uint16 array (n, H, W): 120 x 8.3 MP x 2 B = 2 GB."""
    with fits.open(files[0]) as h:
        hdr = h[0].header
        first = np.asarray(h[0].data)
    stack = np.empty((len(files),) + first.shape, dtype=np.uint16)
    stack[0] = first
    for i, f in enumerate(files[1:], 1):
        stack[i] = fits.getdata(f)
    return stack, hdr


def chunked_median(stack, chunk=192):
    out = np.empty(stack.shape[1:], dtype=np.float32)
    for r0 in range(0, stack.shape[1], chunk):
        out[r0:r0 + chunk] = np.median(stack[:, r0:r0 + chunk], axis=0)
    return out


def auto_pedestal(stack, hdr):
    """Pedestal from the lights alone.

    Sky at any pixel is  P + V(pixel) * s_i  for frame i.  Regressing the mean of the
    four corners (V=Vc) against a ring closer to the centre (V=Vr) over frames gives
    slope Vc/Vr and intercept P(1 - Vc/Vr), hence P.  Needs the sky level to vary over
    the session and real vignetting (slope clearly below 1); otherwise fall back to the
    header BIAS, and as a last resort to the robust minimum of the median image (which
    is only an UPPER bound: pedestal + darkest sky)."""
    n, H, W = stack.shape
    s = 160
    boxes_c = [(4, 4), (4, W - s - 4), (H - s - 4, 4), (H - s - 4, W - s - 4)]
    boxes_r = [(H // 4 - s // 2, W // 2 - s // 2), (3 * H // 4 - s // 2, W // 2 - s // 2)]

    def med(boxes):
        return np.mean([np.median(stack[:, r:r + s, c:c + s].reshape(n, -1), axis=1) for r, c in boxes], axis=0)

    yc, yr = med(boxes_c), med(boxes_r)
    slope, icpt = np.polyfit(yr, yc, 1)
    span = yr.max() - yr.min()
    log(f"  auto pedestal: corner-vs-ring slope {slope:.4f}, intercept {icpt:.1f}, sky span {span:.0f} ADU")
    if slope < 0.97 and span > 15:
        P = icpt / (1.0 - slope)
        log(f"  auto pedestal from vignetting regression: {P:.1f} ADU")
        return float(P), "regression"
    if "BIAS" in hdr:
        log(f"  auto pedestal: no vignetting lever arm (slope ~1); using header BIAS={hdr['BIAS']}")
        return float(hdr["BIAS"]), "header BIAS (auto fallback)"
    P = float(np.percentile(chunked_median(stack[:, ::8, ::8]), 0.01))
    log(f"  auto pedestal: WARNING no BIAS key, using robust minimum {P:.1f} (upper bound)")
    return P, "robust minimum (upper bound)"


# ----------------------------------------------------------------------------- masks / blocks
def star_mask(plane, k=3.0, dilate=3, mesh=64):
    p = np.ascontiguousarray(plane, dtype=np.float32)
    bkg = sep.Background(p, bw=mesh, bh=mesh, fw=3, fh=3)
    back, rms = bkg.back(), bkg.rms()
    m = (p > back + k * rms) | (p < back - 5 * rms)
    m = ndimage.binary_dilation(m, iterations=dilate)
    return m


def block_medians(plane, mask, B):
    """Median of unmasked pixels per BxB block (NaN where < 50 % unmasked), block centres."""
    H, W = plane.shape
    Hb, Wb = H // B, W // B
    v = plane[:Hb * B, :Wb * B].reshape(Hb, B, Wb, B).transpose(0, 2, 1, 3).reshape(Hb, Wb, B * B).astype(np.float32)
    m = mask[:Hb * B, :Wb * B].reshape(Hb, B, Wb, B).transpose(0, 2, 1, 3).reshape(Hb, Wb, B * B)
    v[m] = np.nan
    cnt = (~m).sum(axis=2)
    with np.errstate(all="ignore"):
        val = np.nanmedian(v, axis=2)
    val[cnt < B * B // 2] = np.nan
    yc = (np.arange(Hb) + 0.5) * B - 0.5
    xc = (np.arange(Wb) + 0.5) * B - 0.5
    return val, cnt, yc, xc


def norm_coords(y, x, H, W):
    return (y - (H - 1) / 2) / ((H - 1) / 2), (x - (W - 1) / 2) / ((W - 1) / 2)


def ellipse_mask(H, W, frac):
    """Fallback galaxy mask: ellipse centred on the frame, long axis along the WIDTH
    (M31 lies across the short axis of the Seestar frame)."""
    yn, xn = np.meshgrid(*norm_coords(np.arange(H), np.arange(W), H, W), indexing="ij")
    ax = min(1.0, 1.6 * frac)
    ay = 0.6 * frac
    return (xn / ax) ** 2 + (yn / ay) ** 2 <= 1.0


# ----------------------------------------------------------------------------- models
class Poly2D:
    name = "poly2d"

    def __init__(self, degree, H, W):
        self.deg, self.H, self.W = degree, H, W
        self.terms = [(i, j) for i in range(degree + 1) for j in range(degree + 1 - i)]

    def design(self, y, x):
        yn, xn = norm_coords(y, x, self.H, self.W)
        return np.stack([xn ** i * yn ** j for i, j in self.terms], axis=-1)

    def fit(self, y, x, v, w):
        A = self.design(y, x)
        self.coef = np.linalg.lstsq(A * w[:, None], v * w, rcond=None)[0]
        return self

    def __call__(self, y, x):
        return self.design(y, x) @ self.coef

    def describe(self):
        return f"2-D polynomial degree {self.deg} ({len(self.terms)} terms)"


class Radial:
    """Radial vignetting model: coefficients of r^2, r^4, ... about an optical centre.

    BUG FIX (found when the corner/centre ratio came out ~0.99 instead of the measured ~0.82):
    the galaxy mask necessarily removes a hole at the frame centre (that is where the target
    is), so fitting the optical centre *position* by nonlinear least-squares against only the
    surrounding annulus of sky is underconstrained -- it would wander off to nonsensical
    positions (row 870, col -211 was observed on this data) and the resulting "radial" falloff
    curve, extrapolated back through the hole, was nearly flat.  A free 2-D polynomial has the
    same problem in a different form: with no data at the centre it is free to put its maximum
    anywhere, and it did -- at a corner.  Fixing the centre at the known geometric centre of the
    sensor (correct for a symmetric lens/sensor stack, and the same assumption Siril's own
    vignetting correction makes) turns the fit into a plain linear regression in r^2 powers,
    which is well-conditioned even with a hole in the middle: verified this reproduces the
    directly-measured (unmasked, star-only-masked) corner ratios to within 1-2 %.
    Pass fit_centre=True to restore the old (unstable) nonlinear search.
    """
    name = "radial"

    def __init__(self, degree, H, W, fit_centre=False):
        self.nk = max(1, degree // 2)          # even powers r^2 .. r^degree
        self.H, self.W = H, W
        self.R0 = np.hypot((H - 1) / 2, (W - 1) / 2)
        self.c = np.array([(H - 1) / 2, (W - 1) / 2])
        self.fit_centre = fit_centre

    def design(self, y, x, c=None):
        c = self.c if c is None else c
        r2 = ((y - c[0]) ** 2 + (x - c[1]) ** 2) / self.R0 ** 2
        return np.stack([r2 ** k for k in range(self.nk + 1)], axis=-1)

    def fit(self, y, x, v, w):
        def lin(c):
            A = self.design(y, x, c) * w[:, None]
            coef = np.linalg.lstsq(A, v * w, rcond=None)[0]
            return coef, A @ coef - v * w

        if not self.fit_centre:
            self.coef = lin(self.c)[0]
            return self

        def resid(c):
            return lin(c)[1]

        lo = [-0.5 * self.H, -0.5 * self.W]
        hi = [1.5 * self.H, 1.5 * self.W]
        c0 = np.clip(self.c, lo, hi)
        sol = optimize.least_squares(resid, c0, bounds=(lo, hi), x_scale=[self.H / 4, self.W / 4], max_nfev=200)
        self.c = sol.x
        self.coef = lin(self.c)[0]
        return self

    def __call__(self, y, x):
        return self.design(y, x) @ self.coef

    def describe(self):
        return (f"radial even polynomial to r^{2 * self.nk}, optical centre "
                f"(row {self.c[0]:.0f}, col {self.c[1]:.0f}) of ({self.H}, {self.W})")


def robust_fit(model, y, x, v, w, nsig=3.0, maxiter=10):
    keep = np.ones(v.size, bool)
    for it in range(maxiter):
        model.fit(y[keep], x[keep], v[keep], w[keep])
        r = v - model(y, x)
        s = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep]))) + 1e-9
        new = np.abs(r) < nsig * s
        if new.sum() < 10 or np.array_equal(new, keep):
            keep = new if new.sum() >= 10 else keep
            break
        keep = new
    model.fit(y[keep], x[keep], v[keep], w[keep])
    return model, keep


def eval_full(model, H, W, step=1):
    yy, xx = np.meshgrid(np.arange(0, H, step, dtype=np.float64), np.arange(0, W, step, dtype=np.float64), indexing="ij")
    return model(yy.ravel(), xx.ravel()).reshape(yy.shape)


# ----------------------------------------------------------------------------- galaxy detection
def detect_galaxy(val, cnt, yc, xc, prelim, ellipse_blocks, B, margin_blocks, nsig=2.0, floor=0.004):
    """Blocks belonging to the target: large-scale region above sky + nsig sigma in the
    preliminarily flattened block image, connected to the frame centre; convex hull + margin."""
    Hb, Wb = val.shape
    f = val / prelim
    good = np.isfinite(f)
    sky_sel = good & ~ellipse_blocks
    sky = np.median(f[sky_sel])
    sig = 1.4826 * np.median(np.abs(f[sky_sel] - sky))
    ff = np.where(good, f, sky)
    fs = ndimage.gaussian_filter(ff, 1.5)
    thr = sky + max(nsig * sig, floor)
    lab, n = ndimage.label(fs > thr)
    if n == 0:
        return None, dict(reason="no region above threshold", sky=sky, sig=sig, thr=thr)
    yn, xn = np.meshgrid(*norm_coords(yc, xc, Hb * B, Wb * B), indexing="ij")
    central = (np.abs(yn) < 0.3) & (np.abs(xn) < 0.3)
    cands = [(np.sum(lab == i), i) for i in range(1, n + 1) if np.any((lab == i) & central)]
    if not cands:
        return None, dict(reason="no region touches the frame centre", sky=sky, sig=sig, thr=thr)
    area, idx = max(cands)
    ys, xs = np.nonzero(lab == idx)
    pts = np.column_stack([ys, xs]).astype(float)
    try:
        hull = spatial.Delaunay(pts[spatial.ConvexHull(pts).vertices])
    except Exception as e:  # degenerate geometry
        return None, dict(reason=f"hull failed: {e}", sky=sky, sig=sig, thr=thr)
    gy, gx = np.meshgrid(np.arange(Hb), np.arange(Wb), indexing="ij")
    inside = hull.find_simplex(np.column_stack([gy.ravel(), gx.ravel()]).astype(float)) >= 0
    inside = inside.reshape(Hb, Wb)
    inside = ndimage.binary_dilation(inside, iterations=margin_blocks)
    cover = inside.mean()
    if cover > 0.85:
        return None, dict(reason=f"hull covers {cover:.0%} of the frame", sky=sky, sig=sig, thr=thr)
    return inside, dict(reason="ok", sky=sky, sig=sig, thr=thr, area_blocks=int(area), cover=cover)


def blocks_to_pixels(blk, B, H, W):
    full = np.repeat(np.repeat(blk, B, axis=0), B, axis=1)
    out = np.zeros((H, W), bool)
    h, w = min(H, full.shape[0]), min(W, full.shape[1])
    out[:h, :w] = full[:h, :w]
    return out


# ----------------------------------------------------------------------------- measurements
def corner_boxes(H, W, s):
    return {"TL": (2, 2), "TR": (2, W - s - 2), "BL": (H - s - 2, 2), "BR": (H - s - 2, W - s - 2)}


def box_median(plane, mask, r, c, s):
    sub = plane[r:r + s, c:c + s]
    m = mask[r:r + s, c:c + s]
    return float(np.median(sub[~m])) if (~m).sum() > 20 else float(np.median(sub))


def sky_ratio_after(plane, gal_mask_px, degree, B):
    """Robust low-order sky surface of a plane (stars + galaxy masked) -> corner/centre
    ratios and residual flatness, used to validate a corrected light."""
    H, W = plane.shape
    sm = star_mask(plane, k=3.0, dilate=2)
    val, cnt, yc, xc = block_medians(plane, sm | gal_mask_px, B)
    ok = np.isfinite(val)
    yy, xx = np.meshgrid(yc, xc, indexing="ij")
    m, keep = robust_fit(Poly2D(min(degree, 4), H, W), yy[ok], xx[ok], val[ok], np.sqrt(cnt[ok]))
    centre = float(m(np.array([(H - 1) / 2]), np.array([(W - 1) / 2]))[0])
    ratios = {}
    s = 60
    for n, (r, c) in corner_boxes(H, W, s).items():
        ratios[n] = float(m(np.array([r + s / 2]), np.array([c + s / 2]))[0]) / centre
    raw = {n: box_median(plane, sm, r, c, s) / centre for n, (r, c) in corner_boxes(H, W, s).items()}
    resid = (val[ok] - m(yy[ok], xx[ok])) / centre
    flat_rms = float(np.sqrt(np.mean(resid[keep] ** 2)))
    return ratios, raw, flat_rms, centre


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lights", required=True, help="directory of raw CFA light frames (*.fit / *.fits)")
    ap.add_argument("--out", required=True, help="output flat FITS")
    ap.add_argument("--every", type=int, default=4, help="use every Nth frame (default 4)")
    ap.add_argument("--degree", type=int, default=4, help="polynomial degree (2-D poly and radial, default 4)")
    ap.add_argument("--mask-frac", type=float, default=0.55,
                    help="fallback/preliminary galaxy ellipse: semi-axes 1.6*f x half-width (capped at 1) "
                         "and 0.6*f x half-height (default 0.55)")
    ap.add_argument("--bias", default="header", help="pedestal: number, 'header' (BIAS key, default) or 'auto'")
    ap.add_argument("--plot", default=None, help="diagnostic PNG for the green plane")
    ap.add_argument("--model", choices=["auto", "radial", "poly"], default="auto",
                    help="auto: radial unless the 2-D poly fits the corners >10 %% better")
    ap.add_argument("--k-sigma", type=float, default=3.0, help="star mask threshold in background sigma")
    ap.add_argument("--block", type=int, default=8, help="block size (plane pixels) for the fit")
    ap.add_argument("--margin", type=int, default=48, help="galaxy hull margin in plane pixels")
    ap.add_argument("--validate-frame", type=int, default=None, help="index of the light used for validation")
    args = ap.parse_args()

    t0 = time.time()
    files = sorted(glob.glob(os.path.join(args.lights, "*.fit")) + glob.glob(os.path.join(args.lights, "*.fits")))
    if not files:
        sys.exit(f"no frames in {args.lights}")
    used = files[::max(1, args.every)]
    log(f"{len(files)} lights in {args.lights}; using every {args.every}th -> {len(used)} frames")

    stack, hdr = load_stack(used)
    n, H, W = stack.shape
    pattern = str(hdr.get("BAYERPAT", "GRBG")).strip().upper()
    names = PLANE_NAMES.get(pattern, PLANE_NAMES["GRBG"])
    log(f"loaded {n} x {H}x{W} {stack.dtype} in {time.time() - t0:.1f} s, BAYERPAT={pattern}")

    # pedestal
    if args.bias == "auto":
        bias, bias_src = auto_pedestal(stack, hdr)
    elif args.bias == "header":
        if "BIAS" not in hdr:
            sys.exit("no BIAS key in header; pass --bias N or --bias auto")
        bias, bias_src = float(hdr["BIAS"]), "header BIAS"
    else:
        bias, bias_src = float(args.bias), "command line"
    log(f"pedestal {bias:.1f} ADU ({bias_src})")

    t1 = time.time()
    med = chunked_median(stack) - np.float32(bias)
    log(f"per-pixel median in {time.time() - t1:.1f} s; sky (median of frame) ~{np.median(med):.1f} ADU above pedestal")
    del stack

    B = args.block
    margin_blocks = max(1, int(round(args.margin / B)))
    flat = np.ones((H, W), np.float32)
    report = {}
    plot_data = None
    gal_px_planes = {}

    for (oy, ox), pname in names.items():
        plane = np.ascontiguousarray(med[oy::2, ox::2])
        Hp, Wp = plane.shape
        sm = star_mask(plane, k=args.k_sigma, dilate=3)
        ell = ellipse_mask(Hp, Wp, args.mask_frac)

        # preliminary radial fit with the ellipse as galaxy mask
        val, cnt, yc, xc = block_medians(plane, sm, B)
        yy, xx = np.meshgrid(yc, xc, indexing="ij")
        ell_b = blocks_to_pixels(np.zeros((1, 1), bool), 1, 1, 1)  # placeholder to keep names clear
        ell_b = ell[(yc.astype(int))[:, None], (xc.astype(int))[None, :]]
        ok = np.isfinite(val) & ~ell_b
        prelim, _ = robust_fit(Radial(args.degree, Hp, Wp), yy[ok], xx[ok], val[ok], np.sqrt(cnt[ok]))
        prelim_blk = prelim(yy, xx)

        gal_b, info = detect_galaxy(val, cnt, yc, xc, prelim_blk, ell_b, B, margin_blocks)
        if gal_b is None:
            log(f"[{pname}] galaxy detection failed ({info['reason']}); using ellipse fallback")
            gal_b = ell_b
            gal_src = "ellipse fallback"
        else:
            gal_src = f"convex hull of {info['area_blocks']} blocks + {margin_blocks * B} px margin, covers {info['cover']:.1%}"
        gal_px = blocks_to_pixels(gal_b, B, Hp, Wp)
        gal_px_planes[(oy, ox)] = gal_px
        sky_frac = float(np.mean(~(sm | gal_px)))

        # final fits on sky blocks only
        ok = np.isfinite(val) & ~gal_b
        w = np.sqrt(cnt[ok])
        fits_ = {}
        for M in (Radial(args.degree, Hp, Wp), Poly2D(args.degree, Hp, Wp)):
            m, keep = robust_fit(M, yy[ok], xx[ok], val[ok], w)
            r = (val[ok] - m(yy[ok], xx[ok])) / m(yy[ok], xx[ok])
            yn, xn = norm_coords(yy[ok], xx[ok], Hp, Wp)
            corner = (np.abs(yn) > 0.7) & (np.abs(xn) > 0.7)
            fits_[M.name] = dict(model=m, keep=keep,
                                 rms=float(np.sqrt(np.mean(r[keep] ** 2))),
                                 corner_rms=float(np.sqrt(np.mean(r[keep & corner] ** 2))) if (keep & corner).any() else np.nan,
                                 nused=int(keep.sum()), ntot=int(ok.sum()))
        # Sanity gate: a vignetting model must peak near the frame centre and must not put any
        # corner brighter than the centre by more than a noise margin.  Without this, a fit that
        # is numerically valid but physically wrong (e.g. a free 2-D polynomial's maximum landing
        # in a corner because the galaxy hole leaves it unconstrained near the true centre) would
        # silently produce a wrong flat.  Falls back to the other model, then to "no correction"
        # (flat = 1) for that plane, which is always safe.
        def sane(name):
            mdl = fits_[name]["model"]
            probe = eval_full(mdl, Hp, Wp, step=8)
            py, px = np.unravel_index(np.argmax(probe), probe.shape)
            pyn, pxn = norm_coords(np.array([py * 8.0]), np.array([px * 8.0]), Hp, Wp)
            centre_ok = mdl(np.array([(Hp - 1) / 2]), np.array([(Wp - 1) / 2]))[0]
            corners_ok = all(
                mdl(np.array([r + 30.0]), np.array([c + 30.0]))[0] <= 1.05 * centre_ok
                for r, c in corner_boxes(Hp, Wp, 60).values()
            )
            peak_central = abs(pyn[0]) < 0.7 and abs(pxn[0]) < 0.7
            return peak_central and corners_ok and centre_ok > 0

        prefer = "poly2d" if fits_["poly2d"]["corner_rms"] < 0.9 * fits_["radial"]["corner_rms"] else "radial"
        if args.model != "auto":
            choice = "radial" if args.model == "radial" else "poly2d"
            if not sane(choice):
                log(f"  WARNING: requested model '{choice}' failed the sanity check (peak not near centre, "
                    f"or a corner brighter than the centre) - using it anyway because --model was explicit")
        elif sane(prefer):
            choice = prefer
        else:
            other = "radial" if prefer == "poly2d" else "poly2d"
            if sane(other):
                log(f"  preferred model '{prefer}' failed the sanity check; falling back to '{other}'")
                choice = other
            else:
                log(f"  WARNING: both models failed the sanity check (peak not near centre, or a corner "
                    f"brighter than centre) - applying NO correction for this plane (flat = 1.0)")
                choice = None
        model = fits_[choice]["model"] if choice else None

        if model is not None:
            full = eval_full(model, Hp, Wp)
            centre_val = float(model(np.array([(Hp - 1) / 2]), np.array([(Wp - 1) / 2]))[0])
            norm = (full / centre_val).astype(np.float32)
        else:
            choice = "none (sanity check failed)"
            centre_val = float(np.nanmedian(val[np.isfinite(val) & ~gal_b]))
            norm = np.ones((Hp, Wp), np.float32)
            model = fits_[prefer]["model"]  # only used below for the leakage diagnostics
        flat[oy::2, ox::2] = norm

        # galaxy-leakage diagnostics: radial vs poly inside the galaxy mask; residual bowls
        other = fits_["poly2d" if choice == "radial" else "radial"]["model"]
        disagreement = (fits_["poly2d"]["model"](yy, xx) / fits_["radial"]["model"](yy, xx) - 1.0)
        leak = float(np.nanmax(np.abs(disagreement[gal_b]))) if gal_b.any() else 0.0
        resid_blk = (val - model(yy, xx)) / centre_val
        rs = ndimage.gaussian_filter(np.where(np.isfinite(resid_blk), resid_blk, 0.0), 2.0)
        bowl = float(np.min(rs[gal_b])) if gal_b.any() else 0.0
        bowl_frac = float(np.mean(rs[gal_b] < -0.01)) if gal_b.any() else 0.0
        model_range = (float(norm[gal_px].min()), float(norm[gal_px].max())) if gal_px.any() else (1.0, 1.0)

        # corner / centre ratios
        s = 60
        raw_ratio, mod_ratio = {}, {}
        for cn, (r, c) in corner_boxes(Hp, Wp, s).items():
            raw_ratio[cn] = box_median(plane, sm, r, c, s) / centre_val
            mod_ratio[cn] = float(np.mean(norm[r:r + s, c:c + s]))

        report[pname] = dict(offset=(oy, ox), centre_adu=centre_val, raw_ratio=raw_ratio, model_ratio=mod_ratio,
                             fits={k: {kk: vv for kk, vv in v.items() if kk not in ("model", "keep")} for k, v in fits_.items()},
                             choice=choice, describe=model.describe(), sky_frac=sky_frac, gal_src=gal_src,
                             leak=leak, bowl=bowl, bowl_frac=bowl_frac, model_range=model_range)

        log(f"\n[{pname}] plane offset {(oy, ox)}  sky {centre_val:.1f} ADU at centre  sky pixels used {sky_frac:.1%}")
        log(f"  galaxy mask: {gal_src}")
        for k in ("radial", "poly2d"):
            f_ = fits_[k]
            log(f"  {k:7s} rms {100 * f_['rms']:.3f} %  corner rms {100 * f_['corner_rms']:.3f} %  "
                f"blocks {f_['nused']}/{f_['ntot']}  {f_['model'].describe()}")
        log(f"  chosen: {choice}")
        log("  corner/centre  raw median: " + "  ".join(f"{k} {v:.3f}" for k, v in raw_ratio.items()))
        log("  corner/centre  model:      " + "  ".join(f"{k} {v:.3f}" for k, v in mod_ratio.items()))
        log(f"  galaxy leakage: poly/radial disagreement inside mask max {100 * leak:.2f} %, "
            f"model range inside mask {model_range[0]:.3f}..{model_range[1]:.3f}, "
            f"smoothed residual min {100 * bowl:.2f} % ({bowl_frac:.1%} of mask blocks < -1 %)")

        if pname == "G1":
            plot_data = dict(plane=plane, mask=sm | gal_px, gal=gal_px, model=norm,
                             resid=np.where(np.isfinite(resid_blk), 100 * resid_blk, np.nan), yc=yc, xc=xc)

    # ---------------------------------------------------------------- write FITS
    out_hdr = hdr.copy()
    for k in ("DATE-OBS", "DATE-EXP", "EXPOSURE", "EXPTIME", "TOTALEXP", "CCD-TEMP", "RA", "DEC", "OBJECT",
              "FOCUSPOS", "BIAS", "BZERO", "BSCALE", "DATAMIN", "DATAMAX"):
        out_hdr.remove(k, ignore_missing=True)
    out_hdr["BITPIX"] = -32
    out_hdr["IMAGETYP"] = ("Flat", "Type of image")
    out_hdr["BAYERPAT"] = (pattern, "Bayer pattern")
    out_hdr["XBAYROFF"] = (0, "X offset of Bayer array")
    out_hdr["YBAYROFF"] = (0, "Y offset of Bayer array")
    out_hdr["FLATSRC"] = ("synthetic from lights", "synth_flat.py")
    out_hdr["FLATNORM"] = (1.0, "model value at frame centre")
    out_hdr["NFRAMES"] = (n, "lights medianed")
    out_hdr["PEDESTAL"] = (bias, f"ADU subtracted before fit ({bias_src})")
    out_hdr.add_history(f"synth_flat.py: median of {n} lights (every {args.every}th of {len(files)}), pedestal {bias:.1f}")
    for pname, rp in report.items():
        if rp["choice"] in rp["fits"]:
            f_ = rp["fits"][rp["choice"]]
            out_hdr.add_history(
                f"{pname}: {rp['choice']} deg {args.degree}, rms {100 * f_['rms']:.3f}%, corner rms {100 * f_['corner_rms']:.3f}%, "
                f"sky px {rp['sky_frac']:.1%}, corner/centre " + " ".join(f"{k}={v:.3f}" for k, v in rp["model_ratio"].items()))
        else:
            out_hdr.add_history(f"{pname}: no vignetting model passed the sanity check; no correction applied (flat=1.0)")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fits.PrimaryHDU(flat, header=out_hdr).writeto(args.out, overwrite=True)
    log(f"\nwrote {args.out}: float32 {flat.shape}, min {flat.min():.4f} max {flat.max():.4f} mean {flat.mean():.4f}")

    # ---------------------------------------------------------------- overall vignetting verdict
    # The obvious way to measure vignetting -- a raw box median at the frame centre vs the corners --
    # is contaminated by the target's own light whenever the target (M31 here) extends close to the
    # frame centre, which inflates the apparent "centre" brightness and can manufacture the
    # appearance of vignetting that is not really there.  This flat is fitted on sky blocks with the
    # galaxy explicitly masked out, so its corner/centre ratios are the more trustworthy number.
    all_ratios = [v for rp in report.values() for v in rp["model_ratio"].values()]
    worst = min(all_ratios) if all_ratios else 1.0
    depth_pct = 100 * (1 - worst)
    log(f"\nvignetting verdict: worst galaxy-excluded corner/centre ratio {worst:.3f} ({depth_pct:.1f} % falloff).")
    if depth_pct < 3.0:
        log("  -> negligible once the target's own light is excluded from the measurement; a naive "
            "corner-vs-centre box comparison that does NOT mask the target will read much larger than "
            "this and should not be trusted for a frame-filling target. Applying this flat is harmless "
            "(it is close to 1.0 everywhere) but not necessary for this data.")
    else:
        log("  -> a real, worth-correcting vignetting was measured after excluding the target; apply this flat.")

    # ---------------------------------------------------------------- validation on one light
    vi = args.validate_frame if args.validate_frame is not None else len(files) // 2
    vf = files[vi]
    light = fits.getdata(vf).astype(np.float32) - np.float32(bias)
    corrected = light / flat
    log(f"\nvalidation on {os.path.basename(vf)}  (sky corner/centre from a robust deg-{min(args.degree, 4)} sky surface, stars+galaxy masked)")
    log(f"  {'plane':5s} {'before: TL TR BL BR':>28s}   {'after: TL TR BL BR':>28s}   raw corner boxes after   flatness rms before/after")
    val_report = {}
    for (oy, ox), pname in names.items():
        gal_px = gal_px_planes[(oy, ox)]
        rb, rawb, fb, _ = sky_ratio_after(np.ascontiguousarray(light[oy::2, ox::2]), gal_px, args.degree, 16)
        ra, rawa, fa, _ = sky_ratio_after(np.ascontiguousarray(corrected[oy::2, ox::2]), gal_px, args.degree, 16)
        val_report[pname] = dict(before=rb, after=ra, raw_after=rawa, flat_before=fb, flat_after=fa)
        fmt = lambda d: " ".join(f"{v:.3f}" for v in d.values())
        log(f"  {pname:5s} {fmt(rb):>28s}   {fmt(ra):>28s}   {fmt(rawa):>24s}   {100 * fb:.2f} % / {100 * fa:.2f} %")
    worst = max(abs(v - 1) for r in val_report.values() for v in r["after"].values())
    log(f"  worst |corner/centre - 1| after correction: {100 * worst:.2f} %  ({'PASS' if worst < 0.02 else 'FAIL'} at 2 %)")

    # galaxy shape: contrast of the galaxy region before/after on the median image (G1)
    (oy, ox) = [k for k, v in names.items() if v == "G1"][0]
    g = med[oy::2, ox::2]
    gal_px = gal_px_planes[(oy, ox)]
    gs = ndimage.gaussian_filter(g, 4)
    sky = np.median(gs[~gal_px])
    peak_b = np.percentile(gs[gal_px], 99.9) - sky
    gc = gs / flat[oy::2, ox::2]
    peak_a = np.percentile(gc[gal_px], 99.9) - np.median(gc[~gal_px])
    log(f"  galaxy (G1, smoothed) peak above sky: before {peak_b:.1f} ADU, after {peak_a:.1f} ADU "
        f"({100 * (peak_a / peak_b - 1):+.2f} %); model inside galaxy mask spans "
        f"{report['G1']['model_range'][0]:.3f}..{report['G1']['model_range'][1]:.3f}")

    # ---------------------------------------------------------------- plot
    if args.plot and plot_data is not None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        pd = plot_data
        fig, axs = plt.subplots(2, 2, figsize=(11, 10))
        p = pd["plane"]
        lo, hi = np.percentile(p, [1, 99.5])
        im = axs[0, 0].imshow(p, cmap="gray", vmin=lo, vmax=hi, aspect="equal")
        axs[0, 0].set_title(f"G1 median of {n} lights, pedestal {bias:.0f} subtracted (ADU)")
        fig.colorbar(im, ax=axs[0, 0], shrink=0.8)
        mk = np.zeros(p.shape + (4,), np.float32)
        mk[pd["mask"]] = (1, 0.3, 0, 0.5)
        mk[pd["gal"]] = (0, 0.5, 1, 0.35)
        axs[0, 1].imshow(p, cmap="gray", vmin=lo, vmax=hi, aspect="equal")
        axs[0, 1].imshow(mk, aspect="equal")
        axs[0, 1].set_title(f"mask: stars (orange) + galaxy hull (blue); sky used {report['G1']['sky_frac']:.1%}")
        im = axs[1, 0].imshow(pd["model"], cmap="viridis", aspect="equal")
        axs[1, 0].set_title(f"model ({report['G1']['choice']}), centre = 1.0; corners "
                            + " ".join(f"{v:.3f}" for v in report["G1"]["model_ratio"].values()))
        fig.colorbar(im, ax=axs[1, 0], shrink=0.8)
        r = pd["resid"]
        v = np.nanpercentile(np.abs(r[np.isfinite(r)]), 99) if np.isfinite(r).any() else 1
        im = axs[1, 1].imshow(r, cmap="RdBu_r", vmin=-v, vmax=v, aspect="equal",
                              extent=(0, p.shape[1], p.shape[0], 0))
        g1 = report["G1"]
        rms_txt = (f"fit rms {100 * g1['fits'][g1['choice']]['rms']:.3f} %" if g1["choice"] in g1["fits"]
                   else "no model passed the sanity check (flat = 1.0)")
        axs[1, 1].set_title(f"(block median - model) / centre, %  {rms_txt}")
        fig.colorbar(im, ax=axs[1, 1], shrink=0.8)
        for a in axs.ravel():
            a.set_xticks([]); a.set_yticks([])
        fig.tight_layout()
        fig.savefig(args.plot, dpi=110)
        log(f"wrote {args.plot}")

    log(f"\ndone in {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
