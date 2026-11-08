/* Device cards. Each [data-card] (network/_device_card.html) shows a photo next to live figures; this script keeps them
 * live without a reload and draws a sparkline per figure from the samples it has seen since the page opened.
 *   device detail   live.js polls network:live and fires nw:snapshot; the card's numbers arrive in that snapshot.
 *   list pages      #dc-live (data-url, data-interval) is polled for every card on the page (network:cards_live).
 * Server data is only ever written with textContent / className. A figure that is absent reads "No data" and has no
 * line: a missing sample clears the figure's history instead of drawing a zero. */
(function () {
  'use strict';
  const HISTORY = 40;
  const COLORS = { cpu: 'var(--chart-6)', memory: 'var(--brand)', in: 'var(--chart-6)', out: 'var(--brand)', temp: 'var(--chart-warn)' };
  const hist = {};                       // card pk -> figure key -> [numbers]
  let seq = 0;

  function cardEls() { return Array.prototype.slice.call(document.querySelectorAll('[data-card]')); }

  function setTone(el, tone) {
    Array.from(el.classList).forEach(function (c) { if (c.indexOf('t-') === 0) el.classList.remove(c); });
    if (tone) el.classList.add('t-' + tone);
  }

  function spark(el, pk, key, values) {
    const host = el.querySelector('[data-spark="' + key + '"]');
    if (!host || !window.LinkedEyeCharts) return;
    if (values.length < 2) { host.replaceChildren(); return; }
    let src = host._src;
    if (!src) {
      src = document.createElement('script');
      src.type = 'application/json';
      src.id = 'dcs-' + pk + '-' + key + '-' + (seq += 1);
      document.body.appendChild(src);
      host._src = src;
      host.dataset.chart = 'spark';
      host.dataset.src = src.id;
    }
    src.textContent = JSON.stringify({ values: values, color: COLORS[key] });
    try { window.LinkedEyeCharts.render(host); } catch (e) { host.replaceChildren(); }
  }

  function record(el, figures) {
    const pk = el.dataset.card;
    const h = hist[pk] = hist[pk] || {};
    Object.keys(COLORS).forEach(function (key) {
      const v = figures ? figures[key] : null;
      if (typeof v !== 'number' || !isFinite(v)) { h[key] = []; spark(el, pk, key, []); return; }
      const a = h[key] = h[key] || [];
      a.push(v);
      if (a.length > HISTORY) a.shift();
      spark(el, pk, key, a);
    });
  }

  function applyCard(el, c) {
    if (!c) return;
    el.dataset.status = c.status || el.dataset.status;
    el.querySelectorAll('[data-card-text]').forEach(function (b) {
      const k = b.dataset.cardText, v = c.values && c.values[k];
      if (typeof v === 'string' && b.textContent !== v) b.textContent = v;
      if (c.tones && typeof c.tones[k] === 'string') setTone(b, c.tones[k]);
    });
    const badge = el.querySelector('.dc-status .badge');
    if (badge && typeof c.label === 'string' && badge.textContent !== c.label) {
      badge.className = 'badge ' + (c.tone || 'mute');
      badge.textContent = c.label;
    }
    record(el, c.figures);
  }

  // ---- seed the history with what the server rendered
  cardEls().forEach(function (el) {
    let f = null;
    try { f = JSON.parse(el.dataset.figs || 'null'); } catch (e) { f = null; }
    if (f) record(el, f);
  });

  // ---- detail page: the snapshot carries this device's card
  document.addEventListener('nw:snapshot', function (ev) {
    const d = ev.detail || {}, v = d.values || {}, t = d.tones || {};
    const card = { status: d.status, label: v.status, tone: t.status, figures: d.figures, values: {}, tones: {} };
    ['uptime', 'cpu', 'memory', 'in', 'out', 'temp'].forEach(function (k) {
      card.values[k] = v['card_' + k];
      card.tones[k] = t['card_' + k];
    });
    cardEls().forEach(function (el) { applyCard(el, card); });
  });

  // ---- list pages: poll the batch endpoint
  const root = document.getElementById('dc-live');
  if (!root || !window.fetch || !root.dataset.url) return;
  const url = root.dataset.url;
  const interval = Math.max(10, Number(root.dataset.interval) || 30) * 1000;
  const text = root.querySelector('.dc-live-text');
  let timer = null, failures = 0, lastOk = Date.now(), busy = false;

  function since() {
    const s = Math.round((Date.now() - lastOk) / 1000);
    return s < 5 ? 'just now' : s < 120 ? s + ' s ago' : Math.round(s / 60) + ' min ago';
  }
  function state(s, msg) { root.dataset.state = s; text.textContent = msg; }
  function schedule(delay) {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(poll, delay);
  }
  function poll() {
    if (busy || document.hidden) return;
    const ids = cardEls().map(function (el) { return el.dataset.card; }).filter(function (v, i, a) { return a.indexOf(v) === i; });
    if (!ids.length) return;
    busy = true;
    fetch(url + '?ids=' + encodeURIComponent(ids.join(',')), { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        if ((r.headers.get('Content-Type') || '').indexOf('application/json') === -1) throw new Error('signed out, reload the page');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        cardEls().forEach(function (el) { applyCard(el, (data.cards || {})[el.dataset.card]); });
        failures = 0; lastOk = Date.now();
        state('live', 'Live · updated just now');
        schedule(interval);
      })
      .catch(function (e) {
        failures += 1;
        state('error', 'Live update failing (' + (e.message || 'network error') + '), retrying');
        schedule(Math.min(300000, interval * Math.pow(2, failures)));
      })
      .finally(function () { busy = false; });
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { clearTimeout(timer); state('paused', 'Paused while this tab is hidden'); }
    else poll();
  });
  setInterval(function () { if (root.dataset.state === 'live') text.textContent = 'Live · updated ' + since(); }, 5000);
  root.hidden = false;
  state('live', 'Live · updated just now');
  schedule(interval);
})();
