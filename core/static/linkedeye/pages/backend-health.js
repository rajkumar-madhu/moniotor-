(function () {
  'use strict';

  // Module labels come from core.backend_summary.MODULES; nothing is hardcoded here.
  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showEmpty(root, message) {
    var grid = root.querySelector('[data-summary-grid]');
    grid.replaceChildren(element('p', 'backend-health-empty', message));
  }

  function render(root, data) {
    var status = root.querySelector('[data-summary-status]');
    var title = root.querySelector('[data-summary-fetched]');
    var grid = root.querySelector('[data-summary-grid]');
    var foot = root.querySelector('[data-summary-foot]');
    if (!data.available) {
      status.textContent = data.configured ? 'Unavailable' : 'Not connected';
      showEmpty(root, data.configured
        ? 'Legacy backend is configured but not responding. Status data will appear when its summary API is available.'
        : 'Legacy backend summary is not connected. Configure the authenticated summary URL and shared token to show hardware, software, and application status.');
      return;
    }
    status.textContent = data.partial ? 'Partial data' : 'Connected';
    status.classList.add('is-connected');
    if (data.fetched_at && title) {
      var envLabel = title.dataset.envLabel || title.textContent;
      var fetched = new Date(data.fetched_at);
      var when = isNaN(fetched.getTime()) ? data.fetched_at : fetched.toLocaleString();
      title.textContent = envLabel;
      title.dataset.envLabel = envLabel;
      var sub = root.querySelector('[data-summary-subtitle]');
      if (sub) sub.textContent = 'Summary fetched ' + when;
    }
    grid.replaceChildren();
    (data.modules || []).forEach(function (module) {
      var counts = module.counts || {};
      var label = module.label || module.key;
      var ring = element('div', 'backend-ring' + (module.total ? '' : ' is-empty'));
      ring.setAttribute('role', 'img');
      ring.setAttribute('aria-label', label + ': ' + (counts.critical || 0) + ' critical, ' + (counts.warning || 0) + ' warning, ' + (counts.ok || 0) + ' OK, ' + (counts.unknown || 0) + ' unknown, ' + module.total + ' total');
      ring.style.setProperty('--critical-stop', module.critical_end_pct + '%');
      ring.style.setProperty('--warning-stop', module.warning_end_pct + '%');
      ring.style.setProperty('--ok-stop', module.ok_end_pct + '%');
      // The centre shows the OK share only; the *_end_pct values are cumulative ring stops.
      ring.appendChild(element('span', '', Math.round(module.ok_pct || 0) + '%'));
      var copy = element('div', 'backend-health-copy');
      copy.appendChild(element('span', 'backend-health-label', label));
      copy.appendChild(element('strong', '', (counts.ok || 0) + ' OK / ' + module.total));
      var stats = element('div', 'backend-health-stats');
      [['critical', 'critical'], ['warning', 'warning'], ['unknown', 'unknown']].forEach(function (entry) {
        var item = element('span');
        item.appendChild(element('i', 'dot-' + entry[0]));
        item.appendChild(document.createTextNode((counts[entry[0]] || 0) + ' ' + entry[1]));
        stats.appendChild(item);
      });
      copy.appendChild(stats);
      var card = element('article', 'backend-health-card');
      card.appendChild(ring);
      card.appendChild(copy);
      grid.appendChild(card);
    });
    foot.textContent = 'Status totals from ' + data.site_count + ' connected site' + (data.site_count === 1 ? '' : 's') + '.' + (data.partial ? ' Some sites did not respond.' : '');
  }

  document.querySelectorAll('[data-backend-summary]').forEach(function (root) {
    var controller = new AbortController();
    var timeout = window.setTimeout(function () { controller.abort(); }, 12000);
    var url = new URL(root.dataset.summaryUrl, window.location.href);
    if (root.dataset.site) url.searchParams.set('site', root.dataset.site);
    fetch(url, { method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: controller.signal })
      .then(function (response) { if (!response.ok) throw new Error('Summary request failed'); return response.json(); })
      .then(function (data) { render(root, data); })
      .catch(function () {
        root.querySelector('[data-summary-status]').textContent = 'Unavailable';
        showEmpty(root, 'Legacy backend summary could not be loaded. Retry the page to refresh the status.');
      })
      .finally(function () { window.clearTimeout(timeout); });
  });
}());
