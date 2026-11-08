/* Hardware tab (Redfish iDRAC / iLO, READ-ONLY): renders one JSON snapshot and patches itself from /hardware/server/<id>/live/.
 * Everything that came from a BMC is put in with textContent only (never innerHTML). Polling pauses while the tab is hidden and a
 * banner says when live updates stopped. The only writes this page can make are LinkedEye-side: refresh a poll, record a request. */
(function () {
  'use strict';
  var root = document.getElementById('hw');
  var dataEl = document.getElementById('hw-data'), cfgEl = document.getElementById('hw-config');
  if (!root || !dataEl || !cfgEl) return;
  var data = JSON.parse(dataEl.textContent), cfg = JSON.parse(cfgEl.textContent);
  var range = cfg.range, failedSince = null, timer = null, busy = false, flash = null;

  function $(k) { return root.querySelector('[data-k="' + k + '"]'); }
  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function chip(tone, text) { return el('span', 'chip ' + (tone === 'mute' || !tone ? 'mute' : tone), text); }
  function csrf() {
    var m = document.cookie.match(/csrftoken=([^;]+)/);
    return (m && m[1]) || (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  }
  function num(v, unit) { return v === null || v === undefined ? null : (unit === '%' ? v + ' %' : v + (unit ? ' ' + unit : '')); }
  function notReported(td) { td.appendChild(el('span', 't-mute', 'not reported')); }
  function ago(s) {
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return Math.floor(s / 86400) + ' d ago';
  }
  var TONE_LABEL = { ok: 'Normal', warn: 'Warning', crit: 'Critical', mute: 'Unknown' };
  var STATUS_LABEL = { ok: 'Normal', warning: 'Warning', critical: 'Critical', unknown: 'Unknown' };

  // ---------------------------------------------------------------- pieces
  function statusChip(row) { return chip(row.tone, STATUS_LABEL[row.status] || 'Unknown'); }

  function emptyRow(tb, cols, text) {
    var tr = el('tr'), td = el('td', 't-mute small', text);
    td.colSpan = cols;
    tr.appendChild(td);
    tb.appendChild(tr);
  }

  function renderTemps(rows) {
    var tb = $('temps'); clear(tb);
    if (!rows.length) return emptyRow(tb, 4, 'Temperature sensors not reported by this BMC.');
    rows.forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', '', r.name));
      var cur = el('td', 'num'); if (r.reading === null) notReported(cur); else cur.textContent = num(r.reading, '°C'); tr.appendChild(cur);
      var w = el('td', 'num'); if (r.warn === null) w.appendChild(el('span', 't-mute', '–')); else w.textContent = num(r.warn, '°C'); tr.appendChild(w);
      var st = el('td'); st.appendChild(statusChip(r)); tr.appendChild(st);
      tb.appendChild(tr);
    });
  }
  function renderFans(rows) {
    var tb = $('fans'); clear(tb);
    if (!rows.length) return emptyRow(tb, 3, 'Fans not reported by this BMC.');
    rows.forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', '', r.name));
      var cur = el('td', 'num'); if (r.reading === null) notReported(cur); else cur.textContent = num(r.reading, r.unit); tr.appendChild(cur);
      var st = el('td'); st.appendChild(statusChip(r)); tr.appendChild(st);
      tb.appendChild(tr);
    });
  }
  function renderPsus(rows) {
    var tb = $('psus'); clear(tb);
    if (!rows.length) return emptyRow(tb, 6, 'Power supplies not reported by this BMC.');
    rows.forEach(function (r) {
      var tr = el('tr');
      var nm = el('td'); nm.appendChild(el('span', '', r.name));
      if (r.model) { var mo = el('small', 'hw-sub-line', r.model); mo.title = r.model; nm.appendChild(mo); }
      tr.appendChild(nm);
      var st = el('td'); st.appendChild(statusChip(r)); tr.appendChild(st);
      tr.appendChild(el('td', 'num', r.input_v === null ? '–' : r.input_v + ' V'));
      tr.appendChild(el('td', 'num', r.output_w === null ? '–' : r.output_w + ' W'));
      var load = el('td');
      if (r.load_pct === null || r.load_pct === undefined) load.appendChild(el('span', 't-mute', '–'));
      else {
        var bar = el('span', 'hw-bar-load'), fill = el('i');
        fill.style.width = Math.max(2, Math.min(100, r.load_pct)) + '%';
        bar.appendChild(fill); bar.title = r.load_pct + ' % of ' + (r.capacity_w || '?') + ' W';
        bar.setAttribute('role', 'img'); bar.setAttribute('aria-label', 'Load ' + r.load_pct + ' percent');
        load.appendChild(bar);
      }
      tr.appendChild(load);
      tb.appendChild(tr);
    });
  }
  function renderVolts(rows) {
    var tb = $('volts'); clear(tb);
    if (!rows.length) return emptyRow(tb, 4, 'Voltage sensors not reported by this BMC.');
    rows.forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', '', r.name));
      var cur = el('td', 'num'); if (r.reading === null) notReported(cur); else cur.textContent = num(r.reading, 'V'); tr.appendChild(cur);
      var lo = r.low_warn !== null ? r.low_warn : r.low, hi = r.warn !== null ? r.warn : r.crit;
      tr.appendChild(el('td', 'num', lo !== null && hi !== null ? lo + ' – ' + hi + ' V' : '–'));
      var st = el('td'); st.appendChild(statusChip(r)); tr.appendChild(st);
      tb.appendChild(tr);
    });
  }
  function renderEvents(rows) {
    var tb = $('events'); clear(tb);
    if (!rows.length) return emptyRow(tb, 3, 'No events in the BMC log yet.');
    rows.forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', 'hw-time', r.ts_short));
      var sv = el('td'); sv.appendChild(chip(r.tone === 'info' ? 'mute' : r.tone, r.severity.charAt(0).toUpperCase() + r.severity.slice(1))); tr.appendChild(sv);
      var ev = el('td', 'hw-msg');
      if (r.sensor) ev.appendChild(el('b', 'hw-sensor', r.sensor));
      ev.appendChild(el('span', '', r.message));
      tr.appendChild(ev);
      tb.appendChild(tr);
    });
  }

  function icon(tone) {
    // the icons are rendered once by the server (the {% icon %} tag) and cloned here
    var src = document.querySelector('#hw-icons [data-icon="' + (tone || 'mute') + '"] svg');
    return src ? src.cloneNode(true) : document.createElement('span');
  }
  function renderTiles(tiles) {
    var host = $('tiles'); clear(host);
    tiles.forEach(function (t) {
      var d = el('div', 'hw-tile ' + t.tone), v = el('div', 'hw-tile-v');
      d.appendChild(el('span', 'hw-tile-l', t.label));
      if (t.reported) v.appendChild(icon(t.tone));
      v.appendChild(el('b', '', t.value));
      d.appendChild(v);
      if (!t.reported) d.title = 'The BMC did not report this section';
      host.appendChild(d);
    });
  }
  function renderOverview(ep, rows) {
    $('logo').textContent = ep.vendor === 'dell' ? 'DELL' : ep.vendor === 'hpe' ? 'HPE' : 'BMC';
    $('bmc_name').textContent = (ep.vendor_label || 'Redfish') + (ep.bmc && ep.bmc !== 'BMC' ? '' : '');
    $('ov_title').textContent = (ep.bmc || 'BMC') + ' overview';
    var kv = $('ov_rows'); clear(kv);
    rows.forEach(function (r) {
      kv.appendChild(el('dt', '', r.label));
      var dd = el('dd', r.mono ? 'mono' : '');
      if (r.reported) dd.textContent = r.value; else dd.appendChild(el('span', 't-mute', 'not reported'));
      kv.appendChild(dd);
    });
    var c = $('console');
    $('console_label').textContent = 'Open ' + (ep.bmc || 'BMC') + ' console';
    if (ep.console_url) c.setAttribute('href', ep.console_url);
    var st = $('state'); st.className = 'right chip ' + (ep.tone || 'mute'); st.textContent = ep.state_label;
  }

  function footText() {
    var ep = data.endpoint, parts = ['Redfish API', 'read-only account'];
    if (ep.example) parts.push('example data, no live polling');
    else if (ep.last_ok_ts) parts.push('polled ' + ago(Math.max(0, Math.round(Date.now() / 1000 - ep.last_ok_ts))));
    else parts.push('not polled yet');
    return parts.join(' · ');
  }
  function renderFoot() {
    var ep = data.endpoint;
    $('foot').textContent = footText();
    var dot = $('dot'); dot.className = 'hw-dot ' + (ep.tone === 'ok' ? 'ok' : ep.tone === 'crit' ? 'crit' : ep.tone === 'warn' ? 'warn' : '');
  }

  function renderHealth(h) {
    var r = $('rollup');
    var tone = h.rollup === 'healthy' ? 'ok' : h.rollup === 'warning' ? 'warn' : h.rollup === 'critical' ? 'crit' : 'mute';
    r.setAttribute('data-tone', tone);
    $('rollup_label').textContent = h.label;
    $('rollup_sub').textContent = h.sub;
    var ic = $('rollup_ic'); clear(ic); ic.appendChild(icon(tone));
    renderTiles(h.tiles);
  }

  function renderRequests(list, pending) {
    var host = $('requests'); clear(host);
    list.forEach(function (q) {
      var row = el('div', 'hw-req'), tone = q.state === 'approved' ? 'ok' : q.state === 'rejected' ? 'crit' : q.state === 'expired' ? 'mute' : 'warn';
      row.appendChild(chip(tone, q.state_label));
      var t = el('span', 'small'); t.textContent = q.label + ' · ' + q.by + ' · ' + q.at;
      row.appendChild(t);
      host.appendChild(row);
    });
    $('pending').textContent = pending ? ' · ' + pending + ' waiting for a decision' : '';
  }

  function renderCharts(ch, rangeLabel, rows) {
    $('range_label').textContent = rangeLabel; $('range_label2').textContent = rangeLabel;
    [['temp', 'hw-chart-temp'], ['fan', 'hw-chart-fan']].forEach(function (p) {
      var key = p[0], id = p[1], d = ch ? ch[key] : null, host = $('chart_' + key).firstElementChild;
      var lg = $('legend_' + key), none = $('nochart_' + key);
      clear(lg);
      if (!d) { host.hidden = true; none.hidden = false; return; }
      host.hidden = false; none.hidden = true;
      if (key === 'fan') $('fan_unit').textContent = d.unit;
      d.series.forEach(function (s) {
        var item = el('span', 'hw-lg');
        item.setAttribute('data-series', s.name);
        var sw = el('i'); sw.style.background = s.color;
        item.appendChild(sw); item.appendChild(document.createTextNode(s.name));
        lg.appendChild(item);
      });
      if (window.LinkedEyeCharts) window.LinkedEyeCharts.update(id, d);
      else document.getElementById(id).textContent = JSON.stringify(d);
    });
  }

  function renderVendor(ep) {
    var btns = document.querySelectorAll('#hw-vendor button');
    Array.prototype.forEach.call(btns, function (b) {
      var on = b.getAttribute('data-vendor') === ep.vendor;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.disabled = !data.perms.admin;
      if (!data.perms.admin) b.title = 'Needs the admin role on this site';
    });
    var note = document.getElementById('hw-vendor-note');
    if (ep.override) note.textContent = 'Set by hand' + (ep.detected && ep.detected !== ep.override ? ' (the BMC says ' + ep.detected + ')' : '');
    else if (ep.detected) note.textContent = 'Detected from Redfish';
    else note.textContent = 'Not detected yet';
  }

  function banner(text, tone, link) {
    var b = document.getElementById('hw-banner');
    clear(b);
    if (!text) { b.hidden = true; return; }
    b.hidden = false; b.className = 'hw-banner ' + (tone || '');
    b.appendChild(document.createTextNode(text));
    if (link) { var a = el('a', '', ' ' + link.text); a.setAttribute('href', link.href); b.appendChild(a); }
  }
  function renderBanner() {
    var ep = data.endpoint;
    if (flash) return banner(flash, 'info');
    if (failedSince) return banner('Live updates paused: LinkedEye could not be reached since ' + new Date(failedSince).toLocaleTimeString() + '. Showing the last data received.', 'warn');
    if (ep.example) return banner('Example data: this server and its sensor values are synthetic (manage.py seed_hardware). Nothing is polled.', 'info');
    if (ep.state === 'not_configured') return banner('This endpoint is not configured yet: it needs a stored login' + (ep.verify_mode === 'pin' ? ' and a pinned certificate' : '') + '.', 'warn', data.perms.admin ? { text: 'Configure it', href: cfg.urls.endpoint } : null);
    if (ep.state === 'disabled') return banner('Polling of this BMC is switched off.', 'info', data.perms.admin ? { text: 'Edit the endpoint', href: cfg.urls.endpoint } : null);
    if (ep.state === 'auth_failed') return banner('The BMC rejected the read-only login' + (ep.auth_paused ? '. Polling is paused so the account does not get locked; update the password to resume.' : '.') + (ep.error && ep.error.indexOf('rejected the login') < 0 ? ' ' + ep.error : ''), 'crit', data.perms.admin ? { text: 'Edit the endpoint', href: cfg.urls.endpoint } : null);
    if (ep.state === 'unreachable') return banner('The BMC is not reachable. The values below are the last ones read' + (ep.last_ok_ts ? ' (' + ago(Math.round(Date.now() / 1000 - ep.last_ok_ts)) + ')' : '') + '. ' + (ep.error || ''), 'crit');
    if (ep.state === 'stale') return banner('The last successful poll is older than expected. The values below may be out of date.', 'warn');
    if (ep.state === 'waiting') return banner('Waiting for the first poll of this BMC. This page updates by itself.', 'info');
    if (ep.verify_mode === 'skip') return banner('The BMC certificate is not verified for this endpoint (unsafe). Pin it on the endpoint page.', 'warn', data.perms.admin ? { text: 'Edit the endpoint', href: cfg.urls.endpoint } : null);
    if (ep.insecure_http) return banner('This endpoint uses plain HTTP: the login crosses the network unencrypted.', 'warn');
    banner('');
  }

  function render() {
    renderVendor(data.endpoint);
    renderOverview(data.endpoint, data.overview);
    renderFoot();
    renderHealth(data.health);
    renderTemps(data.temps); renderFans(data.fans); renderPsus(data.psus); renderVolts(data.volts);
    renderEvents(data.events);
    renderRequests(data.requests, data.pending_requests);
    renderCharts(data.charts, data.range_label);
    renderBanner();
  }

  // ---------------------------------------------------------------- live patching
  function load() {
    if (busy) return Promise.resolve();
    busy = true;
    return fetch(cfg.urls.live + '?range=' + encodeURIComponent(range), { credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(function (d) { data = d; failedSince = null; render(); })
      .catch(function () { if (!failedSince) failedSince = Date.now(); renderBanner(); })
      .then(function () { busy = false; });
  }
  function schedule() {
    clearInterval(timer);
    timer = setInterval(function () { if (!document.hidden) load(); else renderFoot(); }, cfg.interval * 1000);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
  setInterval(function () { if (!document.hidden) renderFoot(); }, 5000);

  // ---------------------------------------------------------------- actions
  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrf(), Accept: 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' }, body: body || '' })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); });
  }
  document.getElementById('hw-refresh').addEventListener('click', function () {
    var btn = this; btn.disabled = true;
    post(cfg.urls.refresh).then(function (res) {
      flash = res.body.message || (res.body.error || 'Could not queue a refresh.');
      renderBanner();
      setTimeout(function () { flash = null; renderBanner(); }, 6000);
      if (res.body.ok) { setTimeout(load, 8000); setTimeout(load, 20000); }
    }).catch(function () { flash = 'Could not queue a refresh.'; renderBanner(); })
      .then(function () { setTimeout(function () { btn.disabled = false; }, 4000); });
  });
  document.getElementById('hw-range').addEventListener('change', function () {
    range = this.value;
    try { var u = new URL(location.href); u.searchParams.set('hw_range', range); history.replaceState(null, '', u.toString()); } catch (e) { /* old browser */ }
    load();
  });
  Array.prototype.forEach.call(document.querySelectorAll('#hw-vendor button'), function (b) {
    b.addEventListener('click', function () {
      if (!data.perms.admin) return;
      var v = b.getAttribute('data-vendor');
      post(cfg.urls.vendor, 'vendor=' + encodeURIComponent(data.endpoint.override === v ? '' : v)).then(function () { load(); });
    });
  });
  var dlg = document.getElementById('hw-dialog');
  Array.prototype.forEach.call(root.querySelectorAll('[data-req]'), function (b) {
    b.addEventListener('click', function () {
      var a = b.getAttribute('data-req'), label = '';
      cfg.actions.forEach(function (p) { if (p[0] === a) label = p[1]; });
      dlg.querySelector('input[name=action]').value = a;
      document.getElementById('hw-dialog-h').textContent = label + ': ' + 'request';
      document.getElementById('hw-dialog-steps').textContent = cfg.steps[a] || '';
      if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    });
  });
  Array.prototype.forEach.call(dlg.querySelectorAll('[data-close]'), function (b) { b.addEventListener('click', function () { dlg.close(); }); });
  dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });

  render();
  schedule();
})();
