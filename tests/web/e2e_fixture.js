// Browser checks of web/ against the fixtures and mocked API answers (incl. the 409 refusals).
// Usage: PLAYWRIGHT_MODULE=<path to node_modules/playwright> node tests/web/e2e_fixture.js [--shots web/shots]
// Starts its own static server for web/ on 127.0.0.1 (random port); exits 1 if any check fails.
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const WEB = path.resolve(__dirname, '../../web');
const shotsArg = process.argv.indexOf('--shots');
const SHOTS = shotsArg > 0 ? path.resolve(process.argv[shotsArg + 1]) : null;
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.png': 'image/png' };

function serve() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const file = path.normalize(path.join(WEB, decodeURIComponent(url.pathname)));
    if (!file.startsWith(WEB) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise(ok => server.listen(0, '127.0.0.1', () => ok(server)));
}

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log('PASS ' + name); }
  else { failed++; console.log('FAIL ' + name + (detail !== undefined ? ' :: ' + JSON.stringify(detail) : '')); }
}
const text = (page, sel) => page.$eval(sel, e => e.textContent.replace(/\s+/g, ' ').trim());
const texts = (page, sel) => page.$$eval(sel, es => es.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
const visible = (page, sel) => page.$eval(sel, e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden');

async function newPage(browser, width, height) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
  page._errors = [];
  page.on('console', m => { if (m.type() === 'error') page._errors.push(m.text()); });
  page.on('pageerror', e => page._errors.push(String(e)));
  return page;
}
async function ready(page) {
  await page.waitForFunction(() => document.body.dataset.state && document.body.dataset.state !== 'loading', null, { timeout: 10000 });
  await page.evaluate(() => document.fonts.ready);
}
const geometry = page => page.evaluate(() => ['.band', '.grid', '.covered', '.planb', '#money', '.foot'].map(sel => { const r = document.querySelector(sel).getBoundingClientRect(); return sel + ':' + Math.round(r.top) + '+' + Math.round(r.height); }).join(' '));
async function shot(page, name) { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name) }); }

