/* LinkedEye charts v2: dependency-free SVG charts with synced hover, drag-zoom, legend binding and gauges.
 * Markup: <div class="chart" data-chart="line|area|bars|donut|spark|gauge" data-src="json-script-id" style="height:200px"></div>
 * Data comes from a {{ value|json_script:"id" }} block. null values are gaps (never drawn as zero).
 * Axes data: labels, tipLabels, ts (epoch seconds aligned with labels; enables cross-chart sync and zoom by time),
 *   series[{name,color,values,area,areaOpacity,width,dash,endDot,hidden}], unit, yMin, yMax, yTicks, xLabelEvery,
 *   stacked (bars). Donut: segments, thickness, center. Spark: values, color.
 *   Gauge: value, max (100), unit, label, delta {value,text}, thresholds [{to,color}].
 * Host attributes: data-sync="group" (shared crosshair + zoom window), data-kinds="line,area,bars" (segmented
 *   control, remembered in sessionStorage under le-chart-kind:<src>). A [data-chart-legend="<src>"] element with
 *   [data-series="<name>"] rows is bound: hovered values go into [data-col="last"], click toggles, shift+click isolates.
 * Colours are a literal or 'var(--token)'. Series names, labels and tooltip text are untrusted (scraped Prometheus
 * label values): the DOM is built with textContent only, never innerHTML.
 * API: window.LinkedEyeCharts = { render, init, fmt, update, zoom, resetZoom, refresh }.
 * Events on document: linkedeye-chart-hover {group, ts, index}, linkedeye-chart-zoom {group, t0, t1}.
 */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const AXIS = 'var(--chart-axis)', GRID = 'var(--line-soft)', TEXT = 'var(--muted)';
  const KIND_KEY = 'le-chart-kind:';
  const AXES_KINDS = ['line', 'area', 'bars'];
  const DEFAULT_THRESHOLDS = [{ to: 70, color: 'var(--chart-ok)' }, { to: 90, color: 'var(--chart-warn)' }, { color: 'var(--chart-crit)' }];

  // 'var(--token)' becomes the token's value, read when the chart is drawn so a redraw follows the tokens in force
  // then (print). Literals pass through. A token with no value falls back to the text colour, because stroke=""
  // draws nothing and a missing series reads as "no data". getComputedStyle is absent under node (test_audit_charts).
  function paint(c) {
    const m = typeof c === 'string' && /^var\((--[\w-]+)\)$/.exec(c);
    if (!m) return c;
    const root = document.documentElement;
    const v = typeof getComputedStyle === 'function' && root ? getComputedStyle(root).getPropertyValue(m[1]).trim() : '';
    return v || 'currentColor';
  }
  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, paint(attrs[k]));
    if (parent) parent.appendChild(e);
    return e;
  }
  // Every DOM capability below is optional: charts.js also runs against the minimal document stand-in of
  // core/test_audit_charts.py (and against hosts that are plain objects), so a missing method is a no-op, never a throw.
  function attr(e, k) { return e && typeof e.getAttribute === 'function' ? e.getAttribute(k) : null; }
  function setAttr(e, k, v) { if (e && typeof e.setAttribute === 'function') e.setAttribute(k, v); }
  function cls(e, name, on) { if (e && e.classList && typeof e.classList.toggle === 'function') e.classList.toggle(name, on); }
  function closestIn(node, sel) { return node && typeof node.closest === 'function' ? node.closest(sel) : null; }
  function qsa(root, sel) {
    const r = root || document;
    return r && typeof r.querySelectorAll === 'function' ? Array.prototype.slice.call(r.querySelectorAll(sel)) : [];
  }
  function on(target, type, fn) { if (target && typeof target.addEventListener === 'function') target.addEventListener(type, fn); }
  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function fmt(v, unit) {
    if (v === null || v === undefined || isNaN(v)) return '–';
    const a = Math.abs(v);
    let s = a >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : a >= 1e4 ? (v / 1e3).toFixed(0) + 'K' : a >= 100 ? Math.round(v).toLocaleString() : a >= 10 ? v.toFixed(1) : v.toFixed(2);
    s = s.replace(/\.0+(?=[A-Z]|$)/, '');
    return unit ? s + (unit === '%' ? '%' : ' ' + unit) : s;
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }
  const isGap = v => v === null || v === undefined;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  // first index whose value is >= t (array sorted ascending)
  function lowerBound(a, t) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < t) lo = m + 1; else hi = m; } return lo; }
  function upperBound(a, t) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] <= t) lo = m + 1; else hi = m; } return lo; }
  function nearest(a, t) {
    if (!a.length) return -1;
    const i = lowerBound(a, t);
    if (i <= 0) return 0;
    if (i >= a.length) return a.length - 1;
    return t - a[i - 1] <= a[i] - t ? i - 1 : i;
  }
  function store(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* storage blocked: the choice lives for the page only */ } }
  function stored(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function emit(name, detail) {
    if (typeof CustomEvent !== 'function' || typeof document.dispatchEvent !== 'function') return;
    document.dispatchEvent(new CustomEvent(name, { detail }));
  }

  // ------------------------------------------------------------ registry
  // Per-host state lives here (not in the SVG) so a re-render for a resize, a theme change, a kind switch or an
  // update() keeps the hidden series, the zoom window and the bound legend.
  const states = new WeakMap(), hosts = new Set(), groups = new Map(), zooms = new Map(), boundLegends = new WeakSet();
  let uid = 0, ro = null;

  function stateFor(host) {
    let st = states.get(host);
    if (!st) { st = { id: ++uid, hidden: new Set(), kind: null, view: null, hover: null, seeded: false, sgroups: [], legendOrig: new Map(), legendCells: new Map(), legendRows: [] }; states.set(host, st); }
    return st;
  }
  function keyFor(host) { return host.dataset.sync || '#' + stateFor(host).id; }
  function register(host) {
    hosts.add(host);
    const k = keyFor(host);
    if (!groups.has(k)) groups.set(k, new Set());
    groups.get(k).add(host);
  }
  function peers(key) { return groups.get(key) || new Set(); }
  function byId(id) { return typeof document.getElementById === 'function' ? document.getElementById(id) : null; }
  function readData(host) {
    const src = byId(host.dataset && host.dataset.src);
    if (!src) return null;
    try { return JSON.parse(src.textContent); } catch (e) { return null; }
  }

  // The visible slice of the data: the zoom window of the host's group applied by ts (or by index when the data
  // has no ts and the window came from a chart with the same number of points). Fewer than 3 points: not zoomed.
  function viewOf(d, key) {
    const n = d.labels.length;
    const ts = Array.isArray(d.ts) && d.ts.length === n ? d.ts : null;
    const z = zooms.get(key);
    let i0 = 0, i1 = n - 1, zoomed = false;
    if (z && n) {
      if (ts && z.t0 != null) { i0 = lowerBound(ts, Math.min(z.t0, z.t1)); i1 = upperBound(ts, Math.max(z.t0, z.t1)) - 1; }
      else if (z.i0 != null && z.n === n) { i0 = z.i0; i1 = z.i1; }
      i0 = clamp(i0, 0, n - 1); i1 = clamp(i1, 0, n - 1);
      if (i1 - i0 + 1 < 3) { i0 = 0; i1 = n - 1; } else zoomed = i0 > 0 || i1 < n - 1;
    }
    const sl = a => a.slice(i0, i1 + 1);
    return { i0, i1, n: n ? i1 - i0 + 1 : 0, total: n, zoomed, ts: ts ? sl(ts) : null, labels: sl(d.labels),
      tips: sl(d.tipLabels || d.labels), series: d.series.map(s => Object.assign({}, s, { values: sl(s.values) })) };
  }

  // ------------------------------------------------------------ overlays (created once per host, kept across renders)
  function overlays(host, st) {
    if (st.tip) return;
    st.tip = h('div', 'tip'); st.tip.hidden = true;
    st.reset = h('button', 'chart-zoom-reset', 'Reset zoom'); st.reset.type = 'button'; st.reset.hidden = true;
    on(st.reset, 'click', () => resetZoom(keyFor(host)));
    const list = (host.dataset.kinds || '').split(',').map(s => s.trim()).filter(k => AXES_KINDS.includes(k));
    if (list.length > 1) {
      const box = h('div', 'chart-kinds'); box.setAttribute('role', 'group'); box.setAttribute('aria-label', 'Chart type');
      list.forEach(k => {
        const b = h('button', '', k); b.type = 'button'; b.dataset.kind = k; b.setAttribute('aria-pressed', 'false');
        on(b, 'click', () => { st.kind = k; store(KIND_KEY + host.dataset.src, k); render(host); });
        box.appendChild(b);
      });
      st.kinds = box;
      const want = stored(KIND_KEY + host.dataset.src);
      st.kind = list.includes(want) ? want : list.includes(host.dataset.chart) ? host.dataset.chart : list[0];
    }
    if (attr(host, 'tabindex') == null) setAttr(host, 'tabindex', '0');
    on(host, 'keydown', e => {
      if (e.target !== host || !st.view || !st.view.n) return;
      const n = st.view.n;
      let i = st.hover;
      switch (e.key) {
        case 'ArrowLeft': i = i == null ? n - 1 : i - 1; break;
        case 'ArrowRight': i = i == null ? 0 : i + 1; break;
        case 'Home': i = 0; break;
        case 'End': i = n - 1; break;
        case 'Escape': hideAll(host, st); return;
        default: return;
      }
      e.preventDefault();
      i = clamp(i, 0, n - 1);
      st.show(i, true); broadcast(host, st, i);
    });
    on(host, 'focusout', e => { if (!(e.relatedTarget && host.contains && host.contains(e.relatedTarget))) hideAll(host, st); });
  }

  // ------------------------------------------------------------ legend binding
  function legendOf(host, st) {
    const id = host.dataset.src;
    let found = null;
    qsa(document, '[data-chart-legend]').forEach(e => { if (!found && attr(e, 'data-chart-legend') === id) found = e; });
    if (found !== st.legendEl) { st.legendEl = found; st.legendOrig = new Map(); }
    st.legendRows = found ? qsa(found, '[data-series]') : [];
    st.legendCells = new Map();
    st.legendRows.forEach(r => {
      const name = attr(r, 'data-series');
      cls(r, 'off', st.hidden.has(name));
      if (attr(r, 'tabindex') == null) setAttr(r, 'tabindex', '0');
      const cell = typeof r.querySelector === 'function' ? r.querySelector('[data-col="last"]') : null;
      if (cell) st.legendCells.set(name, cell);
    });
    if (found && !boundLegends.has(found)) { boundLegends.add(found); bindLegend(found, host); }
  }
  function bindLegend(legend, host) {
    const rowOf = e => closestIn(e.target, '[data-series]');
    on(legend, 'click', e => {
      const r = rowOf(e);
      if (!r || closestIn(e.target, 'a, button')) return;
      toggleSeries(host, attr(r, 'data-series'), e.shiftKey);
    });
    on(legend, 'keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const r = rowOf(e);
      if (!r || r !== e.target) return;
      e.preventDefault();
      toggleSeries(host, attr(r, 'data-series'), e.shiftKey);
    });
    on(legend, 'pointerover', e => { const r = rowOf(e); if (r) highlight(host, attr(r, 'data-series')); });
    on(legend, 'pointerout', e => {
      const r = rowOf(e);
      if (r && closestIn(e.relatedTarget, '[data-series]') !== r) highlight(host, null);
    });
  }
  function toggleSeries(host, name, isolate) {
    const st = stateFor(host), d = readData(host);
    if (!d) return;
    const names = d.series.map(s => s.name);
    if (!names.includes(name)) return;   // a legend row the chart has no series for (stale row): isolating it would hide everything
    if (isolate) {
      const shown = names.filter(n => !st.hidden.has(n));
      if (shown.length === 1 && shown[0] === name) st.hidden.clear();   // already alone: bring everything back
      else st.hidden = new Set(names.filter(n => n !== name));
    } else if (st.hidden.has(name)) st.hidden.delete(name);
    else st.hidden.add(name);
    render(host);
  }
  function highlight(host, name) {
    const st = stateFor(host);
    st.sgroups.forEach(sg => { sg.g.style.opacity = name && sg.name !== name ? '0.35' : ''; });
    st.legendRows.forEach(r => cls(r, 'hl', !!name && attr(r, 'data-series') === name));
  }
  function legendValues(st, series, i, unit) {
    series.forEach(s => {
      const cell = st.legendCells.get(s.name);
      if (!cell) return;
      if (!st.legendOrig.has(cell)) st.legendOrig.set(cell, cell.textContent);
      cell.textContent = fmt(s.values[i], unit);
    });
  }
  function legendRestore(st) {
    st.legendOrig.forEach((text, cell) => { cell.textContent = text; });
    st.legendOrig.clear();
  }

  // ------------------------------------------------------------ sync / zoom plumbing
  function broadcast(host, st, i) {
    const view = st.view, ts = view.ts ? view.ts[i] : null, index = view.i0 + i;
    if (host.dataset.sync) peers(keyFor(host)).forEach(p => { const ps = stateFor(p); if (p !== host && ps.sync) ps.sync(ts, index, view.total); });
    emit('linkedeye-chart-hover', { group: host.dataset.sync || null, ts, index });
  }
  function hideAll(host, st) {
    if (st.hide) st.hide();
    if (host.dataset.sync) peers(keyFor(host)).forEach(p => { const ps = stateFor(p); if (p !== host && ps.hide) ps.hide(); });
    emit('linkedeye-chart-hover', { group: host.dataset.sync || null, ts: null, index: null });
  }
  function applyZoom(key, z) {
    if (z) zooms.set(key, z); else zooms.delete(key);
    peers(key).forEach(p => { if (p.isConnected !== false) render(p); });   // hosts removed from the page are skipped
    emit('linkedeye-chart-zoom', { group: key[0] === '#' ? null : key, t0: z ? z.t0 : null, t1: z ? z.t1 : null });
  }
  function zoom(group, t0, t1) { if (group && t0 != null && t1 != null) applyZoom(String(group), { t0: +t0, t1: +t1, i0: null, i1: null, n: null }); }
  function resetZoom(group) { if (group && zooms.has(String(group))) applyZoom(String(group), null); }

  // ------------------------------------------------------------ axes chart (line / area / bars)
  function axesChart(host, d, kind, st) {
    const key = keyFor(host);
    const view = viewOf(d, key);
    const W = host.clientWidth, H = host.clientHeight || 200;
    const m = { l: 52, r: 10, t: st.kinds ? 30 : 10, b: 26 };   // the kind switch sits above the plot, never on the data
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': d.title || 'chart', class: 'axes' });
    const n = view.n, bars = kind === 'bars', stacked = bars && d.stacked !== false;
    const vis = view.series.filter(s => !st.hidden.has(s.name));
    let lo = d.yMin !== undefined ? d.yMin : 0, hi;
    if (stacked) hi = Math.max(0, ...view.labels.map((_, i) => vis.reduce((s, x) => s + (x.values[i] || 0), 0)));
    else {
      const finite = vis.flatMap(s => s.values.filter(v => !isGap(v)));
      hi = finite.length ? Math.max(...finite, lo + 1e-9) : lo + 1;   // every series hidden or empty: a plain 0..1 axis, not 0.00 on every tick
    }
    hi = d.yMax !== undefined ? d.yMax : niceMax(hi);
    if (d.yMin === undefined && lo === 0 && hi === 0) hi = 1;
    const Y = v => m.t + ph - ((v - lo) / (hi - lo)) * ph;
    const step = pw / Math.max(1, bars ? n : n - 1);
    const X = i => bars ? m.l + step * i + step / 2 : m.l + step * i;

    const ticks = d.yTicks || 4;
    for (let i = 0; i <= ticks; i++) {
      const v = lo + ((hi - lo) * i) / ticks, y = Y(v);
      el('line', { x1: m.l, x2: W - m.r, y1: y, y2: y, stroke: GRID }, svg);
      el('text', { x: m.l - 8, y: y + 4, 'text-anchor': 'end', 'font-size': 11, fill: TEXT }, svg).textContent = fmt(v, d.unit);
    }
    const every = (!view.zoomed && d.xLabelEvery) || Math.max(1, Math.ceil(n / Math.max(2, Math.floor(pw / 70))));
    view.labels.forEach((lab, i) => {
      if (i % every) return;
      el('text', { x: X(i), y: H - 8, 'text-anchor': 'middle', 'font-size': 11, fill: TEXT }, svg).textContent = lab;
    });

    st.sgroups = [];
    const dots = [];
    if (!bars) {
      vis.forEach(s => {
        const g = el('g', { class: 's' }, svg);
        st.sgroups.push({ name: s.name, g });
        const filled = kind === 'area' || s.area;
        let path = '', area = '', open = false, first = null, lastX = null;
        s.values.forEach((v, i) => {
          if (isGap(v)) {
            if (open && filled) area += `L${lastX},${Y(lo)}L${first},${Y(lo)}Z`;
            open = false; return;
          }
          const x = X(i), y = Y(v);
          if (!open) { path += `M${x},${y}`; area += `M${x},${y}`; first = x; open = true; } else { path += `L${x},${y}`; area += `L${x},${y}`; }
          lastX = x;
        });
        if (open && filled) area += `L${lastX},${Y(lo)}L${first},${Y(lo)}Z`;
        if (filled) el('path', { d: area, fill: s.color, 'fill-opacity': s.areaOpacity || 0.14, stroke: 'none' }, g);
        // a point with no neighbours would be invisible as a path: draw it as a dot
        s.values.forEach((v, i) => {
          if (!isGap(v) && isGap(s.values[i - 1]) && isGap(s.values[i + 1])) el('circle', { cx: X(i), cy: Y(v), r: 3, fill: s.color }, g);
        });
        el('path', { d: path, fill: 'none', stroke: s.color, 'stroke-width': s.width || 1.8, 'stroke-linejoin': 'round', 'stroke-dasharray': s.dash || 'none' }, g);
        if (s.endDot) { let li = s.values.length - 1; while (li >= 0 && isGap(s.values[li])) li--; if (li >= 0) el('circle', { cx: X(li), cy: Y(s.values[li]), r: 3.5, fill: s.color, stroke: 'var(--panel)', 'stroke-width': 1.5 }, g); }
      });
    } else {
      const bw = Math.max(3, Math.min(46, step * 0.7));
      const gs = vis.map(s => { const g = el('g', { class: 's' }, svg); st.sgroups.push({ name: s.name, g }); return g; });
      view.labels.forEach((_, i) => {
        let base = lo;
        vis.forEach((s, si) => {
          const v = s.values[i];
          if (!v) return;
          if (!stacked) {
            const w = bw / vis.length;
            el('rect', { x: X(i) - bw / 2 + w * si, y: Y(v), width: w - 1, height: Y(lo) - Y(v), fill: s.color, rx: 1.5 }, gs[si]);
          } else {
            el('rect', { x: X(i) - bw / 2, y: Y(base + v), width: bw, height: Math.max(0, Y(base) - Y(base + v)), fill: s.color }, gs[si]);
            base += v;
          }
        });
      });
    }
    const cross = el('line', { y1: m.t, y2: m.t + ph, stroke: AXIS, 'stroke-dasharray': '3 3', visibility: 'hidden', 'pointer-events': 'none' }, svg);
    if (!bars) vis.forEach(s => dots.push(el('circle', { r: 3.5, fill: s.color, stroke: 'var(--panel)', 'stroke-width': 1.5, visibility: 'hidden', 'pointer-events': 'none' }, svg)));
    const sel = el('rect', { class: 'chart-sel', y: m.t, height: ph, x: 0, width: 0, visibility: 'hidden' }, svg);

    // replaceChildren detaches the kind buttons for a moment, which drops keyboard focus: put it back afterwards
    const focusKind = st.kinds && document.activeElement && st.kinds.contains && st.kinds.contains(document.activeElement) ? document.activeElement.dataset.kind : null;
    host.replaceChildren(svg, ...[st.kinds, st.reset, st.tip].filter(Boolean));
    if (st.kinds) Array.from(st.kinds.children).forEach(b => {
      setAttr(b, 'aria-pressed', String(b.dataset.kind === kind));
      if (focusKind && b.dataset.kind === focusKind && typeof b.focus === 'function') b.focus({ preventScroll: true });
    });
    st.reset.hidden = !view.zoomed;
    st.reset.style.left = m.l + 4 + 'px'; st.reset.style.top = m.t + 4 + 'px';
    cls(host, 'zoomed', view.zoomed);
    if (attr(host, 'role') == null) setAttr(host, 'role', 'group');   // an aria-label on a bare div is ignored by screen readers
    if (attr(host, 'aria-label') == null) setAttr(host, 'aria-label', (d.title || 'Chart') + ' – arrow keys read values, Escape hides the cursor');
    legendOf(host, st);
    highlight(host, null);

    const tip = st.tip;
    st.view = view; st.hover = null;
    const toX = e => { const r = svg.getBoundingClientRect(); return (e.clientX - r.left) * (W / (r.width || W)); };
    const idx = x => clamp(Math.round(bars ? (x - m.l - step / 2) / step : (x - m.l) / step), 0, Math.max(0, n - 1));
    const plotX = x => clamp(x, m.l, m.l + pw);

    // Hover never re-renders: it moves the crosshair, the point markers and rewrites text.
    const buildTip = i => {
      tip.replaceChildren(h('b', '', String(view.tips[i])));
      vis.map(s => ({ s, v: s.values[i] })).sort((a, b) => (isGap(b.v) ? -Infinity : b.v) - (isGap(a.v) ? -Infinity : a.v)).forEach(({ s, v }) => {
        const row = h('div', 'row');
        const sw = h('i'); sw.style.background = s.color;
        row.append(sw, h('span', 'n', s.name), h('span', 'v', fmt(v, d.unit)));
        tip.appendChild(row);
      });
    };
    const placeTip = i => {
      const r = svg.getBoundingClientRect(), sx = (r.width || W) / W;
      const px = X(i) * sx, hw = host.clientWidth || r.width || W, hh = host.clientHeight || H;
      tip.hidden = false;
      const tw = tip.offsetWidth || 0, th = tip.offsetHeight || 0;
      let left = px + 14;
      if (left + tw > hw) left = Math.max(0, px - 14 - tw);   // flip to the left near the right edge
      let top = m.t;
      if (top + th > hh) top = Math.max(0, hh - th);
      tip.style.left = left + 'px'; tip.style.top = top + 'px';
    };
    st.show = (i, withTip) => {
      if (!n) return;
      i = clamp(i, 0, n - 1); st.hover = i;
      const x = X(i);
      cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
      dots.forEach((c, k) => {
        const v = vis[k].values[i];
        if (isGap(v)) c.setAttribute('visibility', 'hidden');
        else { c.setAttribute('cx', x); c.setAttribute('cy', Y(v)); c.setAttribute('visibility', 'visible'); }
      });
      legendValues(st, view.series, i, d.unit);
      if (withTip) { buildTip(i); placeTip(i); } else tip.hidden = true;
    };
    st.hide = () => {
      st.hover = null; tip.hidden = true;
      cross.setAttribute('visibility', 'hidden');
      dots.forEach(c => c.setAttribute('visibility', 'hidden'));
      legendRestore(st);
    };
    st.sync = (ts, index, total) => {
      let j;
      if (ts != null && view.ts) {
        // a chart that does not cover that moment (shorter series, a different zoom) shows no cursor instead of
        // pinning one to its nearest edge
        const span = n > 1 ? (view.ts[n - 1] - view.ts[0]) / (n - 1) : 0;
        if (n > 1 && (ts < view.ts[0] - span || ts > view.ts[n - 1] + span)) { st.hide(); return; }
        j = nearest(view.ts, ts);
      } else if (view.total === total) j = index - view.i0;
      else { st.hide(); return; }
      if (j < 0 || j >= n) st.hide(); else st.show(j, false);
    };

    let drag = null;
    const endDrag = () => { drag = null; sel.setAttribute('visibility', 'hidden'); };
    on(svg, 'pointerdown', e => {
      if (e.button !== 0 || !n) return;
      drag = { x0: plotX(toX(e)), moved: false };
      // a touch has no hover: the tap itself reads the value (a drag from here turns into a zoom selection)
      if (e.pointerType === 'touch') { const i = idx(drag.x0); st.show(i, true); broadcast(host, st, i); }
      if (svg.setPointerCapture) { try { svg.setPointerCapture(e.pointerId); } catch (err) { /* synthetic pointer */ } }
    });
    on(svg, 'pointermove', e => {
      if (!n) return;
      const x = toX(e);
      if (drag) {
        const px = plotX(x);
        if (drag.moved || Math.abs(px - drag.x0) > 4) {
          drag.moved = true;
          sel.setAttribute('x', Math.min(px, drag.x0)); sel.setAttribute('width', Math.abs(px - drag.x0)); sel.setAttribute('visibility', 'visible');
          tip.hidden = true;
          const i = idx(px);
          cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.setAttribute('visibility', 'visible');
          return;
        }
      }
      const i = idx(x);
      if (i !== st.hover || tip.hidden) { st.show(i, true); broadcast(host, st, i); }
    });
    on(svg, 'pointerup', e => {
      if (!drag) return;
      const moved = drag.moved, x1 = plotX(toX(e)), x0 = drag.x0;
      endDrag();
      if (!moved) return;
      const a = idx(Math.min(x0, x1)), b = idx(Math.max(x0, x1));
      if (b - a + 1 < 3) return;   // too narrow to be a window
      hideAll(host, st);
      applyZoom(key, { t0: view.ts ? view.ts[a] : null, t1: view.ts ? view.ts[b] : null, i0: view.i0 + a, i1: view.i0 + b, n: view.total });
    });
    on(svg, 'pointercancel', endDrag);
    // a lifted finger "leaves" at once: for touch the readout stays until the next tap elsewhere (see init)
    on(svg, 'pointerleave', e => { if (!drag && e.pointerType !== 'touch') hideAll(host, st); });
    on(svg, 'dblclick', () => resetZoom(key));
  }

  // ------------------------------------------------------------ donut
  function donut(host, d) {
    const S = host.clientWidth || 170, r = S / 2, w = d.thickness || S * 0.13, R = r - w / 2 - 1;
    const svg = el('svg', { viewBox: `0 0 ${S} ${S}`, height: S, role: 'img', 'aria-label': d.title || 'donut chart' });
    const total = d.segments.reduce((s, x) => s + x.value, 0);
    el('circle', { cx: r, cy: r, r: R, fill: 'none', stroke: 'var(--chart-track)', 'stroke-width': w }, svg);
    let a0 = -Math.PI / 2;
    if (total > 0) d.segments.forEach(seg => {
      if (!seg.value) return;
      const frac = seg.value / total;
      if (frac >= 0.9999) { el('circle', { cx: r, cy: r, r: R, fill: 'none', stroke: seg.color, 'stroke-width': w }, svg); return; }
      const a1 = a0 + frac * Math.PI * 2;
      const p = (a) => `${r + R * Math.cos(a)},${r + R * Math.sin(a)}`;
      el('path', { d: `M${p(a0)}A${R},${R} 0 ${frac > 0.5 ? 1 : 0} 1 ${p(a1)}`, fill: 'none', stroke: seg.color, 'stroke-width': w }, svg)
        .appendChild(el('title', {})).textContent = `${seg.label}: ${seg.value}`;
      a0 = a1;
    });
    if (d.center) {
      el('text', { x: r, y: r + (d.center.label ? 2 : 8), 'text-anchor': 'middle', 'font-size': S * 0.17, 'font-weight': 600, 'font-family': 'var(--serif)', fill: 'var(--ink)' }, svg).textContent = d.center.value;
      if (d.center.label) el('text', { x: r, y: r + S * 0.13, 'text-anchor': 'middle', 'font-size': S * 0.075, 'font-weight': 600, fill: 'var(--ink-2)' }, svg).textContent = d.center.label;
    }
    host.replaceChildren(svg);
  }

  // ------------------------------------------------------------ sparkline
  // Degenerate inputs reach this from real data (telemetry.hosts.cpu_sparklines returns whatever finite samples a
  // host had, which can be one or none), so: no points draws an empty svg, one point draws a dot, and a null is a
  // gap that breaks the path instead of being plotted as a zero.
  function spark(host, d) {
    const W = host.clientWidth, H = host.clientHeight || 32;
    const all = Array.isArray(d.values) ? d.values : [];
    const pts = all.map((v, i) => ({ i, v })).filter(p => !isGap(p.v) && !isNaN(p.v));
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, 'aria-hidden': 'true' });
    const color = d.color || 'var(--chart-ok)';
    if (pts.length) {
      const nums = pts.map(p => p.v);
      const lo = Math.min(...nums), hi = Math.max(...nums) || 1;
      const X = i => (i / Math.max(1, all.length - 1)) * (W - 2) + 1, Y = v => H - 2 - ((v - lo) / (hi - lo || 1)) * (H - 4);
      const ok = i => i >= 0 && i < all.length && !isGap(all[i]) && !isNaN(all[i]);
      let path = '', open = false;
      all.forEach((v, i) => {
        if (!ok(i)) { open = false; return; }
        // a point with no neighbour is invisible as a path (two moveTo commands draw nothing): draw it as a dot
        if (!ok(i - 1) && !ok(i + 1)) { el('circle', { cx: X(i), cy: Y(v), r: 1.6, fill: color }, svg); open = false; return; }
        path += (open ? 'L' : 'M') + X(i) + ',' + Y(v);
        open = true;
      });
      if (path) el('path', { d: path, fill: 'none', stroke: color, 'stroke-width': 1.6 }, svg);
    }
    host.replaceChildren(svg);
  }

  // ------------------------------------------------------------ gauge (3/4 arc, Netdata style)
  // thresholds: [{to, color}] ordered by `to`; the band holding the value colours the arc (value < to). The last band
  // may omit `to`. Default: ok below 70, warn below 90, crit above.
  function gaugeBands(d) {
    const raw = Array.isArray(d.thresholds) && d.thresholds.length ? d.thresholds : DEFAULT_THRESHOLDS;
    return raw.map(t => ({ to: typeof t.to === 'number' ? t.to : Infinity, color: t.color || 'var(--chart-unknown)' })).sort((a, b) => a.to - b.to);
  }
  function gaugeColor(d, v) {
    const bands = gaugeBands(d);
    return (bands.find(b => v < b.to) || bands[bands.length - 1]).color;
  }
  function gauge(host, d) {
    const W = host.clientWidth || 160, H = host.clientHeight || Math.round(W * 0.72);
    const w = d.thickness || Math.max(8, Math.round(Math.min(W, H) * 0.09));
    const R = Math.max(12, Math.min((W - w) / 2 - 6, (H - w) / 1.72));
    const cx = W / 2, cy = w / 2 + R + 2;
    const max = typeof d.max === 'number' && d.max > 0 ? d.max : 100;
    const raw = typeof d.value === 'number' && isFinite(d.value) ? d.value : null;
    const v = raw === null ? null : clamp(raw, 0, max);
    const A0 = Math.PI * 0.75, SPAN = Math.PI * 1.5;
    const P = a => `${(cx + R * Math.cos(a)).toFixed(2)},${(cy + R * Math.sin(a)).toFixed(2)}`;
    const arc = (a0, a1) => `M${P(a0)}A${R},${R} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${P(a1)}`;
    const text = fmt(raw, d.unit);
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', class: 'gauge', 'aria-label': `${d.label || 'gauge'}: ${text}` });
    el('path', { d: arc(A0, A0 + SPAN), fill: 'none', stroke: 'var(--chart-track)', 'stroke-width': w, 'stroke-linecap': 'round' }, svg);
    const color = v === null ? 'var(--chart-unknown)' : gaugeColor(d, v);
    if (v !== null && v > 0) {
      const frac = v / max;
      el('path', { d: arc(A0, A0 + SPAN * Math.max(frac, 0.004)), fill: 'none', stroke: color, 'stroke-width': w, 'stroke-linecap': 'round', class: 'val' }, svg);
    }
    // threshold ticks just outside the track, in the colour of the band that starts there
    gaugeBands(d).forEach(b => {
      if (!(b.to > 0 && b.to < max)) return;
      const a = A0 + SPAN * (b.to / max), r1 = R + w / 2 + 3, r2 = r1 + 5;
      el('line', { x1: cx + r1 * Math.cos(a), y1: cy + r1 * Math.sin(a), x2: cx + r2 * Math.cos(a), y2: cy + r2 * Math.sin(a), stroke: b.color, 'stroke-width': 2, 'stroke-linecap': 'round' }, svg);
    });
    const big = Math.max(13, R * 0.48);
    el('text', { x: cx, y: cy + big * 0.35, 'text-anchor': 'middle', 'font-size': big, 'font-weight': 600, 'font-family': 'var(--serif)', fill: 'var(--ink)', class: 'num' }, svg).textContent = text;
    if (d.label) el('text', { x: cx, y: cy + big * 0.35 + Math.max(11, R * 0.2) + 2, 'text-anchor': 'middle', 'font-size': Math.max(10, R * 0.17), 'font-weight': 600, fill: 'var(--muted)', 'letter-spacing': '.04em' }, svg).textContent = String(d.label).toUpperCase();
    if (d.delta && (d.delta.text || typeof d.delta.value === 'number')) {
      const dv = typeof d.delta.value === 'number' ? d.delta.value : 0;
      const glyph = dv > 0 ? '▲ ' : dv < 0 ? '▼ ' : '';
      el('text', { x: cx, y: cy + R * 0.78 + w / 2, 'text-anchor': 'middle', 'font-size': Math.max(10, R * 0.17), fill: 'var(--ink-2)', class: 'delta' }, svg).textContent = glyph + (d.delta.text || fmt(dv, d.unit));
    }
    host.replaceChildren(svg);
  }

  // ------------------------------------------------------------ public surface
  function render(host) {
    const d = readData(host);
    if (!d || !host.dataset) return;
    const st = stateFor(host);
    register(host);
    if (!st.seeded) { st.seeded = true; (d.series || []).forEach(s => { if (s.hidden) st.hidden.add(s.name); }); }
    st.w = host.clientWidth; st.h = host.clientHeight;
    const base = host.dataset.chart;
    if (base === 'donut') donut(host, d);
    else if (base === 'spark') spark(host, d);
    else if (base === 'gauge') gauge(host, d);
    else if (AXES_KINDS.includes(base) && Array.isArray(d.labels) && Array.isArray(d.series)) { overlays(host, st); axesChart(host, d, st.kind || base, st); }
    else return;
    if (ro && !st.observed) { st.observed = true; ro.observe(host); }
  }
  // a host taken out of the page: forget it so the registry does not keep it (and its svg) alive, and drop the zoom
  // window of a sync group nobody is left in (a re-injected chart with the same group must not open pre-zoomed)
  function prune() {
    hosts.forEach(hst => {
      if (hst.isConnected !== false) return;
      hosts.delete(hst);
      const key = keyFor(hst), g = groups.get(key);
      if (g) { g.delete(hst); if (!g.size) { groups.delete(key); zooms.delete(key); } }
      if (ro) ro.unobserve(hst);
      const st = states.get(hst);
      if (st) st.observed = false;
    });
  }
  function renderAll() { prune(); hosts.forEach(render); }
  // Replace a json_script's data and redraw every host bound to it; hidden series (by name) and the zoom window stay.
  function update(srcId, data) {
    const src = byId(srcId);
    if (!src) return;
    src.textContent = JSON.stringify(data);
    // A poll landing mid-hover would otherwise leave the pre-hover legend text in legendOrig and write it back on
    // the next leave: drop the crosshair first, then redraw (hidden series and the zoom window live in st/zooms).
    hosts.forEach(hst => { if (hst.dataset && hst.dataset.src === srcId) { const st = states.get(hst); if (st && st.hide) st.hide(); } });
    hosts.forEach(hst => { if (hst.dataset && hst.dataset.src === srcId) render(hst); });
    qsa(document, '.chart[data-chart]').forEach(hst => { if (hst.dataset.src === srcId && !hosts.has(hst)) render(hst); });
  }
  function init() {
    prune();
    const found = qsa(document, '.chart[data-chart]');
    if (typeof ResizeObserver === 'function' && !ro) {
      ro = new ResizeObserver(entries => entries.forEach(e => {
        const st = states.get(e.target);
        if (st && st.w === e.target.clientWidth && st.h === e.target.clientHeight) return;   // the observe() echo
        render(e.target);
      }));
    }
    found.forEach(render);
    if (!init.tapBound) {
      init.tapBound = true;
      on(document, 'pointerdown', e => {
        if (e.pointerType !== 'touch') return;
        const tapped = closestIn(e.target, '.chart[data-chart]'), tg = tapped && tapped.dataset.sync;
        hosts.forEach(hst => {
          const st = states.get(hst);
          // the tapped chart and its sync peers keep the readout the tap just made
          if (st && st.hover != null && hst !== tapped && !(tg && hst.dataset.sync === tg)) hideAll(hst, st);
        });
      });
    }
    // Colours are baked into attributes, so redraw when printing starts and ends. This must be the media query: at
    // beforeprint the media is still screen, so getComputedStyle would return the screen tokens again.
    if (typeof matchMedia === 'function' && !init.printBound) {
      init.printBound = true;
      const mq = matchMedia('print');
      if (mq && typeof mq.addEventListener === 'function') mq.addEventListener('change', renderAll);
      else if (mq && typeof mq.addListener === 'function') mq.addListener(renderAll);   // Safari < 14
    }
  }
  window.LinkedEyeCharts = { render, init, fmt, update, zoom, resetZoom, refresh: renderAll };
  window.leChartsRefresh = renderAll;   // app.js calls this after a theme switch
  if (document.readyState === 'loading') on(document, 'DOMContentLoaded', init); else init();
})();
