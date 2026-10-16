#!/usr/bin/env bash
# Record a finished post-processing run and point the target's "latest" link at it.
#
#   scripts/record_result.sh SESSION_DIR
#
# Called by run_pipeline.sh after a successful `post` stage.  In SESSION_DIR/final/:
#   history/<YYYYMMDD-HHMMSS>.jpg   a copy of that run's final.jpg (final.* is overwritten by the next run)
#   history.log                     one line per run: when, git commit, frames and minutes stacked, stack
#                                   settings, sky-noise SNR and star FWHM of the linear stack
# In data/:
#   <TARGET>_latest -> SESSION_DIR  symlink to the most recently finished session for that target, where
#                                   TARGET is the session folder name up to the first "_" (M31_2026-09-25 -> M31)
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION="$(cd "${1:?session dir}" && pwd)"
F="$SESSION/final"
[ -f "$F/final.jpg" ] || { echo "record_result: no $F/final.jpg, nothing to record"; exit 0; }
TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$F/history"
cp "$F/final.jpg" "$F/history/$TS.jpg"

COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
git -C "$ROOT" diff --quiet HEAD -- scripts run_pipeline.sh 2>/dev/null || COMMIT="$COMMIT+uncommitted"
STACK_SETTINGS="$(cat "$SESSION/process/stack2.ssf" "$SESSION/process/stack.ssf" 2>/dev/null | grep -m1 -E "^stack " | sed -E 's/ -out=[^ ]+//' || true)"
METRICS="$("$ROOT/.venv/bin/python" - "$SESSION/stack/result.fit" "$ROOT/scripts" <<'PY' 2>/dev/null || echo "metrics unavailable"
import sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, sys.argv[2])
from astropy.io import fits
from measure_stack import measure
h = fits.getheader(sys.argv[1])
m = measure(sys.argv[1], 1, 3.665)
print(f"frames {h.get('STACKCNT', '?')}  {float(h.get('LIVETIME', 0)) / 60:.0f} min  "
      f"SNR {m['snr']:.2f}  FWHM {m['fwhm_arcsec']:.1f}\"  stars {m['nstars']}")
PY
)"
LINE="$TS  commit $COMMIT  $METRICS  history/$TS.jpg  [$STACK_SETTINGS]"
echo "$LINE" >> "$F/history.log"
echo "recorded: $LINE"

NAME="$(basename "$SESSION")"; TARGET="${NAME%%_*}"
DATA="$(dirname "$SESSION")"
ln -sfn "$NAME" "$DATA/${TARGET}_latest"
echo "$DATA/${TARGET}_latest -> $NAME"