(async () => {
  const server = await serve();
  const base = 'http://127.0.0.1:' + server.address().port + '/';
  const browser = await chromium.launch();
  const today = JSON.parse(fs.readFileSync(path.join(WEB, 'fixtures/today.json'), 'utf8'));
  const offerFx = JSON.parse(fs.readFileSync(path.join(WEB, 'fixtures/offers/O-1.json'), 'utf8'));
  try {
    /* ---------- screen C, fixture: first look ---------- */
    let p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    check('C: state ready', await p.evaluate(() => document.body.dataset.state) === 'ready');
    check('C: title', await text(p, 'h1') === 'Who is allowed on deck at 06:00');
    const nav = await texts(p, '.nav li');
    check('C: menu', JSON.stringify(nav) === JSON.stringify(['Today', 'Staff & Certificates', 'Cases', 'Groups', 'Closures', 'Reports', 'Schedule', 'Settings']), nav);
    check('C: Schedule not first', nav.indexOf('Schedule') > 0);
    const dots = await p.$$eval('.dot', ds => ds.map(d => ({ cls: d.className, title: d.title })));
    check('C: 40 dots', dots.length === 40, dots.length);
    check('C: 6 lit', dots.filter(d => /\b(on|gold)\b/.test(d.cls)).length === 6);
    const gold = dots.filter(d => /\bgold\b/.test(d.cls));
    check('C: one gold = Maria Lopez', gold.length === 1 && gold[0].title.startsWith('Maria Lopez'), gold);
    const anna = dots.find(d => d.title.startsWith('Anna Kim'));
    check('C: Anna Kim dimmed in the grid', anna && !/\b(on|gold)\b/.test(anna.cls), anna);
    const counts = await p.$$eval('[data-slot="reason-count"]', es => es.map(e => +e.textContent.replace('×', '')));
    check('C: reason counts add up to 34', counts.reduce((a, b) => a + b, 0) === 34, counts);
    check('C: refusal line', await text(p, '#refusal') === 'Anna Kim · free and willing · BLOCKED: Lifeguard certificate expired Nov 5 · 8 kids · ratio 1:4', await text(p, '#refusal'));
    check('C: RosterElf quote', (await text(p, '#refusalQuote')).includes('RosterElf help center'));
    check('C: timeline times', JSON.stringify(await texts(p, '.tl-time')) === JSON.stringify(['05:12', '05:13', '05:15', '05:19', '06:00']), await texts(p, '.tl-time'));
    check('C: timeline labels', JSON.stringify(await texts(p, '.tl-chip')) === JSON.stringify(['Call-out', 'Sofia Reyes declined', 'Leo Martins no reply in 5 min', 'Maria Lopez accepted']), await texts(p, '.tl-chip'));
    const chipBoxes = await p.$$eval('.tl-chip', es => es.map(e => { const r = e.getBoundingClientRect(); return [r.left, r.right]; }));
    check('C: timeline chips do not overlap', chipBoxes.every((b, i) => i === 0 || chipBoxes[i - 1][1] + 8 < b[0]), chipBoxes);
    check('C: offer rows', JSON.stringify(await texts(p, '.off-row')) === JSON.stringify(['05:13 Sofia Reyes — declined', '05:15 Leo Martins — no reply in 5 min', '05:19 Maria Lopez — accepted']), await texts(p, '.off-row'));
    check('C: why first at the accepted offer', await visible(p, '#whyFirst') && await text(p, '[data-slot="why-first"]') === 'Maria first: 12 h this week, Level 3, same pool');
    check('C: voice note original (RU)', /[А-Яа-я]/.test(await text(p, '[data-slot="callout-original"]')));
    check('C: voice note translation (EN)', (await text(p, '[data-slot="callout-transcript"]')).includes("I can't come at 6"));
    check('C: Marina Petrova from data', await text(p, '[data-slot="callout-staff"]') === 'Marina Petrova · Lifeguard + WSI');
    check('C: both transcripts fit the card uncut', await p.evaluate(() => {
      const card = document.querySelector('.callout').getBoundingClientRect(), tx = document.querySelector('.tx').getBoundingClientRect();
      return tx.bottom <= card.bottom - 8 && [...document.querySelectorAll('.tx-t')].every(e => e.scrollHeight <= e.clientHeight + 1);
    }));
    check('C: whisper line', await text(p, '[data-slot="callout-asr"]') === 'whisper on Vultr · 1.0 s');
    check('C: tagline', await text(p, '.tagline') === "Not who's free — who's allowed. Running on Vultr.");
    check('C: covered banner', await text(p, '[data-slot="covered"]') === '06:00 covered by Maria Lopez · 0 lessons cancelled');
    check('C: money shows only the case cost', await visible(p, '#money') && await text(p, '[data-slot="money-case"]') === '0.3¢' &&
      !(await visible(p, '#moneyHour')) && !(await visible(p, '#moneyUncovered')));
    check('C: plan B standby, button disabled', await text(p, '#planbHead') === 'Plan B ready if no one accepts' && await p.$eval('#approve', b => b.disabled));
    check('C: Cases link goes to the case', (await p.$eval('#navCases', a => a.getAttribute('href'))) === 'case.html?id=C-1109-01&fixture=1');
    check('C: no console errors', p._errors.length === 0, p._errors);
    const geo0 = await geometry(p);
    check('C: blocks where the layout puts them', geo0 === '.band:144+182 .grid:342+500 .covered:861+67 .planb:940+76 #money:861+155 .foot:1044+22', geo0);

    /* judge mode: nobody accepts → approve (200) */
    await p.click('[data-scenario="nobody_accepts"]');
    await p.waitForFunction(() => /C-1109-02/.test(document.getElementById('judgeStatus').textContent));
    await p.waitForFunction(() => !document.getElementById('approve').disabled);
    check('C judge: nobody accepts → case id shown', (await text(p, '#judgeStatus')).includes('case C-1109-02'));
    check('C judge: banner not covered', (await text(p, '[data-slot="covered"]')).startsWith('06:00 not covered yet'));
    check('C judge: timeline has plan B', (await texts(p, '.tl-chip')).includes('Plan B proposed'));
    check('C judge: why first hidden when nobody accepted', !(await visible(p, '#whyFirst')));
    check('C judge: plan B needs approval', await text(p, '#planbHead') === 'Plan B needs your approval');
    check('C judge: layout does not move between states', await geometry(p) === geo0, await geometry(p));
    await shot(p, 'C-fixture-nobody-accepts.png');
    await p.click('#approve');
    await p.waitForFunction(() => document.getElementById('planbHead').textContent === 'Plan B approved');
    check('C approve: 200 → approved', await text(p, '#approve') === 'Approved' && await p.$eval('#approve', b => b.disabled));
    await p.click('[data-scenario="nightly_check"]');
    await p.waitForFunction(() => /C-1109-N1/.test(document.getElementById('judgeStatus').textContent));
    check('C judge: night check → case id shown', (await text(p, '#judgeStatus')).includes('“Night check” done · case C-1109-N1'));
    await p.click('[data-scenario="dangerous_morning"]');
    await p.waitForFunction(() => /C-1109-01/.test(document.getElementById('judgeStatus').textContent));
    await p.waitForFunction(() => document.querySelector('[data-slot="covered"]').textContent.includes('covered by Maria Lopez'));
    check('C judge: 5 a.m. call-out → back to the covered morning', true);
    check('C judge: no console errors', p._errors.length === 0, p._errors);
    await p.close();

    /* plan changed → 409 line */
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1&mock=plan_changed');
    await ready(p);
    await p.click('[data-scenario="nobody_accepts"]');
    await p.waitForFunction(() => !document.getElementById('approve').disabled);
    await p.click('#approve');
    await p.waitForFunction(() => document.getElementById('planb').classList.contains('conflict'));
    check('C approve: 409 → human line', await text(p, '#planbHead') === 'Plan changed since you saw it — not approved', await text(p, '#planbHead'));
    check('C approve 409: layout does not move', await geometry(p) === geo0, await geometry(p));
    await shot(p, 'C-fixture-plan-changed-409.png');
    await p.close();

    /* live wiring: /api/* calls, hash in the approve body, 409 from the server */
    p = await newPage(browser, 1920, 1080);
    const nobody = JSON.parse(fs.readFileSync(path.join(WEB, 'fixtures/today.nobody_accepts.json'), 'utf8'));
    let approveBody = null, todayCalls = 0, judgeBody = null;
    await p.route('**/api/today*', r => { todayCalls++; r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(nobody) }); });
    await p.route('**/api/plans/*/approve', r => { approveBody = r.request().postDataJSON(); r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ reason: 'plan changed', current_hash: 'x' }) }); });
    await p.route('**/api/judge/run', r => { judgeBody = r.request().postDataJSON(); r.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ detail: 'too many runs' }) }); });
    await p.goto(base + 'index.html');
    await ready(p);
    check('live: GET /api/today used', todayCalls >= 1);
    await p.click('#approve');
    await p.waitForFunction(() => document.getElementById('planb').classList.contains('conflict'));
    check('live: approve sends the plan hash', approveBody && approveBody.hash === nobody.plan_b.hash, approveBody);
    check('live: 409 → human line', await text(p, '#planbHead') === 'Plan changed since you saw it — not approved');
    await p.click('[data-scenario="dangerous_morning"]');
    await p.waitForFunction(() => document.getElementById('judgeStatus').classList.contains('err'));
    check('live: judge sends the scenario', judgeBody && judgeBody.scenario === 'dangerous_morning', judgeBody);
    check('live: judge 429 shown, not swallowed', (await text(p, '#judgeStatus')).includes('429'), await text(p, '#judgeStatus'));
    await p.close();

    /* money block layout with all three values (test-only numbers, never in fixtures) */
    p = await newPage(browser, 1920, 1080);
    const full = JSON.parse(JSON.stringify(today));
    full.money = { uncovered_morning_usd: 111, uncovered_morning_label: null, case_cost_cents: 0.3, manager_hour_usd: 22 };
    await p.route('**/fixtures/today.json', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(full) }));
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    check('money: three lines when data has them', await visible(p, '#moneyHour') && await visible(p, '#moneyUncovered') &&
      await text(p, '[data-slot="money-manager-hour"]') === '$22' && await text(p, '[data-slot="money-uncovered"]') === '$111');
    check('money: block stays inside its card', await p.$eval('#money', m => m.scrollHeight <= m.clientHeight + 1 && m.scrollWidth <= m.clientWidth + 1));
    const empty = JSON.parse(JSON.stringify(today));
    empty.money = { uncovered_morning_usd: 0, uncovered_morning_label: '…', case_cost_cents: null, manager_hour_usd: 0 };
    await p.unroute('**/fixtures/today.json');
    await p.route('**/fixtures/today.json', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(empty) }));
    await p.reload();
    await ready(p);
    check('money: hidden when there is no data (0, null, "…")', !(await visible(p, '#money')));
    await p.close();

    /* load failure is shown */
    p = await newPage(browser, 1920, 1080);
    await p.route('**/api/today*', r => r.fulfill({ status: 500, body: 'boom' }));
    await p.goto(base + 'index.html');
    await p.waitForFunction(() => document.body.dataset.state === 'error');
    check('C: load error shown', (await text(p, '#loadErr')).includes('HTTP 500'));
    await p.close();

    /* ---------- screen P2 ---------- */
    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=O-1&fixture=1');
    await ready(p);
    const why = await texts(p, '[data-slot="why-text"]');
    check('P2: why you from data', JSON.stringify(why) === JSON.stringify(offerFx.why), why);
    const hoursC = (today.offers.find(o => o.result === 'accepted').why_first.match(/(\d+(?:\.\d+)?) h this week/) || [])[1];
    const hoursP = (why.join(' ').match(/(\d+(?:\.\d+)?) h this week/) || [])[1];
    check('P2: hours equal to "why first" on screen C', hoursC && hoursC === hoursP, [hoursC, hoursP]);
    check('P2: ask line', await text(p, '.ask') === 'Can you cover 06:00–08:00 today?');
    check('P2: timer 4:12', await text(p, '[data-slot="expires-in"]') === '4:12');
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 accept: 200 → accepted', await text(p, '#result') === 'Accepted. You cover 06:00–08:00 today.', await text(p, '#result'));
    await shot(p, 'P2-fixture-accepted.png');
    check('P2: no console errors', p._errors.length === 0, p._errors);
    await p.close();

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=O-1&fixture=1&mock=not_eligible');
    await ready(p);
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 accept: 409 → no longer eligible + reason', await text(p, '#result') === 'You are no longer eligible for this shift: already booked 06:00–08:00', await text(p, '#result'));
    await shot(p, 'P2-fixture-not-eligible-409.png');
    await p.close();

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=O-1&fixture=1');
    await ready(p);
    await p.click('[data-action="decline"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 decline: 200 → declined', await text(p, '#result') === 'Declined. Thank you for answering.');
    await p.close();

    /* P2 live wiring: 409 reason from the server, then a 500 that keeps the buttons */
    p = await newPage(browser, 430, 1080);
    let acceptCalls = 0;
    await p.route('**/api/offers/O-1', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(offerFx) }));
    await p.route('**/api/offers/O-1/accept', r => { acceptCalls++; r.fulfill({ status: 500, body: '' }); });
    await p.goto(base + 'offer.html?id=O-1');
    await ready(p);
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#loadErr:not([hidden])');
    check('P2 live: 500 shown and buttons stay', acceptCalls === 1 && (await text(p, '#loadErr')).includes('HTTP 500') && !(await p.$eval('[data-action="accept"]', b => b.disabled)));
    await p.unroute('**/api/offers/O-1/accept');
    await p.route('**/api/offers/O-1/accept', r => r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ reason: 'Lifeguard certificate expired Nov 5' }) }));
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 live: 409 reason from the server', await text(p, '#result') === 'You are no longer eligible for this shift: Lifeguard certificate expired Nov 5');
    await p.close();

    /* case stub */
    p = await newPage(browser, 1280, 800);
    await p.goto(base + 'case.html?id=C-1109-01&fixture=1');
    await p.waitForSelector('#err:not([hidden])');
    check('case: stub says what is missing', (await text(p, '#err')).includes('no case fixture yet'));
    await p.close();
  } finally {
    await browser.close();
    server.close();
  }
  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
