#!/usr/bin/env python
"""
Export an aligned before/after pair (from align_pair.py) as web images for one journal stage.

  export_web.py BEFORE.png AFTER.png --stage 03-ring [--single] [--out-root site/public/img] [--rotate 0]
                [--aspect 4:3 [--center-up 110]] [--long 2400] [--long-small 1200] [--manifest site/src/data/metrics/images_03-ring.json]

Writes  <out-root>/<stage>/{before,after}.{avif,webp}  (long edge --long px)
        <out-root>/<stage>/{before,after}-1200.{avif,webp}  (long edge --long-small px)
and a manifest with the URLs, pixel sizes, byte sizes and a ~24 px blur-up WebP data URI of AFTER.

The two inputs must have identical dimensions (align_pair.py guarantees it).  Images are built from bare pixel
arrays, so they carry no EXIF, XMP, ICC profile or FITS header of any kind; --check re-reads every output and fails
if any metadata block is present.

Framing (the site standard, see site/src/data/metrics/journal_spec_v2.json "frame"): the Seestar shoots portrait frames
in which M31's major axis already lies close to horizontal, so the pair is NOT rotated (--rotate 0); --aspect crops
both images identically to a landscape window around M31's nucleus (found in AFTER), moved up by --center-up px
so M110 stays in frame with M32 below the core.
"""
import argparse, base64, io, json, sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

AVIF_Q, WEBP_Q = 62, 80


