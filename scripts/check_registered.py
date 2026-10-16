#!/usr/bin/env python
"""
Find registered (drizzled / interpolated) frames that came out empty, and emit Siril commands to drop them.

Siril 1.4.4's drizzle occasionally writes a frame whose data is all (or nearly all) zero in one or more
channels, even though the input frame and its registration are fine (seen on M31 2026-09-07 + 2026-09-25:
frames 242 and 498 of 996, frame 498 being the stacking reference; the stack then aborts with
"Normalization failed").  This scans every frame of the registered sequence on a coarse grid and
prints, to stdout, Siril commands for the stack script:

    unselect <seq> N N        for each frame with < --min-coverage non-zero pixels in any channel
    setref <seq> M            if the reference frame is one of them (M = nearest good frame)

Usage:  check_registered.py PROCESS_DIR SEQNAME [--min-coverage 0.2]
        (SEQNAME without trailing underscore, e.g. r_light)
Diagnostics go to stderr.
"""
import argparse, glob, os, re, sys
import numpy as np
from astropy.io import fits


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("process_dir"); ap.add_argument("seq")
    ap.add_argument("--min-coverage", type=float, default=0.2,
                    help="minimum fraction of non-zero pixels per channel (framing=max frames are legitimately ~50-70 %%)")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.process_dir, f"{a.seq}_[0-9]*.fit")))
    if not files:
        print(f"check_registered: no {a.seq}_*.fit in {a.process_dir}", file=sys.stderr); return
    bad = []
    for f in files:
        idx = int(re.search(r"_(\d+)\.fit$", f).group(1))
        try:
            with fits.open(f, memmap=True) as h:
                d = h[0].data
                d = d[None] if d.ndim == 2 else d
                cov = [float((np.asarray(d[c, ::16, ::16]) != 0).mean()) for c in range(d.shape[0])]
        except Exception as e:
            cov = [0.0]; print(f"  frame {idx}: unreadable ({e})", file=sys.stderr)
        if min(cov) < a.min_coverage:
            bad.append(idx)
            print(f"  frame {idx}: empty registered output, non-zero fraction per channel "
                  f"{[round(c, 3) for c in cov]} - excluding", file=sys.stderr)
    # reference: 6th field of the S line in <seq>_.seq is the 0-based reference index
    ref = None
    seqfile = os.path.join(a.process_dir, f"{a.seq}_.seq")
    if os.path.exists(seqfile):
        for line in open(seqfile):
            if line.startswith("S "):
                ref = int(line.split()[6]) + 1
                break
    print(f"check_registered: {len(files)} frames, {len(bad)} empty, reference frame {ref}", file=sys.stderr)
    for i in bad:
        print(f"unselect {a.seq}_ {i} {i}")
    if ref is not None and ref in bad:
        good = [int(re.search(r"_(\d+)\.fit$", f).group(1)) for f in files]
        good = [g for g in good if g not in bad]
        if good:
            new = min(good, key=lambda g: abs(g - ref))
            print(f"setref {a.seq}_ {new}")
            print(f"  reference frame {ref} is empty - moving the stacking reference to frame {new}", file=sys.stderr)


if __name__ == "__main__":
    main()
