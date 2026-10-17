/* story-engine.js: the /story page engine, adapted from the visual-story 2D engine.
   The scene (image layers, tile wall, SNR track, counters, progress) is a pure function of one scalar `s`.
   Navigation tweens `s` to an integer; reverse playback falls out by construction. Copy is HTML. */
(function () {
  'use strict';
  var D = JSON.parse(document.getElementById('story-data').textContent);
  var C = StoryCompute.build(D.input);
  var BEATS = D.beats, CFG = D.input.beats, N = BEATS.length;
  var NIGHTS = D.input.nights, IDS = NIGHTS.map(function (n) { return n.id; });
  var $ = function (id) { return document.getElementById(id); };

  /* ---------- one easing curve, no overshoot ---------- */
  function bez(x1, y1, x2, y2) {
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx, cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    function sx(t) { return ((ax * t + bx) * t + cx) * t; }
    function sy(t) { return ((ay * t + by) * t + cy) * t; }
    function dx(t) { return (3 * ax * t + 2 * bx) * t + cx; }
    return function (x) {
      if (x <= 0) return 0; if (x >= 1) return 1;
      var t = x, i, e, d;
      for (i = 0; i < 8; i++) { e = sx(t) - x; if (Math.abs(e) < 1e-6) return sy(t); d = dx(t); if (Math.abs(d) < 1e-6) break; t -= e / d; }
      var lo = 0, hi = 1; t = x;
      for (i = 0; i < 40; i++) { e = sx(t); if (Math.abs(e - x) < 1e-6) break; if (e < x) lo = t; else hi = t; t = (lo + hi) / 2; }
      return sy(t);
    };
  }
  var E = bez(0.45, 0, 0.2, 1);
  function cl(x, a, b) { a = a === undefined ? 0 : a; b = b === undefined ? 1 : b; return x < a ? a : x > b ? b : x; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function seg(u, a, b) { return E(cl((u - a) / (b - a))); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function fmt(n) { return Math.round(n).toLocaleString('en-US'); }

  /* ---------- tokens ---------- */
  function hex(h) { return [parseInt(h.substr(1, 2), 16), parseInt(h.substr(3, 2), 16), parseInt(h.substr(5, 2), 16)]; }
  var T = {
    ground: hex('#211f1c'), ink: hex('#e8e2d6'), ink2: hex('#a39b8c'), rule: hex('#4a453d'), accent: hex('#d9a35b'),
    kept: hex('#8c8476'), moon: hex('#7f8489'), reject: hex('#4f4a42'), drop: hex('#6b6458')
  };
  var colCache = {};
  /* color c at alpha a, composited over the ground, as an opaque css string */
  function over(c, a) {
    var g = T.ground, r = Math.round(g[0] + (c[0] - g[0]) * a), gg = Math.round(g[1] + (c[1] - g[1]) * a), b = Math.round(g[2] + (c[2] - g[2]) * a);
    var key = (r << 16) | (gg << 8) | b, s = colCache[key];
    if (!s) { s = 'rgb(' + r + ',' + gg + ',' + b + ')'; colCache[key] = s; }
    return s;
  }
  function mix(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
  var MONO = '"Martian Mono","Martian Fallback",ui-monospace,Menlo,monospace';

  /* ---------- environment ---------- */
  var root = document.documentElement;
  var mqReduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  var reduced = !!mqReduce.matches;
  if (mqReduce.addEventListener) mqReduce.addEventListener('change', function (e) { reduced = e.matches; dirty = true; kick(); });
  var coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  var hint = $('hint');
  if (coarse) hint.textContent = 'Tap or swipe';
  function vw() { return window.innerWidth; }
  function vh() { return window.innerHeight; }
  function isPhone() { return vw() < 900; }

  /* ---------- DOM ---------- */
  var stageEl = $('stage'), copyEl = $('copy'), eb = $('eb'), hl = $('hl'), ld = $('ld'), more = $('more'), logl = $('logl'),
      noteEl = $('note'), foot = $('foot'), capEl = $('cap'), cntv = $('cntv'), cntc = $('cntc'), rov = $('rov'), ros = $('ros'),
      live = $('live'), cur = $('cur'), prog = $('progress'), prevB = $('prev'), nextB = $('next'), logbtn = $('logbtn'),
      keyEl = $('key'), key2 = $('key2'), wallCv = $('wall'), trackCv = $('track'), rail = $('rail'), trackbox = $('trackbox'),
      well = $('well');
  var wctx = wallCv.getContext && wallCv.getContext('2d'), tctx = trackCv.getContext && trackCv.getContext('2d');
  if (!wctx || !tctx) root.classList.add('no-canvas');

  var segs = [];
  BEATS.forEach(function (b, i) {
    var el = document.createElement('button');
    el.type = 'button';
    el.setAttribute('aria-label', 'Step ' + (i + 1) + ' of ' + N + ': ' + b.n);
    el.appendChild(document.createElement('i'));
    el.addEventListener('click', function (e) { e.stopPropagation(); go(i); });
    prog.appendChild(el); segs.push(el);
  });

  /* ---------- images: current beat and its neighbors only, decoded before they fade in ---------- */
  var layers = {};
  Array.prototype.forEach.call(well.querySelectorAll('picture'), function (p) {
    var img = p.querySelector('img'), src = p.querySelector('source');
    var initial = !!img.getAttribute('src');
    layers[p.getAttribute('data-step')] = { pic: p, img: img, src: src, requested: initial, readyAt: initial ? -1e9 : null, op: initial ? 1 : 0, z: '' };
  });
  function ensure(step) {
    var L = layers[step];
    if (!L || L.requested) return;
    L.requested = true;
    L.src.setAttribute('srcset', L.src.getAttribute('data-srcset'));
    L.img.setAttribute('srcset', L.img.getAttribute('data-srcset'));
    L.img.setAttribute('src', L.img.getAttribute('data-src'));
    var done = function () { if (L.readyAt === null) { L.readyAt = performance.now(); dirty = true; kick(); } };
    if (L.img.decode) L.img.decode().then(done, function () { L.img.addEventListener('load', done, { once: true }); });
    else L.img.addEventListener('load', done, { once: true });
  }
  function loadAround(k) {
    ensure(CFG[k].img);
    if (k + 1 < N) ensure(CFG[k + 1].img);
    if (k > 0) ensure(CFG[k - 1].img);
  }
  function setAlt(k) {
    Object.keys(layers).forEach(function (key) { layers[key].img.alt = key === CFG[k].img ? BEATS[k].alt : ''; });
  }
  function ramp(L, now) { return L.readyAt === null ? 0 : E(cl((now - L.readyAt) / 600)); }

  /* ---------- state ---------- */
  var s = 0, target = 0, tw = null, navigated = false, dirty = true;
  var rmImg = null; // reduced motion: {from, to, t0}

  function hashBeat() { var m = /^#(\d{1,2})$/.exec(location.hash || ''); if (!m) return -1; var v = +m[1]; return v >= 1 && v <= N ? v - 1 : -1; }

  /* ---------- copy ---------- */
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  function fillEyebrow(node, k) {
    var b = BEATS[k];
    node.textContent = '';
    node.appendChild(el('span', 'num', pad(k + 1)));
    if (b.eb.date) { node.appendChild(document.createTextNode(' · ')); node.appendChild(el('span', 'date', b.eb.date)); }
    node.appendChild(document.createTextNode(' · ' + b.eb.kind));
  }
  function fillCopy(k) {
    var b = BEATS[k];
    fillEyebrow(eb, k);
    hl.textContent = b.h; ld.textContent = b.lede;
    logl.textContent = '';
    b.log.forEach(function (l) { logl.appendChild(el('li', '', l)); });
    if (b.note) {
      noteEl.textContent = '';
      noteEl.appendChild(el('span', 'nl', b.note.label));
      noteEl.appendChild(document.createTextNode(b.note.text));
      noteEl.hidden = false;
    } else { noteEl.hidden = true; noteEl.textContent = ''; }
    foot.textContent = '';
    b.foot.forEach(function (f) { foot.appendChild(el('p', '', f)); });
    capEl.textContent = b.cap;
    cntc.textContent = b.cnt;
    copyEl.setAttribute('aria-label', 'Step ' + (k + 1) + ' of ' + N + ': ' + b.n);
    filled = k;
  }
  var filled = 0;
  var parts = [eb, hl, ld, more, capEl, cntc];
  var copyAnim = null; // {to, t0, from: [opacities]}
  function startCopy(k) {
    var now = performance.now();
    // the caption and the counter label only fade when their text changes
    var keep = [false, false, false, false, capEl.textContent === BEATS[k].cap, cntc.textContent === BEATS[k].cnt];
    copyAnim = { to: k, t0: now, keep: keep, from: parts.map(function (p) { return p.style.opacity === '' ? 1 : +p.style.opacity; }) };
  }
  function stepCopy(now) {
    if (!copyAnim) return false;
    var t = now - copyAnim.t0, a = copyAnim, OUT = reduced ? 100 : 270, SWAP = reduced ? 100 : 540, IN = reduced ? 150 : 560, STAG = reduced ? 0 : 70;
    if (t < SWAP) {
      var p = cl(t / OUT);
      parts.forEach(function (el, i) {
        if (a.keep[i] && a.from[i] >= 1) return;
        el.style.opacity = (a.from[i] * (1 - E(p))).toFixed(3);
        el.style.transform = reduced ? 'none' : 'translateY(' + (-8 * E(p)).toFixed(2) + 'px)';
      });
      return true;
    }
    if (filled !== a.to) fillCopy(a.to);
    var running = false;
    parts.forEach(function (el, i) {
      if (a.keep[i] && a.from[i] >= 1) { el.style.opacity = '1'; el.style.transform = 'none'; return; }
      var q = cl((t - SWAP - Math.min(i, 3) * STAG) / IN), e = E(q);
      if (q < 1) running = true;
      el.style.opacity = e.toFixed(3);
      el.style.transform = reduced || q >= 1 ? 'none' : 'translateY(' + (12 * (1 - e)).toFixed(2) + 'px)';
    });
    if (!running) copyAnim = null;
    return running;
  }

  /* ---------- navigation ---------- */
  function go(k) {
    k = Math.max(0, Math.min(N - 1, k | 0));
    if (k === target) return;
    var now = performance.now(), prev = target;
    target = k;
    loadAround(k);
    if (reduced) { rmImg = { from: CFG[prev].img, to: CFG[k].img, t0: now }; tw = null; s = k; }
    else {
      var dist = Math.abs(k - s), back = k < s;
      var dur = dist <= 1 ? 1800 * dist : Math.min(2600, 1800 * dist);
      if (back) dur *= 0.8;
      tw = { s0: s, s1: k, t0: now, dur: Math.max(dur, 200) };
    }
    startCopy(k);
    setAlt(k);
    cur.textContent = pad(k + 1);
    live.textContent = BEATS[k].live;
    prevB.disabled = k === 0; nextB.disabled = k === N - 1;
    segs.forEach(function (b, i) { if (i === k) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current'); });
    if (!navigated) { navigated = true; hint.classList.add('gone'); }
    if (location.hash !== '#' + (k + 1)) { try { history.replaceState(null, '', '#' + (k + 1)); } catch (e) { /* file: or sandbox */ } }
    dirty = true; kick();
  }
  prevB.addEventListener('click', function (e) { e.stopPropagation(); go(target - 1); });
  nextB.addEventListener('click', function (e) { e.stopPropagation(); go(target + 1); });
  logbtn.addEventListener('click', function (e) {
    e.stopPropagation();
    var on = copyEl.classList.toggle('open');
    logbtn.setAttribute('aria-expanded', on ? 'true' : 'false');
    logbtn.textContent = on ? 'Close log' : 'Log';
  });
  window.addEventListener('keydown', function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    var tag = (e.target && e.target.tagName) || '';
    switch (e.key) {
      case 'ArrowRight': case 'PageDown': go(target + 1); e.preventDefault(); break;
      case 'ArrowLeft': case 'PageUp': go(target - 1); e.preventDefault(); break;
      case ' ': case 'Spacebar': if (tag === 'BUTTON' || tag === 'A') return; go(target + 1); e.preventDefault(); break;
      case 'Home': go(0); e.preventDefault(); break;
      case 'End': go(N - 1); e.preventDefault(); break;
    }
  });
  var pd = null;
  function ignoreTarget(t) { return t.closest('button,a') || (copyEl.classList.contains('open') && t.closest('#more')); }
  stageEl.addEventListener('pointerdown', function (e) { if (ignoreTarget(e.target)) return; pd = { x: e.clientX, y: e.clientY, t: performance.now() }; });
  stageEl.addEventListener('pointerup', function (e) {
    if (!pd) return;
    var dx = e.clientX - pd.x, dy = e.clientY - pd.y, adx = Math.abs(dx), ady = Math.abs(dy), p = pd; pd = null;
    if (ignoreTarget(e.target)) return;
    if (adx > 40 && adx > 1.3 * ady) { go(target + (dx < 0 ? 1 : -1)); return; }
    if (adx < 10 && ady < 10 && performance.now() - p.t < 600) go(target + (e.clientX > vw() * 0.35 ? 1 : -1));
  });
  stageEl.addEventListener('pointercancel', function () { pd = null; });
  window.addEventListener('hashchange', function () { var k = hashBeat(); if (k >= 0 && k !== target) go(k); });
  setTimeout(function () { hint.classList.add('gone'); }, 6000);

  /* =========================================================================
     Tile wall: layout computed once per resize from the final beat
     ========================================================================= */
  var W = { ok: false };
  // per-beat, per-night codes over layout slots: 0 hidden, 1 kept, 2 rejected (q or z), 3 dropped
  function nightCodes(strip, firstU, b, collapse) {
    var n = strip.length, codes = [], run = 0, i, j;
    var vis = function (j) { return firstU < 0 || j < firstU; };
    if (collapse) {
      var saved = [];
      for (j = 0; j < n; j++) {
        if (strip[j] === 'd') { if (vis(j)) run++; continue; }
        saved.push(vis(j) ? (strip[j] === 'k' ? 1 : 2) : 0);
      }
      for (i = 0; i < saved.length; i += b) codes.push(binned(saved.slice(i, i + b)));
      return { codes: codes, run: run };
    }
    for (i = 0; i < n; i += b) {
      var grp = [];
      for (j = i; j < Math.min(n, i + b); j++) grp.push(vis(j) ? (strip[j] === 'k' ? 1 : strip[j] === 'd' ? 3 : 2) : 0);
      codes.push(binned(grp));
    }
    return { codes: codes, run: 0 };
  }
  function binned(g) {
    if (g.length === 1) return g[0];
    var c = [0, 0, 0, 0];
    g.forEach(function (x) { c[x]++; });
    if (c[1] + c[2] + c[3] === 0) return 0;
    // the most common visible state; ties go kept > rejected > dropped
    if (c[1] >= c[2] && c[1] >= c[3]) return 1;
    if (c[2] >= c[3]) return 2;
    return 3;
  }
  var wallNoPh = false; // short phones drop the placeholder rows
  function layoutWall(boxW, boxH, phone, dpr) {
    var J = IDS.length, hL = phone ? 12 : 14, gap = 4;
    var last = C.beats[N - 1], hasPh = !wallNoPh && CFG.some(function (c) { return c.placeholder; });
    var nSlots = IDS.map(function (id) { return D.input.frames[id].slots; });
    var nSaved = IDS.map(function (id) { return D.input.frames[id].saved; });
    function height(p, C_, b, collapse, ph) {
      var rows = 0;
      for (var j = 0; j < J; j++) rows += collapse ? Math.ceil(Math.ceil(nSaved[j] / b) / C_) + 1 : Math.ceil(Math.ceil(nSlots[j] / b) / C_);
      return p * rows + hL * J + gap * (J - 1) + (ph ? gap + hL + 3 * p : 0);
    }
    var pick = null, bs = [1, 10, 100], ps = [8, 7, 6, 5, 4];
    outer:
    for (var bi = 0; bi < bs.length; bi++) {
      var b = bs[bi];
      var tries = [[false, hasPh], [false, false]];
      if (phone) tries.push([true, hasPh], [true, false]);
      for (var ti = 0; ti < tries.length; ti++) {
        var col = tries[ti][0], ph = tries[ti][1];
        var plist = col ? [4] : ps;
        for (var pi = 0; pi < plist.length; pi++) {
          var p = plist[pi], C_ = Math.floor((boxW + 1) / p);
          if (C_ < 4) continue;
          if (height(p, C_, b, col, ph) <= boxH) { pick = { p: p, C: C_, b: b, collapse: col, ph: ph }; break outer; }
        }
      }
    }
    if (!pick) pick = { p: 4, C: Math.max(4, Math.floor((boxW + 1) / 4)), b: 100, collapse: false, ph: false };
    // snap pitch to whole device pixels
    var P = Math.max(2, Math.round(pick.p * dpr)), gpx = Math.max(1, Math.round(dpr)), tile = P - gpx;
    var L = { p: pick.p, P: P, tile: tile, C: pick.C, b: pick.b, collapse: pick.collapse, ph: pick.ph, hL: hL, gap: gap, dpr: dpr,
              boxW: boxW, boxH: boxH, blocks: {}, order: IDS.slice() };
    // per-beat codes per night
    L.codes = C.beats.map(function (bt) {
      var o = {};
      IDS.forEach(function (id) {
        var x = bt.nights[id];
        o[id] = x ? nightCodes(x.strip, x.firstU, pick.b, pick.collapse) : null;
      });
      return o;
    });
    // geometry, bottom-up in date order
    var base = 0;
    IDS.forEach(function (id, j) {
      var nCodes = 0;
      for (var bb = 0; bb < N; bb++) if (L.codes[bb][id]) nCodes = Math.max(nCodes, L.codes[bb][id].codes.length);
      var rowsSaved = Math.ceil(nCodes / pick.C), R = rowsSaved + (pick.collapse ? 1 : 0);
      L.blocks[id] = { j: j, base: base, R: R, rowsSaved: rowsSaved, n: nCodes,
                       labelY: boxH - base - R * pick.p - hL };
      base += R * pick.p + hL + gap;
    });
    L.phY = boxH - base - 3 * pick.p; // placeholder block top (css px)
    L.phLabelY = L.phY - hL;
    // fresh (new this beat) flags and per-transition stagger
    L.fresh = L.codes.map(function (bt, bb) {
      var o = {};
      IDS.forEach(function (id) {
        if (!bt[id]) return;
        var prev = bb > 0 ? L.codes[bb - 1][id] : null, a = new Uint8Array(bt[id].codes.length);
        for (var i = 0; i < a.length; i++) a[i] = bt[id].codes[i] === 1 && !(prev && prev.codes[i]) ? 1 : 0;
        o[id] = a;
      });
      return o;
    });
    L.trans = [];
    for (var t = 0; t + 1 < N; t++) {
      var A = L.codes[t], B = L.codes[t + 1], keys = {}, flipRows = { up: {}, down: {} }, tr = {};
      IDS.forEach(function (id) {
        var a = A[id], bq = B[id]; if (!a && !bq) return;
        var n = (a || bq).codes.length;
        for (var i = 0; i < n; i++) {
          var ca = a ? a.codes[i] : 0, cb = bq ? bq.codes[i] : 0, row = Math.floor(i / pick.C);
          if (!ca !== !cb) keys[L.blocks[id].j * 100000 + row] = 1;
          else if (ca && cb && ca !== cb) {
            if (cb === 2) flipRows.down[L.blocks[id].base * 100000 + row] = 1; else flipRows.up[L.blocks[id].base * 100000 + row] = 1;
          }
        }
      });
      var landRank = rankMap(keys, false), downRank = rankMap(flipRows.down, true), upRank = rankMap(flipRows.up, false);
      IDS.forEach(function (id) {
        var a = A[id], bq = B[id]; if (!a && !bq) return;
        var n = (a || bq).codes.length, st = new Float32Array(n), j = L.blocks[id].j, bs_ = L.blocks[id].base;
        for (var i = 0; i < n; i++) {
          var ca = a ? a.codes[i] : 0, cb = bq ? bq.codes[i] : 0, row = Math.floor(i / pick.C);
          if (!ca !== !cb) st[i] = 0.10 + landRank.f(j * 100000 + row) * (0.70 - 0.10 - 0.18);
          else if (ca && cb && ca !== cb) st[i] = 0.10 + (cb === 2 ? downRank : upRank).f(bs_ * 100000 + row) * (0.55 - 0.10 - 0.12);
        }
        tr[id] = st;
      });
      // the run of dropped frames (phone collapse) lands with the block's first row
      L.trans.push(tr);
    }
    W = L; W.ok = true;
    return L;
  }
  function rankMap(obj, desc) {
    var ks = Object.keys(obj).map(Number).sort(function (a, b) { return desc ? b - a : a - b; });
    var idx = {}; ks.forEach(function (k, i) { idx[k] = i; });
    var n = Math.max(1, ks.length - 1);
    return { f: function (k) { return ks.length <= 1 ? 0 : idx[k] / n; } };
  }
  function dimOf(beat, id) { var sd = CFG[beat].side; return sd && sd !== id ? 0.6 : 1; }
  function moonOf(id) { var n = NIGHTS[IDS.indexOf(id)]; return n && n.moonUp; }

  var wallLabels = [];
  function drawWall(k, u) {
    var L = W, ctx = wctx;
    if (!L.ok || !ctx) return;
    var dpr = L.dpr, cw = wallCv.width, P = L.P, tile = L.tile, p = L.p;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cw, wallCv.height);
    wallLabels = [];
    var A = L.codes[k], B = L.codes[Math.min(N - 1, k + 1)], tr = L.trans[Math.min(k, N - 2)];
    var fA = L.fresh[k], fB = L.fresh[Math.min(N - 1, k + 1)];
    var settleT = 1 - seg(u, 0, 0.2), dimT = seg(u, 0.10, 0.70);
    var boxH = L.boxH;
    IDS.forEach(function (id) {
      var a = A[id], b = u > 0 ? B[id] : a; if (!a && !b) return;
      var blk = L.blocks[id], tint = moonOf(id) ? T.moon : T.kept;
      var dim = lerp(a ? dimOf(k, id) : dimOf(k + 1, id), b ? dimOf(Math.min(N - 1, k + 1), id) : dimOf(k, id), dimT);
      var n = (a || b).codes.length, st = tr && tr[id];
      var yBase = Math.round((boxH - blk.base) * dpr); // device px, bottom of block
      for (var i = 0; i < n; i++) {
        var ca = a ? a.codes[i] : 0, cb = b ? b.codes[i] : 0;
        if (!ca && !cb) continue;
        var col = i % L.C, row = Math.floor(i / L.C);
        var x = col * P, y = yBase - (row + 1) * P;
        var alpha = dim, dy = 0, c, outline = false, t0 = st ? st[i] : 0;
        if (!ca) { // lands
          var lt = seg(u, t0, t0 + 0.18); if (lt <= 0) continue;
          alpha *= lt;
          if (cb === 3) outline = true;
          else { c = cb === 1 ? (fB && fB[id] && fB[id][i] ? T.ink : tint) : T.reject; dy = -Math.round((1 - lt) * P); }
        } else if (!cb) { // hides in place
          var ht = 1 - seg(u, t0, t0 + 0.18); if (ht <= 0) continue;
          alpha *= ht;
          if (ca === 3) outline = true; else c = ca === 1 ? mix(tint, T.ink, (fA && fA[id] && fA[id][i] ? settleT : 0)) : T.reject;
        } else if (ca === 3) outline = true;
        else {
          var base = ca === cb ? (ca === 1 ? tint : T.reject) : mix(ca === 1 ? tint : T.reject, cb === 1 ? tint : T.reject, seg(u, t0, t0 + 0.12));
          var fr = fA && fA[id] && fA[id][i] ? settleT : 0;
          c = fr > 0 ? mix(base, T.ink, fr) : base;
        }
        if (outline) {
          ctx.strokeStyle = over(T.drop, alpha); ctx.lineWidth = 1;
          ctx.strokeRect(x + 0.5, y + 0.5, tile - 1, tile - 1);
        } else {
          ctx.fillStyle = over(c, alpha);
          ctx.fillRect(x, y + dy, tile, tile);
          if (p >= 6 && (cb === 2 || (!cb && ca === 2))) { // third cue for rejected on big tiles
            ctx.strokeStyle = over(T.rule, alpha); ctx.beginPath(); ctx.moveTo(x, y + dy); ctx.lineTo(x + tile, y + dy + tile); ctx.stroke();
          }
        }
      }
      // phone: dropped frames collapsed into one hollow run at the top of the block
      var ra = a ? a.run : 0, rb = b ? b.run : 0;
      if (L.collapse && (ra || rb)) {
        var runA = ra ? 1 : 0, runB = rb ? 1 : 0, rAlpha = lerp(runA, runB, seg(u, 0.10, 0.40)) * dim;
        if (rAlpha > 0.003) {
          var ry = yBase - blk.R * P;
          ctx.strokeStyle = over(T.drop, rAlpha); ctx.lineWidth = 1;
          ctx.strokeRect(0.5, ry + 0.5, L.C * P - (P - tile) - 1, tile - 1);
        }
      }
      // block label
      var la = (a ? 1 : seg(u, 0.10, 0.30)) * dim;
      if (la > 0.003) {
        var text = NIGHTS[blk.j].short, runN = u < 0.5 ? ra : rb;
        drawLabel(ctx, text, 0, blk.labelY, la, 'left');
        if (L.collapse && runN) drawLabel(ctx, runN + ' DROPPED', L.boxW, blk.labelY, la, 'right', text);
      }
      // the part of a night still on the scope: reserved space with a label
      var hA = cutInfo(k, id), hB = cutInfo(Math.min(N - 1, k + 1), id);
      var ca_ = hA ? 1 : 0, cb_ = (u > 0 ? hB : hA) ? 1 : 0;
      var cutA = lerp(ca_, cb_, cb_ > ca_ ? seg(u, 0.40, 0.70) : seg(u, 0.10, 0.30)) * dim;
      var info = hA || hB;
      if (cutA > 0.003 && info) {
        var firstRow = Math.ceil(info.firstIdx / L.C), rows = blk.rowsSaved - firstRow;
        if (rows > 0) {
          var top = boxH - blk.base - blk.rowsSaved * p, h = rows * p;
          drawWrapped(ctx, info.n + ' still on the scope', top + h / 2, cutA, L.boxW);
        }
      }
    });
    // placeholder for the next clear night
    if (L.ph) {
      var phA = 0;
      if (CFG[k].placeholder) phA = 1; else if (u > 0 && CFG[Math.min(N - 1, k + 1)].placeholder) phA = seg(u, 0.10, 0.50);
      if (phA > 0.003) {
        ctx.save();
        ctx.setLineDash([4 * dpr, 3 * dpr]);
        ctx.strokeStyle = over(T.rule, phA * 1); ctx.lineWidth = Math.max(1, Math.round(dpr));
        var py = Math.round(L.phY * dpr) + 0.5;
        ctx.strokeRect(0.5, py, L.C * P - (P - tile) - 1, 3 * P - (P - tile) - 1);
        ctx.restore();
        drawLabel(ctx, 'NEXT CLEAR NIGHT', 0, L.phLabelY, phA, 'left');
      }
    }
    var r = wallCv.getBoundingClientRect();
    wallCv._labels = wallLabels.map(function (l) { return { text: l.text, x: r.left + l.x, y: r.top + l.y, w: l.w, h: l.h }; });
  }
  function cutInfo(beat, id) {
    var x = C.beats[beat].nights[id];
    if (!x || x.firstU < 0) return null;
    // index of the first hidden layout slot
    var codes = W.codes[beat][id].codes, firstIdx = codes.length;
    for (var i = codes.length - 1; i >= 0; i--) { if (codes[i]) break; firstIdx = i; }
    return { n: x.u, firstIdx: firstIdx };
  }
  function labelFont() { return '400 ' + (11) + 'px ' + MONO; }
  function drawLabel(ctx, text, x, y, a, align, leftText) {
    var dpr = W.dpr, fs = 11;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = labelFont();
    try { ctx.fontStretch = 'semi-condensed'; } catch (e) { /* older canvases */ }
    var w = ctx.measureText(text).width;
    var lx = align === 'right' ? x - w : x;
    if (leftText) { // drop the right label if it would touch the left one
      var lw = ctx.measureText(leftText).width;
      if (lx < lw + 10) { ctx.restore(); return; }
    }
    ctx.fillStyle = over(T.ink2, a);
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(text, lx, y + fs);
    ctx.restore();
    wallLabels.push({ text: text, x: lx, y: y + 1, w: w, h: fs });
  }
  function drawWrapped(ctx, text, cy, a, maxW) {
    var dpr = W.dpr, fs = 11, lh = fs + 4;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = labelFont();
    try { ctx.fontStretch = 'semi-condensed'; } catch (e) { /* ignore */ }
    text = text.toUpperCase();
    var words = text.split(' '), lines = [], line = '';
    words.forEach(function (wd) {
      var t = line ? line + ' ' + wd : wd;
      if (ctx.measureText(t).width > maxW - 8 && line) { lines.push(line); line = wd; } else line = t;
    });
    if (line) lines.push(line);
    ctx.fillStyle = over(T.ink2, a);
    var y0 = cy - (lines.length * lh) / 2;
    lines.forEach(function (l, i) {
      var w = ctx.measureText(l).width;
      ctx.fillText(l, 0, y0 + i * lh + fs);
      wallLabels.push({ text: l, x: 0, y: y0 + i * lh + 1, w: w, h: fs });
    });
    ctx.restore();
  }

  /* =========================================================================
     SNR track
     ========================================================================= */
  var TK = { ok: false };
  var PTS = C.points;
  var mainSeq = CFG.map(function (c) { return c.main; });
  function firstBeatOf(pred) { for (var i = 0; i < N; i++) if (pred(CFG[i])) return i; return N; }
  var mainFirst = {}, sideFirst = {};
  Object.keys(PTS).forEach(function (key) {
    mainFirst[key] = firstBeatOf(function (c) { return c.main === key; });
    sideFirst[key] = firstBeatOf(function (c) { return (c.sides || []).indexOf(key) >= 0; });
  });
  var curveBeat = firstBeatOf(function (c) { return c.main && c.main !== 'first-light'; });
  var flBeat = mainFirst['first-light'];
  function layoutTrack(w, h, phone, dpr) {
    var spark = h < 50;
    var right = w - (phone ? 112 : 156);
    TK = { ok: true, w: w, h: h, dpr: dpr, phone: phone, spark: spark,
           l: spark ? 2 : (phone ? 22 : 30), r: right, t: spark ? 4 : 8, b: h - (spark ? 4 : 18) };
    // phones: too narrow for a minutes axis, so each measured point gets its own evenly spaced slot,
    // in time order (ties by the beat that introduces it); desktop keeps real minutes
    Object.keys(PTS).forEach(function (k) { if (PTS[k].t0 === undefined) PTS[k].t0 = PTS[k].t; });
    if (phone) {
      var order = Object.keys(PTS).sort(function (a, b) {
        return (PTS[a].t0 - PTS[b].t0) || (Math.min(mainFirst[a], sideFirst[a]) - Math.min(mainFirst[b], sideFirst[b]));
      });
      order.forEach(function (k, i) { PTS[k].t = ((i + 0.5) / order.length) * C.axes.xMax; });
    } else {
      Object.keys(PTS).forEach(function (k) { PTS[k].t = PTS[k].t0; });
    }
    TK.x = function (t) { return TK.l + (t / C.axes.xMax) * (TK.r - TK.l); };
    TK.y = function (v) { return TK.b - (v / C.axes.yMax) * (TK.b - TK.t); };
    // where the dot crosses the first-light line on the beat that climbs past it
    TK.cross = null;
    for (var i = 1; i < N; i++) {
      var a = mainSeq[i - 1], b = mainSeq[i];
      if (!a || !b || a === b || a === 'first-light') continue;
      var fl = PTS['first-light'].snr, ya = PTS[a].snr, yb = PTS[b].snr;
      if (ya < fl && yb > fl) {
        var f = (fl - ya) / (yb - ya), lo = 0, hi = 1;
        for (var it = 0; it < 40; it++) { var mid = (lo + hi) / 2; if (E(mid) < f) lo = mid; else hi = mid; }
        TK.cross = { beat: i, u: 0.55 + 0.45 * lo, t: lerp(PTS[a].t, PTS[b].t, f) };
        break;
      }
    }
  }
  var trackLabels = [];
  function tText(ctx, text, x, y, color, a, align, size) {
    ctx.font = '400 ' + (size || (11)) + 'px ' + MONO;
    try { ctx.fontStretch = 'semi-condensed'; } catch (e) { /* ignore */ }
    var w = ctx.measureText(text).width, lx = align === 'right' ? x - w : align === 'center' ? x - w / 2 : x;
    ctx.fillStyle = over(color, a);
    ctx.fillText(text, lx, y);
    return { text: text, x: lx, y: y - (size || 11) + 1, w: w, h: (size || 11) };
  }
  function hit(r, list) { for (var i = 0; i < list.length; i++) { var o = list[i]; if (r.x < o.x + o.w && r.x + r.w > o.x && r.y < o.y + o.h && r.y + r.h > o.y) return true; } return false; }
  function drawTrack(k, u) {
    var ctx = tctx; if (!TK.ok || !ctx) return;
    var dpr = TK.dpr, x = TK.x, y = TK.y;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, trackCv.width, trackCv.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.textBaseline = 'alphabetic';
    trackLabels = [];
    var occ = [];
    var sv = k + u; // the scalar
    function at(beat) { return sv >= beat ? 1 : sv <= beat - 1 ? 0 : -1; } // -1: arriving in this transition
    // axes
    if (!TK.spark) {
      ctx.strokeStyle = over(T.rule, 1); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(TK.l + 0.5, TK.t); ctx.lineTo(TK.l + 0.5, TK.b + 0.5); ctx.lineTo(TK.r, TK.b + 0.5); ctx.stroke();
      var xt = TK.phone ? [] : [0, C.axes.xMax / 4, C.axes.xMax / 2, 3 * C.axes.xMax / 4, C.axes.xMax];
      xt.forEach(function (t, i) {
        var lab = i === xt.length - 1 ? t + ' min' : String(t);
        var r = tText(ctx, lab, x(t), TK.h - 3, T.ink2, 1, i === 0 ? 'left' : i === xt.length - 1 ? 'right' : 'center', 11);
        occ.push(r); trackLabels.push(r);
      });
      var yt = TK.phone ? [20] : [10, 20];
      yt.forEach(function (v) { var r = tText(ctx, String(v), TK.l - 6, y(v) + 4, T.ink2, 1, 'right', 11); occ.push(r); trackLabels.push(r); });
    }
    // first-light level line, drawn left to right on its first beat
    var FL = PTS['first-light'], flY = Math.round(y(FL.snr)) + 0.5;
    var lp = at(flBeat) === 1 ? 1 : at(flBeat) === 0 ? 0 : seg(u, 0.55, 1.0);
    var bright = 0, crossX = null;
    if (TK.cross) {
      crossX = x(TK.cross.t);
      bright = sv >= TK.cross.beat ? 1 : sv <= TK.cross.beat - 1 ? 0 : seg(u, TK.cross.u, TK.cross.u + 0.22);
    }
    if (lp > 0) {
      var xEnd = lerp(TK.l, TK.r, lp);
      ctx.lineWidth = 1;
      ctx.strokeStyle = over(T.ink2, 0.6);
      ctx.beginPath(); ctx.moveTo(TK.l, flY); ctx.lineTo(xEnd, flY); ctx.stroke();
      if (bright > 0 && crossX !== null) {
        ctx.strokeStyle = over(mix(T.ink2, T.ink, 1), lerp(0.6, 1, bright));
        ctx.beginPath(); ctx.moveTo(crossX, flY); ctx.lineTo(TK.r, flY); ctx.stroke();
      }
      if (!TK.spark) {
        var labA = cl((xEnd - TK.l) / 120);
        var flLab = 'first light ' + FL.snr.toFixed(1);
        var lc = bright > 0 ? mix(T.ink2, T.ink, bright) : T.ink2;
        var rr = tText(ctx, flLab, TK.l + 6, flY - 5, lc, labA, 'left', 11);
        occ.push(rr); trackLabels.push(rr);
      }
      // first light itself: an ink square, the scope's own stack
      var sqA = cl((xEnd - x(FL.t)) / 6 + 1);
      if (sqA > 0) { ctx.fillStyle = over(T.ink, sqA); ctx.fillRect(Math.round(x(FL.t)) - 2.5, flY - 2.5 - 0.5, 5, 5); occ.push({ x: x(FL.t) - 4, y: flY - 4, w: 8, h: 8 }); }
    }
    // sqrt(t) curve
    var cA = at(curveBeat) === 1 ? 0.55 : at(curveBeat) === 0 ? 0 : 0.55 * seg(u, 0.55, 1.0);
    if (TK.phone) cA = 0; // the curve needs a real time axis
    var lastPh = firstBeatOf(function (c) { return c.placeholder; });
    if (!TK.phone && lastPh < N && sv > lastPh - 1) cA = lerp(cA, 0.8, sv >= lastPh ? 1 : seg(u, 0.55, 1.0));
    if (cA > 0) {
      ctx.save();
      ctx.setLineDash([4, 5]); ctx.lineWidth = 1.25; ctx.strokeStyle = over(T.ink2, cA);
      ctx.beginPath();
      for (var i = 0; i <= 64; i++) { var t = (i / 64) * C.axes.xMax, py = y(C.expect(t)); if (i === 0) ctx.moveTo(x(t), py); else ctx.lineTo(x(t), py); }
      ctx.stroke(); ctx.restore();
    }
    // points and the dot
    var trail = [];
    for (var bi = 1; bi < N; bi++) {
      var a = mainSeq[bi - 1], b = mainSeq[bi];
      if (!a || !b || a === b) continue;
      var pr = sv >= bi ? 1 : sv <= bi - 1 ? 0 : seg(sv - (bi - 1), 0.55, 1.0);
      if (pr > 0) trail.push({ a: a, b: b, p: pr });
    }
    ctx.lineWidth = 1; ctx.strokeStyle = over(T.accent, 0.4);
    trail.forEach(function (tr) {
      var ax = x(PTS[tr.a].t), ay = y(PTS[tr.a].snr), bx = x(PTS[tr.b].t), by = y(PTS[tr.b].snr);
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(lerp(ax, bx, tr.p), lerp(ay, by, tr.p)); ctx.stroke();
    });
    function pt(key, alpha, r, filled) {
      if (alpha <= 0) return;
      var px = x(PTS[key].t), py = y(PTS[key].snr);
      ctx.beginPath(); ctx.arc(px, py, r + 2, 0, Math.PI * 2); ctx.fillStyle = over(T.ground, 1); ctx.fill();
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2);
      if (filled) { ctx.fillStyle = over(T.accent, alpha); ctx.fill(); }
      else { ctx.fillStyle = over(T.ground, 1); ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = over(T.accent, alpha); ctx.beginPath(); ctx.arc(px, py, r - 1, 0, Math.PI * 2); ctx.stroke(); }
      occ.push({ x: px - r - 2, y: py - r - 2, w: 2 * r + 4, h: 2 * r + 4 });
    }
    Object.keys(PTS).forEach(function (key) {
      if (key === 'first-light') return;
      var mf = mainFirst[key], sf = sideFirst[key];
      if (mf < N) { var al = sv >= mf ? 1 : sv <= mf - 1 ? 0 : seg(u, 0.92, 1.0); pt(key, al, 4, !PTS[key].below); }
      else if (sf < N) { var as = sv >= sf ? 1 : sv <= sf - 1 ? 0 : seg(u, 0.55, 0.85); pt(key, as, PTS[key].below ? 4 : 3, !PTS[key].below); }
    });
    // the moving dot
    var from = mainSeq[k], to = mainSeq[Math.min(N - 1, k + 1)];
    if (u === 0) to = from;
    if (from || to) {
      var dp = seg(u, 0.55, 1.0), fk = from || to, tk = to || from;
      if (fk === 'first-light' && tk === 'first-light') fk = null;
      var dotA = 1;
      if (!from) dotA = 0; else if (from === 'first-light') dotA = tk !== 'first-light' ? seg(u, 0.55, 0.62) : 0;
      if (fk && dotA > 0) {
        var dx_ = lerp(x(PTS[fk].t), x(PTS[tk].t), dp), dy_ = lerp(y(PTS[fk].snr), y(PTS[tk].snr), dp);
        ctx.beginPath(); ctx.arc(dx_, dy_, 7.5, 0, Math.PI * 2); ctx.fillStyle = over(T.ground, dotA); ctx.fill();
        ctx.beginPath(); ctx.arc(dx_, dy_, 5.5, 0, Math.PI * 2); ctx.fillStyle = over(T.accent, dotA); ctx.fill();
        occ.push({ x: dx_ - 8, y: dy_ - 8, w: 16, h: 16 });
      }
    }
    // the sqrt(t) label: first spot along the curve that touches nothing
    if (cA > 0 && !TK.spark) {
      var cands = [0.42, 0.36, 0.5, 0.3, 0.58, 0.66, 0.24];
      ctx.font = '400 12px ' + MONO;
      for (var ci = 0; ci < cands.length; ci++) {
        var tt = cands[ci] * C.axes.xMax, lx = x(tt) + 4, ly = y(C.expect(tt)) + 15;
        var rect = { x: lx, y: ly - 11, w: ctx.measureText('√t').width + 2, h: 12 };
        if (rect.y + rect.h > TK.b - 1 || hit(rect, occ)) continue;
        var rr2 = tText(ctx, '√t', lx, ly, T.ink2, Math.min(1, cA / 0.55 * 0.9), 'left', 12);
        occ.push(rr2); trackLabels.push(rr2);
        break;
      }
    }
    var r = trackCv.getBoundingClientRect();
    trackCv._labels = trackLabels.map(function (l) { return { text: l.text, x: r.left + l.x, y: r.top + l.y, w: l.w, h: l.h }; });
  }

  /* =========================================================================
     Layout (CSS does the boxes; on phones JS fits the copy, wall and track)
     ========================================================================= */
  var measureEl = null;
  function copyMaxHeight() {
    if (!measureEl) {
      measureEl = document.createElement('section');
      measureEl.setAttribute('aria-hidden', 'true');
      measureEl.style.cssText = 'position:absolute;visibility:hidden;left:-9999px;top:0;display:flex;flex-direction:column';
      document.body.appendChild(measureEl);
    }
    measureEl.style.width = copyEl.getBoundingClientRect().width + 'px';
    var mx = 0;
    for (var k = 0; k < N; k++) {
      measureEl.textContent = '';
      var e1 = el('p', 'eyebrow'); fillEyebrow(e1, k); measureEl.appendChild(e1);
      measureEl.appendChild(el('h2', 'hl', BEATS[k].h));
      measureEl.appendChild(el('p', 'lede', BEATS[k].lede));
      measureEl.appendChild(el('button', 'logbtn', 'Log'));
      mx = Math.max(mx, measureEl.getBoundingClientRect().height);
    }
    return mx;
  }
  function relayout() {
    var phone = isPhone(), dpr = Math.min(window.devicePixelRatio || 1, 2), H = vh(), Wd = vw();
    var rs = root.style;
    if (phone) {
      rs.removeProperty('--imgW'); rs.removeProperty('--wallH'); rs.removeProperty('--trackH');
      var g = Wd >= 640 ? 24 : 16, imgTop = 50;
      var imgW = Math.min(Wd - 2 * g, Wd >= 640 ? H * 0.6 * 4 / 3 : 1e9);
      var copyH = copyMaxHeight(), avail = H - 90 - copyH - 12;
      // wall height: what the final beat needs, up to 170 (more on tall tablets)
      var wallMax = Wd >= 640 ? 240 : 170;
      function wallUsed() {
        layoutWall(Wd - 2 * g, wallMax, true, dpr);
        var used = 0; IDS.forEach(function (id) { used += W.blocks[id].R * W.p + W.hL; }); used += W.gap * (IDS.length - 1);
        if (W.ph) used += W.gap + W.hL + 3 * W.p;
        return Math.min(wallMax, Math.ceil(used));
      }
      wallNoPh = false;
      var wallH = wallUsed();
      // the caption under the image may wrap on narrow phones: reserve the tallest one
      var capText = capEl.textContent, capMax = 0;
      for (var ci = 0; ci < N; ci++) { capEl.textContent = BEATS[ci].cap || ''; capMax = Math.max(capMax, capEl.getBoundingClientRect().height); }
      capEl.textContent = capText;
      var capH = Math.max(26, Math.ceil(capMax) + 10);
      rs.setProperty('--capH', capH + 'px');
      var top = imgTop + capH + 24 + 8; // caption row, counter row, gap under the wall
      // degrade in steps: full track, sparkline, no placeholder rows, no track, then a smaller image
      var trackH = Math.min(72, avail - (top + imgW * 0.75 + wallH));
      if (trackH < 30) {
        wallNoPh = true; wallH = wallUsed();
        trackH = Math.min(72, avail - (top + imgW * 0.75 + wallH));
      }
      // next, a shorter wall (it re-packs at a smaller pitch) before the image gives up any width
      var wallSteps = [140, 120, 100];
      for (var ws = 0; ws < wallSteps.length && trackH < 30 && wallMax > wallSteps[ws]; ws++) {
        wallMax = wallSteps[ws]; wallH = wallUsed();
        trackH = Math.min(72, avail - (top + imgW * 0.75 + wallH));
      }
      if (trackH < 26) {
        // keep a 26 px sparkline by narrowing the image, down to 280 px (still the largest thing on screen)
        var fitW = (avail - (top + wallH + 26)) * 4 / 3;
        if (fitW >= 280) { imgW = Math.min(imgW, fitW); trackH = 26; }
        else { trackH = 0; imgW = Math.max(200, Math.min(imgW, (avail - (top + wallH)) * 4 / 3)); }
      }
      trackbox.style.display = trackH ? '' : 'none';
      rs.setProperty('--imgW', imgW.toFixed(1) + 'px');
      rs.setProperty('--wallH', wallH + 'px');
      rs.setProperty('--trackH', Math.floor(trackH) + 'px');
    } else {
      rs.removeProperty('--imgW'); rs.removeProperty('--wallH'); rs.removeProperty('--trackH'); rs.removeProperty('--capH');
      wallNoPh = false; trackbox.style.display = '';
    }
    // canvases
    var wr = wallCv.getBoundingClientRect(), tr = trackCv.getBoundingClientRect();
    wallCv.width = Math.max(1, Math.round(wr.width * dpr)); wallCv.height = Math.max(1, Math.round(wr.height * dpr));
    trackCv.width = Math.max(1, Math.round(tr.width * dpr)); trackCv.height = Math.max(1, Math.round(tr.height * dpr));
    layoutWall(wr.width, wr.height, phone, dpr);
    layoutTrack(tr.width, tr.height, phone, dpr);
    dirty = true; kick();
  }

  /* =========================================================================
     Render: everything below is a function of s (plus image readiness)
     ========================================================================= */
  function render(now) {
    var moving = false;
    if (tw) {
      var p = cl((now - tw.t0) / tw.dur);
      s = lerp(tw.s0, tw.s1, p);
      if (p >= 1) { s = tw.s1; tw = null; } else moving = true;
    }
    var k = Math.floor(s), u = s - k;
    if (k >= N - 1) { k = N - 1; u = 0; }
    // images
    var imgA = CFG[k].img, imgB = u > 0 ? CFG[k + 1].img : imgA;
    var fadeB = imgB !== imgA ? seg(u, 0.25, 0.75) : 0;
    if (reduced && rmImg) {
      var rp = cl((now - rmImg.t0) / 150);
      imgA = rmImg.from; imgB = rmImg.to; fadeB = imgA === imgB ? 0 : rp;
      if (rp >= 1) { imgA = imgB; fadeB = 0; rmImg = null; } else moving = true;
    }
    Object.keys(layers).forEach(function (key) {
      var L = layers[key], r = ramp(L, now), op = 0, z = '';
      if (key === imgA) { op = r; z = '1'; }
      if (key === imgB && imgB !== imgA) { op = fadeB * r; z = '2'; }
      if (r > 0 && r < 1 && (key === imgA || key === imgB)) moving = true;
      if (Math.abs(op - L.op) > 0.001 || L.z !== z) {
        L.op = op; L.z = z;
        L.pic.style.opacity = op.toFixed(3);
        L.pic.style.visibility = op > 0 ? 'visible' : 'hidden';
        L.pic.style.zIndex = z;
      }
    });
    // alt text follows the settled beat
    // wall and track
    drawWall(k, u);
    drawTrack(k, u);
    // counter
    var cA = C.counters[k], cB = C.counters[Math.min(N - 1, k + 1)];
    cntv.textContent = fmt(lerp(cA, u > 0 ? cB : cA, seg(u, 0.10, 0.70)));
    // readout
    var roA = BEATS[k].ro, roB = u > 0 ? BEATS[k + 1].ro : roA, dp = seg(u, 0.55, 1.0);
    var va = roA ? roA.v : null, vb = roB ? roB.v : null, val, txt;
    if (va === null && vb === null) { rov.parentNode.style.opacity = '0'; }
    else {
      if (va === null) { val = vb; rov.parentNode.style.opacity = dp.toFixed(3); }
      else if (vb === null) { val = va; rov.parentNode.style.opacity = (1 - dp).toFixed(3); }
      else { val = lerp(va, vb, dp); rov.parentNode.style.opacity = '1'; }
      txt = dp > 0 && dp < 1 && va !== vb ? val.toFixed(1) : val.toFixed(2);
      if (rov.textContent !== txt) rov.textContent = txt;
      var sub = (dp < 0.5 ? roA : roB) || roA || roB;
      if (ros.textContent !== sub.sub) ros.textContent = sub.sub;
    }
    // keys: the wall's own key on step 1, then the row under the track
    var lg = k === 0 ? 1 - seg(u, 0, 0.10) : 0;
    keyEl.style.opacity = lg.toFixed(3);
    keyEl.style.visibility = lg > 0 ? 'visible' : 'hidden';
    var lg2 = k === 0 ? seg(u, 0.10, 0.40) : 1;
    key2.style.opacity = lg2.toFixed(3);
    // progress
    for (var i = 0; i < N; i++) {
      var b = segs[i], fill = i === 0 ? 1 : cl(s - (i - 1));
      if (i > target && !tw) fill = 0;
      b.firstChild.style.transform = 'scaleX(' + fill.toFixed(3) + ')';
      b.classList.toggle('done', i < target);
    }
    if (stepCopy(now)) moving = true;
    return moving;
  }

  var raf = 0, running = false, wasMoving = true;
  function kick() { if (!running && !document.hidden) { running = true; raf = requestAnimationFrame(loop); } }
  function loop(now) {
    if (!(dirty || tw || wasMoving || copyAnim || rmImg)) { running = false; return; }
    raf = requestAnimationFrame(loop);
    dirty = false;
    wasMoving = render(now);
  }
  var hiddenAt = 0;
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { hiddenAt = performance.now(); cancelAnimationFrame(raf); running = false; }
    else { var d = performance.now() - hiddenAt; if (tw) tw.t0 += d; if (copyAnim) copyAnim.t0 += d; dirty = true; kick(); }
  });
  var rT = 0;
  window.addEventListener('resize', function () { clearTimeout(rT); rT = setTimeout(relayout, 100); });
  function fontsKick() { relayout(); }
  if (document.fonts && document.fonts.load) {
    var timeout = new Promise(function (r) { setTimeout(r, 1500); });
    Promise.race([Promise.all([document.fonts.load('11px "Martian Mono"'), document.fonts.load('20px "Newsreader"')]), timeout]).then(fontsKick, fontsKick);
  }

  /* ---------- init ---------- */
  (function init() {
    var k = hashBeat();
    if (k > 0) {
      target = k; s = k; fillCopy(k); cur.textContent = pad(k + 1);
      // deep link: the target image first, then its neighbors
      ensure(CFG[k].img);
    }
    loadAround(target);
    setAlt(target);
    prevB.disabled = target === 0; nextB.disabled = target === N - 1;
    segs[target].setAttribute('aria-current', 'step');
    live.textContent = BEATS[target].live;
    relayout();
    render(performance.now());
    kick();
  })();
})();
