/* Device detail live monitoring. Polls network:live (data-url on #nw-live) every data-interval seconds while the
 * browser tab is visible and swaps in, without a reload:
 *   [data-live=key]       formatted values (KPIs, legends, timestamps)
 *   [data-live-tone=key]  status tone classes (t-ok / t-warn / t-crit / t-mute)
 *   json_script ids       chart data, redrawn in place by LinkedEyeCharts
 *   #front-panel ports    port colours and the drawer data (front-panel.js listens for nw:ports-updated)
 *   device card           the card's texts use the same data-live / data-live-tone keys (card_cpu, card_in ...);
 *                         device-card.js listens for nw:snapshot to draw its sparklines
 * A status change or a new alert raises a notice with a reload link. Server data is only ever written with
 * textContent (names and alert titles are device-reported). Failures back off up to 5 minutes. */
(function () {
  'use strict';
  const root = document.getElementById('nw-live');
  if (!root || !window.fetch || !root.dataset.url) return;
  const url = root.dataset.url;
  const interval = Math.max(10, Number(root.dataset.interval) || 30) * 1000;
  const text = root.querySelector('.nw-live-text');
  const toggle = root.querySelector('.nw-live-toggle');
  const notice = document.getElementById('nw-live-notice');
  const TONES = ['up', 'busy', 'down', 'unused', 'unknown'];
  let timer = null, inFlight = null, paused = false, failures = 0, lastOk = Date.now(), note = '';

  function setState(state, msg) {
    root.dataset.state = state;          // live | partial | paused | error
    text.textContent = msg;
  }
  function since() {
    const s = Math.round((Date.now() - lastOk) / 1000);
    return s < 5 ? 'updated just now' : s < 120 ? 'updated ' + s + ' s ago' : 'updated ' + Math.round(s / 60) + ' min ago';
  }
  function showLive() {
    if (root.dataset.state === 'live' || root.dataset.state === 'partial')
      setState(root.dataset.state, 'Live · ' + since() + (note ? ' · some graphs unavailable' : ''));
  }

  function flash(el) {
    el.classList.remove('nw-live-flash');
    void el.offsetWidth;                 // restart the animation
    el.classList.add('nw-live-flash');
  }
  function applyValues(values) {
    document.querySelectorAll('[data-live]').forEach(function (el) {
      const v = values[el.dataset.live];
      if (typeof v !== 'string' || el.textContent === v) return;
      el.textContent = v;
      if (el.matches('.value, b')) flash(el);
    });
  }
  function applyTones(tones) {
    document.querySelectorAll('[data-live-tone]').forEach(function (el) {
      const t = tones[el.dataset.liveTone];
      if (typeof t !== 'string') return;
      Array.from(el.classList).forEach(function (c) { if (c.indexOf('t-') === 0) el.classList.remove(c); });
      if (t) el.classList.add('t-' + t);
    });
  }
  function applyCharts(charts) {
    Object.keys(charts).forEach(function (id) {
      const data = charts[id], src = document.getElementById(id);
      if (!data || !src) return;         // a chart that was empty at load time appears on the next page load
      const json = JSON.stringify(data);
      if (src.textContent === json) return;
      src.textContent = json;
      if (window.LinkedEyeCharts)
        document.querySelectorAll('.chart[data-src="' + id + '"]').forEach(function (h) { window.LinkedEyeCharts.render(h); });
    });
  }
  function applyPorts(ports) {
    const src = document.getElementById('nw-ports');
    if (!src || !ports || !ports.length) return;
    let old;
    try { old = JSON.parse(src.textContent); } catch (e) { return; }
    const same = old.length === ports.length && old.every(function (p, i) { return p.ifIndex === ports[i].ifIndex; });
    if (!same) { notify('The device’s interface list changed.'); return; }
    document.querySelectorAll('#front-panel .fp-port').forEach(function (b) {
      const p = ports[Number(b.dataset.port)];
      if (!p) return;
      TONES.forEach(function (t) { b.classList.toggle(t, t === p.tone); });
      b.setAttribute('aria-label', 'Port ' + p.n + ', ' + p.name + ', ' + p.toneLabel);
    });
    src.textContent = JSON.stringify(ports);
    document.dispatchEvent(new CustomEvent('nw:ports-updated'));
  }
  function applyBadge(tone, label) {
    // header status pill: new tone and text; the icon is dropped rather than left showing the old state
    if (typeof tone !== 'string' || typeof label !== 'string') return;
    document.querySelectorAll('.nw-live-badge .badge').forEach(function (b) {   // the title and the device card
      b.className = 'badge ' + (tone || 'mute');
      b.textContent = label;
    });
  }
  function notify(msg) {
    if (!notice) return;
    notice.querySelector('.nw-live-notice-text').textContent = msg;
    notice.querySelector('a').href = location.href;
    notice.hidden = false;
  }

  function apply(data) {
    applyValues(data.values || {});
    applyTones(data.tones || {});
    applyCharts(data.charts || {});
    applyPorts(data.ports);
    document.dispatchEvent(new CustomEvent('nw:snapshot', { detail: data }));   // device-card.js: sparklines
    if (data.status !== root.dataset.status) {
      applyBadge((data.tones || {}).status, (data.values || {}).status);
      notify('Status changed to ' + (data.values || {}).status + '.');
    }
    else if (data.latest_event && String(data.latest_event.id) !== root.dataset.event)
      notify('New alert: ' + data.latest_event.title + ' (' + data.latest_event.ago + ').');
    note = (data.notes || [])[0] || '';
    root.title = note;
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
    fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store',
                 signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        const type = r.headers.get('Content-Type') || '';
        if (type.indexOf('application/json') === -1) {
          const e = new Error('signed out, reload the page to sign in again'); e.fatal = true; throw e;
        }
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
        setState(note ? 'partial' : 'live', '');
        showLive();
        schedule(interval);
      })
      .catch(function (e) {
        if (paused) return;              // aborted by the pause button
        failures += 1;
        const why = e.name === 'AbortError' ? 'timed out' : (e.message || 'network error');
        if (e.fatal) { setState('error', 'Live updates stopped: ' + why); return; }
        setState('error', 'Live updates failing (' + why + '), retrying · last ' + since());
        schedule(Math.min(300000, interval * Math.pow(2, failures)));
      })
      .finally(function () { clearTimeout(killer); inFlight = null; });
  }

  toggle.addEventListener('click', function () {
    paused = !paused;
    toggle.setAttribute('aria-pressed', String(paused));
    toggle.textContent = paused ? 'Resume' : 'Pause';
    if (paused) {
      clearTimeout(timer);
      if (inFlight && inFlight.abort) inFlight.abort();
      setState('paused', 'Paused · ' + since());
    } else {
      setState('live', '');
      poll();
    }
  });
  document.addEventListener('visibilitychange', function () {
    if (paused) return;
    if (document.hidden) { clearTimeout(timer); setState('paused', 'Paused while this tab is hidden'); }
    else { setState('live', ''); poll(); }
  });

  root.hidden = false;
  setState('live', '');
  showLive();
  setInterval(showLive, 5000);
  schedule(interval);                  // the page was rendered with fresh data
})();
