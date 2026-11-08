/* LinkedEye APM dashboard.
 * The page ships one JSON snapshot (json_script) and draws everything from it; /apm/live/ returns the same shape and the
 * page is patched in place. Text is only ever written with textContent / setAttribute on elements created here: nothing
 * the producer sent is parsed as HTML. */
(function () {
  'use strict';
  var dataEl = document.getElementById('apm-data');
  var cfgEl = document.getElementById('apm-config');
  if (!dataEl || !cfgEl) return;

  var snap = JSON.parse(dataEl.textContent);
  var cfg = JSON.parse(cfgEl.textContent);
  var st = {exchange: cfg.state.exchange || '', client: cfg.state.client || '', status: cfg.state.status || '',
            q: cfg.state.q || '', sel: cfg.state.sel || null, logs: cfg.state.logs || 100};
  var STATE_LABEL = {failure: 'Failed', warning: 'Warning', success: 'Success', nodata: 'No data', stale: 'Stale'};
  var ROW_LABEL = {failure: 'FAILED', warning: 'WARNING', success: 'SUCCESS', nodata: 'NO DATA', stale: 'STALE'};
  var OVERALL = {healthy: ['Healthy', 'All systems operational'], degraded: ['Degraded', 'Some adapters need attention'],
                 down: ['Down', 'Every adapter is failing'], stale: ['Stale', 'No fresh report'], nodata: ['No data', 'Waiting for the first report']};

  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text, attrs) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    if (attrs) Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  var store = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window: not remembered */ } }
  };
  function csrf() {
    var m = document.cookie.match(/csrftoken=([^;]+)/);
    return (m && m[1]) || (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  }
  function cellUrl(base, id) { return base.replace('/0/', '/' + id + '/'); }
  function human(sec) {
    if (sec === null || sec === undefined) return '';
    if (sec < 90) return Math.round(sec) + ' s';
    if (sec < 5400) return Math.round(sec / 60) + ' min';
    if (sec < 172800) return Math.round(sec / 3600) + ' h';
    return Math.round(sec / 86400) + ' d';
  }
  function num(v, digits) { return v === null || v === undefined ? '-' : (digits === undefined ? String(v) : Number(v).toFixed(digits)); }

  // ------------------------------------------------------------------ state kept between renders
  var detail = null, tab = 'details', retryTimer = null, noteMode = '';
  var paused = false, clearedBefore = 0, pausedLogs = null;
  var auto = store.get('apm-auto', 'on') !== 'off';
  var interval = parseInt(store.get('apm-interval', '30'), 10);
  if ([10, 30, 60].indexOf(interval) < 0) interval = 30;
  var failures = 0, timer = null, lastOk = Date.now(), lastError = '';

  // ------------------------------------------------------------------ URL state
  function syncUrl() {
    var p = new URLSearchParams();
    p.set('source', String(cfg.source));
    ['exchange', 'client', 'status', 'q'].forEach(function (k) { if (st[k]) p.set(k, st[k]); });
    if (st.sel) p.set('sel', String(st.sel));
    if (st.logs !== 100) p.set('logs', String(st.logs));
    try { history.replaceState(null, '', cfg.urls.page + '?' + p.toString()); } catch (e) { /* ignore */ }
    var q = new URLSearchParams();
    q.set('source', String(cfg.source));
    ['exchange', 'client', 'status', 'q'].forEach(function (k) { if (st[k]) q.set(k, st[k]); });
    $('apm-export').setAttribute('href', cfg.urls.export + '?' + q.toString());
  }

  // ------------------------------------------------------------------ KPI strip, banner
  function setK(name, text) {
    var n = document.querySelector('[data-k="' + name + '"]');
    if (n) n.textContent = text;
  }
  function renderKpis() {
    var k = snap.kpis;
    var o = OVERALL[k.overall] || OVERALL.nodata;
    $('apm-overall').setAttribute('data-state', k.overall);
    setK('overall', o[0]);
    var sub = o[1];
    if (k.overall === 'degraded') {
      var bits = [];
      if (k.failure) bits.push(k.failure + ' failing');
      if (k.warning) bits.push(k.warning + ' warning');
      if (k.processes_down) bits.push(k.processes_down + ' process' + (k.processes_down === 1 ? '' : 'es') + ' down');
      sub = bits.join(' · ') || sub;
    } else if (k.overall === 'stale') {
      sub = 'Last report ' + (snap.stale.last_data || 'unknown');
    }
    setK('overall_sub', sub);
    setK('total', String(k.total));
    setK('total_sub', 'Across ' + k.exchanges + ' exchange' + (k.exchanges === 1 ? '' : 's'));
    setK('success', String(k.success));
    setK('success_sub', k.success_pct === null ? '' : k.success_pct.toFixed(2) + '%');
    setK('failure', String(k.failure));
    setK('failure_sub', k.stale ? k.stale + ' stale' : (k.failure_pct === null ? '' : k.failure_pct.toFixed(2) + '%'));
    setK('avg', k.avg_response_ms === null ? '-' : Math.round(k.avg_response_ms) + ' ms');
    setK('last', k.last_executed || '-');
    setK('duration', k.duration || '-');
  }
  function renderBanner() {
    var b = $('apm-banner'), s = snap.stale;
    b.className = 'apm-banner';
    if (s.banner) {
      b.textContent = 'Stale: no report for ' + human(s.age_s) + ' (expected every ' + human(s.expected_interval_s) +
        '). The cron job or the push may have stopped. What you see is the last known state' + (s.last_data ? ' from ' + s.last_data : '') + '.';
      b.classList.add('show', 'warn');
      b.hidden = false;
    } else if (s.waiting && !snap.kpis.total && !snap.kpis.processes) {
      b.textContent = 'Waiting for the first report from this source. Check the token and the cron job (see Sources).';
      b.classList.add('show', 'info');
      b.hidden = false;
    } else {
      b.hidden = true;
    }
  }

  // ------------------------------------------------------------------ exchange tabs
  function renderTabs() {
    var nav = $('apm-ex');
    var items = [['', 'Dashboard']].concat(snap.segments.map(function (s) { return [s, s]; }));
    if (st.exchange && snap.segments.indexOf(st.exchange) < 0) st.exchange = '';
    nav.replaceChildren.apply(nav, items.map(function (it) {
      var a = el('a', it[0] === st.exchange ? 'on' : '', it[1]);
      var p = new URLSearchParams({source: String(cfg.source)});
      if (it[0]) p.set('exchange', it[0]);
      a.setAttribute('href', cfg.urls.page + '?' + p.toString());
      if (it[0] === st.exchange) a.setAttribute('aria-current', 'page');
      a.addEventListener('click', function (e) {
        e.preventDefault();
        st.exchange = it[0];
        syncUrl(); renderTabs(); renderMatrix();
      });
      return a;
    }));
  }

  // ------------------------------------------------------------------ matrix
  function cellMatches(cell) {
    return !st.status || cell[3] === st.status;
  }
  function visibleClients() {
    var q = st.q.toLowerCase();
    var cols = st.exchange ? [st.exchange] : snap.segments;
    return snap.clients.filter(function (c) {
      if (st.client && c.client !== st.client) return false;
      var any = false, hit = !q;
      cols.forEach(function (seg) {
        var cell = c.cells[seg];
        if (!cell) return;
        if (!cellMatches(cell)) return;
        any = true;
        if (q && (seg.toLowerCase().indexOf(q) >= 0 || String(cell[1]).toLowerCase().indexOf(q) >= 0 || String(cell[2]).toLowerCase().indexOf(q) >= 0)) hit = true;
      });
      if (!st.exchange && !st.status && !any && !Object.keys(c.cells).length) any = true;
      if (q && c.client.toLowerCase().indexOf(q) >= 0 && any) hit = true;
      return any && hit;
    });
  }
  function selectCell(id) {
    st.sel = id;
    tab = tab || 'details';
    noteMode = '';
    syncUrl();
    renderMatrix();
    loadDetail(id);
  }
  // "08-11-2026 18:10:54" is drawn as "08-11 18:10:54" within the current year (full text in the tooltip): narrower columns
  function stamp(text, cls) {
    var td = el('td', cls);
    if (!text) { td.textContent = '-'; return td; }
    var m = /^(\d\d-\d\d)-(\d{4}) (.*)$/.exec(text);
    var thisYear = String(new Date(snap.now).getFullYear());
    td.textContent = m && m[2] === thisYear ? m[1] + ' ' + m[3] : text;
    td.setAttribute('title', text);
    return td;
  }
  function renderMatrix() {
    var table = $('apm-matrix');
    var cols = st.exchange ? [st.exchange] : snap.segments;
    var head = el('tr');
    head.appendChild(el('th', 'first', 'Client ID'));
    cols.forEach(function (s) { head.appendChild(el('th', 'c', s)); });
    ['Last Success', 'Last Failure', 'Status'].forEach(function (t) { head.appendChild(el('th', t === 'Status' ? 'st' : 'ts', t)); });
    table.tHead.replaceChildren(head);

    var rows = visibleClients();
    var body = rows.map(function (c) {
      var tr = el('tr');
      var selHere = false;
      var first = el('td', 'first');
      var btn = el('button', 'apm-client', c.client, {type: 'button', title: 'Open ' + c.client});
      btn.addEventListener('click', function () {
        var pick = null;
        cols.forEach(function (s) {
          var cell = c.cells[s];
          if (!cell) return;
          if (!pick || (cell[3] === 'failure' && pick[3] !== 'failure')) pick = cell;
        });
        if (pick) selectCell(pick[0]);
      });
      first.appendChild(btn);
      tr.appendChild(first);
      cols.forEach(function (s) {
        var cell = c.cells[s];
        var td;
        if (!cell) {
          td = el('td', 'c none', '-');
        } else {
          var show = cell[1] || cell[2] || '-';
          td = el('td', 'c ' + cell[3], show, {title: c.client + ' / ' + s + ': ' + (STATE_LABEL[cell[3]] || ''), 'data-id': String(cell[0])});
          if (cell[0] === st.sel) { td.classList.add('sel'); selHere = true; }
          td.addEventListener('click', function () { selectCell(cell[0]); });
        }
        tr.appendChild(td);
      });
      tr.appendChild(stamp(c.last_success, 'ts'));
      tr.appendChild(stamp(c.last_failure, 'ts' + (c.state === 'failure' ? ' bad' : '')));
      tr.appendChild(el('td', 'st ' + c.state, ROW_LABEL[c.state] || ''));
      if (selHere) tr.classList.add('sel');
      return tr;
    });
    table.tBodies[0].replaceChildren.apply(table.tBodies[0], body);
    if (!rows.length) {
      var empty = el('tr'), td = el('td', 'empty', snap.clients.length ? 'No adapters match the filters.' : 'No adapters reported yet.');
      td.setAttribute('colspan', String(cols.length + 4));
      empty.appendChild(td);
      table.tBodies[0].appendChild(empty);
    }
    $('apm-count').textContent = rows.length === snap.clients.length ? snap.clients.length + ' client' + (snap.clients.length === 1 ? '' : 's')
      : rows.length + ' of ' + snap.clients.length + ' clients';
  }
  function fillClientFilter() {
    var sel = $('apm-f-client');
    var opts = [el('option', '', 'All Clients', {value: ''})];
    snap.clients.forEach(function (c) { opts.push(el('option', '', c.client, {value: c.client})); });
    sel.replaceChildren.apply(sel, opts);
    sel.value = st.client;
    if (sel.value !== st.client) { st.client = ''; sel.value = ''; }
  }

  // ------------------------------------------------------------------ process table
  function renderProcesses() {
    var tb = $('apm-proc').tBodies[0];
    var k = snap.stale;
    $('apm-proc-note').textContent = snap.processes.length ? (k.table_stale ? 'Stale: no process report within the expected interval' : '') : '';
    if (!snap.processes.length) {
      var tr = el('tr'), td = el('td', 'empty', 'No process report yet. processStatus.py pushes this table.');
      td.setAttribute('colspan', '13');
      tr.appendChild(td);
      tb.replaceChildren(tr);
      return;
    }
    tb.replaceChildren.apply(tb, snap.processes.map(function (p) {
      var tr = el('tr');
      var label = p.state === 'success' ? 'RUNNING' : p.state === 'failure' ? 'NOT RUNNING' : p.state === 'warning' ? 'WARNING' : p.state === 'stale' ? 'STALE' : 'UNKNOWN';
      [['first', p.name], ['', p.host || '-'], ['num', p.pid === null ? '-' : String(p.pid)]].forEach(function (c) { tr.appendChild(el('td', c[0], c[1])); });
      tr.appendChild(el('td', 'st ' + p.state, label));
      tr.appendChild(el('td', 'num', num(p.cpu, 2)));
      tr.appendChild(el('td', 'num', num(p.mem, 2)));
      tr.appendChild(el('td', '', p.uptime || '-'));
      tr.appendChild(stamp(p.heartbeat, 'ts'));
      tr.appendChild(stamp(p.last_execution, 'ts'));
      tr.appendChild(stamp(p.next_execution, 'ts'));
      tr.appendChild(el('td', 'num resp', p.response_ms === null ? '-' : Math.round(p.response_ms) + ' ms'));
      tr.appendChild(el('td', 'num', p.exit_code === null ? '-' : String(p.exit_code)));
      tr.appendChild(el('td', 'rem', p.remarks || (p.state === 'success' ? 'OK' : '-')));
      return tr;
    }));
  }

  // ------------------------------------------------------------------ logs
  function renderLogs(force) {
    if (paused && !force) return;
    var box = $('apm-lscroll');
    var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    var lines = snap.logs.filter(function (l) { return l.id > clearedBefore; });
    var tb = $('apm-log').tBodies[0];
    if (!lines.length) {
      var tr = el('tr'), td = el('td', 'empty', clearedBefore ? 'View cleared. New lines appear here; the stored logs were not deleted.' : 'No log lines yet.');
      td.setAttribute('colspan', '4');
      tr.appendChild(td);
      tb.replaceChildren(tr);
    } else {
      tb.replaceChildren.apply(tb, lines.map(function (l) {
        var tr = el('tr', 'lv-' + l.level.toLowerCase());
        tr.appendChild(el('td', 'ts', l.ts));
        tr.appendChild(el('td', 'lvl', l.level));
        tr.appendChild(el('td', '', l.process || '-'));
        tr.appendChild(el('td', 'msg', l.message));
        return tr;
      }));
    }
    if (atBottom || force) box.scrollTop = box.scrollHeight;
    $('apm-l-note').textContent = (paused ? 'Paused: new lines are held back. ' : '') + 'Showing the last ' + snap.logs_n + ' stored lines.';
  }

  // ------------------------------------------------------------------ detail panel
  function kv(label, value, cls) {
    var row = el('div', 'kv');
    row.appendChild(el('dt', '', label));
    row.appendChild(el('dd', cls || '', value === null || value === undefined || value === '' ? '-' : String(value)));
    return row;
  }
  function snippetBox(lines) {
    var pre = el('div', 'apm-snippet');
    if (!lines.length) { pre.appendChild(el('div', 'ln mute', 'No log lines mention this adapter.')); return pre; }
    lines.forEach(function (l) { pre.appendChild(el('div', 'ln lv-' + l.level.toLowerCase(), l.ts + ' - ' + l.level + ' - ' + l.message)); });
    return pre;
  }
  function section(title) { return el('h3', 'apm-h3', title); }

  function bodyDetails(d) {
    var f = el('div');
    var dl = el('dl', 'apm-dl');
    dl.appendChild(kv('Client ID', d.client));
    dl.appendChild(kv('Exchange', d.segment));
    dl.appendChild(kv('Adapter ID / CTCL ID', d.ctcl_id));
    dl.appendChild(kv('Value', d.value));
    dl.appendChild(kv('Status', d.stale ? d.status_label + ' (stale)' : d.status_label, 's-' + d.state));
    dl.appendChild(kv('Last Execution', d.details.last_execution));
    dl.appendChild(kv('Duration', d.details.duration));
    dl.appendChild(kv('Script Name', d.details.script));
    dl.appendChild(kv('Host Name', d.details.host));
    dl.appendChild(kv('Next Execution', d.details.next_execution));
    f.appendChild(dl);
    if (d.failure.active) {
      f.appendChild(section('Failure Summary'));
      var s = el('dl', 'apm-dl');
      s.appendChild(kv('Error Message', d.failure.error || 'not provided', 's-failure'));
      s.appendChild(kv('Exit Code', d.failure.exit_code));
      s.appendChild(kv('Failure Time', d.failure.failure_time));
      s.appendChild(kv('Failure Count', d.failure.count));
      f.appendChild(s);
      f.appendChild(section('Recent Log Snippet'));
      f.appendChild(snippetBox(d.failure.snippet));
    }
    return f;
  }
  function bodyFailure(d) {
    var f = el('div');
    if (!d.failure.active) {
      f.appendChild(el('p', 'apm-d-empty', 'This adapter is not failing.' + (d.failure.last_success ? ' Last success: ' + d.failure.last_success + '.' : '')));
      if (d.failure.count) f.appendChild(el('p', 'small t-mute', 'It has failed ' + d.failure.count + ' time(s) in total.'));
      return f;
    }
    f.appendChild(el('p', 'apm-summary', d.failure.summary));
    var dl = el('dl', 'apm-dl');
    dl.appendChild(kv('Error Message', d.failure.error || 'not provided', 's-failure'));
    dl.appendChild(kv('Exit Code', d.failure.exit_code));
    dl.appendChild(kv('Failure Time', d.failure.failure_time));
    dl.appendChild(kv('Last Success', d.failure.last_success));
    dl.appendChild(kv('Failure Count', d.failure.count));
    dl.appendChild(kv('Consecutive Failures', d.failure.streak));
    f.appendChild(dl);
    f.appendChild(section('Recent Log Snippet'));
    f.appendChild(snippetBox(d.failure.snippet));
    return f;
  }
  function bodyHistory(d) {
    var f = el('div');
    f.appendChild(section('Last 24 hours'));
    var strip = el('div', 'apm-spark', null, {role: 'img', 'aria-label': 'Status over the last 24 hours'});
    d.spark.forEach(function (s) { strip.appendChild(el('i', s)); });
    f.appendChild(strip);
    f.appendChild(el('div', 'apm-spark-key t-mute small', '24 h ago  to  now'));
    f.appendChild(section('Status changes'));
    if (!d.history.length) { f.appendChild(el('p', 'apm-d-empty', 'No status changes recorded yet.')); return f; }
    var ul = el('ul', 'apm-timeline');
    d.history.forEach(function (h) {
      var li = el('li', h.new_key);
      li.appendChild(el('span', 'when', h.ts));
      li.appendChild(el('span', 'what', (h.old ? h.old + ' to ' : 'Seen as ') + h.new));
      if (h.error) li.appendChild(el('span', 'why', h.error));
      ul.appendChild(li);
    });
    f.appendChild(ul);
    return f;
  }
  function bodyLogs(d) {
    var f = el('div');
    if (!d.logs.length) { f.appendChild(el('p', 'apm-d-empty', 'No stored log lines mention this adapter.')); return f; }
    var box = el('div', 'apm-snippet tall');
    d.logs.forEach(function (l) { box.appendChild(el('div', 'ln lv-' + l.level.toLowerCase(), l.ts + ' - ' + l.level + ' - ' + (l.process ? l.process + ' - ' : '') + l.message)); });
    f.appendChild(box);
    return f;
  }
  function bodyEvidence(d) {
    var f = el('div');
    f.appendChild(el('pre', 'apm-evidence', d.evidence));
    var row = el('div', 'apm-actions');
    var copy = el('button', 'btn', 'Copy Evidence', {type: 'button'});
    copy.addEventListener('click', copyEvidence);
    var dl = el('a', 'btn', 'Download .txt', {href: cellUrl(cfg.urls.evidence, d.id)});
    row.appendChild(copy); row.appendChild(dl);
    f.appendChild(row);
    return f;
  }

  function retryLabel(d) {
    var r = d.retry;
    if (!r) return ['Retry Now', false];
    if (r.state === 'pending') return ['Retry requested', true];
    if (r.state === 'picked') return ['Picked up', true];
    return ['Retry Now', false];
  }
  function renderDetail() {
    var d = detail;
    $('apm-d-empty').hidden = !!d;
    $('apm-d').hidden = !d;
    if (!d) return;
    $('apm-d-head').setAttribute('data-state', d.state);
    $('apm-d-title').textContent = d.client + ' - ' + d.segment;
    var pill = $('apm-d-status');
    pill.textContent = (d.stale ? 'STALE' : d.status_label.toUpperCase());
    pill.className = 'apm-pill ' + d.state;
    Array.prototype.forEach.call($('apm-d-tabs').children, function (b) {
      var on = b.getAttribute('data-tab') === tab;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    var body = $('apm-d-body');
    var content = tab === 'failure' ? bodyFailure(d) : tab === 'history' ? bodyHistory(d) : tab === 'logs' ? bodyLogs(d) : tab === 'evidence' ? bodyEvidence(d) : bodyDetails(d);
    var sibs = siblings(d);
    body.replaceChildren.apply(body, sibs ? [sibs, content] : [content]);
    var rl = retryLabel(d);
    var rb = $('apm-a-retry');
    rb.lastChild.textContent = rl[0];
    rb.disabled = rl[1] || !d.can_retry;
    rb.title = !d.can_retry ? 'Needs the operator role on this site and an enabled source' : (rl[1] ? 'Waiting for the script to pick this up' : '');
    var rca = $('apm-a-rca');
    rca.disabled = false;
    rca.title = d.can_rca ? 'Create a Problem in Service Desk with these facts' : 'Shows an RCA draft to copy';
    $('apm-a-export').setAttribute('href', cfg.urls.export + '?source=' + cfg.source + '&cell=' + d.id);
    renderNote();
  }
  function siblings(d) {
    var row = snap.clients.filter(function (c) { return c.client === d.client; })[0];
    if (!row) return null;
    var segs = Object.keys(row.cells);
    if (segs.length < 2) return null;
    var wrap = el('div', 'apm-sibs', null, {role: 'group', 'aria-label': 'Other adapters of ' + d.client});
    segs.forEach(function (s) {
      var cell = row.cells[s];
      var b = el('button', 'apm-sib ' + cell[3] + (cell[0] === d.id ? ' on' : ''), s, {type: 'button', title: (STATE_LABEL[cell[3]] || '') + ' ' + (cell[1] || '')});
      b.addEventListener('click', function () { selectCell(cell[0]); });
      wrap.appendChild(b);
    });
    return wrap;
  }
  function renderNote() {
    var n = $('apm-d-note');
    n.className = 'apm-note';
    n.replaceChildren();
    if (!detail) { n.hidden = true; return; }
    if (noteMode === 'script') {
      n.hidden = false;
      n.classList.add('info');
      n.appendChild(el('div', '', 'Script: ' + (detail.details.script || 'not reported') + ' on ' + (detail.details.host || 'an unreported host') + '.'));
      n.appendChild(el('div', 't-mute small', 'LinkedEye does not run scripts or reach into your hosts. A retry is a request the script pulls (GET /api/apm/commands/).'));
      return;
    }
    if (noteMode && typeof noteMode === 'object') {
      n.hidden = false;
      n.classList.add(noteMode.cls || 'info');
      n.appendChild(el('div', '', noteMode.text));
      if (noteMode.link) { var a = el('a', '', noteMode.link.label); a.setAttribute('href', noteMode.link.href); n.appendChild(a); }
      if (noteMode.draft) {
        var ta = el('textarea', 'apm-draft', noteMode.draft, {readonly: 'readonly', rows: '8', 'aria-label': 'RCA draft'});
        n.appendChild(ta);
        var cp = el('button', 'btn', 'Copy draft', {type: 'button'});
        cp.addEventListener('click', function () { copyText(noteMode.draft, 'RCA draft copied.'); });
        n.appendChild(cp);
      }
      return;
    }
    var r = detail.retry;
    if (r && (r.state === 'pending' || r.state === 'picked' || r.state === 'done' || r.state === 'expired')) {
      n.hidden = false;
      n.classList.add(r.state === 'done' ? 'ok' : r.state === 'expired' ? 'warn' : 'info');
      var txt = r.state === 'pending' ? 'Retry requested by ' + r.requested_by + ' at ' + r.requested_at + '. Waiting for the script to pick it up.'
        : r.state === 'picked' ? 'Picked up by the script at ' + r.picked_at + '. Waiting for its next report.'
        : r.state === 'done' ? 'Retry completed at ' + r.done_at + '.' : 'The last retry request expired without being picked up.';
      n.appendChild(el('div', '', txt));
      return;
    }
    n.hidden = true;
  }

  function loadDetail(id) {
    clearTimeout(retryTimer);
    if (!id) { detail = null; renderDetail(); return; }
    fetch(cellUrl(cfg.urls.cell, id), {headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        if (st.sel !== id) return;
        detail = d;
        renderDetail();
        if (d.retry && (d.retry.state === 'pending' || d.retry.state === 'picked')) {
          retryTimer = setTimeout(function () { loadDetail(id); }, 5000);
        }
      })
      .catch(function () {
        if (st.sel !== id) return;
        noteMode = {cls: 'warn', text: 'The adapter details could not be loaded. They will load again on the next refresh.'};
        $('apm-d-empty').hidden = true; $('apm-d').hidden = false;
        renderNote();
      });
  }

  function copyText(text, okMsg) {
    function done(ok) {
      noteMode = {cls: ok ? 'ok' : 'warn', text: ok ? okMsg : 'Copy failed: select the text and copy it by hand.'};
      renderNote();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      return;
    }
    var ta = el('textarea', 'sr', text);
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    done(ok);
  }
  function copyEvidence() { if (detail) copyText(detail.evidence, 'Evidence copied to the clipboard.'); }

  function post(url) {
    return fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'X-CSRFToken': csrf(), Accept: 'application/json'}})
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (b) { if (!r.ok) throw new Error(b.error || ('HTTP ' + r.status)); return b; }); });
  }
  function doRetry() {
    if (!detail) return;
    var id = detail.id;
    $('apm-a-retry').disabled = true;
    post(cellUrl(cfg.urls.retry, id)).then(function (b) {
      if (st.sel !== id) return;
      detail.retry = b.retry;
      noteMode = '';
      renderDetail();
      clearTimeout(retryTimer);
      retryTimer = setTimeout(function () { loadDetail(id); }, 5000);
    }).catch(function (e) {
      noteMode = {cls: 'crit', text: 'Retry failed: ' + e.message};
      renderDetail();
    });
  }
  function doRca() {
    if (!detail) return;
    var id = detail.id;
    $('apm-a-rca').disabled = true;
    post(cellUrl(cfg.urls.rca, id)).then(function (b) {
      if (st.sel !== id) return;
      if (b.number) {
        noteMode = {cls: 'ok', text: (b.created ? 'Problem ' : 'Open problem ') + b.number + (b.created ? ' created.' : ' already exists for this adapter.'),
                    link: b.url && b.url.charAt(0) === '/' ? {href: b.url, label: 'Open ' + b.number} : null};
      } else {
        noteMode = {cls: 'info', text: (b.reason || 'RCA draft') + ' Copy it into your ticket or report.', draft: b.draft};
      }
      renderDetail();
    }).catch(function (e) {
      noteMode = {cls: 'crit', text: 'Generate RCA failed: ' + e.message};
      renderDetail();
    });
  }

  // ------------------------------------------------------------------ live patching
  function renderAll() {
    renderKpis(); renderBanner(); renderTabs(); fillClientFilter(); renderMatrix(); renderProcesses(); renderLogs(false);
    $('apm-foot').textContent = 'Times are shown in ' + snap.tz + '. Data comes from ' + (snap.source.script || 'the producer') +
      (snap.source.host ? ' on ' + snap.source.host : '') + (snap.source.redis_poll ? ' (read from Redis)' : '') + '.';
  }
  function setNote(text) { $('apm-auto-note').textContent = text; }
  function tickNote() {
    if (!auto) { setNote('Auto refresh is off'); return; }
    if (lastError) { setNote('Live updates paused: ' + lastError + ' (retrying)'); return; }
    var s = Math.max(0, Math.round((Date.now() - lastOk) / 1000));
    setNote('Updated ' + (s < 2 ? 'just now' : s + ' s ago'));
  }
  function schedule() {
    clearTimeout(timer);
    if (!auto) return;
    var delay = Math.min(300, interval * Math.pow(2, Math.min(failures, 5))) * 1000;
    timer = setTimeout(poll, delay);
  }
  function poll(force) {
    if (document.hidden && force !== true) { schedule(); return Promise.resolve(); }
    var url = cfg.urls.live + '?source=' + cfg.source + '&logs=' + st.logs + '&rev=' + encodeURIComponent(snap.rev);
    return fetch(url, {headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        failures = 0; lastError = ''; lastOk = Date.now();
        if (d.unchanged) {
          Object.keys(d.stale).forEach(function (k) { snap.stale[k] = d.stale[k]; });
          renderBanner();
        } else {
          var changedSel = st.sel && detail;
          snap = d;
          renderAll();
          if (changedSel) loadDetail(st.sel);
        }
        tickNote();
      })
      .catch(function (e) { failures += 1; lastError = e.message || 'network error'; tickNote(); })
      .then(schedule);
  }

  // ------------------------------------------------------------------ wiring
  function wire() {
    var n = $('apm-logs-n');
    cfg.log_choices.forEach(function (c) { n.appendChild(el('option', '', 'Last ' + c + ' Logs', {value: String(c)})); });
    n.value = String(st.logs);
    n.addEventListener('change', function () { st.logs = parseInt(n.value, 10); syncUrl(); clearedBefore = 0; poll(true); });
    $('apm-f-client').addEventListener('change', function (e) { st.client = e.target.value; syncUrl(); renderMatrix(); });
    var q = $('apm-f-q'); q.value = st.q;
    q.addEventListener('input', function () { st.q = q.value.trim().slice(0, 60); syncUrl(); renderMatrix(); });
    var f = $('apm-f-status'); f.value = st.status;
    f.addEventListener('change', function () { st.status = f.value; syncUrl(); renderMatrix(); });
    var sw = $('apm-auto');
    sw.setAttribute('aria-checked', auto ? 'true' : 'false');
    sw.addEventListener('click', function () {
      auto = !auto;
      sw.setAttribute('aria-checked', auto ? 'true' : 'false');
      store.set('apm-auto', auto ? 'on' : 'off');
      tickNote();
      if (auto) poll(true); else clearTimeout(timer);
    });
    var iv = $('apm-interval'); iv.value = String(interval);
    iv.addEventListener('change', function () { interval = parseInt(iv.value, 10); store.set('apm-interval', String(interval)); schedule(); });
    $('apm-l-pause').addEventListener('click', function () {
      paused = !paused;
      var b = $('apm-l-pause');
      b.setAttribute('aria-pressed', paused ? 'true' : 'false');
      b.lastChild.textContent = paused ? 'Resume' : 'Pause';
      if (!paused) renderLogs(true); else $('apm-l-note').textContent = 'Paused: new lines are held back.';
    });
    $('apm-l-clear').addEventListener('click', function () {
      var last = snap.logs.length ? snap.logs[snap.logs.length - 1].id : 0;
      clearedBefore = Math.max(clearedBefore, last);
      renderLogs(true);
    });
    Array.prototype.forEach.call($('apm-d-tabs').children, function (b) {
      b.addEventListener('click', function () { tab = b.getAttribute('data-tab'); noteMode = ''; renderDetail(); });
    });
    $('apm-a-retry').addEventListener('click', doRetry);
    $('apm-a-script').addEventListener('click', function () { noteMode = noteMode === 'script' ? '' : 'script'; renderNote(); });
    $('apm-a-logs').addEventListener('click', function () { tab = 'logs'; noteMode = ''; renderDetail(); });
    $('apm-a-copy').addEventListener('click', copyEvidence);
    $('apm-a-rca').addEventListener('click', doRca);
    document.addEventListener('visibilitychange', function () { if (!document.hidden && auto && Date.now() - lastOk > interval * 1000) poll(true); });
    setInterval(tickNote, 1000);
  }

  function firstFailing() {
    for (var i = 0; i < snap.clients.length; i++) {
      var cells = snap.clients[i].cells;
      for (var s in cells) { if (cells[s][3] === 'failure') return cells[s][0]; }
    }
    return null;
  }
  wire();
  if (!st.sel) st.sel = firstFailing();
  renderAll();
  syncUrl();
  if (st.sel) loadDetail(st.sel); else renderDetail();
  tickNote();
  schedule();
})();
