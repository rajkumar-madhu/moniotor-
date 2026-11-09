const { chromium } = require('playwright');
// Browser check for the sidebar (docs/UI.md). Run against a running console with a demo user:
//   LE_BASE=http://127.0.0.1:8000 LE_USER=admin LE_PASS=... LE_SHOTS=/tmp/shots/ node docs/ui-checks/nav-check.js
// Needs Playwright (require('playwright')). Prints ok/FAIL per behaviour and any console errors.
const BASE = process.env.LE_BASE || 'http://127.0.0.1:8000';
const SHOTS = process.env.LE_SHOTS || '/tmp/';
async function login(page) {
  await page.goto(BASE + '/accounts/login/?next=/');
  await page.fill('#id_username', process.env.LE_USER || 'admin');
  await page.fill('#id_password', process.env.LE_PASS || '');
  await page.click('form.card button[type=submit], form.card input[type=submit]');
  await page.waitForLoadState('networkidle');
}
const res = [];
const ok = (n, c, x) => { res.push((c ? 'ok   ' : 'FAIL ') + n + (x !== undefined ? ' ' + JSON.stringify(x) : '')); };
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror ' + e));
  page.on('console', m => { if (m.type() === 'error') errors.push('console ' + m.text()); });
  await login(page);
  await page.goto(BASE + '/monitoring/fleet/');
  await page.waitForLoadState('networkidle');
  // sections: collapse + remember
  await page.click('[data-nav-sec][aria-controls="nav-sec-infrastructure"]');
  ok('section collapses', (await page.getAttribute('[data-nav-sec][aria-controls="nav-sec-infrastructure"]', 'aria-expanded')) === 'false');
  ok('body hidden', !(await page.isVisible('#nav-sec-infrastructure')));
  await page.reload(); await page.waitForLoadState('networkidle');
  ok('collapsed state remembered', !(await page.isVisible('#nav-sec-infrastructure')));
  ok('active section stays open', await page.isVisible('#nav-sec-observe'));
  await page.click('[data-nav-sec][aria-controls="nav-sec-infrastructure"]');
  // group open/close remembered
  await page.click('details[data-key="servicedesk"] > summary'.replace('servicedesk','service-desk'));
  ok('group opens', await page.evaluate(() => document.querySelector('details[data-key="service-desk"]').open));
  await page.reload(); await page.waitForLoadState('networkidle');
  ok('group open remembered', await page.evaluate(() => document.querySelector('details[data-key="service-desk"]').open));
  // filter
  await page.keyboard.press('/');
  ok('/ focuses filter', await page.evaluate(() => document.activeElement && document.activeElement.hasAttribute('data-nav-filter')));
  await page.keyboard.type('kube');
  const vis = await page.evaluate(() => Array.from(document.querySelectorAll('.nav-row:not([hidden]) a')).filter(a => a.getClientRects().length).map(a => a.textContent.trim()));
  ok('filter shows kube matches from collapsed groups', vis.some(t => /Kubernetes \(live\)/.test(t)) && vis.some(t => /Inventory Overview/.test(t)) && !vis.includes('Fleet'), vis.slice(0, 6));
  await page.screenshot({ path: SHOTS + 'nav-filter.png', clip: { x: 0, y: 0, width: 260, height: 900 } });
  await page.keyboard.type('zzzz');
  ok('no match note', await page.isVisible('[data-nav-empty]'));
  await page.keyboard.press('Escape');
  ok('Escape clears', (await page.inputValue('[data-nav-filter]')) === '');
  ok('groups restored after clear', await page.evaluate(() => !document.querySelector('details[data-key="k8s-inventory"]') && !document.querySelector('.nav-row[hidden]')));
  // pin
  await page.hover('.nav-row[data-key="telemetry:explorer"]');
  await page.click('.nav-row[data-key="telemetry:explorer"] .nav-pin');
  ok('pinned section shows', await page.isVisible('.nav-pinned'));
  ok('pinned link', (await page.textContent('[data-pinned-list] a')).includes('Metrics Explorer'));
  await page.reload(); await page.waitForLoadState('networkidle');
  ok('pin remembered', await page.isVisible('.nav-pinned'));
  await page.screenshot({ path: SHOTS + 'nav-pinned.png', clip: { x: 0, y: 0, width: 260, height: 900 } });
  // keyboard
  await page.focus('[data-nav] a[href="/"]');
  await page.keyboard.press('ArrowDown');
  ok('arrow down moves', await page.evaluate(() => document.activeElement.textContent.includes('Ops Agent')));
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowRight');
  ok('arrow right on open group enters it', await page.evaluate(() => document.activeElement.closest('details') && document.activeElement.closest('details').dataset.key === 'service-desk' && document.activeElement.tagName === 'A'));
  await page.keyboard.press('ArrowLeft');
  ok('arrow left returns to summary', await page.evaluate(() => document.activeElement.tagName === 'SUMMARY'));
  ok('aria-current', await page.evaluate(() => !!document.querySelector('a[aria-current="page"]')));
  // collapsed rail + flyout
  await page.click('[data-toggle-menu]');
  ok('rail collapsed', await page.evaluate(() => !document.getElementById('shell').classList.contains('rail-open')));
  await page.waitForTimeout(400);
  const w = await page.evaluate(() => document.getElementById('side').getBoundingClientRect().width);
  ok('rail is 72px', w === 72, w);
  await page.hover('details[data-key="checks"] > summary');
  await page.waitForTimeout(300);
  ok('flyout shows group links', await page.evaluate(() => { const f = document.getElementById('nav-flyout'); return !f.hidden && f.querySelectorAll('a').length === 3 && f.textContent.includes('Uptime Checks'); }));
  await page.screenshot({ path: SHOTS + 'nav-rail-flyout.png', clip: { x: 0, y: 0, width: 420, height: 900 } });
  await page.keyboard.press('Escape');
  ok('Escape closes flyout', await page.evaluate(() => document.getElementById('nav-flyout').hidden));
  await page.hover('.nav-row[data-key="telemetry:fleet"]');
  await page.waitForTimeout(300);
  ok('single link flyout', await page.evaluate(() => { const f = document.getElementById('nav-flyout'); return !f.hidden && f.textContent.includes('Fleet'); }));
  await page.mouse.move(700, 400);
  await page.waitForTimeout(400);
  ok('flyout closes on leave', await page.evaluate(() => document.getElementById('nav-flyout').hidden));
  await page.click('[data-toggle-menu]');
  // mobile
  await page.setViewportSize({ width: 400, height: 800 });
  await page.reload(); await page.waitForLoadState('networkidle');
  ok('drawer closed', !(await page.isVisible('#side')));
  await page.click('[data-toggle-menu]');
  ok('drawer open', await page.isVisible('#side'));
  await page.fill('[data-nav-filter]', 'alert');
  const mv = await page.evaluate(() => Array.from(document.querySelectorAll('.nav-row:not([hidden]) a')).filter(a => a.getClientRects().length).map(a => a.textContent.trim()));
  ok('mobile filter', mv.some(t => t.startsWith('Alert Manager')), mv);
  await page.screenshot({ path: SHOTS + 'nav-mobile.png' });
  await page.fill('[data-nav-filter]', '');
  // tab trap: shift-tab from first goes to last
  await page.focus('#side .brand');
  const seen = new Set();
  for (let i = 0; i < 80; i++) { await page.keyboard.press('Tab'); seen.add(await page.evaluate(() => { const a = document.activeElement; return a.tagName + ':' + (a.textContent || a.getAttribute('aria-label') || '').trim().slice(0, 20) + ':' + (document.getElementById('side').contains(a)); })); }
  ok('focus stays in drawer', await page.evaluate(() => document.getElementById('side').contains(document.activeElement)));
  await page.keyboard.press('Escape');
  ok('Escape closes drawer', !(await page.isVisible('#side')));
  console.log(res.join('\n')); console.log('errors', errors);
  await browser.close();
})();
