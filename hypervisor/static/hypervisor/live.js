/* Hypervisor live patching: polls a JSON endpoint and patches text with textContent only (no HTML is ever injected). Pauses while the tab
 * is hidden and shows a stale banner when vCenter data is old or the last sync failed. Config: <script id="hv-config"> {url, interval,
 * kind: "vm" | "datastores"}. */
(function () {
  'use strict';
  var cfgEl = document.getElementById('hv-config');
  var cfg = {};
  try { cfg = cfgEl ? JSON.parse(cfgEl.textContent) : {}; } catch (e) { cfg = {}; }
  var root = document.getElementById('hv-root');
  var banner = document.getElementById('hv-banner');
  var bannerText = document.getElementById('hv-banner-text');
  var liveEl = document.getElementById('hv-live');
  var liveText = document.getElementById('hv-live-text');
  var interval = Math.max(10, Number(cfg.interval) || 30) * 1000;
  var timer = null, rev = null, failures = 0, exampleBanner = banner && !banner.hidden && !banner.classList.contains('warn');

  function ago(epoch, now) {
    if (!epoch) return 'never';
    var s = Math.max(0, Math.round(now - epoch));
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return Math.floor(s / 86400) + ' d ago';
  }
  function setText(sel, value, scope) {
    (scope || root || document).querySelectorAll(sel).forEach(function (el) { el.textContent = value == null ? 'Not reported' : String(value); });
  }
  function tone(el, t) {
    if (!el || !el.hasAttribute('data-hv-tone')) return;
    el.className = 'chip' + (t && t !== 'info' ? ' ' + t : '');
  }
  function showBanner(text, warn) {
    if (!banner || !bannerText) return;
    if (!text) { if (!exampleBanner) banner.hidden = true; return; }
    banner.hidden = false;
    banner.classList.toggle('warn', !!warn);
    bannerText.textContent = text;
  }
  function status(st, now) {
    if (!st) return;
    setText('[data-hv="synced_ago"]', ago(st.synced_at, now));
    if (st.example) return;
    if (st.stale) {
      showBanner('vCenter data is stale: ' + String(st.label || '').toLowerCase() + (st.error ? ' (' + st.error + ')' : '') + '. Last good sync ' + ago(st.synced_at, now) + '.', true);
    } else showBanner('', false);
    if (liveText) liveText.textContent = st.stale ? 'Stale' : 'Live';
  }
  function patchVm(d) {
    var v = d.vm || {};
    document.querySelectorAll('[data-hv="power"]').forEach(function (el) { el.textContent = v.power || 'Unknown'; tone(el, v.power_tone); });
    document.querySelectorAll('[data-hv="tools"]').forEach(function (el) { el.textContent = v.tools || 'Not reported'; tone(el, v.tools_tone); });
    var ds = d.datastore;
    if (ds) {
      setText('[data-hv="ds_used"]', ds.used_pct == null ? 'Not reported' : ds.used + ' (' + ds.used_pct + '%)');
      setText('[data-hv="ds_free"]', ds.free_pct == null ? 'Not reported' : ds.free + ' (' + ds.free_pct + '%)');
      setText('[data-hv="ds_status"]', ds.status);
      document.querySelectorAll('[data-hv="ds_status"]').forEach(function (el) { tone(el, ds.tone); });
      document.querySelectorAll('[data-hv-bar="ds_used_pct"]').forEach(function (el) { if (ds.used_pct != null) { el.style.width = ds.used_pct + '%'; el.className = ds.tone; } });
      document.querySelectorAll('[data-hv-bar="ds_free_pct"]').forEach(function (el) { if (ds.free_pct != null) el.style.width = ds.free_pct + '%'; });
    }
    if (rev !== null && d.rev && d.rev !== rev) {
      showBanner('New hypervisor data arrived (snapshots, events or placement). Reload the page to see it.', false);
    }
    if (d.rev) rev = rev === null ? d.rev : rev;
  }
  function patchDatastores(d) {
    var known = {};
    (d.rows || []).forEach(function (r) {
      known[r.pk] = true;
      var row = document.querySelector('[data-hv-row="' + r.pk + '"]');
      if (!row) { showBanner('A datastore was added. Reload the page to see it.', false); return; }
      var used = row.querySelector('[data-f="used"]'), bar = row.querySelector('[data-f="bar"]'), free = row.querySelector('[data-f="free"]'), st = row.querySelector('[data-f="status"]');
      if (used && r.used_pct != null) used.textContent = r.used + ' (' + r.used_pct + '%)';
      if (bar && r.used_pct != null) { bar.style.width = r.used_pct + '%'; bar.className = r.tone; }
      if (free) free.textContent = r.free || '–';
      if (st) { st.textContent = r.status; tone(st, r.tone); }
    });
    document.querySelectorAll('[data-hv-row]').forEach(function (row) { if (!known[row.getAttribute('data-hv-row')]) showBanner('A datastore is no longer listed. Reload the page.', false); });
    var stale = (d.sources || []).filter(function (s) { return s.state === 'error' || s.state === 'auth' || s.state === 'stale'; });
    if (stale.length) showBanner('vCenter data is stale for: ' + stale.map(function (s) { return s.name + (s.error ? ' (' + s.error + ')' : ''); }).join('; ') + '.', true);
    if (liveText) liveText.textContent = stale.length ? 'Stale' : 'Live';
  }
  function poll() {
    if (document.hidden || !cfg.url) return;
    fetch(cfg.url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        failures = 0;
        if (liveEl) liveEl.hidden = false;
        if (cfg.kind === 'datastores') patchDatastores(d); else { status(d.status, d.now); patchVm(d); }
      })
      .catch(function () {
        failures += 1;
        if (failures >= 2) showBanner('Live updates are paused: LinkedEye could not be reached. The page shows the last data it had.', true);
      });
  }
  function start() { if (timer) return; poll(); timer = setInterval(poll, interval); }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  document.addEventListener('visibilitychange', function () { if (document.hidden) stop(); else start(); });
  if (cfg.url) start();

  // ---- VM actions: a button fills the request form (a request is recorded; nothing is sent to vCenter)
  var form = document.getElementById('hv-req');
  if (form) {
    var needs = {};
    try { needs = JSON.parse(document.getElementById('hv-needs-detail').textContent) || {}; } catch (e) { needs = {}; }
    var act = document.getElementById('hv-req-action'), title = document.getElementById('hv-req-title');
    var wrap = document.getElementById('hv-req-detail-wrap'), label = document.getElementById('hv-req-detail-label'), detail = document.getElementById('hv-req-detail');
    document.querySelectorAll('.hv-act[data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        act.value = b.getAttribute('data-act');
        title.textContent = 'Request: ' + (b.textContent || '').trim();
        var need = needs[act.value];
        wrap.hidden = !need; detail.required = !!need; if (need) label.textContent = need; else detail.value = '';
        form.hidden = false;
        (need ? detail : form.querySelector('input[name="reason"]')).focus();
      });
    });
    var cancel = document.getElementById('hv-req-cancel');
    if (cancel) cancel.addEventListener('click', function () { form.hidden = true; });
  }
})();