def load8(path, rotate):
    im = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if im is None:
        sys.exit(f"cannot read {path}")
    im = im[..., :3][..., ::-1]
    if im.dtype == np.uint16:
        im = (im.astype(np.float32) / 257.0 + 0.5).clip(0, 255).astype(np.uint8)
    if rotate:
        im = np.rot90(im, k=-rotate // 90)  # clockwise
    return Image.fromarray(np.ascontiguousarray(im), "RGB")


def frame(imgs, aspect, center_up):
    """Crop both images to ASPECT ("4:3") around the nucleus of AFTER, shifted up by CENTER_UP px."""
    aw, ah = (float(x) for x in aspect.split(":"))
    W, H = imgs["after"].size
    lum = np.asarray(imgs["after"].convert("L"), dtype=np.float32)
    ny, nx = np.unravel_index(np.argmax(cv2.GaussianBlur(lum, (0, 0), 25)), lum.shape)
    w, h = W, round(W * ah / aw)
    if h > H:  # not tall enough: use the full height and narrow the window instead
        w, h = round(H * aw / ah), H
    x0 = int(np.clip(nx - w // 2, 0, W - w)); y0 = int(np.clip(ny - center_up - h // 2, 0, H - h))
    box = (x0, y0, x0 + w, y0 + h)
    return {k: v.crop(box) for k, v in imgs.items()}, dict(nucleus_xy=[int(nx), int(ny)], box=list(box))


def resized(img, long_edge):
    w, h = img.size
    s = long_edge / max(w, h)
    if s >= 1:
        return img.copy()
    return img.resize((round(w * s), round(h * s)), Image.LANCZOS)


def save(img, path, fmt, max_bytes=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = Image.frombytes("RGB", img.size, img.tobytes())  # fresh image: no info dict, no exif/icc/xmp
    if fmt == "avif":
        q = AVIF_Q
        while True:  # step quality down until the file fits its byte budget (page first-load budget)
            clean.save(path, "AVIF", quality=q, speed=4)
            if not max_bytes or path.stat().st_size <= max_bytes or q <= 30:
                break
            q -= 4
    else:
        q = WEBP_Q
        while True:
            clean.save(path, "WEBP", quality=q, method=6)
            if not max_bytes or path.stat().st_size <= max_bytes or q <= 40:
                break
            q -= 5
    return path.stat().st_size, q


def metadata_blocks(path):
    """Names of any metadata a decoder exposes (EXIF/XMP/ICC) plus raw container chunk markers."""
    found = []
    with Image.open(path) as im:
        for k in ("exif", "xmp", "icc_profile", "XML:com.adobe.xmp"):
            if im.info.get(k):
                found.append(k)
        if im.getexif():
            found.append("exif-tags")
    raw = path.read_bytes()
    for marker in (b"EXIF", b"Exif", b"XMP ", b"ICCP", b"iccp", b"<x:xmpmeta", b"SITE" + b"LAT", b"OBJCTRA"):
        if marker in raw:
            found.append(marker.decode(errors="replace"))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before"); ap.add_argument("after")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--out-root", default="site/public/img")
    ap.add_argument("--url-root", default="/img")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("--aspect", help="crop both images to this landscape aspect around M31, e.g. 4:3")
    ap.add_argument("--center-up", type=int, default=0, help="with --aspect: move the window up by this many px")
    ap.add_argument("--long", type=int, default=2400); ap.add_argument("--long-small", type=int, default=1200)
    ap.add_argument("--max-kb", type=int, default=400, help="byte budget per full-size file (quality steps down to fit)")
    ap.add_argument("--max-kb-small", type=int, default=120, help="byte budget per 1200 px file")
    ap.add_argument("--manifest")
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--single", action="store_true",
                    help="export only AFTER, as image*.{avif,webp} (side-note images); BEFORE is ignored")
    a = ap.parse_args()

    imgs = {"before": load8(a.after if a.single else a.before, a.rotate), "after": load8(a.after, a.rotate)}
    if imgs["before"].size != imgs["after"].size:
        sys.exit(f"pair sizes differ: {imgs['before'].size} vs {imgs['after'].size}")
    out = Path(a.out_root) / a.stage
    man = {"stage": a.stage, "images": {}, "bytes": {}}
    if a.aspect:
        imgs, man["frame"] = frame(imgs, a.aspect, a.center_up)
        man["frame"].update(aspect=a.aspect, center_up=a.center_up, rotate=a.rotate)
    sides = {"image": imgs["after"]} if a.single else imgs
    for side, img in sides.items():
        big, small = resized(img, a.long), resized(img, a.long_small)
        entry = {"width": big.size[0], "height": big.size[1], "width_1200": small.size[0], "height_1200": small.size[1]}
        variants = [("", big, a.max_kb), ("-1200", small, a.max_kb_small)]
        if a.single:  # side notes are shown as thumbnails: add a ~400 px variant
            thumb = resized(img, 400)
            variants.append(("-400", thumb, 25))
            entry.update(width_400=thumb.size[0], height_400=thumb.size[1])
        for fmt in ("avif", "webp"):
            for tag, im, kb in variants:
                p = out / f"{side}{tag}.{fmt}"
                budget = kb * 1024
                man["bytes"][p.name], man.setdefault("quality", {})[p.name] = save(im, p, fmt, budget)
                entry[f"{fmt}{tag.replace('-', '_')}"] = f"{a.url_root}/{a.stage}/{p.name}"
                if not a.no_check:
                    bad = metadata_blocks(p)
                    if bad:
                        sys.exit(f"{p}: metadata found: {bad}")
        man["images"][side] = entry
    # ~24 px blur-up placeholder of AFTER
    w, h = imgs["after"].size
    tiny = imgs["after"].resize((24, max(1, round(24 * h / w))) if w >= h else (max(1, round(24 * w / h)), 24), Image.LANCZOS)
    buf = io.BytesIO(); Image.frombytes("RGB", tiny.size, tiny.tobytes()).save(buf, "WEBP", quality=50)
    man["images"]["placeholder"] = "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    man["metadata_check"] = "skipped" if a.no_check else "passed: no EXIF/XMP/ICC/FITS keywords in any output"
    if a.manifest:
        Path(a.manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(a.manifest).write_text(json.dumps(man, indent=1))
    print(f"{a.stage}: {imgs['after'].size[0]}x{imgs['after'].size[1]} -> "
          + ", ".join(f"{k} {v // 1024} KB" for k, v in man["bytes"].items()))


if __name__ == "__main__":
    main()
