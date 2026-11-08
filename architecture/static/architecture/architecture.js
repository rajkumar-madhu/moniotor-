/* Data Center Architecture page.
 *   hover / focus a device  -> its links and neighbours stay lit, the rest dims; a tooltip lists its facts
 *   click a link            -> the side panel shows both endpoints, state, utilisation and an edit link
 *   Live                    -> /network/architecture/live/ is polled every 30 s, only while the tab is visible, with
 *                              back-off after errors; dots, link colours and the KPI strip are patched in place
 *   Download SVG            -> a standalone copy with the theme colours resolved (photos are embedded)
 * Everything the server sent is written with textContent / setAttribute; no markup is ever built from it. */
(function () {
  'use strict';
  const svg = document.getElementById('ar-svg');
  const dataEl = document.getElementById('ar-data');
  if (!svg || !dataEl) return;
  let DATA, CONF;
  try { DATA = JSON.parse(dataEl.textContent); CONF = JSON.parse(document.getElementById('ar-conf').textContent); } catch (e) { return; }

  const tip = document.getElementById('ar-tip');
  const side = document.getElementById('ar-side');
  const stage = side ? side.parentElement : null;
  const nodes = {}, edges = {};
  svg.querySelectorAll('.nd-a[data-dev]').forEach(function (a) { nodes[a.dataset.dev] = a; });
  svg.querySelectorAll('.lk[data-link]').forEach(function (g) { edges[g.dataset.link] = g; });
  const STATES = ['up', 'down', 'unknown', 'disabled'];
  let selected = null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function setState(node, state) {
    const g = node.querySelector('.nd, .nd-chip');
    if (!g) return;
    STATES.forEach(function (s) { g.classList.remove('st-' + s); });
    g.classList.add('st-' + state);
  }
  function speed(m) {
    if (!m) return '–';
    return m >= 1000 ? (Math.round(m / 100) / 10) + ' Gbps' : m + ' Mbps';
  }
  function rate(v) { return typeof v === 'number' ? (v >= 1000 ? (v / 1000).toFixed(2) + ' Gbps' : v.toFixed(v >= 100 ? 0 : 1) + ' Mbps') : '–'; }

  // ---------------------------------------------------------------- highlight
  function clearHl() {
    svg.classList.remove('has-hl');
    svg.querySelectorAll('.is-hl').forEach(function (n) { n.classList.remove('is-hl'); });
  }
  function highlight(devId) {
    clearHl();
    const keys = DATA.adj[devId] || [];
    const near = new Set([devId]);
    keys.forEach(function (k) {
      const l = DATA.links[k];
      if (!l) return;
      near.add(String(l.a)); near.add(String(l.b));
      if (edges[k]) edges[k].classList.add('is-hl');
    });
    near.forEach(function (id) { if (nodes[id]) nodes[id].classList.add('is-hl'); });
    svg.classList.add('has-hl');
  }

  // ---------------------------------------------------------------- tooltip
  function row(box, k, v) {
    if (v === '' || v === null || v === undefined) return;
    const r = el('div', 'r');
    r.appendChild(el('span', '', k));
    r.appendChild(el('span', '', v));
    box.appendChild(r);
  }
  function showTip(e, build) {
    if (!tip) return;
    tip.replaceChildren();
    build(tip);
    tip.hidden = false;
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > window.innerWidth - 8) x = Math.max(8, e.clientX - w - pad);
    if (y + h > window.innerHeight - 8) y = Math.max(8, e.clientY - h - pad);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function hideTip() { if (tip) tip.hidden = true; }
  function deviceTip(id) {
    const d = DATA.devices[id];
    return function (box) {
      if (!d) return;
      box.appendChild(el('b', '', d.name));
      row(box, 'Status', d.state_label);
      row(box, 'Type', d.type);
      row(box, 'Management IP', d.ip);
      row(box, 'Site', d.site);
      row(box, 'Environment', d.env);
      row(box, 'Tier', d.tier + (d.group ? ' · ' + d.group : ''));
      row(box, 'Uptime', d.uptime);
      row(box, 'CPU', d.cpu);
      row(box, 'Memory', d.mem);
      row(box, 'Last seen', d.seen);
      if (d.weak) box.appendChild(el('div', 'why', 'Inferred: ' + d.why));
    };
  }
  function linkTip(key) {
    const l = DATA.links[key];
    return function (box) {
      if (!l) return;
      box.appendChild(el('b', '', l.a_name + ' ' + (l.a_port || '') + ' ↔ ' + l.b_name + ' ' + (l.b_port || '')));
      row(box, 'State', l.state);
      row(box, 'Utilisation', l.util === null ? '–' : l.util + '%');
      row(box, 'Speed', speed(l.speed));
    };
  }

  svg.addEventListener('mouseover', function (e) {
    const a = e.target.closest('.nd-a');
    if (a) { highlight(a.dataset.dev); showTip(e, deviceTip(a.dataset.dev)); return; }
    const g = e.target.closest('.lk[data-link]');
    if (g) { showTip(e, linkTip(g.dataset.link)); }
  });
  svg.addEventListener('mousemove', function (e) {
    if (tip && !tip.hidden && (e.target.closest('.nd-a') || e.target.closest('.lk[data-link]'))) {
      const pad = 14;
      let x = e.clientX + pad, y = e.clientY + pad;
      if (x + tip.offsetWidth > window.innerWidth - 8) x = Math.max(8, e.clientX - tip.offsetWidth - pad);
      if (y + tip.offsetHeight > window.innerHeight - 8) y = Math.max(8, e.clientY - tip.offsetHeight - pad);
      tip.style.left = x + 'px'; tip.style.top = y + 'px';
    }
  });
  svg.addEventListener('mouseout', function (e) {
    const to = e.relatedTarget;
    if (!to || !svg.contains(to) || (!to.closest('.nd-a') && !to.closest('.lk[data-link]'))) { hideTip(); }
    if (!to || !svg.contains(to) || !to.closest('.nd-a')) { clearHl(); }
  });
  svg.addEventListener('focusin', function (e) {
    const a = e.target.closest('.nd-a');
    if (!a) return;
    highlight(a.dataset.dev);
    const r = a.getBoundingClientRect();
    showTip({ clientX: r.right, clientY: r.top }, deviceTip(a.dataset.dev));
  });
  svg.addEventListener('focusout', function () { clearHl(); hideTip(); });

  // ---------------------------------------------------------------- link panel
  function dl(box, k, v, node) {
    box.appendChild(el('dt', '', k));
    const dd = el('dd');
    if (node) dd.appendChild(node); else dd.textContent = v;
    box.appendChild(dd);
  }
  function deviceLink(id, port) {
    const d = DATA.devices[id];
    const span = el('span');
    if (d && typeof d.href === 'string' && d.href.charAt(0) === '/') {
      const a = el('a', '', d.name); a.href = d.href; span.appendChild(a);
    } else { span.textContent = d ? d.name : '?'; }
    if (port) span.appendChild(document.createTextNode('  ' + port));
    return span;
  }
  function showLink(key) {
    const l = DATA.links[key];
    if (!l || !side) return;
    if (selected && edges[selected]) edges[selected].classList.remove('is-sel');
    selected = key;
    if (edges[key]) edges[key].classList.add('is-sel');
    document.getElementById('ar-side-title').textContent = 'Link';
    const body = document.getElementById('ar-side-body');
    body.replaceChildren();
    dl(body, 'Device A', '', deviceLink(l.a, l.a_port));
    dl(body, 'Device B', '', deviceLink(l.b, l.b_port));
    const st = el('span', 'ar-st ' + l.state); st.appendChild(el('i')); st.appendChild(document.createTextNode(l.state));
    dl(body, 'State', '', st);
    dl(body, 'Medium', l.medium);
    dl(body, 'Speed', speed(l.speed) + (l.speed && !l.speed_set ? ' (from the interface)' : ''));
    dl(body, 'Utilisation', l.util === null ? 'no data' : l.util + '% (busiest end)');
    dl(body, 'Port A', (l.oper_a ? 'oper ' + l.oper_a : 'no interface data') + ' · in ' + rate(l.rate_a[0]) + ' · out ' + rate(l.rate_a[1]));
    dl(body, 'Port B', (l.oper_b ? 'oper ' + l.oper_b : 'no interface data') + ' · in ' + rate(l.rate_b[0]) + ' · out ' + rate(l.rate_b[1]));
    dl(body, 'Kind', l.kind || '–');
    dl(body, 'Origin', l.origin + (l.confirmed ? '' : ' (not confirmed)'));
    dl(body, 'Last change', l.updated ? new Date(l.updated).toLocaleString() : '–');
    if (l.note) dl(body, 'Note', l.note);
    const act = document.getElementById('ar-side-actions');
    act.replaceChildren();
    if (l.edit && l.edit.charAt(0) === '/') {
      const a = el('a', 'btn sm', 'Edit link');
      a.href = l.edit + '?next=' + encodeURIComponent(location.pathname + location.search);
      act.appendChild(a);
    }
    side.hidden = false;
    if (stage) stage.classList.add('with-side');
  }
  function closeLink() {
    if (selected && edges[selected]) edges[selected].classList.remove('is-sel');
    selected = null;
    if (side) side.hidden = true;
    if (stage) stage.classList.remove('with-side');
  }
  svg.addEventListener('click', function (e) {
    const g = e.target.closest('.lk[data-link]');
    if (g && !e.target.closest('.nd-a')) { e.preventDefault(); hideTip(); showLink(g.dataset.link); }
  });
  const close = document.getElementById('ar-side-close');
  if (close) close.addEventListener('click', closeLink);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { closeLink(); hideTip(); } });

  // ---------------------------------------------------------------- live
  const label = { up: 'Up', down: 'Down', unknown: 'Unknown', disabled: 'Monitoring disabled' };
  function kpi(name, text) {
    document.querySelectorAll('[data-kpi="' + name + '"]').forEach(function (n) { n.textContent = text; });
  }
  function banner(prom) {
    const host = document.getElementById('ar-prom');
    if (!host) return;
    let box = host.querySelector('[data-prom]');
    if (!prom || prom.state === 'ok') { if (box) box.remove(); return; }
    if (!box) {
      box = el('div', 'ar-alert');
      box.setAttribute('role', 'status'); box.setAttribute('data-prom', '');
      box.appendChild(el('span', '', '')).setAttribute('data-prom-text', '');
      host.appendChild(box);
    }
    box.className = 'ar-alert ' + (prom.state === 'down' ? 'crit' : 'warn');
    const t = box.querySelector('[data-prom-text]');
    if (t) t.textContent = prom.message;
  }
  function apply(j) {
    Object.keys(j.devices || {}).forEach(function (id) {
      const n = nodes[id], d = DATA.devices[id], v = j.devices[id];
      if (!d || !v) return;
      d.state = v.state; d.state_label = v.state_label;
      if (n) {
        setState(n, v.state);
        const al = n.getAttribute('aria-label') || '';
        const parts = al.split(', ');
        if (parts.length >= 3) { parts[2] = v.state_label; n.setAttribute('aria-label', parts.join(', ')); }
      }
    });
    Object.keys(j.links || {}).forEach(function (k) {
      const g = edges[k], l = DATA.links[k], v = j.links[k];
      if (!l || !v) return;
      Object.assign(l, { state: v.state, util: v.util, tone: v.tone, oper_a: v.oper_a, oper_b: v.oper_b, rate_a: v.rate_a, rate_b: v.rate_b });
      if (g) {
        ['up', 'down', 'unknown'].forEach(function (s) { g.classList.remove('st-' + s); });
        ['ok', 'warn', 'crit', 'none'].forEach(function (s) { g.classList.remove('ut-' + s); });
        g.classList.add('st-' + v.state, 'ut-' + v.tone);
        if (v.util === null) g.style.removeProperty('--lw'); else g.style.setProperty('--lw', (1.6 + 2.6 * v.util / 100).toFixed(2));
      }
    });
    const s = j.summary || {};
    kpi('links', String(s.links)); kpi('links_up', String(s.links_up)); kpi('links_down', String(s.links_down));
    kpi('links_unknown', String(s.links_unknown)); kpi('devices_down', String(s.devices_down));
    const tile = document.querySelector('[data-kpi-tile="devices_down"]');
    if (tile) { tile.classList.toggle('s-crit', s.devices_down > 0); tile.classList.toggle('s-ok', !s.devices_down); }
    if (s.over) { kpi('over', s.over.ratio + ' : 1'); }
    document.querySelectorAll('[data-group-count]').forEach(function (n) {
      const ids = (DATA.groups || {})[n.getAttribute('data-group-count')] || [];
      let up = 0, down = 0;
      ids.forEach(function (id) { const d = DATA.devices[id]; if (d && d.state === 'up') up++; if (d && d.state === 'down') down++; });
      n.textContent = ids.length + ' server' + (ids.length === 1 ? '' : 's') + ' · ' + up + ' up · ' + down + ' down';
    });
    banner(j.prometheus);
    if (selected) showLink(selected);
  }

  const base = (CONF.interval || 30) * 1000;
  let delay = base, timer = null, fails = 0, live = !!CONF.live;
  function schedule() { clearTimeout(timer); if (live) timer = setTimeout(poll, delay); }
  function poll() {
    if (!live) return;
    if (document.hidden) { schedule(); return; }
    fetch(CONF.liveUrl + (CONF.query ? '?' + CONF.query : ''), { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) { apply(j); fails = 0; delay = base; })
      .catch(function () { fails += 1; delay = Math.min(base * Math.pow(2, fails), 300000); })
      .then(schedule);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden && live) { clearTimeout(timer); poll(); } });
  const toggle = document.getElementById('ar-live');
  if (toggle) {
    toggle.addEventListener('click', function (e) {
      e.preventDefault();
      live = !live;
      toggle.classList.toggle('on', live);
      toggle.setAttribute('aria-checked', live ? 'true' : 'false');
      const u = new URL(location.href);
      if (live) u.searchParams.delete('live'); else u.searchParams.set('live', '0');
      try { history.replaceState(null, '', u.toString()); } catch (err) { /* ignore */ }
      if (live) { delay = base; poll(); } else { clearTimeout(timer); }
    });
  }
  if (live) schedule();

  // ---------------------------------------------------------------- download
  const PROPS = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-opacity', 'stroke-dasharray', 'stroke-linecap',
                 'stroke-linejoin', 'opacity', 'font-family', 'font-size', 'font-weight', 'text-anchor', 'paint-order'];
  function toDataUrl(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.blob(); }).then(function (b) {
      return new Promise(function (res, rej) { const f = new FileReader(); f.onload = function () { res(f.result); }; f.onerror = rej; f.readAsDataURL(b); });
    });
  }
  function download() {
    const clone = svg.cloneNode(true);
    const src = svg.querySelectorAll('*'), dst = clone.querySelectorAll('*');
    const drop = [];
    src.forEach(function (s, i) {
      const d = dst[i];
      if (!d) return;
      const cs = getComputedStyle(s);
      if (cs.display === 'none' || s.classList.contains('lk-hit')) { drop.push(d); return; }
      d.setAttribute('style', PROPS.map(function (p) { return p + ':' + cs.getPropertyValue(p); }).join(';'));
    });
    drop.forEach(function (d) { if (d.parentNode) d.parentNode.removeChild(d); });
    const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    bg.setAttribute('width', '100%'); bg.setAttribute('height', '100%');
    bg.setAttribute('fill', getComputedStyle(svg.parentElement).backgroundColor || '#ffffff');
    clone.insertBefore(bg, clone.querySelector('desc').nextSibling);
    const jobs = [];
    clone.querySelectorAll('image').forEach(function (im) {
      const href = im.getAttribute('href');
      if (href && href.charAt(0) === '/') jobs.push(toDataUrl(href).then(function (u) { im.setAttribute('href', u); }).catch(function () { im.remove(); }));
    });
    Promise.all(jobs).then(function () {
      const text = '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone);
      const url = URL.createObjectURL(new Blob([text], { type: 'image/svg+xml' }));
      const a = document.createElement('a');
      a.href = url; a.download = 'data-center-architecture.svg';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    });
  }
  const dlBtn = document.getElementById('ar-download');
  if (dlBtn) dlBtn.addEventListener('click', download);
})();
