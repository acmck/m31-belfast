#!/usr/bin/env bash
# Post-process a linear stack:
#   1 plate solve (ASTAP, with SIP distortion terms) -> 2 gradient removal (Siril subsky | GraXpert AI)
#   -> 3 colour calibration (Siril PCC; --spcc tries SPCC first) -> 4 denoise (GraXpert AI)
#   -> 5 star removal (destar.py) -> 6 stretch + export (stretch.py).
#
#   scripts/post_process.sh SESSION_DIR [--skip-denoise] [--skip-cc] [--spcc] [--no-destar]
#                                       [--bge subsky|graxpert] [--subsky-degree 1] [--bge-smoothing 0.5] [--denoise-strength 0.8]
#                                       [--palette rgb|hoo]
#                                       [--crop 0.04|auto] [--target-bg 0.10] [--asinh 3] [--saturation 1.15] [--bg-smooth 1.0]
#                                       [--star-reduce 0.7] [--rotate 0|90|180|270] ... (other stretch.py flags pass through)
#
# Gradient removal runs BEFORE colour calibration: PCC measures its background reference on the sky, so the
# sky must already be flat.  The default is Siril `subsky 1` (first-order polynomial): it flattens the sky as
# well as GraXpert's AI background model but keeps M31's outer halo, which the AI model fits as "background"
# and removes about half of.  `--bge graxpert` restores the AI extraction (with `--bge-smoothing`).
#
# Denoise strength: GraXpert's strength is an exact linear blend between the input and the fully denoised
# image (residual noise = 0.5 / 0.8 / 1.0 of the input's at strength 0.5 / 0.8 / 1.0), not a convergence
# knob.  Default 0.8 keeps a little grain so the sky does not look plastic.
#
# Narrowband: if the stack header has FILTER='LP' (the Seestar's dual-band Ha/OIII filter position) colour
# calibration is skipped automatically (no broadband star colours to calibrate against) and stretch.py gets
# `--palette hoo`.  `--palette rgb|hoo` overrides the detection.
#
# Inputs : SESSION/stack/result.fit     (from stack.sh)
# Outputs: SESSION/final/01_solved.fit, 02_bg.fit, 03_cc.fit, 04_dn.fits, 05_starless.fits, 05_stars.fits,
#          final.{tif,png,jpg}, final_starless.jpg, post_process.log
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
DENOISE=1; CC=1; SPCC=0; BGE=subsky; SUBSKY_DEG=1; SMOOTH=0.5; DN_STRENGTH=0.8; DESTAR=1; PALETTE=""; STRETCH_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-denoise) DENOISE=0;;
    --skip-cc) CC=0;;
    --spcc) SPCC=1;;
    --no-destar) DESTAR=0;;
    --bge) BGE="$2"; shift;;
    --subsky-degree) SUBSKY_DEG="$2"; shift;;
    --bge-smoothing) SMOOTH="$2"; shift;;
    --denoise-strength) DN_STRENGTH="$2"; shift;;
    --palette) PALETTE="$2"; shift;;
    --crop|--crop-noise-ratio|--target-bg|--asinh|--saturation|--rotate|--scnr|--bg-smooth|--bg-desat|--bg-chroma-blur|--shadow-sigma|--star-reduce|--knee)
      STRETCH_ARGS+=("$1" "$2"); shift;;
    *) echo "unknown option $1"; exit 2;;
  esac; shift
done
case "$BGE" in subsky|graxpert) ;; *) echo "--bge must be subsky or graxpert (got $BGE)"; exit 2;; esac
case "$PALETTE" in ""|rgb|hoo) ;; *) echo "--palette must be rgb or hoo (got $PALETTE)"; exit 2;; esac

IN="$SESSION/stack/result.fit"; [ -f "$IN" ] || { echo "missing $IN - run stack.sh first"; exit 1; }
F="$SESSION/final"; mkdir -p "$F"; LOG="$F/post_process.log"; : > "$LOG"
cp "$IN" "$F/01_solved.fit"

# Narrowband detection from the stack header (Seestar: FILTER='IRCUT' broadband, 'LP' dual-band Ha/OIII)
FILTER="$("$PY" -c 'import sys; from astropy.io import fits; print(str(fits.getheader(sys.argv[1]).get("FILTER", "")).strip())' "$IN")"
NARROWBAND=0; [ "$FILTER" = "LP" ] && NARROWBAND=1
if [ -z "$PALETTE" ] && [ "$NARROWBAND" = 1 ]; then PALETTE=hoo; fi
echo "filter: ${FILTER:-unknown}$([ "$NARROWBAND" = 1 ] && echo ' (dual-band Ha/OIII)')${PALETTE:+; palette $PALETTE}"

