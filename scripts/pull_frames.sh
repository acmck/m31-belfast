#!/usr/bin/env bash
# Pull Seestar sub-frames for one target/night from the scope's SMB share.
#
#   scripts/pull_frames.sh "M 31" 20260908 [session_dir]
#
# - Mounts smb://seestar.local/EMMC Images read-only at ~/mnt/seestar if needed
# - Copies only the raw .fit lights matching the date (skips JPEG previews)
# - Verifies every copied file's size against the source
# - Also grabs the Seestar's own live stack(s) for that night as a reference
# macOS ships bash 3.2; re-exec under Homebrew bash 5 if available
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail

TARGET="${1:?target name as shown in MyWorks, e.g. \"M 31\"}"
DATE="${2:?capture date YYYYMMDD as it appears in the filenames}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION="${3:-$ROOT/data/${TARGET// /}_${DATE}}"
MNT="$HOME/mnt/seestar"
HOST="${SEESTAR_HOST:-seestar.local}"

mkdir -p "$MNT" "$SESSION/lights" "$SESSION/seestar_stack" "$ROOT/logs"
LOG="$ROOT/logs/pull_${TARGET// /}_${DATE}.log"

if ! mount | grep -q " $MNT "; then
  echo "mounting //guest@$HOST/EMMC Images (read-only) ..."
  mount_smbfs -o ro,soft -N "//guest@$HOST/EMMC%20Images" "$MNT"
fi

SRC="$MNT/MyWorks/${TARGET}_sub"
[ -d "$SRC" ] || { echo "no sub-frame folder at $SRC"; ls "$MNT/MyWorks"; exit 1; }

shopt -s nullglob
files=( "$SRC"/Light_*_"$DATE"-*.fit )
echo "${#files[@]} lights for $TARGET on $DATE" | tee "$LOG"
[ ${#files[@]} -gt 0 ] || exit 1

n=0
for f in "${files[@]}"; do
  b="$(basename "$f")"
  dst="$SESSION/lights/$b"
  if [ -f "$dst" ] && [ "$(stat -f %z "$dst")" = "$(stat -f %z "$f")" ]; then
    continue
  fi
  # cp -p fails on chflags over SMB; copy contents only.
  cp "$f" "$dst" 2>>"$LOG" || { echo "FAILED $b" | tee -a "$LOG"; continue; }
  n=$((n+1))
  (( n % 25 == 0 )) && echo "$(date +%H:%M:%S) copied $n" | tee -a "$LOG"
done

# verify sizes
bad=0
for f in "${files[@]}"; do
  b="$(basename "$f")"
  [ "$(stat -f %z "$SESSION/lights/$b" 2>/dev/null || echo 0)" = "$(stat -f %z "$f")" ] || { echo "SIZE MISMATCH $b" | tee -a "$LOG"; bad=$((bad+1)); }
done

# reference stacks from the scope for the same night
for s in "$MNT/MyWorks/$TARGET"/Stacked_*_"$DATE"-*.{fit,jpg}; do
  cp "$s" "$SESSION/seestar_stack/" 2>/dev/null || true
done

echo "done: $(ls "$SESSION/lights" | wc -l | tr -d ' ') lights in $SESSION/lights, $bad size mismatches" | tee -a "$LOG"
[ "$bad" -eq 0 ]
