/* LinkedEye console behaviour: async "run now" buttons, confirm dialogs, auto-refresh. */
(function () {
  'use strict';
  const csrf = () => {
    const meta = document.querySelector('meta[name="csrf-cookie-name"]');
    const name = (meta && meta.content) || 'linkedeyecsrftoken';
    const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    if (match) return decodeURIComponent(match[1]);
    const input = document.querySelector('[name=csrfmiddlewaretoken]');
    return (input && input.value) || '';
  };

  // <button data-post="/url" data-reload> : POST via fetch, then reload (or show JSON result in data-target)
  document.addEventListener('click', async e => {
    const b = e.target.closest('[data-post]');
    if (!b) return;
    e.preventDefault();
    if (b.dataset.confirm && !window.confirm(b.dataset.confirm)) return;
    const label = b.innerHTML;
    b.disabled = true;
    if (b.dataset.busy) b.textContent = b.dataset.busy;
    try {
      const r = await fetch(b.dataset.post, { method: 'POST', headers: { 'X-CSRFToken': csrf(), 'Accept': 'application/json' }, credentials: 'same-origin' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || r.statusText);
      const out = b.dataset.target && document.getElementById(b.dataset.target);
      if (out) { out.hidden = false; out.textContent = d.summary || JSON.stringify(d, null, 2); b.disabled = false; b.innerHTML = label; }
      else location.reload();
    } catch (err) {
      b.disabled = false; b.innerHTML = label;
      const out = b.dataset.target && document.getElementById(b.dataset.target);
      if (out) { out.hidden = false; out.textContent = 'Failed: ' + err.message; } else window.alert('Failed: ' + err.message);
    }
  });

  // <form data-confirm="..."> asks before submitting
  document.addEventListener('submit', e => {
    const f = e.target;
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) e.preventDefault();
  });

  // filters that submit on change
  document.querySelectorAll('[data-autosubmit]').forEach(s => s.addEventListener('change', () => s.form.submit()));

  const RAIL_KEY = 'linkedeye-rail-pinned';
  const shell = document.getElementById('shell');
  const side = document.getElementById('side');
  const menuBtn = document.querySelector('[data-toggle-menu]');
  const pinRail = pinned => {
    if (!shell) return;
    shell.classList.toggle('rail-open', pinned);
    if (menuBtn) menuBtn.setAttribute('aria-expanded', String(pinned));
    try { localStorage.setItem(RAIL_KEY, pinned ? '1' : '0'); } catch (_) { /* private mode */ }
  };
  if (shell && window.innerWidth > 860) {
    let pinned = true;
    try {
      const saved = localStorage.getItem(RAIL_KEY);
      if (saved === '0') pinned = false;
      if (saved === '1') pinned = true;
    } catch (_) { /* ignore */ }
    pinRail(pinned);
  }
  // hamburger: pin the icon rail open on desktop, slide it in on phones (with a scrim behind it)
  const scrim = document.getElementById('scrim');
  const setDrawer = open => {
    if (!side) return;
    side.classList.toggle('open', open);
    if (scrim) scrim.hidden = !open;
    if (menuBtn) menuBtn.setAttribute('aria-expanded', String(open));
  };
  const closeDrawer = () => {
    if (!side || !side.classList.contains('open')) return;
    setDrawer(false);
    if (menuBtn) menuBtn.focus();
  };
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-toggle-menu]');
    if (!btn) return;
    if (window.innerWidth > 860) {
      pinRail(!shell.classList.contains('rail-open'));
    } else if (side) {
      setDrawer(!side.classList.contains('open'));
      if (side.classList.contains('open')) { const first = side.querySelector('a[href]'); if (first) first.focus(); }
    }
  });
  scrim?.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
  side?.addEventListener('click', e => { if (e.target.closest('a[href]') && window.innerWidth <= 860) setDrawer(false); });
  window.addEventListener('resize', () => { if (window.innerWidth > 860 && side && side.classList.contains('open')) setDrawer(false); });

  // the user menu closes on a click anywhere else
  document.addEventListener('click', e => {
    document.querySelectorAll('details.user-menu[open]').forEach(d => { if (!d.contains(e.target)) d.removeAttribute('open'); });
  });

  // per-page selector in pagers keeps the other query parameters
  document.querySelectorAll('[data-per-page]').forEach(s => s.addEventListener('change', () => {
    const u = new URL(location.href); u.searchParams.set('per', s.value); u.searchParams.delete('page'); location.href = u;
  }));

  // optional auto refresh: <body data-refresh="60">
  // A timer that fires while the tab is hidden must not end the loop: it keeps ticking, and coming back to a tab
  // whose data is older than the interval refreshes it at once.
  const page = document.querySelector('[data-refresh]');
  if (page) {
    const every = Math.max(5, +page.dataset.refresh || 60) * 1000;
    const loadedAt = Date.now();
    setInterval(() => { if (!document.hidden) location.reload(); }, every);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && Date.now() - loadedAt >= every) location.reload();
    });
  }

  const THEME_KEY = 'linkedeye-theme';
  const themeBtn = document.querySelector('[data-toggle-theme]');
  const sun = '<svg class="ic" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  const moon = '<svg class="ic" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 0010 9.8z"/></svg>';
  const applyTheme = t => {
    document.documentElement.setAttribute('data-theme', t);
    document.documentElement.style.backgroundColor = t === 'dark' ? '#0f1419' : '#f3f4f6';
    // blocked site data (private window) throws here: the theme still applies, it is just not remembered
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* not remembered */ }
    if (themeBtn) {
      themeBtn.innerHTML = t === 'light' ? moon : sun;
      themeBtn.title = t === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
      themeBtn.setAttribute('aria-label', themeBtn.title);
    }
    document.dispatchEvent(new CustomEvent('linkedeye-theme', { detail: { theme: t } }));
  };
  const initial = document.documentElement.getAttribute('data-theme');
  applyTheme(initial === 'dark' || initial === 'light' ? initial : 'light');
  document.addEventListener('linkedeye-theme', () => {
    if (typeof window.leChartsRefresh === 'function') window.leChartsRefresh();
  });
  themeBtn?.addEventListener('click', () => {
    applyTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
  });
})();