echo "== 1/6 plate solve (ASTAP, D50 database, SIP distortion terms)"
"$ASTAP" -f "$F/01_solved.fit" -d "$ASTAP_DB" -r 20 -z 2 -sip -update -log >>"$LOG" 2>&1 || true
if "$PY" - "$F/01_solved.fit" <<'PY'
import sys; from astropy.io import fits
h = fits.getheader(sys.argv[1]); ok = "CRVAL1" in h and "CD1_1" in h
sip = "A_ORDER" in h
print("  WCS:", ("solved  RA=%.4f Dec=%.4f  SIP order %s" % (h["CRVAL1"], h["CRVAL2"], h["A_ORDER"] if sip else "none")) if ok else "NOT solved")
sys.exit(0 if ok else 1)
PY
then SOLVED=1; else SOLVED=0; fi
CUR="$F/01_solved.fit"

if [ "$BGE" = subsky ]; then
  echo "== 2/6 gradient removal (Siril subsky, polynomial degree $SUBSKY_DEG)"
  rm -f "$F/02_bg.fit"
  cat > "$F/bg.ssf" <<SSF
requires 1.4.0
load $CUR
subsky $SUBSKY_DEG
save $F/02_bg
close
SSF
  if "$SIRIL" -s "$F/bg.ssf" >>"$LOG" 2>&1 && [ -f "$F/02_bg.fit" ]; then
    CUR="$F/02_bg.fit"; echo "  ok -> $CUR"
  else
    echo "  subsky failed (see $LOG), continuing without"
  fi
else
  echo "== 2/6 gradient removal (GraXpert AI, subtraction, smoothing $SMOOTH)"
  "$GRAX" -cli -cmd background-extraction -correction Subtraction -smoothing "$SMOOTH" -output "$F/02_bg" "$CUR" >>"$LOG" 2>&1 || true
  BG=""; for f in "$F"/02_bg.fit*; do [ -f "$f" ] && BG="$f" && break; done
  if [ -n "$BG" ]; then CUR="$BG"; echo "  ok -> $BG"; else echo "  GraXpert BGE failed (see $LOG), continuing without"; fi
fi

if [ "$NARROWBAND" = 1 ] && [ "$CC" = 1 ]; then
  echo "== 3/6 colour calibration skipped: FILTER='LP' (dual-band Ha/OIII) - no broadband star colours to calibrate against; stretch uses --palette $PALETTE"
elif [ "$CC" = 1 ] && [ "$SOLVED" = 1 ]; then
  echo "== 3/6 colour calibration (Siril PCC on the flattened sky; --spcc tries spectrophotometric first - crashes headless without a sensor DB)"
  CMDS="pcc"; [ "$SPCC" = 1 ] && CMDS="spcc pcc"
  for cmd in $CMDS; do
    rm -f "$F/03_cc.fit"
    cat > "$F/cc.ssf" <<SSF
requires 1.4.0
load $CUR
$cmd
save $F/03_cc
close
SSF
    if "$SIRIL" -s "$F/cc.ssf" >>"$LOG" 2>&1 && [ -f "$F/03_cc.fit" ]; then
      echo "  $cmd ok"; CUR="$F/03_cc.fit"; break
    else
      echo "  $cmd failed (see $LOG)"
    fi
  done
else
  echo "== 3/6 colour calibration skipped (solved=$SOLVED cc=$CC)"
fi

if [ "$DENOISE" = 1 ]; then
  echo "== 4/6 denoise (GraXpert AI, strength $DN_STRENGTH = linear blend with the fully denoised image)"
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

echo "== 6/6 stretch + export${PALETTE:+ (palette $PALETTE)}"
PAL_ARGS=(); [ -n "$PALETTE" ] && PAL_ARGS=(--palette "$PALETTE")
"$PY" "$ROOT/scripts/stretch.py" "$CUR" --out "$F/final" ${STAR_ARGS[@]+"${STAR_ARGS[@]}"} ${PAL_ARGS[@]+"${PAL_ARGS[@]}"} ${STRETCH_ARGS[@]+"${STRETCH_ARGS[@]}"}
echo "done. outputs in $F"
