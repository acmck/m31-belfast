# Design brief: M31 from Belfast (revised after critique)

1. **What it is:** one long page, an observing log kept by one person learning to image M31 with a Seestar S30 Pro from a Bortle 6 Belfast garden. It should read like a notebook that got typeset, not like a product page.
2. **Ground:** `#211f1c`, the median sky color of my own Sep 7 Seestar live stack (darkest 40 % of pixels). The other two nights sample to `#1c1c1a` and `#1b1a1d`; the page sits on the sky it's about. Text `#e8e2d6`, secondary `#a39b8c`.
3. **Accent:** one amber, `#d9a35b`, with three jobs only: the slider line, the measured points on the chart, and the hover/focus underline. Links are underlined text, never colored blocks.
4. **Type:** Newsreader (variable, wght axis only, to keep it at 58 KB) for headings, prose, and the numbers inside prose (tabular lining figures). Martian Mono (variable, wdth + wght) only for log lines and slider readouts, set narrow (wdth 87.5) so they fit on a phone. Both self-hosted Latin woff2, font-display: swap, the serif preloaded.
5. **Layout:** left-aligned single column (~66ch prose), images allowed wider. A left margin carries the stage number 00–07 like a log margin. Nothing centered. Square corners, 1px hairline rules, no cards, no shadows, no blur panels, no icons, no emoji.
6. **Signature moment:** the compare slider itself: the hero is first light against the latest stack, and the handle carries each side's frames, minutes, FWHM and SNR. (A red-light night-vision mode was built and then removed on 2026-10-17 at Andrew's request.)
7. **Slider:** hand-written `<compare-slider>`: clip-path reveal, role=slider with aria-valuenow and an aria-valuetext that says what is showing in words, arrows/Home/End/PageUp/PageDown, pointer + touch, 44px invisible hit area around a 1px amber line with dark outlines either side so it shows over M31's core. Wide screens: two mono readouts hang off the handle (before on the left, after on the right), clamped and flipped inward near the edges. Under 600px the readouts sit in a fixed two-column caption row under the image and the side that's showing more is emphasized as you drag.
8. **Data voice:** stage meta lines and the per-night conditions strip are mono log entries: `2026-09-25 20:30–04:50 UTC · 1249 × 10 s · 9.4″ · moon 96 % · gusts 23 mph · 31 % dropped`. Every figure comes from journal.json; missing values print "—".
9. **Chart:** small inline SVG, SNR against total minutes. Processing-only stages share an x and are drawn as a short vertical run; a faint dashed √t curve, anchored on the first measured point, sits behind; points below the curve are hollow. A visually hidden table carries the same numbers for screen readers.
10. **Motion and copy:** almost none. Images reserve space with width/height and decode normally; the hero alone shows its tiny placeholder as a background until it loads (no fade animation). Copy is first person, short and plain, no marketing verbs.

---

## Critique summary (independent agent, before revision)

Passed the anti-AI checklist (no gradients, glass, emoji, centering, rounded-xl, Inter/Space Grotesk, stock icons or "dive in" copy) but read as the default "tasteful dark editorial" look. Changes made:

- **Type:** JetBrains Mono called the "Inter of monospace". Replaced with Martian Mono, used narrow, only for log lines and readouts; prose numbers stay in Newsreader.
- **Palette:** near-black + amber called stock, and "sodium-lamp amber" probably untrue (Belfast is mostly white LED now). Dropped the sodium claim; the ground is now sampled from my own live-stack sky; amber limited to three named jobs.
- **Red mode rationale:** the S30 Pro has no eyepiece. Rationale changed to the phone in the garden. Specified exact tokens, no muted shade, head script before paint, `?red` not persisted, no transition, image brightness capped.
- **SVG color-matrix filter** would repaint on every drag and stutter on Safari. Replaced with grayscale + multiply layer.
- **Readouts don't fit at 390 px** and a 1px handle is an invisible touch target. Added 44px hit area, outlined line, edge clamping, caption row under 600px, worded aria-valuetext.
- **Blur-up placeholder** contradicts "no blur". Kept only for the hero (the spec requires it) as a static background, no fade.
- **Chart:** stacked x for processing-only stages, hollow points below √t, hidden data table.
- Not adopted: clouded-out nights as empty rows, a running-total margin and verbatim `history.log` lines. journal.json has no data for them and I won't invent it. Worth adding to the Phase A schema later.
