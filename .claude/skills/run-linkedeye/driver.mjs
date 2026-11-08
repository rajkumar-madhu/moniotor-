// Drive signed-in LinkedEye pages in a real headless Chrome over the DevTools protocol (Node 22+: built-in
// WebSocket, no npm install). Steps run in order:
//
//   node .claude/skills/run-linkedeye/driver.mjs goto /network/devices/1/ shot sw wait-poll live
//
//   goto <path|url>            load a page and wait for the load event
//   click <css>                click an element (no navigation wait)
//   click-nav <css>            click and wait for the next page load
//   wait <seconds>             sleep
//   wait-poll [timeout_s]      wait for the next network:live JSON response (default 45 s)
//   wait-for <js> [timeout_s]  poll every 2 s until the expression is truthy (default 240 s)
//   eval <js>                  print the expression's value as JSON
//   live                       print the device page's live state (values, tones, badge, notice, charts)
//   shot <name>                screenshot -> $LE_RUN_DIR/shots/<name>.png
//   size <w>x<h>               viewport (default 1440x1700)
//
// Signs in with the session cookie that `lab.sh login` writes. At exit prints every live poll seen and every JS
// exception, console error and failed same-origin request; exit code 1 if there were any, or a step failed.
// Env: LE_RUN_DIR (shared with lab.sh), LE_BASE (default http://127.0.0.1:8001), CHROME (browser binary).
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const RUN = (process.env.LE_RUN_DIR || join(process.env.TMPDIR || tmpdir(), 'linkedeye-run')).replace(/\/$/, '');
const BASE = process.env.LE_BASE || `http://127.0.0.1:${process.env.LE_WEB_PORT || 8001}`;
const SHOTS = join(RUN, 'shots');
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = ms => new Promise(r => setTimeout(r, ms));

function chromeBinary() {
  if (process.env.CHROME) return process.env.CHROME;
  const mac = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  if (existsSync(mac)) return mac;
  for (const name of ['google-chrome', 'chromium', 'chromium-browser']) {
    try { return execFileSync('which', [name]).toString().trim(); } catch { /* try the next one */ }
  }
  throw new Error('Chrome not found: set CHROME=/path/to/chrome');
}

const steps = process.argv.slice(2);
if (!steps.length) { console.log(readFileSync(new URL(import.meta.url)).toString().split('\n').slice(0, 20).join('\n')); process.exit(2); }
mkdirSync(SHOTS, { recursive: true });

