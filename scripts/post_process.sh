#!/usr/bin/env bash
# Post-process a linear stack:  plate-solve (ASTAP) -> colour calibration (Siril SPCC/PCC)
#   -> background extraction (GraXpert AI) -> denoise (GraXpert AI) -> stretch + export.
#
#   scripts/post_process.sh SESSION_DIR [--skip-denoise] [--skip-cc] [--spcc] [--no-destar] [--bge-smoothing 0.5] [--denoise-strength 0.8]
#                                       [--crop 0.04] [--target-bg 0.10] [--asinh 3] [--saturation 1.15] [--bg-smooth 1.0] [--star-reduce 0.7] [--rotate 0|90|180|270]
#
# Inputs : SESSION/stack/result.fit     (from stack.sh)
# Outputs: SESSION/final/01_solved.fit, 02_cc.fit, 03_bge.fits, 04_dn.fits, 05_starless.fits, 05_stars.fits, final.{tif,png,jpg}, final_starless.jpg
# macOS ships bash 3.2; re-exec under Homebrew bash 5 if available
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail
SESSION="$(cd "${1:?session dir}" && pwd)"; shift
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIRIL="${SIRIL:-/Applications/Siril.app/Contents/MacOS/siril-cli}"
GRAX="${GRAX:-/Applications/GraXpert.app/Contents/MacOS/GraXpert}"
ASTAP="${ASTAP:-$ROOT/tools/astap/cli/astap_cli}"
ASTAP_DB="${ASTAP_DB:-$ROOT/tools/astap/db}"
PY="$ROOT/.venv/bin/python"
DENOISE=1; CC=1; SPCC=0; SMOOTH=0.5; DN_STRENGTH=0.8; DESTAR=1; STRETCH_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-denoise) DENOISE=0;;
    --skip-cc) CC=0;;
    --spcc) SPCC=1;;
    --bge-smoothing) SMOOTH="$2"; shift;;
    --denoise-strength) DN_STRENGTH="$2"; shift;;
    --no-destar) DESTAR=0;;
    --crop|--target-bg|--asinh|--saturation|--rotate|--scnr|--bg-smooth|--bg-desat|--shadow-sigma|--star-reduce) STRETCH_ARGS+=("$1" "$2"); shift;;
    *) echo "unknown option $1"; exit 2;;
  esac; shift
done

IN="$SESSION/stack/result.fit"; [ -f "$IN" ] || { echo "missing $IN - run stack.sh first"; exit 1; }
F="$SESSION/final"; mkdir -p "$F"; LOG="$F/post_process.log"; : > "$LOG"
cp "$IN" "$F/01_solved.fit"

echo "== 1/6 plate solve (ASTAP, D50 database)"
"$ASTAP" -f "$F/01_solved.fit" -d "$ASTAP_DB" -r 20 -z 2 -update -log >>"$LOG" 2>&1 || true
if "$PY" - "$F/01_solved.fit" <<'PY'
import sys; from astropy.io import fits
h = fits.getheader(sys.argv[1]); ok = "CRVAL1" in h and "CD1_1" in h
print("  WCS:", "solved  RA=%.4f Dec=%.4f" % (h["CRVAL1"], h["CRVAL2"]) if ok else "NOT solved")
sys.exit(0 if ok else 1)
PY
then SOLVED=1; else SOLVED=0; fi

CUR="$F/01_solved.fit"
if [ "$CC" = 1 ] && [ "$SOLVED" = 1 ]; then
  echo "== 2/6 colour calibration (Siril PCC; --spcc tries spectrophotometric first - crashes headless without a sensor DB)"
  CMDS="pcc"; [ "$SPCC" = 1 ] && CMDS="spcc pcc"
  for cmd in $CMDS; do
    rm -f "$F/02_cc.fit"
    cat > "$F/cc.ssf" <<SSF
requires 1.4.0
load $CUR
$cmd
save $F/02_cc
close
SSF
    if "$SIRIL" -s "$F/cc.ssf" >>"$LOG" 2>&1 && [ -f "$F/02_cc.fit" ]; then
      echo "  $cmd ok"; CUR="$F/02_cc.fit"; break
    else
      echo "  $cmd failed (see $LOG)"
    fi
  done
else
  echo "== 2/6 colour calibration skipped (solved=$SOLVED cc=$CC)"
fi

echo "== 3/6 background extraction (GraXpert AI, subtraction, smoothing $SMOOTH)"
"$GRAX" -cli -cmd background-extraction -correction Subtraction -smoothing "$SMOOTH" -output "$F/03_bge" "$CUR" >>"$LOG" 2>&1 || true
BGE=""; for f in "$F"/03_bge.fit*; do [ -f "$f" ] && BGE="$f" && break; done
if [ -n "$BGE" ]; then CUR="$BGE"; echo "  ok -> $BGE"; else echo "  GraXpert BGE failed (see $LOG), continuing without"; fi

if [ "$DENOISE" = 1 ]; then
  echo "== 4/6 denoise (GraXpert AI, strength $DN_STRENGTH)"
  # GraXpert's CLI takes the denoise strength from a preferences file, not a flag.
  PREFS="$HOME/Library/Application Support/GraXpert/preferences.json"
  "$PY" - "$PREFS" "$F/graxpert_prefs.json" "$DN_STRENGTH" <<'PY'
import json, sys, os
d = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
d["denoise_strength"] = float(sys.argv[3])
json.dump(d, open(sys.argv[2], "w"))
PY
  "$GRAX" -cli -cmd denoising -preferences_file "$F/graxpert_prefs.json" -output "$F/04_dn" "$CUR" >>"$LOG" 2>&1 || true
  DN=""; for f in "$F"/04_dn.fit*; do [ -f "$f" ] && DN="$f" && break; done
  if [ -n "$DN" ]; then CUR="$DN"; echo "  ok -> $DN"; else echo "  denoise failed (see $LOG), continuing without"; fi
else
  echo "== 4/6 denoise skipped"
fi

if [ "$DESTAR" = 1 ]; then
  echo "== 5/6 star removal (Dark Star via onnxruntime)"
  if "$PY" "$ROOT/scripts/destar.py" "$CUR" --out "$F/05" >>"$LOG" 2>&1 && [ -f "$F/05_starless.fits" ]; then
    echo "  ok -> $F/05_starless.fits + 05_stars.fits"
    STAR_ARGS=(--stars "$F/05_stars.fits"); CUR="$F/05_starless.fits"
  else
    echo "  destar failed (see $LOG), stretching without star separation"; STAR_ARGS=()
  fi
else
  echo "== 5/6 star removal skipped"; STAR_ARGS=()
fi

echo "== 6/6 stretch + export"
"$PY" "$ROOT/scripts/stretch.py" "$CUR" --out "$F/final" ${STAR_ARGS[@]+"${STAR_ARGS[@]}"} ${STRETCH_ARGS[@]+"${STRETCH_ARGS[@]}"}
echo "done. outputs in $F"
