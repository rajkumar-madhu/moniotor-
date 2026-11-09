/* Fleet Overview page (telemetry/overview.html).
 *
 * - sections: <details> that remember open/closed in localStorage (try/catch: private windows throw), a section list on
 *   the right that follows the scroll position
 * - Live mode: while "Live" is pressed, poll data.json (same GET parameters as the page) every N seconds and update every
 *   chart (LinkedEyeCharts.update keeps zoom and hidden series), the legend tables and the node counts in place. Polling
 *   pauses while the tab is hidden; the toggle and interval are remembered in sessionStorage
 * - node picker (filter + select all / clear), the alerts / hot nodes / nodes drawer, a share button
 * Everything coming from the server (series names are scraped label values) is written with textContent only.
 */
(function () {
  'use strict';
  var root = document.getElementById('ov');
  if (!root) return;
  var SEC_KEY = 'le-ov-sections', LIVE_KEY = 'le-ov-live', INT_KEY = 'le-ov-live-int';

  function store(kind, k, v) { try { var s = window[kind]; if (v === undefined) return s.getItem(k); s.setItem(k, v); } catch (e) { /* blocked */ } return null; }

  // ------------------------------------------------------------ sections
  // whatever is in storage (a string, a number, an array, broken JSON) becomes a plain {section: bool} object
  function savedSections() {
    var v = null;
    try { v = JSON.parse(store('localStorage', SEC_KEY) || '{}'); } catch (e) { v = null; }
    var out = Object.create(null);
    if (v && typeof v === 'object' && !Array.isArray(v)) Object.keys(v).forEach(function (k) { if (typeof v[k] === 'boolean') out[k] = v[k]; });
    return out;
  }
  var sections = Array.prototype.slice.call(document.querySelectorAll('details.ov-sec[data-section]'));
  (function restore() {
    var saved = savedSections();
    sections.forEach(function (d) {
      if (saved[d.dataset.section] === false) d.open = false;
      d.addEventListener('toggle', function () {
        var cur = savedSections();
        cur[d.dataset.section] = d.open;
        store('localStorage', SEC_KEY, JSON.stringify(cur));
        if (d.open && window.LinkedEyeCharts) window.LinkedEyeCharts.refresh();   // a chart drawn while hidden has no size
      });
    });
  })();
  // an anchor to a collapsed section opens it
  function openFor(id) {
    var d = id && document.querySelector('details.ov-sec[data-section="' + id.replace(/[^\w-]/g, '') + '"]');
    if (d && !d.open) d.open = true;
  }
  window.addEventListener('hashchange', function () { openFor(location.hash.slice(1)); });
  openFor(location.hash.slice(1));

  (function toc() {
    var sel = document.querySelector('[data-toc-select]');
    if (sel) sel.addEventListener('change', function () { if (sel.value) { openFor(sel.value); location.hash = sel.value; } });
    var links = document.querySelectorAll('.ov-toc nav a[data-toc]');
    links.forEach(function (a) { a.addEventListener('click', function () { openFor(a.dataset.toc); }); });
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
    document.querySelectorAll('.ov-section[id]').forEach(function (s) { io.observe(s); });
    var first = document.querySelector('.ov-section[id]');
    mark((location.hash || (first ? '#' + first.id : '')).slice(1));
  })();

  // ------------------------------------------------------------ node picker
  (function nodes() {
    var filter = document.querySelector('[data-ov-node-filter]');
    var items = Array.prototype.slice.call(document.querySelectorAll('.ov-node-list li[data-name]'));
    if (filter) filter.addEventListener('input', function () {
      var q = filter.value.trim().toLowerCase();
      items.forEach(function (li) { li.hidden = !!q && li.dataset.name.indexOf(q) < 0; });
    });
    document.querySelectorAll('[data-ov-nodes]').forEach(function (b) {
      b.addEventListener('click', function () {
        var on = b.dataset.ovNodes === 'all';
        items.forEach(function (li) {
          if (li.hidden) return;
          var cb = li.querySelector('input[type=checkbox]');
          if (cb) cb.checked = on;
        });
      });
    });
    // a popover closes on Escape or when focus / a click leaves it
    var pops = document.querySelectorAll('details.ov-pop-wrap, details.ov-nodes-pick');
    pops.forEach(function (d) {
      d.addEventListener('keydown', function (e) { if (e.key === 'Escape' && d.open) { d.open = false; var s = d.querySelector('summary'); if (s) s.focus(); } });
    });
    document.addEventListener('click', function (e) {
      pops.forEach(function (d) { if (d.open && !d.contains(e.target)) d.open = false; });
    });
  })();

  // ------------------------------------------------------------ drawer
  (function drawer() {
    var box = document.getElementById('ov-drawer');
    if (!box) return;
    var toggles = document.querySelectorAll('[data-ov-drawer-toggle]');
    var opener = null;
    var tabs = Array.prototype.slice.call(box.querySelectorAll('[data-ov-tab]'));
    var panes = Array.prototype.slice.call(box.querySelectorAll('[data-ov-pane]'));
    function setOpen(v) {
      box.hidden = !v;
      toggles.forEach(function (t) { if (t.hasAttribute('aria-controls')) t.setAttribute('aria-expanded', v ? 'true' : 'false'); });
      if (v) { opener = document.activeElement; var t = box.querySelector('[role=tab][aria-selected=true]'); if (t) t.focus(); }
      else if (opener && opener.focus) { opener.focus(); opener = null; }
    }
    function show(name) {
      tabs.forEach(function (t) { var on = t.dataset.ovTab === name; t.setAttribute('aria-selected', on ? 'true' : 'false'); t.tabIndex = on ? 0 : -1; });
      panes.forEach(function (p) { p.hidden = p.dataset.ovPane !== name; });
    }
    toggles.forEach(function (t) { t.addEventListener('click', function () { setOpen(box.hidden); }); });
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { show(t.dataset.ovTab); });
      t.addEventListener('keydown', function (e) {
        var n = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : null;
        if (n === null) return;
        e.preventDefault();
        var nt = tabs[(n + tabs.length) % tabs.length];
        show(nt.dataset.ovTab); nt.focus();
      });
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !box.hidden) setOpen(false); });
    var f = box.querySelector('[data-ov-drawer-filter]');
    if (f) f.addEventListener('input', function () {
      var q = f.value.trim().toLowerCase();
      box.querySelectorAll('.ov-nodes-list li[data-name]').forEach(function (li) { li.hidden = !!q && li.dataset.name.indexOf(q) < 0; });
    });
  })();

  // ------------------------------------------------------------ share link
  (function share() {
    var btn = document.querySelector('[data-ov-share]');
    if (!btn) return;
    var label = btn.querySelector('[data-ov-share-text]');
    btn.addEventListener('click', function () {
      var u = new URL(location.href);
      u.searchParams.delete('refresh'); u.searchParams.delete('format'); u.searchParams.delete('chart');
      if (live.on) u.searchParams.set('live', String(live.interval())); else u.searchParams.delete('live');
      var text = u.toString();
      function done(ok) {
        if (!label) return;
        label.textContent = ok ? 'Link copied' : 'Copy failed';
        setTimeout(function () { label.textContent = 'Share'; }, 2000);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      else {
        var ta = document.createElement('textarea');
        ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        var ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(ta); done(ok);
      }
    });
  })();

  // ------------------------------------------------------------ live mode
  var live = { on: false, interval: function () { return 30; } };
  (function liveMode() {
    var btn = document.getElementById('ov-live');
    if (!btn) return;
    var sel = document.getElementById('ov-live-int');
    var updated = document.getElementById('ov-updated');
    var errEl = document.getElementById('ov-live-err');
    var base = root.dataset.jsonUrl, qs = root.dataset.qLive || '';
    var timer = null, inflight = false;
    live.interval = function () { var n = sel ? parseInt(sel.value, 10) : 30; return n === 15 || n === 30 || n === 60 ? n : 30; };

    function showError(msg) { if (!errEl) return; errEl.textContent = msg || ''; errEl.hidden = !msg; }
    function stamp() {
      if (!updated) return;
      var d = new Date(), p = function (n) { return (n < 10 ? '0' : '') + n; };
      updated.textContent = 'updated ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    }
    function apply(j) {
      var api = window.LinkedEyeCharts, charts = j.charts || {};
      Object.keys(charts).forEach(function (id) { if (api && document.getElementById(id)) api.update(id, charts[id]); });
      var legends = j.legends || {};
      document.querySelectorAll('[data-chart-legend]').forEach(function (box) {
        var rows = legends[box.dataset.chartLegend];
        if (!rows) return;
        var by = Object.create(null);            // series names are scraped label values: no plain-object lookup
        rows.forEach(function (r) { by[r.name] = r; });
        box.querySelectorAll('tr[data-series]').forEach(function (tr) {
          var r = by[tr.dataset.series];
          if (!r) return;
          tr.querySelectorAll('[data-col]').forEach(function (td) { if (r[td.dataset.col] !== undefined) td.textContent = String(r[td.dataset.col]); });
        });
      });
      var counts = j.counts || {};
      document.querySelectorAll('[data-ov-count]').forEach(function (el) {
        if (Object.prototype.hasOwnProperty.call(counts, el.dataset.ovCount)) el.textContent = String(counts[el.dataset.ovCount]);
      });
      var selected = document.querySelector('[data-ov="selected"]');
      if (selected && j.selected !== undefined) selected.textContent = String(j.selected);
    }
    function poll() {
      if (inflight) return Promise.resolve();
      inflight = true;
      return fetch(base + '?' + qs, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } })
        .then(function (r) {
          return r.text().then(function (t) {
            var j;
            try { j = JSON.parse(t); } catch (e) { throw new Error(r.ok ? 'unexpected response (not JSON)' : 'HTTP ' + r.status); }
            if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
            return j;
          });
        })
        .then(function (j) { if (!live.on) return; apply(j); stamp(); showError(j.error ? 'Prometheus: ' + j.error : ''); })
        .catch(function (e) { if (!live.on) return; showError('Live update failed: ' + (e && e.message ? e.message : e)); })
        .then(function () { inflight = false; });
    }
    function schedule() { clearTimeout(timer); if (live.on && !document.hidden) timer = setTimeout(tick, live.interval() * 1000); }
    function tick() { if (!live.on || document.hidden) return; poll().then(schedule); }
    function setOn(v) {
      live.on = !!v;
      btn.setAttribute('aria-pressed', live.on ? 'true' : 'false');
      store('sessionStorage', LIVE_KEY, live.on ? '1' : '0');
      clearTimeout(timer);
      if (live.on) poll().then(schedule); else { showError(''); if (updated) updated.textContent = ''; }
    }
    btn.addEventListener('click', function () { setOn(!live.on); });
    if (sel) sel.addEventListener('change', function () { store('sessionStorage', INT_KEY, sel.value); schedule(); });
    document.addEventListener('visibilitychange', function () {
      if (!live.on) return;
      if (document.hidden) clearTimeout(timer); else poll().then(schedule);
    });
    var wantedInt = store('sessionStorage', INT_KEY);
    if (sel && wantedInt && !root.dataset.live) { for (var i = 0; i < sel.options.length; i++) if (sel.options[i].value === wantedInt) sel.value = wantedInt; }
    if (root.dataset.live && root.dataset.live !== '0') setOn(true);
    else if (store('sessionStorage', LIVE_KEY) === '1') setOn(true);
  })();
})();
