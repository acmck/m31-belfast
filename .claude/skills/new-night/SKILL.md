---
name: new-night
description: Add a new night of Seestar M31 data end to end - copy the frames off the scope over Wi-Fi (with retry and remount), run the pipeline on that night, re-combine all nights, add a journal stage, and deploy the site. Use when Andrew says he imaged M31 again, asks to "pull last night", "process tonight's frames", "add the new night", or wants the latest result on the site.
---

# New night, end to end

Work in the repo root. Run every Siril / GraXpert / Dark Star job one at a time (never in parallel or from
parallel agents). Read `CLAUDE.md` for the conventions; never change a default without new measurements.

## 1. Name the session

One folder per night, named by the **evening the night started**: frames from the night of 16/17 Oct go to
`data/M31_2026-10-16`, including those dated 2026-10-17. Ask for the date only if it can't be read from the
scope's file names.

## 2. Pull the frames from the scope

1. The scope must be in Station mode on the home Wi-Fi, reachable as `seestar.local`.
2. Check the route: `route -n get seestar.local` must report `interface: en0`. If not, some tunnel or
   split route on the Mac is capturing the home LAN address. Ask Andrew to disconnect it (or add a host route
   via `en0`). The scope doesn't answer ping, so don't use ping to test it.
3. Copy each capture date into the same session folder. A night that crosses midnight needs two runs:
   ```bash
   scripts/pull_frames.sh "M 31" 20261016 data/M31_2026-10-16
   scripts/pull_frames.sh "M 31" 20261017 data/M31_2026-10-16
   ```
   Run it in the background with `caffeinate -i`. It is resumable (same-size files are skipped) and exits
   non-zero on any size mismatch.
4. Expect about 10 frames a minute (about an hour per 600 frames) and avoid big downloads at the same time.
   The scope's Wi-Fi drops out on long copies. A dead soft mount makes every later copy fail at once with
   "size mismatch". Retry loop: `umount -f ~/mnt/seestar`, wait 30 s, rerun the same command; stop after 5
   attempts with no new files and tell Andrew (the scope may need a restart or moving closer to the router).
5. Done when the script prints `0 size mismatches`. `seestar_stack/` should hold the scope's own live stack.

## 3. Run the pipeline on the night

```bash
make night SESSION=data/M31_2026-10-16        # qa deband flat stack post, about 10+ min
```

Check `data/<session>/qa/summary.json` (kept / rejected, scope drop rate) and the last line of
`data/<session>/final/history.log` (frames, minutes, SNR, FWHM). Report those numbers.

## 4. Combine every night

```bash
make combine                                  # data/M31_combined_<today> from every data/M31_YYYY-MM-DD
```

This links each night's QA-selected frames and stacks them with noise weighting (QA stays per night).
Look at `final/final.jpg`: if auto-crop clipped M31's disc, say so rather than silently re-cropping.

## 5. Add the journal stage

Use the `journal-stage` skill: before = the previous combined result (or the previous best single night),
after = the new combined result, plus a conditions entry for the new night via `scripts/night_conditions.py`.

## 6. Deploy

Use the `deploy-site` skill (build, privacy checks, Cloudflare Pages). Then commit to `main` (signed) and,
if asked to push, switch to `acmck` first and back afterwards (see CLAUDE.md).
