#!/usr/bin/env python
"""
Bring a Siril PCC result to the SPCC (spectrophotometric) white balance with a fixed per-catalogue correction.

Headless `spcc` segfaults in Siril 1.4.4, so the pipeline runs PCC.  PCC's colour depends on the catalogue's colour
system, not on the sensor: on the Seestar S30 Pro (IMX585, UV/IR-cut) it is biased the same way on every night.
Measured as SPCC factors (Siril GUI, Sony IMX585 + UV/IR Block, Average Spiral Galaxy, local Gaia DR3 xp_sampled)
divided by PCC factors on the same image:

    catalogue   stack                         R       G      (B = 1)
    NOMAD       M31 Sep 7 + Sep 25/26 (995)   1.517   1.380
    NOMAD       M31 Sep 7 (467, dark sky)     1.523   1.401
    localgaia   M31 Sep 7 + Sep 25/26 (995)   0.710   0.864
    localgaia   M31 Sep 7 (467, dark sky)     0.706   0.874
    localgaia   M31 Oct 16 (375, independent)  0.744   0.858   <- not used to derive the first two; predicted
                                                               SPCC R 0.732 G 0.583, measured 0.769 / 0.576

NOMAD PCC is too blue (the lavender cast), local-Gaia PCC too red.  The local-Gaia correction is the mean of all three
stacks (spread +/-3 % in R, +/-1 % in G); NOMAD's is the mean of its two (VizieR was down for the third).
Each channel is scaled about its own sky median, so the neutral sky PCC produced stays neutral.  Factors are
normalised so the largest is 1 (no channel is pushed above the input's range).

Usage:  cc_correct.py IN.fit OUT.fit --catalog localgaia|nomad
"""
import argparse, sys
import numpy as np
from astropy.io import fits

CORRECTION = {  # mean of the measured stacks, (R, G, B)
    "nomad": (1.520, 1.390, 1.000),
    "localgaia": (0.720, 0.865, 1.000),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp"); ap.add_argument("out")
    ap.add_argument("--catalog", required=True, choices=sorted(CORRECTION))
    a = ap.parse_args()
    with fits.open(a.inp) as h:
        d = h[0].data.astype(np.float32); hdr = h[0].header.copy()
    if d.ndim != 3 or d.shape[0] != 3:
        sys.exit(f"cc_correct: expected a 3-channel image, got shape {d.shape}")
    c = np.array(CORRECTION[a.catalog], np.float32)
    c /= c.max()
    for i in range(3):
        sky = float(np.median(d[i]))
        d[i] = (d[i] - sky) * c[i] + sky
    hdr["HISTORY"] = f"cc_correct.py: PCC({a.catalog}) -> SPCC balance, R/G/B x {c[0]:.3f} {c[1]:.3f} {c[2]:.3f} about sky"
    fits.PrimaryHDU(d, header=hdr).writeto(a.out, overwrite=True)
    print(f"cc_correct: {a.catalog} PCC -> SPCC balance, channel factors R {c[0]:.3f} G {c[1]:.3f} B {c[2]:.3f}")


if __name__ == "__main__":
    main()
