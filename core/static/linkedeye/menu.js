/* LinkedEye sidebar menu: collapsible sections and groups that remember their state, a live filter, pinned
 * links, the icon-rail flyout and arrow-key navigation. The markup is rendered by core/templates/core/base.html from
 * core/nav.py, so the menu works (all sections open, every link reachable) without this file.
 *
 * Contract (tests: core/test_nav.py, the browser check in docs/UI.md):
 *   [data-nav]                      the <nav>; [data-nav-filter] the filter input; [data-nav-empty] the "no match" line
 *   .nav-sec[data-sec] > button[data-nav-sec] + .nav-sec-body    a section; aria-expanded on the button
 *   details.nav-group[data-key] > summary + .sub                 a group (native open/close, aria-expanded kept in step)
 *   .nav-row[data-key] > a[data-nav-key] (+ button[data-pin])    one link and its star
 *   [data-pinned-list]              where pinned links are rebuilt from the stars
 * Stored in localStorage (every access in try/catch: private windows throw): linkedeye-nav-sections {key: bool open},
 * linkedeye-nav-groups {key: bool open}, linkedeye-nav-pins [link keys]. Text is only ever set with textContent;
 * links are cloned from the server-rendered ones, never built from strings.
 */
(function () {
  'use strict';
  const nav = document.querySelector('[data-nav]');
  if (!nav) return;
  const shell = document.getElementById('shell');
  const side = document.getElementById('side');
  const flyout = document.getElementById('nav-flyout');
  const filterInput = document.querySelector('[data-nav-filter]');
  const emptyNote = nav.querySelector('[data-nav-empty]');
  const pinnedBox = nav.querySelector('.nav-pinned');
  const pinnedList = nav.querySelector('[data-pinned-list]');
  const SEC_KEY = 'linkedeye-nav-sections', GRP_KEY = 'linkedeye-nav-groups', PIN_KEY = 'linkedeye-nav-pins';
  const wide = () => window.innerWidth > 860;
  const railOpen = () => !shell || shell.classList.contains('rail-open');

  const read = (key, fallback) => {
    try { const v = JSON.parse(localStorage.getItem(key)); return v === null || v === undefined ? fallback : v; } catch (_) { return fallback; }
  };
  const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* not remembered */ } };
  const plain = v => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});

  const sections = Array.from(nav.querySelectorAll('.nav-sec[data-sec]')).filter(s => s !== pinnedBox);
  const groups = Array.from(nav.querySelectorAll('details.nav-group'));
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };

  // ---------------------------------------------------------------- sections: remembered open/closed
  const secState = plain(read(SEC_KEY, {}));
  function setSection(sec, open, save) {
    const btn = sec.querySelector('[data-nav-sec]');
    sec.classList.toggle('is-collapsed', !open);
    if (btn) btn.setAttribute('aria-expanded', String(open));
    if (save) { secState[sec.dataset.sec] = open; write(SEC_KEY, secState); }
  }
  sections.forEach(sec => {
    // the section holding the current page is always shown: you must be able to see where you are
    setSection(sec, sec.classList.contains('has-active') || secState[sec.dataset.sec] !== false, false);
  });
  nav.addEventListener('click', e => {
    const btn = e.target.closest('[data-nav-sec]');
    if (!btn) return;
    const sec = btn.closest('.nav-sec');
    setSection(sec, sec.classList.contains('is-collapsed'), true);
  });

  // ---------------------------------------------------------------- groups: server opens the active one, the user's choice wins for the rest
  const grpState = plain(read(GRP_KEY, {}));
  let quiet = false;   // true while this file (not the user) changes a <details>
  const syncSummary = d => { const s = d.querySelector(':scope > summary'); if (s) s.setAttribute('aria-expanded', String(d.open)); };
  groups.forEach(d => {
    if (!d.classList.contains('on') && typeof grpState[d.dataset.key] === 'boolean') { quiet = true; d.open = grpState[d.dataset.key]; }
    syncSummary(d);
    d.addEventListener('toggle', () => {
      syncSummary(d);
      if (quiet || nav.classList.contains('is-filtering')) return;
      grpState[d.dataset.key] = d.open;
      write(GRP_KEY, grpState);
    });
  });
  setTimeout(() => { quiet = false; }, 0);   // the toggle events of the restore above are queued: drop them

  // ---------------------------------------------------------------- pins
  const linkFor = key => nav.querySelector('.nav-sec:not(.nav-pinned) [data-nav-key="' + CSS.escape(key) + '"]');
  let pins = read(PIN_KEY, []);
  // storage can hold anything (another tab, an extension, a hand edit): keep short strings only, a few of them, once each
  pins = Array.isArray(pins) ? Array.from(new Set(pins.slice(0, 1000).filter(k => typeof k === 'string' && k.length <= 120))).slice(0, 100) : [];
  function renderPins() {
    pins = pins.filter(linkFor);   // a pinned link that left the menu (module removed, no permission) just disappears
    if (!pinnedList || !pinnedBox) return;
    pinnedList.replaceChildren();
    pins.forEach(key => {
      const src = linkFor(key);
      const group = src.closest('details.nav-group');
      const row = el('div', 'nav-row nav-top');
      row.dataset.key = key;
      const a = src.cloneNode(false);
      const icon = (group ? group.querySelector(':scope > summary > .ic') : src.querySelector('.ic'));
      if (icon) a.appendChild(icon.cloneNode(true));
      const label = src.dataset.label || src.textContent.trim();
      a.dataset.label = label;
      a.appendChild(el('span', 'lbl', label));
      if (group) a.appendChild(el('span', 'nav-ctx', group.dataset.label || ''));
      const btn = el('button', 'nav-pin');
      btn.type = 'button';
      btn.dataset.pin = key;
      btn.setAttribute('aria-pressed', 'true');
      btn.setAttribute('aria-label', 'Unpin ' + label);
      btn.title = 'Remove from pinned';
      const star = nav.querySelector('.nav-pin svg');
      if (star) btn.appendChild(star.cloneNode(true));
      row.append(a, btn);
      pinnedList.appendChild(row);
    });
    pinnedBox.hidden = !pins.length || nav.classList.contains('is-filtering');
    nav.querySelectorAll('.nav-sec:not(.nav-pinned) .nav-pin').forEach(b => {
      const on = pins.includes(b.dataset.pin);
      b.setAttribute('aria-pressed', String(on));
      const label = (linkFor(b.dataset.pin) || {}).textContent || '';
      b.setAttribute('aria-label', (on ? 'Unpin ' : 'Pin ') + label.trim());
      b.title = on ? 'Remove from pinned' : 'Pin to the top of the menu';
    });
  }
  nav.addEventListener('click', e => {
    const btn = e.target.closest('.nav-pin');
    if (!btn) return;
    e.preventDefault();
    const key = btn.dataset.pin;
    pins = pins.includes(key) ? pins.filter(k => k !== key) : pins.concat(key);
    write(PIN_KEY, pins);
    renderPins();
    if (btn.isConnected && !btn.closest('.nav-pinned')) btn.focus();
    else { const first = pinnedList && pinnedList.querySelector('a'); if (first) first.focus(); }
  });
  renderPins();

  // ---------------------------------------------------------------- filter
  const wasOpen = new WeakMap();
  function applyFilter(raw) {
    const words = raw.toLowerCase().split(/\s+/).filter(Boolean);
    const filtering = words.length > 0;
    nav.classList.toggle('is-filtering', filtering);
    const hit = text => words.every(w => text.indexOf(w) !== -1);
    let shown = 0;
    quiet = true;
    sections.forEach(sec => {
      let secHits = 0;
      sec.querySelectorAll('.nav-sec-body > .nav-row, .nav-sec-body > details.nav-group').forEach(item => {
        if (item.matches('details')) {
          const g = (item.dataset.label || '').toLowerCase();
          const rows = Array.from(item.querySelectorAll('.nav-row'));
          let n = 0;
          rows.forEach(r => {
            const a = r.querySelector('a');
            const ok = !filtering || hit(g + ' ' + a.textContent.toLowerCase());
            r.hidden = !ok;
            if (ok) n += 1;
          });
          item.hidden = filtering && n === 0;
          if (filtering && n) { if (!wasOpen.has(item)) wasOpen.set(item, item.open); item.open = true; }
          else if (!filtering && wasOpen.has(item)) { item.open = wasOpen.get(item); wasOpen.delete(item); }
          syncSummary(item);
          secHits += item.hidden ? 0 : n;
        } else {
          const a = item.querySelector('a');
          const ok = !filtering || hit((a.dataset.label || a.textContent).toLowerCase());
          item.hidden = !ok;
          secHits += ok ? 1 : 0;
        }
      });
      sec.hidden = filtering && secHits === 0;
      shown += secHits;
    });
    setTimeout(() => { quiet = false; }, 0);
    if (emptyNote) emptyNote.hidden = !filtering || shown > 0;
    if (pinnedBox) pinnedBox.hidden = filtering || !pins.length;
    if (!filtering) { nav.querySelectorAll('.nav-row[hidden], details[hidden]').forEach(n => { n.hidden = false; }); }
  }
  if (filterInput) {
    filterInput.addEventListener('input', () => applyFilter(filterInput.value));
    filterInput.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        if (filterInput.value) { filterInput.value = ''; applyFilter(''); } else filterInput.blur();
        e.preventDefault();
      } else if (e.key === 'ArrowDown') {
        const first = focusables()[0];
        if (first) { first.focus(); e.preventDefault(); }
      } else if (e.key === 'Enter') {
        const first = nav.querySelector('.nav-sec:not([hidden]) .nav-row:not([hidden]) a');
        if (first && nav.classList.contains('is-filtering')) { window.location.href = first.href; e.preventDefault(); }
      }
    });
  }
  document.addEventListener('keydown', e => {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || !filterInput) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    const btn = document.querySelector('[data-toggle-menu]');
    if (wide() && !railOpen() && btn) btn.click();            // the filter lives in the expanded menu
    else if (!wide() && side && !side.classList.contains('open') && btn) btn.click();
    filterInput.focus();
    filterInput.select();
  });

  // ---------------------------------------------------------------- keyboard: arrows move, Enter/Space toggle, Escape closes
  // checkVisibility() also sees content a closed <details> skips rendering; older browsers fall back to having a box
  function visible(n) {
    if (n.closest('[hidden]')) return false;
    return typeof n.checkVisibility === 'function' ? n.checkVisibility() : n.getClientRects().length > 0;
  }
  function focusables() {
    // while filtering every match is already shown, so the arrows walk the matching links only
    const sel = nav.classList.contains('is-filtering') ? '.nav-row > a' : '.nav-sec-h:not(.is-static), details.nav-group > summary, .nav-row > a';
    return Array.from(nav.querySelectorAll(sel)).filter(visible);
  }
  nav.addEventListener('keydown', e => {
    const t = e.target;
    if (!t.matches('.nav-sec-h, summary, .nav-row > a')) return;
    const list = focusables();
    const i = list.indexOf(t);
    let next = null;
    switch (e.key) {
      case 'ArrowDown': next = list[Math.min(list.length - 1, i + 1)]; break;
      case 'ArrowUp': if (i === 0 && filterInput) { filterInput.focus(); e.preventDefault(); return; } next = list[Math.max(0, i - 1)]; break;
      case 'Home': next = list[0]; break;
      case 'End': next = list[list.length - 1]; break;
      case 'ArrowRight':
        if (t.matches('summary')) {
          const d = t.parentElement;
          if (wide() && !railOpen()) { openFlyout(d, true); e.preventDefault(); return; }
          if (!d.open) d.open = true; else next = d.querySelector('.sub a');
        }
        break;
      case 'ArrowLeft':
        if (t.matches('summary') && t.parentElement.open) t.parentElement.open = false;
        else if (t.closest('details')) next = t.closest('details').querySelector(':scope > summary');
        break;
      case 'Escape': closeFlyout(); return;
      default: return;
    }
    e.preventDefault();
    if (next) next.focus();
  });

  // ---------------------------------------------------------------- icon-rail flyout
  let flyTimer = 0, flyFor = null;
  const collapsedRail = () => wide() && !railOpen();
  function closeFlyout(restore) {
    clearTimeout(flyTimer);
    if (!flyout || flyout.hidden) return;
    flyout.hidden = true;
    flyout.replaceChildren();
    if (restore && flyFor) { const f = flyFor.matches('details') ? flyFor.querySelector(':scope > summary') : flyFor.querySelector('a'); if (f) f.focus(); }
    flyFor = null;
  }
  function openFlyout(item, focusFirst) {
    if (!flyout || !collapsedRail()) return;
    clearTimeout(flyTimer);
    if (flyFor === item && !flyout.hidden) { if (focusFirst) { const a = flyout.querySelector('a'); if (a) a.focus(); } return; }
    const isGroup = item.matches('details');
    const title = isGroup ? item.dataset.label : (item.querySelector('a') || {}).dataset.label;
    const rows = isGroup ? Array.from(item.querySelectorAll('.sub .nav-row > a')) : [item.querySelector('a')];
    flyout.replaceChildren(el('h3', '', title || ''));
    rows.forEach(a => {
      const c = el('a', a.classList.contains('on') ? 'on' : '');
      c.href = a.href;
      c.setAttribute('role', 'menuitem');
      if (a.hasAttribute('aria-current')) c.setAttribute('aria-current', 'page');
      c.appendChild(el('span', '', isGroup ? a.textContent.trim() : (a.dataset.label || a.textContent.trim())));
      const badge = !isGroup && a.querySelector('.nav-badge');
      if (badge) c.appendChild(el('span', 'nav-badge', badge.textContent));
      flyout.appendChild(c);
    });
    if (isGroup) { const b = item.querySelector(':scope > summary .nav-badge'); if (b) flyout.firstChild.appendChild(el('span', 'nav-badge', b.textContent)); }
    flyout.hidden = false;
    flyFor = item;
    const anchor = (isGroup ? item.querySelector(':scope > summary') : item).getBoundingClientRect();
    const left = side.getBoundingClientRect().right + 6;
    flyout.style.left = left + 'px';
    const h = flyout.offsetHeight;
    flyout.style.top = Math.max(8, Math.min(anchor.top - 6, window.innerHeight - h - 8)) + 'px';
    if (focusFirst) { const a = flyout.querySelector('a'); if (a) a.focus(); }
  }
  const itemOf = n => n.closest('details.nav-group, .nav-row.nav-top');
  if (flyout) {
    nav.addEventListener('mouseover', e => {
      if (!collapsedRail()) return;
      const item = itemOf(e.target);
      if (!item) return;
      clearTimeout(flyTimer);
      flyTimer = setTimeout(() => openFlyout(item, false), 70);
    });
    nav.addEventListener('mouseout', e => {
      if (!collapsedRail() || (e.relatedTarget && (flyout.contains(e.relatedTarget) || nav.contains(e.relatedTarget) && itemOf(e.relatedTarget) === itemOf(e.target)))) return;
      clearTimeout(flyTimer);
      flyTimer = setTimeout(() => closeFlyout(false), 160);
    });
    flyout.addEventListener('mouseenter', () => clearTimeout(flyTimer));
    flyout.addEventListener('mouseleave', () => { clearTimeout(flyTimer); flyTimer = setTimeout(() => closeFlyout(false), 160); });
    nav.addEventListener('focusin', e => {
      if (!collapsedRail()) return;
      const item = itemOf(e.target);
      if (item) openFlyout(item, false); else closeFlyout(false);
    });
    nav.addEventListener('click', e => {
      const sum = e.target.closest('details.nav-group > summary');
      if (!sum || !collapsedRail()) return;
      e.preventDefault();                      // a click on a rail icon opens its flyout, it does not expand a hidden list
      openFlyout(sum.parentElement, false);
    });
    flyout.addEventListener('keydown', e => {
      const items = Array.from(flyout.querySelectorAll('a'));
      const i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { items[Math.min(items.length - 1, i + 1)].focus(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { items[Math.max(0, i - 1)].focus(); e.preventDefault(); }
      else if (e.key === 'Escape' || e.key === 'ArrowLeft') { closeFlyout(true); e.preventDefault(); e.stopPropagation(); }
    });
    document.addEventListener('click', e => { if (!flyout.contains(e.target) && !nav.contains(e.target)) closeFlyout(false); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !flyout.hidden) closeFlyout(false); });
    window.addEventListener('resize', () => closeFlyout(false));
    nav.addEventListener('scroll', () => closeFlyout(false), { passive: true });
  }

  // ---------------------------------------------------------------- phone drawer: keep Tab inside it while it is open
  if (side) {
    side.addEventListener('keydown', e => {
      if (e.key !== 'Tab' || wide() || !side.classList.contains('open')) return;
      const list = Array.from(side.querySelectorAll('a[href], button, input, select, summary')).filter(n => !n.disabled && visible(n));
      if (!list.length) return;
      const first = list[0], last = list[list.length - 1];
      if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
      else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
    });
  }

  // ---------------------------------------------------------------- show the current page: scroll the menu (not the page) so its link is in view
  (function revealCurrent() {
    const cur = nav.querySelector('.nav-sec:not(.nav-pinned) a[aria-current="page"]');
    if (!cur || nav.scrollHeight <= nav.clientHeight) return;
    const nr = nav.getBoundingClientRect(), cr = cur.getBoundingClientRect();
    if (cr.top < nr.top + 8 || cr.bottom > nr.bottom - 8) nav.scrollTop += cr.top - nr.top - nr.height / 3;
  }());
}());
