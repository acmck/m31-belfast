#!/usr/bin/env python
"""
Privacy check for the public repo and the built site.  Exits 1 on any hit.

  privacy_check.py                 scan the repo: tracked files plus untracked, non-ignored ones
                                   (data/, logs/, node_modules/ and .git/ are always skipped)
  privacy_check.py --dist DIR      scan a build output (e.g. site/dist) before it is deployed

What counts as a hit:
  - private terms (employer and work-network names, private LAN addresses, FITS site keywords, the scope's serial).
    They are stored base64-encoded below so this file does not itself publish them or trip the check;
  - email addresses other than andrewmckinney@outlook.com;
  - the observing site's coordinates at 2-3 decimal places.  They are read at run time from the site latitude
    and longitude keywords in the header of a frame in data/ (never written anywhere); with no frames on disk
    this part is skipped with a warning;
  - in --dist mode: any .fit/.fits/.fts/.log file;
  - in images (both modes): EXIF, XMP, IPTC or GPS metadata (exiftool when installed, else byte markers).

Belfast city centre (54.60, -5.93), used for the weather, is public and is not a hit.
"""
import argparse, base64, glob, json, os, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"data", "logs", "node_modules", ".git", ".venv"}
ALLOWED_EMAILS = {"andrewmckinney@outlook.com"}
PRIVATE = json.loads(base64.b64decode(
    "WyJcXGJlZ2VuXFxiIiwgIlxcYmloZ1xcYiIsICJjaGVja1xccz9wb2ludCIsICJcXGJ2cG5cXGIiLCAiXFxidXR1blxcZCIsICIxOTJcXC4xNjhc"
    "XC4iLCAiXFxic2l0ZWxhdFxcYiIsICJcXGJzaXRlbG9uZ1xcYiIsICIxOGViNWYxNSJd"))
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".tif", ".tiff", ".gif", ".heic"}
FORBIDDEN_EXT = {".fit", ".fits", ".fts", ".log"}
MARKERS = [b"Exif\x00\x00", b"<x:xmpmeta", b"http://ns.adobe.com/xap", b"eXIf"]


def coordinate_patterns():
    """Regexes for the site's coordinates, built from a frame header at run time."""
    frames = sorted(glob.glob(str(ROOT / "data/*/lights/*.fit")))[:1]
    if not frames:
        print("warning: no frames under data/*/lights; coordinate check skipped", file=sys.stderr)
        return []
    from astropy.io import fits
    h = fits.getheader(frames[0])
    pats = []
    # keyword names are assembled so this file does not trip a plain-text grep for them
    for key, two_dec_needs_sign in (("SITE" + "LAT", False), ("SITE" + "LONG", True)):
        v = h.get(key)
        if v is None:
            continue
        a = abs(float(v))
        forms = set()
        for d in (2, 3):
            forms.add((f"{a:.{d}f}", d))                              # rounded
            forms.add((f"{int(a * 10**d) / 10**d:.{d}f}", d))         # truncated
        for s, d in forms:
            body = re.escape(s).encode()
            if d == 2 and two_dec_needs_sign:   # short longitude forms are common numbers; require a sign or a W
                pats.append(re.compile(rb"(?<![0-9])(?:-" + body + rb"|" + body + rb"\s*\xc2?\xb0?\s*W)"))
            else:
                pats.append(re.compile(rb"(?<![0-9.])-?" + body))
    return pats


def repo_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout.decode()
    for rel in filter(None, out.split("\0")):
        if SKIP_DIRS & set(Path(rel).parts):
            continue
        p = ROOT / rel
        if p.is_file():
            yield p


def dist_files(d):
    for p in sorted(Path(d).rglob("*")):
        if p.is_file():
            yield p


def image_metadata_hits(images):
    hits = []
    if not images:
        return hits
    if shutil.which("exiftool"):
        for i in range(0, len(images), 200):
            batch = [str(p) for p in images[i:i + 200]]
            r = subprocess.run(["exiftool", "-j", "-G0", "-a", "-q", "-q", *batch], capture_output=True, text=True)
            for rec in json.loads(r.stdout or "[]"):
                bad = sorted({k.split(":")[0] for k in rec if ":" in k
                              and (k.split(":")[0] in {"EXIF", "XMP", "IPTC", "MakerNotes"} or "GPS" in k)})
                if bad:
                    hits.append((rec["SourceFile"], "image metadata: " + ", ".join(bad)))
    else:
        for p in images:
            b = p.read_bytes()
            for m in MARKERS:
                if m in b:
                    hits.append((str(p), f"image metadata marker {m!r}"))
                    break
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dist", help="scan this build directory instead of the repo")
    a = ap.parse_args()
    files = list(dist_files(a.dist) if a.dist else repo_files())
    terms = [re.compile(p.encode(), re.I) for p in PRIVATE]
    coords = coordinate_patterns()
    hits = []
    for p in files:
        rel = os.path.relpath(p, ROOT)
        ext = p.suffix.lower()
        if ext in FORBIDDEN_EXT:
            hits.append((rel, f"forbidden file type {ext}"))
        if ext in IMAGE_EXT or ext == ".onnx" or p.stat().st_size > 50_000_000:
            continue
        data = p.read_bytes()
        for lineno, line in enumerate(data.split(b"\n"), 1):
            found = [r.pattern.decode(errors="replace") for r in terms if r.search(line)]
            found += [f"email {m.decode(errors='replace')}" for m in EMAIL.findall(line)
                      if m.decode(errors="replace").lower() not in ALLOWED_EMAILS]
            if any(r.search(line) for r in coords):
                found.append("site coordinates")
            for f in found:
                hits.append((f"{rel}:{lineno}", f))
    hits += [(os.path.relpath(f, ROOT), why) for f, why in
             image_metadata_hits([p for p in files if p.suffix.lower() in IMAGE_EXT])]
    scope = a.dist or "repo"
    if hits:
        for where, why in hits:
            print(f"{where}: {why}")
        print(f"privacy check FAILED ({scope}): {len(hits)} hit(s) in {len(files)} files")
        sys.exit(1)
    print(f"privacy check passed ({scope}): {len(files)} files, {len(coords)} coordinate patterns")


if __name__ == "__main__":
    main()
