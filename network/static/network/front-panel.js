/* Device detail front panel: a port opens its details below the chassis; prev / next / Esc; the drawer
 * links to ?port=<ifIndex> to narrow the overview charts. Port data comes from the "nw-ports" json_script
 * and is only ever written with textContent (names and descriptions are device-reported). live.js rewrites that
 * json_script with fresh port data and fires nw:ports-updated; an open drawer then shows the new values. */
(function () {
  const root = document.getElementById('front-panel');
  const src = document.getElementById('nw-ports');
  if (!root || !src) return;
  let data = JSON.parse(src.textContent);
  const ports = Array.from(root.querySelectorAll('.fp-port'));
  const drawer = root.querySelector('.fp-drawer');
  const name = drawer.querySelector('#fp-d-name');
  const desc = drawer.querySelector('.fp-d-desc');
  const badge = drawer.querySelector('.fp-d-badge');
  const kv = drawer.querySelector('.fp-d-kv');
  const filter = drawer.querySelector('.fp-d-filter');
  let current = -1;

  function show(i, focus) {
    if (!data.length) return;
    current = (i + data.length) % data.length;
    const p = data[current];
    ports.forEach(function (b) { b.setAttribute('aria-pressed', String(Number(b.dataset.port) === current)); });
    name.textContent = 'Port ' + p.n + ' · ' + p.name;
    desc.textContent = p.alias || 'No description';
    badge.className = 'fp-d-badge ' + p.tone;
    badge.textContent = p.toneLabel;
    kv.replaceChildren();
    [['Admin', p.admin], ['Oper', p.oper], ['Speed', p.speed], ['Type', p.type], ['In', p.in], ['Out', p.out],
     ['Utilisation', p.util], ['Errors', p.errors], ['Discards', p.discards], ['Flaps (24 h)', p.flaps],
     ['ifIndex', String(p.ifIndex)]].forEach(function (row) {
      const dt = document.createElement('dt'); dt.textContent = row[0];
      const dd = document.createElement('dd'); dd.textContent = row[1];
      kv.append(dt, dd);
    });
    filter.href = '?tab=overview&port=' + encodeURIComponent(p.ifIndex) + '#nw-charts';
    drawer.hidden = false;
    if (focus) name.focus();
  }

  function close() {
    const opener = ports.find(function (b) { return Number(b.dataset.port) === current; });
    drawer.hidden = true;
    ports.forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
    current = -1;
    if (opener) opener.focus();
  }

  ports.forEach(function (b) {
    b.addEventListener('click', function () {
      Number(b.dataset.port) === current && !drawer.hidden ? close() : show(Number(b.dataset.port), true);
    });
  });
  drawer.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-fp]');
    if (!btn) return;
    if (btn.dataset.fp === 'prev') show(current - 1, false);
    else if (btn.dataset.fp === 'next') show(current + 1, false);
    else close();
  });
  document.addEventListener('keydown', function (e) {
    if (drawer.hidden || !root.contains(document.activeElement)) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
  });
  document.addEventListener('nw:ports-updated', function () {
    data = JSON.parse(src.textContent);
    if (current >= 0 && !drawer.hidden) show(current, false);
  });
  if (root.dataset.selected !== undefined) show(Number(root.dataset.selected), false);
})();
