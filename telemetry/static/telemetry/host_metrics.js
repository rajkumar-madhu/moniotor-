/* Host metrics page (telemetry/host_metrics.html).
 *
 * Live mode: while the "Live" button is pressed, poll <instance>/metrics.json every POLL_MS and update every chart
 * (window.LinkedEyeCharts.update keeps zoom and hidden series), the summary gauges / KPIs, the legend tables and the
 * per-core bars in place. Polling pauses while the tab is hidden and resumes when it is shown again; the toggle is
 * remembered in sessionStorage. Every value coming back is written with textContent only (series names are scraped
 * Prometheus label values). Also drives the "On this page" section list.
 */
(function () {
  'use strict';
  var POLL_MS = 30000;
  var KEY = 'le-hm-live';

  // ------------------------------------------------------------ section TOC
  (function toc() {
    var sel = document.querySelector('[data-toc-select]');
    if (sel) sel.addEventListener('change', function () { if (sel.value) { location.hash = sel.value; } });
    var links = document.querySelectorAll('.hm-toc nav a[data-toc]');
    if (!links.length || !('IntersectionObserver' in window)) return;
    var current = null;
    function mark(id) {
      if (id === current) return;
      current = id;
      links.forEach(function (a) { a.classList.toggle('on', a.dataset.toc === id); });
      if (sel && sel.value !== id) sel.value = id;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) mark(e.target.id); });
    }, { rootMargin: '-80px 0px -60% 0px' });
    document.querySelectorAll('.hm-section[id]').forEach(function (s) { io.observe(s); });
    mark((location.hash || '#summary').slice(1));
  })();

  // ------------------------------------------------------------ live mode
  var btn = document.getElementById('hm-live');
  if (!btn) return;
  var updated = document.getElementById('hm-updated');
  var errEl = document.getElementById('hm-live-err');
  var url = btn.getAttribute('data-live-url');
  var timer = null, inflight = false, on = false;

  function setTone(el, t) {
    el.className = el.className.replace(/\bt-(ok|warn|crit|mute)\b/g, '').replace(/\s{2,}/g, ' ').trim();
    if (t) el.classList.add('t-' + t);
  }
  function setKpi(el, text) {
    if (!el.hasAttribute('data-unit-small')) { el.textContent = text; return; }
    // "1.0 MB/s" -> 1.0<small> MB/s</small>, like the unit_small template filter
    var i = text.indexOf(' ');
    el.replaceChildren();
    if (i > 0 && /\d/.test(text.charAt(0))) {
      el.appendChild(document.createTextNode(text.slice(0, i)));
      var small = document.createElement('small');
      small.textContent = text.slice(i);
      el.appendChild(small);
    } else {
      el.textContent = text;
    }
  }
  function showError(msg) {
    if (!errEl) return;
    errEl.textContent = msg || '';
    errEl.hidden = !msg;
  }
  function stamp() {
    if (!updated) return;
    var d = new Date(), p = function (n) { return (n < 10 ? '0' : '') + n; };
    updated.textContent = 'updated ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function updateChart(id, payload) {
    var api = window.LinkedEyeCharts;
    if (!api) return;
    if (typeof api.update === 'function') { api.update(id, payload); return; }
    // older engine: swap the json_script content and redraw every host bound to it
    var src = document.getElementById(id);
    if (!src) return;
    src.textContent = JSON.stringify(payload);
    document.querySelectorAll('.chart[data-src]').forEach(function (h) { if (h.dataset.src === id) api.render(h); });
  }
  function apply(j) {
    var charts = j.charts || {};
    Object.keys(charts).forEach(function (id) { if (document.getElementById(id)) updateChart(id, charts[id]); });
    var kpis = j.summary || {}, tones = j.tones || {};
    document.querySelectorAll('[data-kpi]').forEach(function (el) {
      var k = el.dataset.kpi;
      if (k === 'health') {
        if (j.health && el.hasAttribute('data-badge')) { el.textContent = j.health.label; el.className = 'badge ' + j.health.tone; }
        return;
      }
      if (Object.prototype.hasOwnProperty.call(kpis, k)) setKpi(el, String(kpis[k]));
      if (el.hasAttribute('data-tone') && tones[k]) setTone(el, tones[k]);
    });
    var legends = j.legends || {};
    document.querySelectorAll('[data-chart-legend]').forEach(function (box) {
      var rows = legends[box.dataset.chartLegend];
      if (!rows) return;
      // keys are scraped Prometheus label values, so a plain object literal would let a series called
      // "__proto__" replace the lookup table's prototype and silently stop every row from updating
      var byName = Object.create(null);
      rows.forEach(function (r) { byName[r.name] = r; });
      box.querySelectorAll('tr[data-series]').forEach(function (tr) {
        var r = byName[tr.dataset.series];
        if (!r) return;
        tr.querySelectorAll('[data-col]').forEach(function (td) {
          var c = td.dataset.col;
          if (r[c] !== undefined) td.textContent = String(r[c]);
        });
      });
    });
    var cores = Object.create(null);                 // same: the cpu label is scraped too
    (j.cores || []).forEach(function (c) { cores[String(c.cpu)] = c; });
    document.querySelectorAll('.hm-cores .core[data-cpu]').forEach(function (row) {
      var c = cores[row.dataset.cpu];
      if (!c) return;
      var fill = row.querySelector('.track i'), val = row.querySelector('.val');
      if (fill) { fill.style.width = Math.max(0, Math.min(100, Number(c.busy) || 0)) + '%'; fill.className = 'fill-' + c.grade; }
      if (val) { val.textContent = c.text; setTone(val, c.grade); }
    });
  }
  function poll() {
    if (inflight) return Promise.resolve();
    inflight = true;
    return fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (r) {
        return r.text().then(function (t) {
          var j;
          try { j = JSON.parse(t); } catch (e) { throw new Error(r.ok ? 'unexpected response (not JSON)' : 'HTTP ' + r.status); }
          if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
          return j;
        });
      })
      .then(function (j) { if (!on) return; apply(j); stamp(); showError(''); })   // switched off while in flight: drop the answer
      .catch(function (e) { if (!on) return; showError('Live update failed: ' + (e && e.message ? e.message : e)); })
      .then(function () { inflight = false; });
  }
  function schedule() {
    clearTimeout(timer);
    if (on && !document.hidden) timer = setTimeout(tick, POLL_MS);
  }
  function tick() {
    if (!on) return;
    if (document.hidden) return;            // paused: visibilitychange resumes it
    poll().then(schedule);
  }
  function setOn(v) {
    on = !!v;
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    try { sessionStorage.setItem(KEY, on ? '1' : '0'); } catch (e) { /* storage blocked: the toggle still works */ }
    clearTimeout(timer);
    if (on) { poll().then(schedule); } else { showError(''); if (updated) updated.textContent = ''; }
  }
  btn.addEventListener('click', function () { setOn(!on); });
  document.addEventListener('visibilitychange', function () {
    if (!on) return;
    if (document.hidden) clearTimeout(timer); else poll().then(schedule);
  });
  var remembered = null;
  try { remembered = sessionStorage.getItem(KEY); } catch (e) { /* ignore */ }
  if (remembered === '1') setOn(true);
})();
