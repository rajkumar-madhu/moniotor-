/* Per-port page (network/port.html). Polls network:interface_live (data-url on #pd-live) every data-interval seconds while the
 * browser tab is visible and patches, without a reload:
 *   [data-pd=key]       formatted values (KPIs, state, header facts)  - textContent only
 *   [data-pd-tone=key]  tone classes (t-ok / t-warn / t-crit / t-mute)
 *   charts              json_script ids pd-traffic ... redrawn in place by LinkedEyeCharts.update (newest points)
 *   legends             the Mean / Min / Max / Last cells of the legend tables
 *   #pd-timeline        operational state bands
 * Everything that came from the server is written with textContent (port names and aliases are device-reported).
 * Failures back off up to 5 minutes; a stale snapshot or a run of failures raises the banner. Also here: the port picker
 * submits on change, and "Copy link" copies the page address. */
(function () {
  'use strict';
  const root = document.getElementById('pd-live');
  const banner = document.getElementById('pd-stale');

  // ---- copy link
  const copy = document.getElementById('pd-copy');
  if (copy) copy.addEventListener('click', function () {
    const label = copy.querySelector('span');
    const done = function (ok) {
      label.textContent = ok ? 'Link copied' : 'Copy failed';
      setTimeout(function () { label.textContent = copy.dataset.label; }, 1800);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(location.href).then(function () { done(true); }, function () { done(false); });
    else done(false);
  });

  // ---- port picker
  const picker = document.getElementById('pd-goto');
  if (picker) picker.addEventListener('change', function () { picker.form.submit(); });

  if (!root || !window.fetch || !root.dataset.url) return;
  const url = root.dataset.url;
  const interval = Math.max(10, Number(root.dataset.interval) || 30) * 1000;
  const text = root.querySelector('.nw-live-text');
  const toggle = root.querySelector('.nw-live-toggle');
  let timer = null, inFlight = null, paused = false, failures = 0, lastOk = Date.now(), stale = root.dataset.stale === '1';
  root.hidden = false;

  function setState(state, msg) { root.dataset.state = state; text.textContent = msg; }
  function since() {
    const s = Math.round((Date.now() - lastOk) / 1000);
    return s < 5 ? 'updated just now' : s < 120 ? 'updated ' + s + ' s ago' : 'updated ' + Math.round(s / 60) + ' min ago';
  }
  function showBanner(msg) {
    if (!banner) return;
    banner.querySelector('.pd-stale-text').textContent = msg || '';
    banner.hidden = !msg;
  }
  function applyValues(values) {
    document.querySelectorAll('[data-pd]').forEach(function (el) {
      const v = values[el.dataset.pd];
      if (typeof v !== 'string') return;
      if (el.textContent !== v) el.textContent = v;
      el.classList.toggle('t-mute', v === 'not reported' || (el.classList.contains('pd-kpi-v') && v === '–'));
    });
  }
  function applyTones(tones) {
    document.querySelectorAll('[data-pd-tone]').forEach(function (el) {
      const t = tones[el.dataset.pdTone];
      if (typeof t !== 'string') return;
      Array.from(el.classList).forEach(function (c) { if (c.indexOf('t-') === 0) el.classList.remove(c); });
      if (t) el.classList.add('t-' + t);
    });
  }
  function applyBadge(values, tones) {
    const label = values.state_oper, tone = tones.oper;
    if (typeof label !== 'string' || typeof tone !== 'string') return;
    document.querySelectorAll('.pd-badge .badge').forEach(function (b) { b.className = 'badge ' + (tone || 'mute'); b.textContent = label; });
  }
  function applyCharts(data) {
    const charts = data.charts || {}, legends = data.legends || {}, sparks = data.sparks || {};
    const redraw = window.LinkedEyeCharts ? window.LinkedEyeCharts.update : null;
    Object.keys(charts).forEach(function (id) {
      if (redraw && document.getElementById(id)) redraw(id, charts[id]);
    });
    Object.keys(sparks).forEach(function (id) { if (redraw && document.getElementById(id)) redraw(id, sparks[id]); });
    if (redraw && data.gauge && document.getElementById('pd-gauge')) redraw('pd-gauge', data.gauge);
    Object.keys(legends).forEach(function (id) {
      const table = document.querySelector('[data-chart-legend="' + id + '"]');
      if (!table) return;
      legends[id].forEach(function (row) {
        const tr = Array.from(table.querySelectorAll('[data-series]')).find(function (r) { return r.dataset.series === row.name; });
        if (!tr) return;
        ['last', 'mean', 'min', 'max'].forEach(function (col) {
          const cell = tr.querySelector('[data-col="' + col + '"]');
          if (cell && typeof row[col] === 'string') cell.textContent = row[col];
        });
      });
    });
  }
  function applyTimeline(oper) {
    const bar = document.getElementById('pd-timeline');
    if (!bar || !oper || !Array.isArray(oper.segments)) return;
    const nodes = oper.segments.map(function (s) {
      const span = document.createElement('span');
      span.className = 'pd-seg ' + (['up', 'down', 'other', 'nodata'].indexOf(s.cls) >= 0 ? s.cls : 'other');
      span.style.flexGrow = String(Math.max(1, Number(s.seconds) || 1));
      span.title = String(s.label || '');
      return span;
    });
    bar.replaceChildren.apply(bar, nodes);
    const set = function (id, v) { const e = document.getElementById(id); if (e && typeof v === 'string') e.textContent = v; };
    set('pd-tl-start', oper.start_label);
    set('pd-tl-end', oper.end_label);
    set('pd-oper-summary', oper.summary);
    bar.setAttribute('aria-label', 'Operational state: ' + (oper.summary || ''));
  }

  function apply(data) {
    const values = data.values || {}, tones = data.tones || {};
    applyValues(values);
    applyTones(tones);
    applyBadge(values, tones);
    applyCharts(data);
    applyTimeline(data.oper);
    stale = !!data.stale;
    showBanner(stale ? 'The stored figures for this port are old (' + (values.state_updated || 'unknown age') + '): metrics collection may have stopped.' : '');
  }

  function schedule(delay) {
    clearTimeout(timer);
    timer = null;
    if (!paused && !document.hidden) timer = setTimeout(poll, delay);
  }
  function poll() {
    timer = null;
    if (paused || document.hidden || inFlight) return;
    const ctrl = 'AbortController' in window ? new AbortController() : null;
    const killer = ctrl ? setTimeout(function () { ctrl.abort(); }, 25000) : null;
    inFlight = ctrl || true;
    fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store', signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        const type = r.headers.get('Content-Type') || '';
        if (type.indexOf('application/json') === -1) { const e = new Error('signed out, reload the page to sign in again'); e.fatal = true; throw e; }
        return r.json().then(function (data) {
          if (!r.ok) { const e = new Error(data.error || 'HTTP ' + r.status); e.fatal = r.status === 403 || r.status === 404; throw e; }
          return data;
        });
      })
      .then(function (data) {
        apply(data);
        failures = 0;
        lastOk = Date.now();
        if (paused) return;
        if (document.hidden) { setState('paused', 'Paused while this tab is hidden'); return; }
        const notes = (data.notes || []).length;
        setState(notes ? 'partial' : 'live', 'Live · ' + since() + (notes ? ' · some graphs unavailable' : ''));
        schedule(interval);
      })
      .catch(function (e) {
        failures += 1;
        if (e && e.fatal) { setState('error', 'Live updates stopped: ' + e.message); showBanner('Live updates stopped: ' + e.message); return; }
        const wait = Math.min(300000, interval * Math.pow(2, failures));
        setState('error', 'Live update failed (' + ((e && e.name === 'AbortError') ? 'timed out' : (e && e.message) || 'network error') + '), retrying');
        if (failures >= 3) showBanner('Live updates are failing; the figures on this page may be out of date.');
        schedule(wait);
      })
      .finally(function () { clearTimeout(killer); inFlight = null; });
  }

  toggle.addEventListener('click', function () {
    paused = !paused;
    toggle.setAttribute('aria-pressed', String(paused));
    toggle.textContent = paused ? 'Resume' : 'Pause';
    if (paused) { clearTimeout(timer); setState('paused', 'Paused'); }
    else { setState('live', 'Resuming…'); poll(); }
  });
  document.addEventListener('visibilitychange', function () {
    if (paused) return;
    if (document.hidden) { clearTimeout(timer); setState('paused', 'Paused while this tab is hidden'); }
    else { setState('live', 'Resuming…'); poll(); }
  });
  setInterval(function () {   // keep the "updated N s ago" text honest between polls
    if (root.dataset.state === 'live' || root.dataset.state === 'partial') text.textContent = text.textContent.replace(/updated .*?(?= ·|$)/, since());
  }, 5000);
  setState('live', 'Live · waiting for the first update');
  schedule(interval);
})();
