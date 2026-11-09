/* BOD / EOD pages.
 *
 * 1. Busy state for synchronous runs and uploads.
 * 2. Live checks: the automated rows (domain / URL check / TCP check) are patched in place from /bod-eod/api/live/ every
 *    15-30 s. The endpoint only reads the stored results of the referenced checks; "Re-check now" asks the server to run
 *    them again (POST, operator only). Nothing here reloads the page and nothing signs anything off: a live failure only
 *    changes what is shown. Every value from the server is written with textContent / classList / setAttribute, never parsed
 *    as HTML.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ busy forms
  document.addEventListener('submit', function (e) {
    var f = e.target.closest('[data-busy-form]');
    if (!f || e.defaultPrevented) return;
    var b = f.querySelector('button[type=submit][data-busy]');
    if (b) setTimeout(function () { b.disabled = true; b.textContent = b.dataset.busy; }, 0);
  });

  // ------------------------------------------------------------------ live checks
  var root = document.querySelector('[data-bod-live]');
  var cfgEl = document.getElementById('bod-live-cfg');
  if (!root || !cfgEl) return;
  var cfg;
  try { cfg = JSON.parse(cfgEl.textContent); } catch (e) { return; }
  cfg.scope = cfg.scope || {};

  var MIN_MS = 15000, MAX_MS = 30000, FAST_MS = 3000, MAX_BACKOFF_MS = 120000, FLASH_MS = 6500, MAX_CHANGES = 30;
  var CODE = {ok: 0, warning: 1, failed: 2, unknown: 3};
  var WORD = {ok: 'OK', warning: 'warning', failed: 'failed', unknown: 'unknown'};
  var TONES = ['bc-ok', 'bc-warn', 'bc-crit', 'bc-mute', 'bc-info'];

  function clampMs(sec) {
    var ms = (+sec || 20) * 1000;
    return Math.max(MIN_MS, Math.min(MAX_MS, ms));
  }
  var interval = clampMs(cfg.interval);
  var loadedAt = Date.now();
  var st = {paused: false, inflight: false, failures: 0, retryMs: 0, lastOk: loadedAt, lastError: '', graceUntil: 0,
            fastUntil: 0, timer: null, signedOut: false, watching: {}, lastAnnounce: 0, annTimer: null, annQueue: [], notified: {},
            changes: 0};

  var $ = function (sel, ctx) { return (ctx || root).querySelector(sel); };
  var $$ = function (sel, ctx) { return Array.prototype.slice.call((ctx || root).querySelectorAll(sel)); };

  // same wording as bodeod.live.human_age on the server
  function human(sec) {
    var s = Math.max(0, Math.floor(sec));
    if (s < 90) return s + ' s';
    if (s < 5400) return Math.round(s / 60) + ' min';
    if (s < 172800) return Math.round(s / 3600) + ' h';
    return Math.round(s / 86400) + ' d';
  }
  function csrf() {
    var meta = document.querySelector('meta[name="csrf-cookie-name"]');
    var name = (meta && meta.content) || 'csrftoken';
    var m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    if (m) return decodeURIComponent(m[1]);
    var input = document.querySelector('[name=csrfmiddlewaretoken]');
    return (input && input.value) || '';
  }
  var IST = null;
  try { IST = new Intl.DateTimeFormat('en-GB', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Kolkata'}); } catch (e) { IST = null; }
  function clock(iso) {
    var d = iso ? new Date(iso) : new Date();
    if (isNaN(d.getTime())) d = new Date();
    return (IST ? IST.format(d) : d.toTimeString().slice(0, 8)) + (IST ? ' IST' : '');
  }

  // ------------------------------------------------------------------ patching rows
  function setPill(p, tone, code, label) {
    p.className = 'bc bc-' + tone;
    var b = document.createElement('b');
    b.textContent = String(code);
    p.replaceChildren(b, document.createTextNode(label));
  }
  function setAge(el, ageS, istText, now) {
    if (ageS === null || ageS === undefined) { el.removeAttribute('data-age0'); el.textContent = 'not checked yet'; el.removeAttribute('title'); el._t0 = now; return; }
    el.setAttribute('data-age0', String(ageS));
    el._t0 = now;
    el.textContent = 'checked ' + human(ageS) + ' ago';
    if (istText) el.setAttribute('title', istText);
  }
  function flash(el) {
    el.classList.add('bod-flash');
    clearTimeout(el._flashT);
    el._flashT = setTimeout(function () { el.classList.remove('bod-flash'); }, FLASH_MS);
  }
  function patchRow(el, it, mode, changes, now) {
    var prev = el.getAttribute('data-live-status');
    el.setAttribute('data-live-status', it.status);
    if (el.tagName === 'LI') { // status-grid cell
      TONES.forEach(function (c) { el.classList.remove(c); });
      el.classList.add('bc-' + it.tone);
      var b = $('b', el);
      if (b) b.textContent = String(it.code);
      el.setAttribute('title', it.name + ' — ' + it.label + ': ' + it.detail_long);
    }
    $$('[data-live="pill"]', el).forEach(function (p) { setPill(p, it.tone, it.code, it.label); });
    $$('[data-live="detail"]', el).forEach(function (d) { d.textContent = it.detail; d.setAttribute('title', it.detail_long); });
    $$('[data-live="age"]', el).forEach(function (a) { setAge(a, it.age_s, it.checked_at_ist, now); });
    var rec = el.getAttribute('data-recorded');
    $$('[data-live="diff"]', el).forEach(function (d) { d.hidden = !rec || rec === it.status; });
    el.removeAttribute('aria-busy');
    if (prev && prev !== it.status) {
      flash(el);
      changes.push({mode: mode.code, name: it.name, from: prev, to: it.status});
    }
  }
  function patchMode(m, data) {
    $$('[data-live-mode="' + m.code + '"]').forEach(function (box) {
      var pill = $('[data-live="mode-pill"]', box);
      if (pill) setPill(pill, m.tone, CODE[m.status], m.label);
      var cnt = $('[data-live="mode-counts"]', box);
      if (cnt) {
        var c = m.counts, parts = [];
        if (!c.total) parts.push('no automated items');
        else {
          parts.push(c.ok + ' ok');
          if (c.warning) parts.push(c.warning + ' warning');
          if (c.failed) parts.push(c.failed + ' failed');
          if (c.unknown) parts.push(c.unknown + ' unknown');
        }
        cnt.textContent = parts.join(' · ');
      }
      var run = $('[data-live="running"]', box);
      if (run) run.hidden = !m.rechecking;
      // a run that started or finished since the page was drawn changes cards and tables this script does not rebuild
      var shown = box.getAttribute('data-run-id') || '', shownStatus = box.getAttribute('data-run-status') || '';
      var latest = m.run;
      if (latest && (String(latest.id) !== shown || latest.status !== shownStatus)) {
        var key = m.code + ':' + latest.id + ':' + latest.status;
        if (!st.notified[key]) {
          st.notified[key] = true;
          var text = String(latest.id) !== shown && shown
            ? m.code + ' run #' + latest.id + ' is newer than the one on this page (' + latest.status_label + ').'
            : m.code + ' run #' + latest.id + ' is now ' + latest.status_label + '.';
          showNotice(text);
          addChange({time: clock(data.generated_at), text: text});
          queueAnnounce(text);
        }
      }
    });
    // release re-check buttons once the server says the mode is no longer being re-checked
    if (!m.rechecking) {
      $$('[data-recheck][aria-busy="true"]').forEach(function (btn) {
        if (btn.getAttribute('data-mode') === String(m.id)) setBusy(btn, false);
      });
      if (st.watching[m.code]) {
        delete st.watching[m.code];
        setMsg('Re-check of ' + m.code + ' finished.');
        queueAnnounce('Re-check of ' + m.code + ' finished.');
      }
    }
  }
  function applyData(data, full) {
    var now = Date.now();
    var changes = [];
    (data.modes || []).forEach(function (m) {
      patchMode(m, data);
      (m.items || []).forEach(function (it) {
        $$('[data-live-row="' + it.key + '"]').forEach(function (el) { patchRow(el, it, m, changes, now); });
      });
    });
    if (data.interval) interval = clampMs(data.interval);
    if (full) {
      st.failures = 0; st.retryMs = 0; st.lastOk = now; st.lastError = ''; st.signedOut = false;
    }
    if (!Object.keys(st.watching).length) st.fastUntil = 0;
    if (changes.length) {
      // the totals (tiles, count chips) are rendered once by the server: say so instead of leaving them quietly out of step with the rows
      if ($('[data-live-kpis]')) showNotice('Live states changed, so the totals on this page are out of date.');
      var at = clock(data.generated_at);
      changes.forEach(function (c) { addChange({time: at, mode: c.mode, name: c.name, from: c.from, to: c.to}); });
      announceChanges(changes);
    }
    renderStatus();
  }

  // ------------------------------------------------------------------ change list, notice, announcements
  function addChange(c) {
    var box = $('[data-live-changes]'), list = $('[data-live-changes-list]');
    if (!box || !list) return;
    var li = document.createElement('li');
    var t = document.createElement('time');
    t.textContent = c.time;
    li.appendChild(t);
    if (c.text) {
      var s = document.createElement('span');
      s.textContent = c.text;
      li.appendChild(s);
    } else {
      var who = document.createElement('strong');
      who.textContent = c.mode + ' · ' + c.name;
      var arrow = document.createElement('span');
      arrow.className = 'arrow';
      arrow.textContent = WORD[c.from] + ' → ' + WORD[c.to];
      var tag = document.createElement('span');
      tag.className = 'chip ' + (c.to === 'ok' ? 'ok' : c.to === 'failed' ? 'crit' : c.to === 'warning' ? 'warn' : 'mute');
      tag.textContent = c.to === 'ok' && c.from !== 'unknown' ? 'recovered' : c.to === 'ok' ? 'ok' : WORD[c.to];
      li.appendChild(who); li.appendChild(arrow); li.appendChild(tag);
    }
    list.insertBefore(li, list.firstChild);
    while (list.children.length > MAX_CHANGES) list.removeChild(list.lastChild);
    st.changes = list.children.length;
    box.hidden = false;
  }
  function showNotice(text) {
    var box = $('[data-live-notice]'), t = $('[data-live-notice-text]');
    if (!box || !t) return;
    t.textContent = text;
    box.hidden = false;
  }
  function setMsg(text) {
    var m = $('[data-live-msg]');
    if (m) m.textContent = text;
  }
  // screen readers hear a summary of changes, never the routine updates, and at most one message every 10 s
  function announceChanges(changes) {
    var text;
    if (changes.length === 1) {
      var c = changes[0];
      text = c.mode + ': ' + c.name + (c.to === 'ok' ? ' recovered.' : ' is now ' + WORD[c.to] + '.');
    } else {
      var failed = changes.filter(function (c) { return c.to === 'failed'; }).length;
      text = changes.length + ' live checks changed' + (failed ? ', ' + failed + ' now failed' : '') + '.';
    }
    queueAnnounce(text);
  }
  function queueAnnounce(text) {
    st.annQueue.push(text);
    if (st.annTimer) return;
    var wait = Math.max(500, st.lastAnnounce + 10000 - Date.now());
    st.annTimer = setTimeout(function () {
      st.annTimer = null;
      var region = $('[data-live-announce]');
      var msg = st.annQueue.splice(0).join(' ');
      st.lastAnnounce = Date.now();
      if (region && msg) region.textContent = msg;
    }, wait);
  }

  // ------------------------------------------------------------------ status line and stale banner
  function formFocused() {
    var a = document.activeElement;
    if (!a || a === document.body) return false;
    if (a.isContentEditable) return true;
    if (a.tagName === 'TEXTAREA' || a.tagName === 'SELECT') return true;
    if (a.tagName === 'INPUT') return !/^(checkbox|radio|button|submit|reset|file|range|color|image)$/i.test(a.type || 'text');
    return false;
  }
  function renderStatus() {
    var now = Date.now();
    var age = now - st.lastOk;
    var stale = !st.paused && !document.hidden && now > st.graceUntil && age > 3 * interval;
    var pill = $('.bod-livepill'), label = $('[data-live-label]'), upd = $('[data-live-updated]');
    var banner = $('[data-live-stale]'), btext = $('[data-live-stale-text]');
    var mode = st.paused ? 'paused' : stale ? (st.failures ? 'error' : 'stale') : 'live';
    if (pill) pill.setAttribute('data-state', mode);
    if (label) label.textContent = st.paused ? 'Paused' : stale ? (st.failures ? 'Offline' : 'Stale') : 'Live';
    if (upd) upd.textContent = (st.paused ? 'Paused · ' : '') + 'Updated ' + human(age / 1000) + ' ago';
    if (banner && btext) {
      if (stale) {
        var why = st.signedOut ? 'Your session may have expired: sign in again.'
          : st.failures ? 'The last update failed' + (st.lastError ? ' (' + st.lastError + ')' : '') + '; retrying.'
          : formFocused() ? 'Updates are on hold while you edit a field.' : 'Updates are on hold.';
        btext.textContent = 'The live data on this page is ' + human(age / 1000) + ' old, so what you see may be out of date. ' + why;
        banner.hidden = false;
      } else {
        banner.hidden = true;
      }
    }
  }
  function tickAges() {
    var now = Date.now();
    $$('[data-live="age"][data-age0]').forEach(function (el) {
      var base = parseFloat(el.getAttribute('data-age0'));
      if (isNaN(base)) return;
      el.textContent = 'checked ' + human(base + (now - (el._t0 || loadedAt)) / 1000) + ' ago';
    });
    renderStatus();
  }
  setInterval(tickAges, 1000);

  // ------------------------------------------------------------------ polling
  function liveUrl() {
    var p = new URLSearchParams();
    Object.keys(cfg.scope).forEach(function (k) { p.set(k, String(cfg.scope[k])); });
    var q = p.toString();
    return cfg.url + (q ? '?' + q : '');
  }
  function delay() {
    if (Date.now() < st.fastUntil) return FAST_MS;
    var base = interval * (0.9 + Math.random() * 0.2);
    if (st.retryMs) return Math.min(Math.max(st.retryMs, base), MAX_BACKOFF_MS);
    return Math.min(base * Math.pow(2, Math.min(st.failures, 3)), MAX_BACKOFF_MS);
  }
  function schedule(ms) {
    clearTimeout(st.timer);
    st.timer = setTimeout(tick, ms);
  }
  function tick() {
    if (st.paused || document.hidden) return; // resumed by the toggle / visibilitychange
    if (formFocused()) { schedule(5000); renderStatus(); return; }
    poll();
  }
  function fail(msg, retryMs) {
    st.failures += 1;
    st.lastError = msg;
    st.retryMs = retryMs || 0;
    renderStatus();
  }
  function poll() {
    if (st.inflight) return;
    st.inflight = true;
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timeout = setTimeout(function () { if (ctl) ctl.abort(); }, 10000);
    fetch(liveUrl(), {credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}, signal: ctl ? ctl.signal : undefined})
      .then(function (r) {
        if (r.status === 429) {
          var wait = parseInt(r.headers.get('Retry-After') || '30', 10);
          throw {soft: 'rate limited', retry: (isNaN(wait) ? 30 : wait) * 1000};
        }
        var type = r.headers.get('Content-Type') || '';
        if (r.status === 401 || r.status === 403 || (r.ok && type.indexOf('json') < 0)) { st.signedOut = true; throw {soft: 'not signed in'}; }
        if (!r.ok) throw {soft: 'HTTP ' + r.status};
        return r.json();
      })
      .then(function (data) { applyData(data, true); })
      .catch(function (e) { fail(e && e.soft ? e.soft : 'no connection', e && e.retry); })
      .then(function () {
        clearTimeout(timeout);
        st.inflight = false;
        if (!st.paused && !document.hidden) schedule(delay());
      });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { clearTimeout(st.timer); return; }
    st.graceUntil = Date.now() + 6000; // no stale banner while the first fetch after returning is on its way
    if (!st.paused) { clearTimeout(st.timer); poll(); }
    renderStatus();
  });
  var toggle = $('[data-live-toggle]');
  if (toggle) toggle.addEventListener('click', function () {
    st.paused = !st.paused;
    toggle.setAttribute('aria-pressed', st.paused ? 'true' : 'false');
    var s = $('span', toggle);
    if (s) s.textContent = st.paused ? 'Resume' : 'Pause';
    if (st.paused) clearTimeout(st.timer);
    else { st.graceUntil = Date.now() + 6000; poll(); }
    renderStatus();
  });
  var clear = $('[data-live-clear]');
  if (clear) clear.addEventListener('click', function () {
    var list = $('[data-live-changes-list]'), box = $('[data-live-changes]');
    if (list) list.replaceChildren();
    if (box) box.hidden = true;
  });
  var reload = $('[data-live-reload]');
  if (reload) reload.addEventListener('click', function (e) { e.preventDefault(); location.reload(); });

  // ------------------------------------------------------------------ re-check now
  function setBusy(btn, on) {
    if (on) {
      if (btn.getAttribute('aria-busy') === 'true') return;
      btn.setAttribute('aria-busy', 'true');
      btn.disabled = true;
      btn._title = btn.getAttribute('title');
      btn.setAttribute('title', 'Re-checking…');
      var s = $('span', btn);
      if (s) { btn._label = s.textContent; s.textContent = 'Re-checking…'; }
    } else {
      btn.removeAttribute('aria-busy');
      btn.disabled = false;
      if (btn._title !== undefined && btn._title !== null) btn.setAttribute('title', btn._title);
      var s2 = $('span', btn);
      if (s2 && btn._label) s2.textContent = btn._label;
    }
  }
  var RECHECK_TEXT = {
    done: function (n, code) { return code + ': re-checked ' + n + ' check' + (n === 1 ? '' : 's') + '.'; },
    partial: function (n, code) { return code + ': re-checked ' + n + ', but some checks could not be re-run (stored results shown for those).'; },
    failed: function (n, code) { return code + ': the re-check failed; the stored results are shown.'; },
    skipped: function (n, code) { return code + ': nothing to re-check (the checks are disabled or the references are missing).'; },
    running: function (n, code) { return code + ': the re-check is still running; results will appear here as they arrive.'; },
    busy: function (n, code) { return code + ': a re-check is already running.'; }
  };
  function recheck(btn) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    var body = new URLSearchParams();
    body.set('mode', btn.getAttribute('data-mode'));
    if (btn.getAttribute('data-item')) body.append('item', btn.getAttribute('data-item'));
    if (cfg.scope.run) body.set('run', String(cfg.scope.run));
    var rows = btn.getAttribute('data-item') ? $$('[data-live-row="' + btn.getAttribute('data-item') + '"]') : [];
    rows.forEach(function (r) { r.setAttribute('aria-busy', 'true'); });
    setBusy(btn, true);
    setMsg('Re-checking…');
    var release = function () { setBusy(btn, false); rows.forEach(function (r) { r.removeAttribute('aria-busy'); }); };
    fetch(cfg.recheckUrl, {method: 'POST', credentials: 'same-origin', headers: {'X-CSRFToken': csrf(), 'Accept': 'application/json',
         'Content-Type': 'application/x-www-form-urlencoded'}, body: body.toString()})
      .then(function (r) {
        var type = r.headers.get('Content-Type') || '';
        if (type.indexOf('json') < 0) throw {msg: r.status === 403 ? 'You do not have permission to re-check.' : 'The re-check could not be started (HTTP ' + r.status + ').'};
        return r.json().then(function (j) { return {status: r.status, json: j}; });
      })
      .then(function (res) {
        var j = res.json;
        if (res.status >= 400) throw {msg: j.error || 'The re-check could not be started.'};
        var code = j.mode && j.mode.code ? j.mode.code : 'Re-check';
        var running = j.state === 'running' || j.state === 'busy';
        if (running) {
          st.watching[code] = true;
          st.fastUntil = Date.now() + 45000;
          if (j.mode) j.mode.rechecking = true;
          clearTimeout(st.timer);
          schedule(FAST_MS);
        }
        applyData({modes: j.mode ? [j.mode] : [], generated_at: j.generated_at}, false);
        if (running) {
          // keep the button busy until the server reports the mode idle again
          setBusy(btn, true);
          rows.forEach(function (r) { r.setAttribute('aria-busy', 'true'); });
        } else {
          release();
        }
        var text = (RECHECK_TEXT[j.state] || RECHECK_TEXT.done)(j.ran, code);
        setMsg(text);
        queueAnnounce(text);
      })
      .catch(function (e) {
        release();
        var text = e && e.msg ? e.msg : 'The re-check failed: no connection.';
        setMsg(text);
        queueAnnounce(text);
      });
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-recheck]') : null;
    if (!btn || !root.contains(btn)) return;
    e.preventDefault();
    recheck(btn);
  });

  renderStatus();
  schedule(delay());
})();
