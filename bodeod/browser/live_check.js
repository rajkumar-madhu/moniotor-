/* Browser check of the BOD / EOD live integration (manual; not part of CI).
 *
 * Starts its own throwaway database, a stub HTTP / TCP server and the real dev server, then drives the Key Validation page in
 * Chromium with a FAKE CLOCK (Playwright page.clock) so five minutes of polling take seconds:
 *   - no full-page reload, the endpoint is polled repeatedly;
 *   - a row flips in place (OK -> failed -> recovered), flashes, lands in "Changes since you opened this page" and is announced;
 *   - no polling while a field has focus, polling stops on Pause and resumes;
 *   - the endpoint failing shows the stale banner, polls back off, and the banner clears on recovery;
 *   - "Re-check now" re-runs a URL check against the stub through probes.checks, a slow probe answers "still running".
 *
 * Needs: node, `npm i playwright` (or any install providing require('playwright')) with Chromium, and a Python with the project's
 * requirements. From the repository root:
 *     PYTHON=.venv/bin/python node bodeod/browser/live_check.js
 * Optional: PORT (default 8231), SHOTS (directory for screenshots).
 * It sends no request outside 127.0.0.1 and exits non-zero when a check fails.
 */
const { chromium } = require('playwright');
const { execSync, spawn } = require('child_process');
const http = require('http');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REPO = process.cwd();
const PYTHON = process.env.PYTHON || 'python3';
const PORT = process.env.PORT || '8231';
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS || '';
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'bodeod-live-'));
const ENV = { ...process.env, LINKEDEYE_DEBUG: 'true', LINKEDEYE_DB_NAME: path.join(TMP, 'db.sqlite3'),
  LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS: '127.0.0.0/8', LINKEDEYE_PROMETHEUS_URL: '' };
const dj = (code) => execSync(`${PYTHON} manage.py shell`, { cwd: REPO, env: ENV, input: code + '\n', stdio: ['pipe', 'pipe', 'pipe'] }).toString();

let stubMode = 'ok';
const httpStub = http.createServer((req, res) => {
  const reply = () => { res.writeHead(stubMode === 'fail' ? 503 : 200, { 'Content-Length': 2 }); res.end('ok'); };
  if (stubMode === 'slow') setTimeout(reply, 12000); else reply();
});
const tcpStub = net.createServer((s) => s.end());

const results = [];
const check = (name, cond, extra) => results.push([cond ? 'PASS' : 'FAIL', name, extra || '']);

