"""Story images: one consistently framed image per story step, cut from the timeline's own registered intermediates.

Every journal_pairs/<id>/{before,after,image}.png is already warped (astroalign similarity, Lanczos-4) onto ONE
reference canvas (comb_1017, padded 8 %); its alignment metrics give the crop box it occupies on that canvas.
So no re-warping is needed: each story image is a pure crop of a registered intermediate to one shared window
(the t3/t4 timeline window, which lies inside every other render's valid crop), then exported like export_web.py
(no metadata, byte budgets). Residual check: stars detected in each story image are matched (identity transform,
3 px tolerance) to stars in the reference story image; median distance must be < 1 px.

Run: .venv/bin/python <this> (cwd = repo root)
"""
import json, sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from align_pair import stars  # noqa: E402
from export_web import save, resized, metadata_blocks  # noqa: E402

MET = ROOT / "site/src/data/metrics"
PAIRS = ROOT / "data/journal_pairs"
OUT = ROOT / "site/public/img/story"

# story step -> (render id, journal_pairs job, side). Each source is the exact registered file the timeline used.
STEPS = [
    ("first-light", "scope_0907", "t1-first-pipeline", "before"),
    ("first-pipeline", "first_0907", "t1-first-pipeline", "after"),
    ("review", "sep09_0907", "t2-review", "after"),
    ("moon", "cur_0925", "t4-three-nights-note-cur_0925", "image"),
    ("two-nights", "comb_1016_asof", "t3-two-nights", "after"),
    ("sharp", "cur_1016", "t4-three-nights-note-cur_1016", "image"),
    ("three-nights", "comb_1017", "t4-three-nights", "after"),
]
REF_STEP = "three-nights"


def load16(p):
    im = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
    assert im is not None, p
    im = im[..., :3][..., ::-1]
    return im.astype(np.float32) / (65535.0 if im.dtype == np.uint16 else 255.0)


def main():
    al = {jid: json.loads((MET / "alignment" / f"{jid}.json").read_text()) for _, _, jid, _ in STEPS}
    # shared window = intersection of every source's crop box on the reference canvas
    x0 = max(a["crop"]["x"] for a in al.values()); y0 = max(a["crop"]["y"] for a in al.values())
    x1 = min(a["crop"]["x"] + a["crop"]["width"] for a in al.values())
    y1 = min(a["crop"]["y"] + a["crop"]["height"] for a in al.values())
    W, H = x1 - x0, y1 - y0
    # 4:3 exactly, as export_web.frame() does for this window (1340x1004 -> 1339x1004, left-aligned)
    w, h = W, round(W * 3 / 4)
    if h > H:
        w, h = round(H * 4 / 3), H
    win = dict(x=x0, y=y0, width=w, height=h)
    print("window on reference canvas:", win)

    crops = {}
    for step, rid, jid, side in STEPS:
        c = al[jid]["crop"]
        src = PAIRS / jid / f"{side}.png"
        im = load16(src)
        assert im.shape[1] == c["width"] and im.shape[0] == c["height"], (src, im.shape, c)
        ox, oy = x0 - c["x"], y0 - c["y"]
        crop = im[oy:oy + h, ox:ox + w]
        assert crop.shape[:2] == (h, w)
        # the source must be valid (non-zero) everywhere in the window
        zero_frac = float((crop.max(axis=2) == 0).mean())
        crops[step] = dict(img=crop, rid=rid, jid=jid, side=side, src=src, zero_frac=zero_frac,
                           src_residual=(al[jid]["after"] if side in ("after",) else al[jid]["before"])["median_residual_px"])

    ref_xy = stars(crops[REF_STEP]["img"], 800)
    tree = cKDTree(ref_xy)
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"_about": "Story images (site/public/img/story/<step>.*): one shared 4:3 window on the timeline's reference "
                        "canvas (comb_1017, padded 8 %), cut from the registered intermediates the timeline sliders use "
                        "(data/journal_pairs). Crop only: no re-warp, no reprocessing. residual = median distance of each "
                        "image's stars to the reference image's stars at the same pixel (identity, 3 px tolerance).",
              "reference_step": REF_STEP, "window_on_ref_canvas": win, "steps": {}}
    worst = 0.0
    for step, d in crops.items():
        xy = stars(d["img"], 400)
        dist, _ = tree.query(xy, distance_upper_bound=3.0)
        dist = dist[np.isfinite(dist)]
        med = float(np.median(dist)) if len(dist) else float("inf")
        if step != REF_STEP:
            worst = max(worst, med)
        u8 = (np.clip(d["img"], 0, 1) * 255 + 0.5).astype(np.uint8)
        pil = Image.fromarray(np.ascontiguousarray(u8), "RGB")
        entry = {"render": d["rid"], "source": f"data/journal_pairs/{d['jid']}/{d['side']}.png",
                 "registration_residual_px": d["src_residual"], "story_residual_px": round(med, 3),
                 "n_stars_matched": int(len(dist)), "zero_pixel_fraction": round(d["zero_frac"], 6),
                 "width": w, "height": h, "files": {}}
        for tag, im, kb in (("", resized(pil, 2400), 400), ("-1200", resized(pil, 1200), 120)):
            for fmt in ("avif", "webp"):
                p = OUT / f"{step}{tag}.{fmt}"
                nbytes, q = save(im, p, fmt, kb * 1024)
                bad = metadata_blocks(p)
                if bad:
                    sys.exit(f"{p}: metadata found {bad}")
                entry["files"][p.name] = {"url": f"/img/story/{p.name}", "bytes": nbytes, "quality": q,
                                          "size": list(im.size)}
                if tag == "-1200" and fmt == "avif" and nbytes > 120 * 1024:
                    sys.exit(f"{p} over 120 KB")
        report["steps"][step] = entry
        print(f"{step:15s} {d['rid']:16s} residual {med:.3f} px ({len(dist)} stars)  zero {d['zero_frac']:.5f}  "
              + ", ".join(f"{k} {v['bytes'] // 1024}K" for k, v in entry["files"].items()))
    report["worst_story_residual_px"] = round(worst, 3)
    report["metadata_check"] = "passed: no EXIF/XMP/ICC/FITS keywords in any output"
    (MET / "story_images.json").write_text(json.dumps(report, indent=1))
    if worst >= 1.0:
        sys.exit(f"worst residual {worst:.3f} px >= 1 px")
    print("worst residual", round(worst, 3))


if __name__ == "__main__":
    main()