// Separate profile per run: the user's own Chrome can stay open. Port 0 + DevToolsActivePort avoids port clashes.
const profile = join(RUN, `chrome-profile-${process.pid}`);
const chrome = spawn(chromeBinary(), ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, '--window-size=1440,1700', 'about:blank'], { stdio: 'ignore' });
// Chrome keeps writing to its profile for a moment after kill(): wait for it to exit before deleting the profile.
const chromeExited = new Promise(r => chrome.once('exit', r));
async function finish(code) {
  chrome.kill();
  await Promise.race([chromeExited, sleep(5000)]);
  try { rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch { /* leave it in $LE_RUN_DIR */ }
  process.exit(code);
}
process.on('exit', () => { try { chrome.kill(); } catch { /* already gone */ } });

let port;
for (let i = 0; i < 100 && !port; i++) {
  try { port = readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]; } catch { await sleep(100); }
}
let page;
for (let i = 0; i < 50 && !page; i++) {
  try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page'); } catch { await sleep(100); }
}
if (!page) { console.error('could not reach Chrome DevTools'); await finish(1); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let seq = 0;
const pending = new Map(), listeners = new Set();
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id) { pending.get(m.id)?.(m); pending.delete(m.id); } else listeners.forEach(f => f(m));
});
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq;
  pending.set(id, m => (m.error ? rej(new Error(`${method}: ${m.error.message}`)) : res(m.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expr => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error('eval: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
const nextEvent = name => new Promise(r => { const f = m => { if (m.method === name) { listeners.delete(f); r(m); } }; listeners.add(f); });

const polls = [], problems = [];
listeners.add(m => {
  const p = m.params || {};
  if (m.method === 'Network.responseReceived') {
    const { url, status } = p.response;
    if (url.includes('/live/')) polls.push({ at: ((Date.now() - t0) / 1000).toFixed(1), url: url.replace(BASE, ''), status });
    if (url.startsWith(BASE) && status >= 400) problems.push(`HTTP ${status} ${url.replace(BASE, '')}`);
  }
  if (m.method === 'Runtime.exceptionThrown') problems.push('JS exception: ' + (p.exceptionDetails.exception?.description || p.exceptionDetails.text));
  if (m.method === 'Runtime.consoleAPICalled' && p.type === 'error') problems.push('console.error: ' + p.args.map(a => a.value ?? a.description).join(' '));
});
await send('Network.enable');
await send('Runtime.enable');
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1700, deviceScaleFactor: 1, mobile: false });
await send('Page.bringToFront');   // live.js pauses while document.hidden

const jar = join(RUN, 'cookies.txt');
if (existsSync(jar)) {
  for (const line of readFileSync(jar, 'utf8').split('\n')) {
    const f = line.replace(/^#HttpOnly_/, '').split('\t');
    if (f.length === 7) await send('Network.setCookie', { name: f[5], value: f[6].trim(), domain: f[0], path: f[2] });
  }
} else {
  log(`no ${jar}: pages will redirect to sign-in (run lab.sh login)`);
}

const LIVE = `(() => {
  const q = s => document.querySelector(s), t = s => { const e = q(s); return e ? e.textContent.trim() : undefined; };
  const root = q('#nw-live'), n = q('#nw-live-notice');
  if (!root) return { error: 'no #nw-live on ' + location.pathname };
  const values = {}, tones = {};
  document.querySelectorAll('[data-live]').forEach(e => { values[e.dataset.live] ??= e.textContent.trim(); });
  document.querySelectorAll('[data-live-tone]').forEach(e => { tones[e.dataset.liveTone] ??= [...e.classList].find(c => c.startsWith('t-')) || ''; });
  return { page: location.pathname + location.search, indicator: t('#nw-live .nw-live-text'), state: root.dataset.state,
    pollUrl: root.dataset.url, badge: t('.nw-live-badge .badge'), notice: n && !n.hidden ? t('.nw-live-notice-text') : null,
    values, tones, charts: document.querySelectorAll('.chart svg').length, portsDown: document.querySelectorAll('.fp-port.down').length };
})()`;

async function load(promise) { const loaded = nextEvent('Page.loadEventFired'); await promise; await loaded; await sleep(500); }

let failed = false;
try {
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    const arg = () => { if (i + 1 >= steps.length) throw new Error(`${step} needs an argument`); return steps[++i]; };
    const optNum = dflt => (i + 1 < steps.length && /^\d+(\.\d+)?$/.test(steps[i + 1]) ? Number(steps[++i]) : dflt);
    switch (step) {
      case 'goto': { const u = arg(); await load(send('Page.navigate', { url: u.startsWith('http') ? u : BASE + u }));
        log('goto', await evaluate('location.pathname + location.search'), '·', await evaluate('document.title')); break; }
      case 'click': { const s = arg(); await evaluate(`document.querySelector(${JSON.stringify(s)}).click()`); log('click', s); break; }
      case 'click-nav': { const s = arg(); await load(evaluate(`document.querySelector(${JSON.stringify(s)}).click()`));
        log('click-nav', s, '->', await evaluate('location.pathname + location.search')); break; }
      case 'wait': { const s = Number(arg()); await sleep(s * 1000); log('waited', s, 's'); break; }
      case 'wait-poll': { const timeout = optNum(45), n = polls.length, start = Date.now();
        while (polls.length === n && Date.now() - start < timeout * 1000) await sleep(250);
        if (polls.length === n) throw new Error(`no live poll within ${timeout}s`);
        await sleep(500); log('live poll', polls.at(-1)); break; }
      case 'wait-for': { const expr = arg(), timeout = optNum(240), start = Date.now();
        while (!(await evaluate(expr)) && Date.now() - start < timeout * 1000) await sleep(2000);
        if (!(await evaluate(expr))) throw new Error(`wait-for timed out after ${timeout}s: ${expr}`);
        log('wait-for ok after', ((Date.now() - start) / 1000).toFixed(0), 's:', expr); break; }
      case 'eval': { const expr = arg(); log('eval', expr, '=>', JSON.stringify(await evaluate(expr))); break; }
      case 'live': log('live', JSON.stringify(await evaluate(LIVE), null, 1)); break;
      case 'shot': { const name = arg(), { data } = await send('Page.captureScreenshot', { format: 'png' });
        const file = join(SHOTS, `${name}.png`); writeFileSync(file, Buffer.from(data, 'base64')); log('shot', file); break; }
      case 'size': { const [w, h] = arg().split('x').map(Number);
        await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: false }); log('size', w, h); break; }
      default: throw new Error(`unknown step "${step}"`);
    }
  }
} catch (e) {
  failed = true;
  console.error('STEP FAILED:', e.message);
}
log(`live polls: ${polls.length}`, polls.length ? JSON.stringify(polls.map(p => `${p.at}s ${p.status} ${p.url}`)) : '');
log('problems:', problems.length ? problems : 'none');
await finish(failed || problems.length ? 1 : 0);
