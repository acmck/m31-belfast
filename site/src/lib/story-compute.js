/* story-compute.js: every number the /story page prints or plots, derived from the journal data.
   A classic script with no dependencies. It defines one global, StoryCompute, so the same source runs
   inline in the page, in the Astro build (story.astro evaluates it to fill the copy) and in the node check.
   No measured value is written here: they all arrive in `input` (built from journal.json and the
   story metrics files by story.astro). */
var StoryCompute = (function () {
  'use strict';

  /** 'k5d1q2' -> 'kkkkkdqq' */
  function decodeRle(r) {
    var out = '', re = /([a-z])(\d+)/g, m;
    while ((m = re.exec(r))) out += new Array(+m[2] + 1).join(m[1]);
    return out;
  }

  /** count of status chars in str[from, to) */
  function count(str, chars, from, to) {
    var n = 0, a = from || 0, b = to === undefined ? str.length : to;
    for (var i = a; i < b; i++) if (chars.indexOf(str[i]) >= 0) n++;
    return n;
  }

  function pct(a, b) { return (a / b - 1) * 100; }

  /**
   * input = {
   *   nights: [{id, frames_total, frames_kept, qa_rejected, scope_drop_rate, sky_adu, span_min, ...}],
   *   frames: {nightId: {slots, saved, scope_dropped}},
   *   rle: [string], stepStrips: {stepKey: {nightId: rleIndex}},
   *   steps: {stepKey: {frames, minutes, fwhm, snr}},
   *   anchor: {minutes, snr},                    // first measured point of journal.chart
   *   beats: [{strip: stepKey|null, img: stepKey, side: nightId|null, main: stepKey|null,
   *            sides: [stepKey], counter: stepKey|null}]
   * }
   */
  function build(input) {
    var nights = input.nights, ids = nights.map(function (n) { return n.id; });
    var stepKeys = Object.keys(input.steps);

    // ---- strips, decoded per step ----
    var strips = {};
    Object.keys(input.stepStrips).forEach(function (st) {
      strips[st] = {};
      Object.keys(input.stepStrips[st]).forEach(function (nid) {
        strips[st][nid] = decodeRle(input.rle[input.stepStrips[st][nid]]);
      });
    });

    // ---- the sqrt(t) curve, anchored as on the site's chart ----
    var k = input.anchor.snr / Math.sqrt(input.anchor.minutes);
    function expect(t) { return k * Math.sqrt(t); }
    var points = {};
    stepKeys.forEach(function (key) {
      var s = input.steps[key], e = expect(s.minutes);
      points[key] = { t: s.minutes, snr: s.snr, exp: e, below: s.snr < e - 1e-9 };
    });

    // ---- per-beat tile states ----
    // For each beat and night: the strip, the first not-yet-copied slot, and counts of what is visible.
    var beats = input.beats.map(function (b) {
      var out = { nights: {}, present: [] };
      if (!b.strip) return out;
      ids.forEach(function (nid) {
        var s = strips[b.strip][nid];
        if (!s) return;
        var firstU = s.indexOf('u');
        var end = firstU < 0 ? s.length : firstU;
        out.present.push(nid);
        out.nights[nid] = {
          strip: s, firstU: firstU,
          k: count(s, 'k', 0, end), q: count(s, 'q', 0, end), z: count(s, 'z', 0, end), d: count(s, 'd', 0, end),
          u: count(s, 'u'), hiddenSlots: s.length - end, hiddenD: count(s, 'd', end),
          rejectedDrawn: count(s, 'qz', 0, end)
        };
      });
      return out;
    });

    // counter = frames in that beat's stack: the main stack, or one night's own stack on a side beat
    var counters = input.beats.map(function (b, i) {
      var t = beats[i];
      if (!b.strip) return 0;
      if (b.side) return t.nights[b.side].k;
      return t.present.reduce(function (a, nid) { return a + t.nights[nid].k; }, 0);
    });

    // ---- transitions: what lands, flips, hides ----
    var transitions = [];
    for (var i = 0; i + 1 < beats.length; i++) {
      var A = beats[i], B = beats[i + 1], tr = {};
      ids.forEach(function (nid) {
        var a = A.nights[nid], b = B.nights[nid];
        if (!a && !b) return;
        var n = (a || b).strip.length, c = { land: 0, landK: 0, landQ: 0, landD: 0, hide: 0, kq: 0, qk: 0 };
        for (var j = 0; j < n; j++) {
          var va = a && (a.firstU < 0 || j < a.firstU), vb = b && (b.firstU < 0 || j < b.firstU);
          var ca = va ? a.strip[j] : '', cb = vb ? b.strip[j] : '';
          var fa = ca === 'k' ? 'k' : (ca === 'q' || ca === 'z') ? 'q' : ca;
          var fb = cb === 'k' ? 'k' : (cb === 'q' || cb === 'z') ? 'q' : cb;
          if (!fa && fb) { c.land++; if (fb === 'k') c.landK++; else if (fb === 'q') c.landQ++; else c.landD++; }
          else if (fa && !fb) c.hide++;
          else if (fa === 'k' && fb === 'q') c.kq++;
          else if (fa === 'q' && fb === 'k') c.qk++;
        }
        tr[nid] = c;
      });
      transitions.push(tr);
    }

    // ---- totals over the final beat ----
    var last = beats[beats.length - 1];
    var totals = { slots: 0, saved: 0, dropped: 0, qaRejected: 0, empties: 0, qaKept: 0 };
    ids.forEach(function (nid) {
      var f = input.frames[nid];
      totals.slots += f.slots; totals.saved += f.saved; totals.dropped += f.scope_dropped;
    });
    nights.forEach(function (n) { totals.qaRejected += n.qa_rejected; totals.qaKept += n.frames_kept; });
    last.present.forEach(function (nid) { totals.empties += last.nights[nid].z; });
    totals.rejectedInclEmpty = totals.qaRejected + totals.empties;

    // ---- the 16 Oct partial copy (the beat whose strip still has frames on the scope) ----
    var partial = null;
    beats.forEach(function (t, bi) {
      if (partial) return;
      t.present.forEach(function (nid) {
        var x = t.nights[nid];
        if (partial || x.firstU < 0) return;
        var fin = last.nights[nid] ? last.nights[nid].strip : null;
        partial = {
          beat: bi, night: nid, firstU: x.firstU,
          copied: x.k + x.q + x.z, passed: x.k + x.z, rejected: x.q, stillOnScope: x.u,
          visibleD: x.d, hiddenD: x.hiddenD, hiddenSlots: x.hiddenSlots,
          todayRejectsAmongCopied: fin ? count(fin, 'q', 0, x.firstU) : null,
          revealK: fin ? count(fin, 'k', x.firstU) : null,
          revealQ: fin ? count(fin, 'qz', x.firstU) : null
        };
      });
    });

    // ---- is each rejected set a subset of the previous step's (for the "still rejected" note) ----
    function subsetOf(stA, stB, nid) { // rejected in A all rejected in B?
      var a = strips[stA] && strips[stA][nid], b = strips[stB] && strips[stB][nid];
      if (!a || !b) return false;
      for (var j = 0; j < a.length; j++) if ((a[j] === 'q' || a[j] === 'z') && !(b[j] === 'q' || b[j] === 'z')) return false;
      return true;
    }

    var S = input.steps;
    var derived = {
      fpVsFl: pct(S['first-pipeline'].snr, S['first-light'].snr),
      rvVsFp: pct(S['review'].snr, S['first-pipeline'].snr),
      tnVsRv: pct(S['two-nights'].snr, S['review'].snr),
      thVsTn: pct(S['three-nights'].snr, S['two-nights'].snr),
      thVsFl: pct(S['three-nights'].snr, S['first-light'].snr),
      culled: S['first-light'].frames - S['first-pipeline'].frames,
      restored: S['review'].frames - S['first-pipeline'].frames,
      reviewSubset: subsetOf('review', 'first-pipeline', ids[0])
    };

    // axes: minutes rounded up to 40s, SNR to even numbers with headroom
    var maxT = 0, maxS = 0;
    stepKeys.forEach(function (key) { maxT = Math.max(maxT, S[key].minutes); maxS = Math.max(maxS, S[key].snr); });
    var axes = { xMax: Math.ceil(maxT / 40) * 40, yMax: Math.ceil(maxS * 1.1 / 2) * 2 };

    return {
      k: k, expect: expect, points: points, strips: strips, beats: beats, counters: counters,
      transitions: transitions, totals: totals, partial: partial, derived: derived, axes: axes,
      decodeRle: decodeRle, count: count, pct: pct
    };
  }

  return { build: build, decodeRle: decodeRle, count: count, pct: pct };
})();
