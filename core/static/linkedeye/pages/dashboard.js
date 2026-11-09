/* Operations dashboard behaviour: sparkline history, a minute-by-minute patch of the figures and the Needs attention
 * table, the sparkline range switch, and the legacy backend card. Everything is built with textContent and
 * createElement (the JSON carries scraped names and messages); hrefs only come from the server and are checked to be
 * same-site paths. The page is complete without this file: it only adds history and keeps the numbers fresh.
 * Polling pauses while the tab is hidden and catches up when it returns. */
(function () {
  'use strict';
  const root = document.querySelector('[data-dash]');
  if (!root) return;
  const every = Math.max(15, +root.dataset.every || 60) * 1000;
  const dataUrl = root.dataset.dataUrl;
  let range = root.dataset.range || '1h';
  let lastAt = 0, busy = false, timer = 0;

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const safeHref = h => (typeof h === 'string' && /^(\/|#)/.test(h) && !/^\/\//.test(h)) ? h : '#';
  const icons = document.getElementById('dash-icons');
  const icon = name => {
    const src = icons && icons.content.querySelector('[data-ic="' + name + '"] svg');
    return src ? src.cloneNode(true) : document.createTextNode('');
  };
  const TONES = ['tone-ok', 'tone-warn', 'tone-crit', 'tone-info', 'tone-mute'];
  const setTone = (node, tone) => { TONES.forEach(t => node.classList.remove(t)); node.classList.add('tone-' + tone); };

  function patchBanner(b) {
    const box = root.querySelector('[data-banner]');
    if (!box || !b) return;
    setTone(box, b.tone);
    const t = box.querySelector('[data-banner-title]'), d = box.querySelector('[data-banner-detail]');
    if (t) t.textContent = b.title;
    if (d) d.textContent = b.detail;
  }

  function patchKpis(kpis) {
    (kpis || []).forEach(k => {
      const card = root.querySelector('[data-kpi="' + k.key + '"]');
      if (!card) return;
      setTone(card, k.tone);
      const v = card.querySelector('[data-kpi-value]'), s = card.querySelector('[data-kpi-sub]');
      if (v) v.textContent = k.value;
      if (s) s.textContent = k.sub;
      card.setAttribute('href', safeHref(k.href));
    });
  }

  function patchSpark(spark) {
    if (!spark || !window.LinkedEyeCharts) return;
    Object.keys(spark).forEach(key => {
      const src = document.getElementById('dk-spark-' + key);
      if (!src || !Array.isArray(spark[key])) return;
      let color = 'var(--chart-ok)';
      try { color = JSON.parse(src.textContent).color || color; } catch (_) { /* keep the default */ }
      window.LinkedEyeCharts.update('dk-spark-' + key, { values: spark[key], color: color });
    });
  }

  function problemRow(p) {
    const tr = el('tr');
    const sev = el('span', 'dash-sev ' + p.tone);
    sev.append(icon(p.tone === 'crit' ? 'x-circle' : 'alert'), document.createTextNode(p.status_label));
    tr.appendChild(el('td')).appendChild(sev);
    const src = el('span', 'dash-src');
    src.append(icon(p.icon), document.createTextNode(p.kind));
    tr.appendChild(el('td')).appendChild(src);
    const name = el('td', 'wrap');
    const a = el('a', '', p.name);
    a.setAttribute('href', safeHref(p.href));
    name.appendChild(a);
    if (p.detail) name.appendChild(el('small', 'dash-detail', p.detail));
    tr.appendChild(name);
    tr.appendChild(el('td', '', p.site || 'All sites'));
    tr.appendChild(el('td', '', p.since));
    const act = el('td', 'dash-act');
    if (p.ack_url) {
      const b = el('button', 'btn sm', 'Acknowledge');
      b.type = 'button';
      b.dataset.post = safeHref(p.ack_url);
      b.dataset.busy = 'Acknowledging…';
      act.appendChild(b);
    }
    tr.appendChild(act);
    return tr;
  }

  function patchProblems(rows, total) {
    const table = root.querySelector('[data-problems]');
    if (!table || !Array.isArray(rows)) return;
    const body = table.tBodies[0];
    const empty = body.querySelector('.dash-empty-row');
    if (!rows.length) { if (!empty && body.children.length) location.reload(); }   // the empty-state links are server-rendered
    else body.replaceChildren(...rows.map(problemRow));
    const c = root.querySelector('[data-problem-count]');
    if (c) { c.textContent = total; c.classList.toggle('is-on', total > 0); }
  }

  function patchEvents(events) {
    const list = root.querySelector('[data-events]');
    if (!list || !Array.isArray(events) || !events.length) return;
    list.replaceChildren(...events.map(e => {
      const li = el('li', 'sev-' + e.severity);
      li.appendChild(el('span', 'dash-ev-dot')).setAttribute('aria-hidden', 'true');
      const box = el('div');
      box.append(el('span', 'dash-ev-title', e.title), el('small', '', e.source + ' · ' + e.when + ' · ' + e.severity.charAt(0).toUpperCase() + e.severity.slice(1)));
      li.appendChild(box);
      return li;
    }));
  }

  function stamp() {
    const u = root.querySelector('[data-updated]');
    if (u) u.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  // sparkOnly: the page already holds the current figures (first load, range switch), so ask for the history alone
  async function refresh(sparkOnly) {
    if (busy || !dataUrl) return;
    busy = true;
    const btn = root.querySelector('[data-dash-refresh]');
    if (btn) btn.classList.add('is-busy');
    try {
      const url = new URL(dataUrl, location.href);
      new URLSearchParams(location.search).forEach((v, k) => { if (k !== 'range') url.searchParams.set(k, v); });
      url.searchParams.set('range', range);
      if (sparkOnly === true) url.searchParams.set('only', 'spark');
      const r = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (!r.ok) throw new Error(String(r.status));
      const d = await r.json();
      patchSpark(d.spark);
      if (sparkOnly !== true) {
        patchBanner(d.banner);
        patchKpis(d.kpis);
        patchProblems(d.problems, d.problem_total);
        patchEvents(d.events);
        stamp();
      }
      lastAt = Date.now();
    } catch (_) {
      const u = root.querySelector('[data-updated]');
      if (u) u.textContent = 'Update failed';
    } finally {
      busy = false;
      if (btn) btn.classList.remove('is-busy');
    }
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(() => { if (!document.hidden) refresh(false); }, every);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && Date.now() - lastAt >= every) refresh(false); });
  const refreshBtn = root.querySelector('[data-dash-refresh]');
  if (refreshBtn) refreshBtn.addEventListener('click', () => refresh(false));

  // the range switch re-fetches the sparklines without reloading the page; the URL still carries it for sharing
  root.querySelectorAll('[data-range-link]').forEach(a => a.addEventListener('click', e => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || !window.history || !history.replaceState) return;
    e.preventDefault();
    range = a.dataset.rangeLink;
    root.querySelectorAll('[data-range-link]').forEach(o => {
      const on = o === a;
      o.classList.toggle('on', on);
      if (on) o.setAttribute('aria-current', 'true'); else o.removeAttribute('aria-current');
    });
    const u = new URL(location.href);
    u.searchParams.set('range', range);
    history.replaceState(null, '', u);
    refresh(true);
  }));

  refresh(true);
  schedule();

  // ---------------------------------------------------------------- legacy backend card (drawn only when connected)
  document.querySelectorAll('[data-backend-summary]').forEach(box => {
    const status = box.querySelector('[data-summary-status]'), grid = box.querySelector('[data-summary-grid]'), foot = box.querySelector('[data-summary-foot]');
    const ctl = new AbortController();
    const timeout = setTimeout(() => ctl.abort(), 12000);
    const url = new URL(box.dataset.summaryUrl, location.href);
    if (box.dataset.site) url.searchParams.set('site', box.dataset.site);
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctl.signal })
      .then(r => { if (!r.ok) throw new Error('failed'); return r.json(); })
      .then(data => {
        if (!data.available) {
          status.textContent = 'Not responding';
          grid.replaceChildren(el('p', 'dash-legacy-empty', 'The legacy backend is configured but its summary is not available right now.'));
          return;
        }
        status.textContent = data.partial ? 'Partial data' : 'Connected';
        grid.replaceChildren(...(data.modules || []).map(m => {
          const c = m.counts || {};
          const card = el('div', 'dash-legacy-card');
          card.appendChild(el('b', '', m.label || m.key));
          const bar = el('div', 'dash-legacy-bar');
          bar.setAttribute('role', 'img');
          bar.setAttribute('aria-label', (m.label || m.key) + ': ' + (c.critical || 0) + ' critical, ' + (c.warning || 0) + ' warning, ' + (c.ok || 0) + ' OK, ' + (c.unknown || 0) + ' unknown');
          ['critical', 'warning', 'ok', 'unknown'].forEach(k => {
            const seg = el('i', k);
            seg.style.width = (m.total ? (100 * (c[k] || 0) / m.total) : 0) + '%';
            bar.appendChild(seg);
          });
          card.append(bar, el('small', '', (c.ok || 0) + ' OK of ' + m.total + ' · ' + (c.critical || 0) + ' critical · ' + (c.warning || 0) + ' warning'));
          return card;
        }));
        if (foot) foot.textContent = 'Totals from ' + data.site_count + ' connected site' + (data.site_count === 1 ? '' : 's') + '.' + (data.partial ? ' Some sites did not respond.' : '');
      })
      .catch(() => {
        status.textContent = 'Unavailable';
        grid.replaceChildren(el('p', 'dash-legacy-empty', 'The legacy backend summary could not be loaded. Reload the page to retry.'));
      })
      .finally(() => clearTimeout(timeout));
  });
}());