(async () => {
  await new Promise((r) => httpStub.listen(9311, '127.0.0.1', r));
  await new Promise((r) => tcpStub.listen(9312, '127.0.0.1', r));
  execSync(`${PYTHON} manage.py migrate --noinput`, { cwd: REPO, env: ENV, stdio: 'ignore' });
  dj(`
from datetime import time, timedelta
from django.contrib.auth.models import User
from django.utils import timezone
from bodeod.models import Mode, ChecklistItem
from domainmonitor.models import Domain
from urlchecker.models import UrlCheck
from tcpcheck.models import TcpCheck
User.objects.create_superuser('admin', 'a@example.com', 'Check-pass-2026!')
m = Mode.objects.create(code='BOD', name='Beginning of Day', window_start=time(7), window_end=time(9), refresh_checks=False)
now = timezone.now()
Domain.objects.create(tenant_key='default', site_key='All Sites', hostname='nseindia.com', status='up', dns_status='Resolved', ssl_status='Valid',
                      ssl_expires_at=now + timedelta(days=41, hours=1), last_checked_at=now)
UrlCheck.objects.create(name='Portal', url='http://127.0.0.1:9311/', last_status='online', last_checked_at=now, last_response_ms=4)
TcpCheck.objects.create(name='Gateway', host='127.0.0.1', port=9312, site='All Sites', last_status='online', last_checked_at=now)
ChecklistItem.objects.create(mode=m, name='nseindia.com DNS & SSL', kind='domain', ref_name='nseindia.com', order=10)
ChecklistItem.objects.create(mode=m, name='Portal reachable', kind='url_check', ref_name='Portal', order=20)
ChecklistItem.objects.create(mode=m, name='Gateway open', kind='tcp_check', ref_name='Gateway', order=30, critical=False)
ChecklistItem.objects.create(mode=m, name='Feed connected', kind='manual', order=40)
`);
  const server = spawn(PYTHON, ['manage.py', 'runserver', PORT, '--noreload'], { cwd: REPO, env: ENV, stdio: 'ignore' });
  const stopAll = async () => { server.kill(); httpStub.close(); tcpStub.close(); fs.rmSync(TMP, { recursive: true, force: true }); };
  try {
    for (let i = 0; i < 60; i++) { try { await new Promise((ok, no) => http.get(BASE + '/accounts/login/', (r) => { r.resume(); ok(); }).on('error', no)); break; } catch (e) { await new Promise((r) => setTimeout(r, 500)); } }

    const browser = await chromium.launch({ args: ['--trace', 'off'] }).catch(() => chromium.launch());
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    await page.goto(BASE + '/accounts/login/');
    await page.fill('input[name=username]', 'admin'); await page.fill('input[name=password]', 'Check-pass-2026!');
    await Promise.all([page.waitForNavigation().catch(() => {}), page.click('button[type=submit]')]);

    await page.clock.install();
    let liveReqs = 0, navs = 0;
    page.on('request', (r) => { if (r.url().includes('/bod-eod/api/live/')) liveReqs++; if (r.isNavigationRequest()) navs++; });
    await page.goto(BASE + '/bod-eod/keys/?mode=BOD');
    await page.evaluate(() => { window.__marker = 'same-document'; });
    navs = 0;
    const shot = async (n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `bod-live-${n}.png`) }); };
    const step = async (ms) => { await page.clock.runFor(ms); await page.waitForTimeout(150); };

    for (let i = 0; i < 12; i++) await step(25000);
    check('no full-page reload in 5 min', (await page.evaluate(() => window.__marker)) === 'same-document' && navs === 0, `navs=${navs}`);
    check('endpoint polled repeatedly', liveReqs >= 8, `requests=${liveReqs}`);
    const row = page.locator('[data-live-row]', { hasText: 'nseindia.com DNS & SSL' });
    const detail = () => row.locator('[data-live="detail"]').textContent();
    check('short DNS/certificate detail', (await detail()).includes('certificate valid, 41 days left'), await detail());
    await shot('0-before');

    // the certificate expires in the stored data: the row flips in place
    dj("from domainmonitor.models import Domain; from django.utils import timezone; Domain.objects.filter(hostname='nseindia.com').update(ssl_status='Expired', last_checked_at=timezone.now())");
    let flashed = false;
    for (let i = 0; i < 12; i++) {
      await step(4000);
      if ((await row.locator('[data-live="pill"]').textContent()).includes('CRIT')) { flashed = await row.evaluate((e) => e.classList.contains('bod-flash')); break; }
    }
    check('row patched to CRIT in place', (await row.locator('[data-live="pill"]').textContent()).includes('CRIT'));
    check('row flashed', flashed);
    check('detail says certificate expired', (await detail()).includes('certificate expired'), await detail());
    const list = page.locator('[data-live-changes-list] li');
    check('change list entry', (await list.count()) >= 1 && (await list.first().textContent()).includes('OK → failed'), await list.first().textContent());
    await step(2500);
    check('screen-reader announcement', (await page.locator('[data-live-announce]').textContent()).includes('nseindia.com DNS & SSL is now failed'));
    await shot('1-flipped');
    dj("from domainmonitor.models import Domain; from django.utils import timezone; Domain.objects.filter(hostname='nseindia.com').update(ssl_status='Valid', last_checked_at=timezone.now())");
    for (let i = 0; i < 12; i++) { await step(4000); if ((await list.count()) >= 2) break; }
    check('recovery listed as recovered', (await list.first().textContent()).includes('recovered'), await list.first().textContent());

    // fields and pause
    await page.focus('input[name=q]');
    let r0 = liveReqs; await step(70000);
    check('no polling while a field has focus', liveReqs === r0);
    await page.evaluate(() => document.activeElement.blur());
    await step(8000); await page.waitForTimeout(300);
    check('polling resumes after blur', liveReqs > r0);
    await page.click('[data-live-toggle]');
    r0 = liveReqs; await step(90000);
    check('Pause stops polling', liveReqs === r0);
    await page.click('[data-live-toggle]'); await page.waitForTimeout(500);
    check('Resume polls again', liveReqs > r0);

    // outage: back-off and the stale banner
    await page.route('**/bod-eod/api/live/**', (r) => r.abort());
    for (let i = 0; i < 8; i++) await step(30000);
    check('stale banner when data is older than 3 intervals', await page.locator('[data-live-stale]').isVisible(), await page.locator('[data-live-stale-text]').textContent());
    await shot('2-stale');
    const rb = liveReqs; for (let i = 0; i < 4; i++) await step(30000);
    check('failing polls back off', liveReqs - rb <= 4, `${liveReqs - rb} requests in 120 s`);
    await page.unroute('**/bod-eod/api/live/**');
    for (let i = 0; i < 6; i++) { await step(30000); await page.waitForTimeout(200); if (!(await page.locator('[data-live-stale]').isVisible())) break; }
    check('stale banner clears after recovery', !(await page.locator('[data-live-stale]').isVisible()));

    // re-check through probes.checks against the stub
    const portal = page.locator('[data-live-row]', { hasText: 'Portal reachable' });
    const msg = () => page.locator('[data-live-msg]').textContent();
    stubMode = 'fail';
    await portal.locator('[data-recheck]').click();
    await page.waitForFunction(() => /re-checked|still running/.test(document.querySelector('[data-live-msg]').textContent), null, { timeout: 15000, polling: 200 }).catch(() => {});
    check('re-check turned the URL check CRIT', (await portal.locator('[data-live="pill"]').textContent()).includes('CRIT'), `${await portal.locator('[data-live="detail"]').textContent()} | ${await msg()}`);
    await shot('3-recheck');
    stubMode = 'slow';
    await portal.locator('[data-recheck]').click();
    await page.waitForFunction(() => /still running/.test(document.querySelector('[data-live-msg]').textContent), null, { timeout: 15000, polling: 200 }).catch(() => {});
    check('slow probe answers "still running"', (await msg()).includes('still running'), await msg());
    check('button busy while running', (await portal.locator('[data-recheck]').getAttribute('aria-busy')) === 'true');
    await shot('4-running');
    stubMode = 'ok';
    for (let i = 0; i < 14; i++) { await step(4000); await page.waitForTimeout(1200); if ((await portal.locator('[data-recheck]').getAttribute('aria-busy')) !== 'true') break; }
    check('background re-check finished and released the button', (await portal.locator('[data-recheck]').getAttribute('aria-busy')) !== 'true');
    check('no page errors', errors.length === 0, errors.join(' | '));
    await browser.close();
  } finally {
    await stopAll();
  }
  results.forEach((r) => console.log(r.join(' | ')));
  process.exit(results.some((r) => r[0] === 'FAIL') ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
