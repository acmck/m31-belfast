---
name: deploy-site
description: Build the M31-from-Belfast journal site (Astro, site/), run the privacy checks on the build (no FITS/log files, no image metadata, no site coordinates or private terms) and deploy it to Cloudflare Pages project m31-belfast with the project-local wrangler. Use when asked to deploy, publish, ship or update the live site, or after a journal stage changes.
---

# Build, check, deploy

Everything runs from the repo root. Use the project-local wrangler only (`npm --prefix site exec wrangler`),
never a global install or a bare `npx wrangler` outside `site/`.

1. **Build**
   ```bash
   npm --prefix site run build              # = make site; output in site/dist
   ```
   If `site/node_modules` is missing, run `npm --prefix site ci` first.

2. **Check the build** (all must pass; any hit blocks the deploy)
   ```bash
   make site-check                          # scripts/privacy_check.py --dist site/dist
   make privacy-check                       # the repo itself
   ```
   `site-check` fails on any `.fit/.fits/.fts/.log` in `site/dist`, any EXIF/XMP/IPTC/GPS block in an image
   (via exiftool), any email other than andrewmckinney@outlook.com, private terms, and the observing site's
   coordinates (read at run time from a frame header in `data/`; never write them anywhere). Belfast city
   centre (54.60, -5.93) is allowed. Also glance at `du -sh site/dist`: no stray originals or TIFF/PNG masters.
   If an image fails, regenerate it with `scripts/export_web.py`; don't strip it by hand.

3. **Deploy**
   ```bash
   npm --prefix site exec wrangler -- whoami          # if not logged in, ask Andrew to run: ! npm --prefix site exec wrangler -- login
   npm --prefix site exec wrangler -- pages deploy site/dist --project-name m31-belfast
   ```
   (`make deploy` runs steps 1-3 in one go.) The project is created once with
   `npm --prefix site exec wrangler -- pages project create m31-belfast --production-branch main`.

4. **Verify** the deployed URL that wrangler prints (production is https://m31-belfast.pages.dev): the page
   loads on a phone-width viewport, the sliders move, and the network panel shows no `.fit`/`.fits` requests.
   Report the URL.
