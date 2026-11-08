// Browser checks of web/ against the fixtures and mocked API answers (incl. the 409 refusals).
// Usage: PLAYWRIGHT_MODULE=<path to node_modules/playwright> node tests/web/e2e_fixture.js [--shots web/shots]
// Starts its own static server for web/ on 127.0.0.1 (random port); exits 1 if any check fails.
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const WEB = path.resolve(__dirname, '../../web');
// the sample certificate panel: its test harness page and the bundled synthetic cards the API serves
const ROUTES = [['/harness/', path.resolve(__dirname, 'harness')], ['/media/sample-cards/', path.resolve(__dirname, '../../coachcover/samples/cards')]];
const SC_SHOTS = process.env.SC_SHOTS ? path.resolve(process.env.SC_SHOTS) : null;
const shotsArg = process.argv.indexOf('--shots');
const SHOTS = shotsArg > 0 ? path.resolve(process.argv[shotsArg + 1]) : null;
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.png': 'image/png', '.jpg': 'image/jpeg', '.pdf': 'application/pdf' };

function serve() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const route = ROUTES.find(([prefix]) => url.pathname.startsWith(prefix));
    const root = route ? route[1] : WEB;
    const rel = route ? url.pathname.slice(route[0].length) : url.pathname;
    const file = path.normalize(path.join(root, decodeURIComponent(rel)));
    if (!file.startsWith(root) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end(); return; }
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
  const fx = name => JSON.parse(fs.readFileSync(path.join(WEB, 'fixtures', name), 'utf8'));
  const today = fx('today.json'), nobody = fx('today.nobody_accepts.json'), night = fx('today.nightly_check.json');
  const ratioOpen = fx('today.ratio_open.json'), ratioCovered = fx('today.ratio_covered.json'), mocks = fx('mocks.json');
  const caseId = today.callout.case_id, caseFx = fx('cases/' + caseId + '.json');
  const accepted = today.offers.find(o => o.result === 'accepted');
  const acceptedFx = fx('offers/' + accepted.id + '.json');
  const pendingId = fs.readdirSync(path.join(WEB, 'fixtures/offers')).map(f => f.replace('.json', ''))
    .find(id => fx('offers/' + id + '.json').state === 'pending');
  const pendingFx = fx('offers/' + pendingId + '.json');
  const hoursOf = s => ((s || '').match(/((?:\d+ h)?(?: ?\d+ min)?) this week/) || [])[1];  // "5 h 45 min", "6 h"
  const when = o => o.shift_start + '–' + o.shift_end + ' ' + o.shift_day;
  const lessonSource = String(caseFx.risk.assumptions.lesson_price.source || '').replace(/\s*\(TG \d+\)\s*$/, '');
  try {
    /* ---------- screen C, fixture: the morning as the API answers it ---------- */
    let p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    check('C: state ready', await p.evaluate(() => document.body.dataset.state) === 'ready');
    check('C: title from data', await text(p, 'h1') === today.title);
    const nav = await texts(p, '.nav li');
    check('C: menu', JSON.stringify(nav) === JSON.stringify(['Today', 'Week ahead', 'Staff & Certificates', 'Cases', 'Network', 'Reports']), nav);  // the four items without a page were removed
    const e = today.eligibility;
    const dots = await p.$$eval('.dot', ds => ds.map(d => ({ cls: d.className, title: d.title })));
    check('C: one dot per person', dots.length === e.staff.length && e.staff.length === e.total, dots.length);
    check('C: lit dots = eligible', dots.filter(d => /\b(on|gold)\b/.test(d.cls)).length === e.eligible);
    const gold = dots.filter(d => /\bgold\b/.test(d.cls));
    check('C: gold dot = the person who took it', gold.length === 1 && gold[0].title.startsWith(accepted.staff), gold);
    const anna = dots.find(d => d.title.startsWith(e.first_refusal.staff));
    check('C: the first refusal is dimmed in the grid', anna && !/\b(on|gold)\b/.test(anna.cls), anna);
    const counts = await p.$$eval('[data-slot="reason-count"]', es => es.map(x => +x.textContent.replace('×', '')));
    check('C: chip counts add up to the filtered people', counts.reduce((a, b) => a + b, 0) === e.total - e.eligible, counts);
    const chipText = (await texts(p, '[data-slot="reason-chip"]')).join(' | ');
    for (const code of ['CERT_REVOKED', 'REST_TOO_SHORT']) {
      const r = e.reasons.find(x => x.code === code);
      // the 09:30 morning has nobody short of rest: that chip is checked only when the data has it
      const needed = code === 'CERT_REVOKED';
      check('C: chip from data ' + code + (needed ? '' : ' (when the morning has it)'), r ? chipText.includes(r.label) : !needed, r && r.label);
    }
    check('C: revoked chip says simulated registry', /REVOKED by issuer .*\(simulated registry\)/.test(chipText), chipText);
    check('C: refusal line from data', await text(p, '#refusal') === e.first_refusal.staff + ' · ' + e.first_refusal.label, await text(p, '#refusal'));
    check('C: RosterElf quote from data', await text(p, '#refusalQuote') === e.first_refusal.contrast_quote);
    const inCard = await p.evaluate(() => { const c = document.querySelector('.staff').getBoundingClientRect();
      return ['#chips', '#refusal', '#refusalQuote'].every(s => { const r = document.querySelector(s).getBoundingClientRect(); return r.bottom <= c.bottom - 2 && r.right <= c.right; }); });
    check('C: chips, refusal and quote stay inside the card', inCard);
    check('C: timeline times', JSON.stringify(await texts(p, '.tl-time')) === JSON.stringify(today.timeline.map(t => t.time_label)), await texts(p, '.tl-time'));
    const chipBoxes = await p.$$eval('.tl-chip', es => es.map(x => { const r = x.getBoundingClientRect(); return [r.left, r.right]; }));
    check('C: timeline chips do not overlap', chipBoxes.every((b, i) => i === 0 || chipBoxes[i - 1][1] + 8 < b[0]), chipBoxes);
    const rows = await texts(p, '.off-row');
    check('C: offer rows from data', rows.length === Math.min(3, today.offers.length) &&
      today.offers.slice(-3).slice().sort((x, y) => x.time_label.localeCompare(y.time_label))
        .every((o, i) => rows[i].startsWith(o.time_label + ' ' + o.staff + ' — ')), rows);  // a log in time order
    check('C: why line under the accepted offer', await visible(p, '#whyFirst') && await text(p, '[data-slot="why-first"]') === accepted.why_first);
    const whyClear = await p.evaluate(() => { const w = document.getElementById('whyFirst').getBoundingClientRect();
      return [...document.querySelectorAll('.off-row')].every(r => { const b = r.getBoundingClientRect(); return w.top >= b.bottom - 3 || w.bottom <= b.top + 3; }); });
    check('C: why line overlaps no offer row', whyClear);
    check('C: why label names the person', await text(p, '[data-slot="why-name"]') === 'why ' + accepted.staff.split(' ')[0] &&
      await p.$eval('[data-slot="why-name"]', x => x.innerText) === 'WHY ' + accepted.staff.split(' ')[0].toUpperCase());
    check('C: voice note original + translation from data', await text(p, '[data-slot="callout-original"]') === '“' + today.callout.transcript_original + '”' &&
      await text(p, '[data-slot="callout-transcript"]') === '“' + today.callout.transcript_en + '”');
    check('C: whisper line from data', await text(p, '[data-slot="callout-asr"]') === today.callout.asr_label);
    check('C: footer line says replay in ?fixture=1 (answers captured in replay)', await text(p, '.tagline') === 'CoachCover · Replay mode.');
    const folderC = await p.$eval('#folder', a => ({ text: a.textContent.trim(), href: a.href, target: a.target, r: a.getBoundingClientRect().toJSON() }));
    const pdfC = await p.evaluate(async href => { const r = await fetch(href); const b = new Uint8Array(await r.arrayBuffer()); return { status: r.status, type: r.headers.get('content-type'), head: String.fromCharCode(...b.slice(0, 5)) }; }, folderC.href);
    check('C: inspector folder button opens a 200 PDF', folderC.text === "Inspector's folder (PDF)" && folderC.target === '_blank' &&
      pdfC.status === 200 && /application\/pdf/.test(pdfC.type) && pdfC.head === '%PDF-', [folderC, pdfC]);
    check('C: folder button under the side cards, clear of them and of the money card', await p.evaluate(() => {
      const f = document.getElementById('folder').getBoundingClientRect(), cards = [...document.querySelectorAll('[data-slot="summary-card"]')].map(c => c.getBoundingClientRect());
      const m = document.getElementById('money'), mr = m && !m.hidden ? m.getBoundingClientRect() : null;
      return cards.every(c => c.bottom <= f.top) && (!mr || f.bottom <= mr.top) && f.right <= 1920;
    }));
    const heroStart = today.shift.time_label.split('–')[0];
    check('C: covered banner names the shift start', await text(p, '[data-slot="covered"]') === heroStart + ' covered by ' + today.outcome.covered_by + ' · ' + today.outcome.lessons_cancelled + ' lessons cancelled' +
      (today.outcome.covered_time !== heroStart ? 'closed ' + today.outcome.covered_time : ''), await text(p, '[data-slot="covered"]'));
    const label = today.money.uncovered_morning_label.match(/^(.*?):\s*(\$[\d,.]+)\s*\((.*)\)$/);
    check('C: money from data', await text(p, '[data-slot="money-head"]') === label[1] && await text(p, '[data-slot="money-big"]') === label[2] &&
      await text(p, '[data-slot="money-detail"]') === label[3], [await text(p, '#money'), label]);
    check('C: money source caption', lessonSource && await text(p, '[data-slot="money-source"]') === lessonSource, await text(p, '[data-slot="money-source"]'));
    check('C: case cost line', await visible(p, '#moneyCase') && (await text(p, '#moneyCase')).includes('¢'));
    check('C: plan B standby, button disabled', await text(p, '#planbHead') === 'Plan B ready if no one accepts' && await p.$eval('#approve', b => b.disabled));
    check('C: plan B text fits two lines', await p.evaluate(() => { const t = document.querySelector('[data-slot="planb-text"]');
      return t.scrollHeight - parseFloat(getComputedStyle(t).paddingTop) <= parseFloat(getComputedStyle(t).lineHeight) * 2 + 2; }));
    check('C: no ratio banner without a ratio case', !(await visible(p, '#alert')));
    check('C: Cases link goes to the case', (await p.$eval('#navCases', a => a.getAttribute('href'))) === 'case.html?id=' + caseId + '&fixture=1');
    check('C: no console errors', p._errors.length === 0, p._errors);
    const geo0 = await geometry(p);
    check('C: blocks where the layout puts them', geo0 === '.band:144+182 .grid:342+500 .covered:861+67 .planb:940+76 #money:861+155 .foot:1044+22', geo0);

    /* judge mode: nobody accepts → approve (200), night check, back to the morning */
    await p.click('[data-scenario="nobody_accepts"]');
    await p.waitForFunction(() => /Nobody accepts” done/.test(document.getElementById('judgeStatus').textContent));
    await p.waitForFunction(() => !document.getElementById('approve').disabled);
    check('C judge: nobody accepts → case id shown', (await text(p, '#judgeStatus')).includes('case ' + mocks.judge_run.nobody_accepts.case_id));
    check('C judge: banner not covered', (await text(p, '[data-slot="covered"]')).includes('not covered yet'));
    check('C judge: plan B needs approval', await text(p, '#planbHead') === 'Plan B needs your approval');
    check('C judge: why line hidden when nobody accepted', !(await visible(p, '#whyFirst')));
    check('C judge: layout does not move between states', await geometry(p) === geo0, await geometry(p));
    await shot(p, 'C-fixture-nobody-accepts.png');
    await p.click('#approve');
    await p.waitForFunction(() => document.getElementById('planbHead').textContent === 'Plan B approved');
    check('C approve: 200 → approved', await text(p, '#approve') === 'Approved' && await p.$eval('#approve', b => b.disabled));
    await p.click('[data-scenario="nightly_check"]');
    await p.waitForFunction(() => /Night check” done/.test(document.getElementById('judgeStatus').textContent));
    await p.waitForFunction(() => !document.getElementById('alert').hidden);
    check('C judge: night check banner from data', await text(p, '[data-slot="alert-text"]') === night.night_check.label && await p.$eval('#alert', a => a.classList.contains('amber')));
    check('C judge: night check title from data', await text(p, 'h1') === night.title);
    await shot(p, 'C-fixture-night-check.png');
    await p.click('[data-scenario="dangerous_morning"]');
    await p.waitForFunction(t => document.querySelector('h1').textContent === t && document.getElementById('alert').hidden, today.title);
    check('C judge: the call-out → back to the covered morning', (await text(p, '[data-slot="covered"]')).includes('covered by ' + today.outcome.covered_by));
    check('C judge: no console errors', p._errors.length === 0, p._errors);
    await p.close();

    /* ratio norm banner: red while the seat is open, green once covered */
    for (const [mock, fxd, tone] of [['ratio_open', ratioOpen, 'red'], ['ratio_covered', ratioCovered, 'green']]) {
      p = await newPage(browser, 1920, 1080);
      await p.goto(base + 'index.html?fixture=1&mock=' + mock);
      await ready(p);
      const a = fxd.ratio_alert;
      check('C ratio ' + mock + ': banner tone ' + tone, await visible(p, '#alert') && await p.$eval('#alert', (x, t) => x.classList.contains(t), tone));
      check('C ratio ' + mock + ': banner text from data', await text(p, '[data-slot="alert-text"]') === a.label + ' · ' + a.action_label);
      check('C ratio ' + mock + ': banner opens the ratio case', (await p.$eval('#alert', x => x.getAttribute('href'))) === 'case.html?id=' + a.case_id + '&fixture=1');
      check('C ratio ' + mock + ': banner clear of the title', await p.evaluate(() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('h1'));
        return document.getElementById('alert').getBoundingClientRect().left > r.getBoundingClientRect().right + 10; }));
      check('C ratio ' + mock + ': layout does not move', await geometry(p) === geo0);
      await shot(p, 'C-fixture-' + mock.replace('_', '-') + '.png');
      await p.close();
    }
    check('C ratio: covered says who', ratioCovered.ratio_alert.action_label.startsWith('Covered by '));

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

    /* live wiring: /api/* calls, hash in the approve body, 409 and 429 from the server */
    p = await newPage(browser, 1920, 1080);
    let approveBody = null, todayCalls = 0, judgeBody = null;
    await p.route('**/health', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, db: 'ok', mode: 'live', model_mode: 'live' }) }));
    await p.route('**/api/today*', r => { todayCalls++; r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(nobody) }); });
    await p.route('**/api/cases/*', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(caseFx) }));
    await p.route('**/api/plans/*/approve', r => { approveBody = r.request().postDataJSON(); r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify(mocks.plans[nobody.plan_b.id].conflict) }); });
    await p.route('**/api/judge/run', r => { judgeBody = r.request().postDataJSON(); r.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ error: 'limit of 6 judge runs per hour reached' }) }); });
    await p.goto(base + 'index.html');
    await ready(p);
    check('live: GET /api/today used', todayCalls >= 1);
    await p.waitForFunction(() => document.querySelector('.tagline').dataset.mode);
    check('live: footer from /health, live mode', await text(p, '.tagline') === 'CoachCover · Running on Vultr.' &&
      await p.$eval('.tagline', e => e.dataset.mode) === 'live');
    check('live: folder button asks the API for the venue on screen', await p.$eval('#folder', a => a.getAttribute('href')) === '/api/binder.pdf?venue=' + nobody.venue.id);
    await p.click('#approve');
    await p.waitForFunction(() => document.getElementById('planb').classList.contains('conflict'));
    check('live: approve sends the plan hash', approveBody && approveBody.hash === nobody.plan_b.hash, approveBody);
    check('live: 409 → human line', await text(p, '#planbHead') === 'Plan changed since you saw it — not approved');
    await p.click('[data-scenario="dangerous_morning"]');
    await p.waitForFunction(() => document.getElementById('judgeStatus').classList.contains('err'));
    check('live: judge sends the scenario', judgeBody && judgeBody.scenario === 'dangerous_morning', judgeBody);
    check('live: judge 429 shown, not swallowed', (await text(p, '#judgeStatus')).includes('429'), await text(p, '#judgeStatus'));
    await p.close();

    /* money block: hidden without data; case cost alone when nothing else is known (test-only values) */
    p = await newPage(browser, 1920, 1080);
    const empty = JSON.parse(JSON.stringify(today));
    empty.money = { uncovered_morning_usd: null, uncovered_morning_label: null, case_cost_cents: null, manager_hour_usd: null };
    await p.route('**/fixtures/today.json', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(empty) }));
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    check('money: hidden when there is no data', !(await visible(p, '#money')));
    await p.unroute('**/fixtures/today.json');
    const caseOnly = JSON.parse(JSON.stringify(today));
    caseOnly.money = { uncovered_morning_usd: null, uncovered_morning_label: null, case_cost_cents: 0.25, manager_hour_usd: null };
    await p.route('**/fixtures/today.json', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(caseOnly) }));
    await p.reload();
    await ready(p);
    check('money: case cost alone', await text(p, '[data-slot="money-head"]') === 'Cost of this cover' && await text(p, '[data-slot="money-big"]') === '0.250¢' &&
      !(await visible(p, '[data-slot="money-source"]')));
    await p.close();

    /* load failure is shown */
    p = await newPage(browser, 1920, 1080);
    await p.route('**/api/today*', r => r.fulfill({ status: 500, body: 'boom' }));
    await p.goto(base + 'index.html');
    await p.waitForFunction(() => document.body.dataset.state === 'error');
    check('C: load error shown', (await text(p, '#loadErr')).includes('HTTP 500'));
    check('C: a load error is red', !(await p.$eval('#loadErr', e => e.classList.contains('calm'))));
    await p.close();

    /* a judge run rebuilds the school: the API answers 503 {"resetting": true}; a calm note, then the morning */
    p = await newPage(browser, 1920, 1080);
    let resetting = true;
    await p.route('**/api/today*', r => resetting
      ? r.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ resetting: true, error: 'Resetting the training school…' }) })
      : r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(today) }));
    await p.goto(base + 'index.html');
    await p.waitForFunction(() => !document.querySelector('#loadErr').hidden);
    check('C: during a reset a calm note, not an error', await text(p, '#loadErr') === 'Resetting the training school…' &&
      await p.$eval('#loadErr', e => e.classList.contains('calm') && getComputedStyle(e).backgroundColor === 'rgb(243, 238, 230)') &&
      !(await text(p, '#loadErr')).includes('HTTP'), await text(p, '#loadErr'));
    check('C: during a reset the page is not in its error state', await p.evaluate(() => document.body.dataset.state) !== 'error');
    resetting = false;
    await p.reload();
    await p.waitForFunction(() => document.body.dataset.state === 'ready');
    check('C: after the reset the morning, no note', await p.$eval('#loadErr', e => e.hidden));
    await p.close();

    /* ---------- screen P2 ---------- */
    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=' + accepted.id + '&fixture=1');
    await ready(p);
    const why = await texts(p, '[data-slot="why-text"]');
    check('P2: why you from data', JSON.stringify(why) === JSON.stringify(acceptedFx.why), why);
    check('P2: hours equal to the why line on screen C', hoursOf(accepted.why_first) && hoursOf(accepted.why_first) === hoursOf(why.join(' ')), [accepted.why_first, why]);
    check('P2: answered offer shows its state, no buttons', await text(p, '#result') === 'Accepted. You cover ' + when(acceptedFx) + '.' && !(await visible(p, '#buttons')));
    check('P2: no console errors', p._errors.length === 0, p._errors);
    await shot(p, 'P2-fixture-answered.png');
    await p.close();

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=' + pendingId + '&fixture=1');
    await ready(p);
    check('P2 pending: ask line', await text(p, '.ask') === 'Can you cover ' + pendingFx.shift_start + '–' + pendingFx.shift_end + ' ' + pendingFx.shift_day + '?');
    const left = pendingFx.expires_in_s;
    check('P2 pending: timer from data', await text(p, '[data-slot="expires-in"]') === Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0'));
    check('P2 pending: buttons', await visible(p, '#buttons'));
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 accept: 200 → accepted', await text(p, '#result') === 'Accepted. You cover ' + when(pendingFx) + '.', await text(p, '#result'));
    await shot(p, 'P2-fixture-accepted.png');
    await p.close();

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=' + pendingId + '&fixture=1&mock=not_eligible');
    await ready(p);
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 accept: 409 → no longer eligible + reason from the API', await text(p, '#result') === 'You are no longer eligible for this shift: ' + mocks.offers[pendingId].accept_conflict.reason, await text(p, '#result'));
    await shot(p, 'P2-fixture-not-eligible-409.png');
    await p.close();

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=' + pendingId + '&fixture=1');
    await ready(p);
    await p.click('[data-action="decline"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 decline: 200 → declined', await text(p, '#result') === 'Declined. Thank you for answering.');
    await p.close();

    /* P2 live wiring: a 500 keeps the buttons, then a 409 reason from the server */
    p = await newPage(browser, 430, 1080);
    let acceptCalls = 0;
    await p.route('**/api/offers/' + pendingId, r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(pendingFx) }));
    await p.route('**/api/offers/' + pendingId + '/accept', r => { acceptCalls++; r.fulfill({ status: 500, body: '' }); });
    await p.goto(base + 'offer.html?id=' + pendingId);
    await ready(p);
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#loadErr:not([hidden])');
    check('P2 live: 500 shown and buttons stay', acceptCalls === 1 && (await text(p, '#loadErr')).includes('HTTP 500') && !(await p.$eval('[data-action="accept"]', b => b.disabled)));
    await p.unroute('**/api/offers/' + pendingId + '/accept');
    await p.route('**/api/offers/' + pendingId + '/accept', r => r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ reason: 'WSI certificate expired Nov 5' }) }));
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    check('P2 live: 409 reason from the server', await text(p, '#result') === 'You are no longer eligible for this shift: WSI certificate expired Nov 5');
    await p.close();

    /* ---------- the family's page of a private lesson ---------- */
    const cfx = name => fx('clients/' + name + '.json');
    const q1 = cfx('Q-1'), q1yes = cfx('Q-1.covered'), q1no = cfx('Q-1.no'), q2 = cfx('Q-2'), q2moved = cfx('Q-2.moved'),
      q2credit = cfx('Q-2.credit'), q1moved = cfx('Q-1.moved'), q1credit = cfx('Q-1.credit'), cmocks = cfx('mocks');
    const first = s => String(s).split(' ')[0];
    const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
    const sides = page => page.$$eval('[data-slot="message"]', es => es.map(e => e.dataset.from).join(','));
    const fromOf = q => q.thread.map(m => m.from).join(',');
    const creditLine = q => q.child + "'s lesson is cancelled. You have " + q.outcome.line.charAt(0).toLowerCase() + q.outcome.line.slice(1) + '.';
    // the page never names a question, an offer or a staff member by id
    const noIds = page => page.evaluate(() => !/\bQ-\d|\bO-\d|\bS[01]\d\b/.test(document.body.innerText));
    // every visible box stays inside the 430 px phone, nothing in the card sticks out of it, no block text is cut
    const overflow = page => page.evaluate(() => {
      const card = document.querySelector('.card').getBoundingClientRect();
      const bad = [...document.querySelectorAll('body *')].filter(e => {
        if (!e.getClientRects().length) return false;
        const r = e.getBoundingClientRect();
        const block = getComputedStyle(e).display !== 'inline';
        return r.right > 430.5 || r.left < -0.5 || (block && e.scrollWidth > e.clientWidth + 1) ||
          (e.closest('.card') && (r.right > card.right + 0.5 || r.bottom > card.bottom + 0.5));
      });
      return bad.map(e => e.tagName + '.' + (typeof e.className === 'string' ? e.className : '') + ': ' + e.textContent.trim().slice(0, 30))
        .concat(document.documentElement.scrollWidth > 430 ? ['the page scrolls sideways'] : []);
    });

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'client.html?id=Q-1&fixture=1');
    await ready(p);
    check('family: the open question from data', await text(p, '[data-slot="question"]') === q1.question, await text(p, '[data-slot="question"]'));
    check('family: signed as the parent of the child, never by name', await text(p, '[data-slot="signed"]') === q1.signed && q1.signed === 'Parent of ' + q1.child);
    check('family: when, where, lesson, instead of whom', JSON.stringify(await texts(p, '.fact:not([hidden]) .fact-text')) ===
      JSON.stringify([cap(q1.day_label) + ' · ' + q1.time_label, q1.venue, q1.lesson, 'Instead of ' + q1.instead_of]), await texts(p, '.fact:not([hidden]) .fact-text'));
    check('family: Yes and No while the question is open', await visible(p, '#buttons') && JSON.stringify(await texts(p, '[data-action]')) === '["Yes","No"]');
    check('family: answer-by time from data', await text(p, '#answerBy') === 'Please answer by ' + q1.answer_by_label);
    check('family: the thread so far', await sides(p) === fromOf(q1) &&
      JSON.stringify(await texts(p, '[data-slot="message-text"]')) === JSON.stringify(q1.thread.map(m => m.text)));
    check('family: sent for the manager', await text(p, '.sent-by') === 'Sent by CoachCover for ' + q1.manager.name + ', ' + q1.manager.role);
    check('family asked: no ids on the page', await noIds(p));
    check('family asked: nothing overflows', (await overflow(p)).length === 0, await overflow(p));
    await shot(p, 'client-fixture-asked.png');
    await p.click('[data-action="yes"]');
    await p.waitForSelector('#result:not([hidden])');
    check('family yes: the line from the API names the coach', await text(p, '#result') === 'Thank you! ' + q1yes.outcome.line + '.' &&
      (await text(p, '#result')).includes(first(q1yes.coach)), await text(p, '#result'));
    check('family yes: the headline says what happened, not the question', await text(p, '[data-slot="question"]') === q1yes.question &&
      !q1yes.question.endsWith('?') && q1yes.question.includes(first(q1yes.coach)), await text(p, '[data-slot="question"]'));
    check('family yes: ok tone, no buttons, no answer-by', await p.$eval('#result', r => r.classList.contains('ok')) && !(await visible(p, '#buttons')) && !(await visible(p, '#answerBy')));
    check('family yes: the thread has the answer and the reply', await sides(p) === fromOf(q1yes) &&
      JSON.stringify(await texts(p, '[data-slot="message-time"]')) === JSON.stringify(q1yes.thread.map(m => (m.from === 'family' ? 'You · ' : '') + m.time_label)));
    check('family yes: nothing overflows', (await overflow(p)).length === 0, await overflow(p));
    check('family yes: no console errors', p._errors.length === 0, p._errors);
    await shot(p, 'client-fixture-yes.png');
    await p.close();

    /* "no" to another coach: the page moves to the new time with the family's own coach, then yes */
    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'client.html?id=Q-1&fixture=1');
    await ready(p);
    await p.click('[data-action="no"]');
    await p.waitForFunction(t => document.querySelector('[data-slot="question"]').textContent === t, q2.question);
    check('family no: the follow-up question opens (the API names it in current)', q1no.state === 'no' && q1no.current === q2.ask_id && q2.kind === 'reschedule' &&
      await text(p, '[data-slot="question"]') === q2.question);
    check('family no: a note above points to the new time', await visible(p, '#notice') &&
      await text(p, '#notice') === 'Got it. ' + q2.child + ' stays with ' + first(q2.coach) + ' — see the new time below.', await text(p, '#notice'));
    check('family no: the new time from data, no "instead of"', JSON.stringify(await texts(p, '.fact:not([hidden]) .fact-text')) ===
      JSON.stringify([cap(q2.day_label) + ' · ' + q2.time_label, q2.venue, q2.lesson]), await texts(p, '.fact:not([hidden]) .fact-text'));
    check('family no: Yes and No again, answer-by from data', await visible(p, '#buttons') && !(await visible(p, '#result')) &&
      await text(p, '#answerBy') === 'Please answer by ' + q2.answer_by_label);
    check('family no: the thread keeps the whole story', await sides(p) === fromOf(q2));
    check('family no: no ids on the page', await noIds(p));
    check('family no: nothing overflows', (await overflow(p)).length === 0, await overflow(p));
    await shot(p, 'client-fixture-follow-up.png');
    await p.click('[data-action="yes"]');
    await p.waitForSelector('#result:not([hidden])');
    check('family new time yes: moved, line from the API', await text(p, '#result') === 'All set. ' + q2moved.outcome.line + '.' &&
      await p.$eval('#result', r => r.classList.contains('ok')), await text(p, '#result'));
    check('family new time yes: the headline says the lesson moved', await text(p, '[data-slot="question"]') === q2moved.question &&
      q2moved.question.includes('is moved to') && !q2moved.question.endsWith('?'), await text(p, '[data-slot="question"]'));
    check('family new time yes: the note goes, the thread is complete', !(await visible(p, '#notice')) && await sides(p) === fromOf(q2moved) && !(await visible(p, '#buttons')));
    check('family new time yes: no ids, nothing overflows', await noIds(p) && (await overflow(p)).length === 0, await overflow(p));
    check('family no then yes: no console errors', p._errors.length === 0, p._errors);
    await shot(p, 'client-fixture-moved.png');
    await p.close();

    /* "no" twice: a credit for one lesson */
    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'client.html?id=Q-1&fixture=1');
    await ready(p);
    await p.click('[data-action="no"]');
    await p.waitForSelector('#notice:not([hidden])');
    await p.click('[data-action="no"]');
    await p.waitForSelector('#result:not([hidden])');
    check('family second no: a credit, line from the API', await text(p, '#result') === creditLine(q2credit) && await p.$eval('#result', r => r.classList.contains('neutral')), await text(p, '#result'));
    check('family second no: the cancellation is the last message', (await texts(p, '[data-slot="message-text"]')).pop() === q2credit.thread[q2credit.thread.length - 1].text);
    await p.close();

    /* opened after the last answer: final state, no buttons */
    for (const [mock, q, line, tone, name] of [['client_credit', q1credit, creditLine(q1credit), 'neutral', 'credit'],
      ['client_declined', q1moved, 'All set. ' + q1moved.outcome.line + '.', 'ok', 'moved-final']]) {
      p = await newPage(browser, 430, 1080);
      await p.goto(base + 'client.html?id=Q-1&fixture=1&mock=' + mock);
      await ready(p);
      check('family ' + mock + ': no buttons, the outcome line', !(await visible(p, '#buttons')) && !(await visible(p, '#answerBy')) &&
        await text(p, '#result') === line && await p.$eval('#result', (r, t) => r.classList.contains(t), tone), await text(p, '#result'));
      check('family ' + mock + ': the whole thread', await sides(p) === fromOf(q));
      check('family ' + mock + ': a statement for the headline', !(await text(p, '[data-slot="question"]')).endsWith('?'),
        await text(p, '[data-slot="question"]'));
      check('family ' + mock + ': no ids, nothing overflows', await noIds(p) && (await overflow(p)).length === 0, await overflow(p));
      check('family ' + mock + ': no console errors', p._errors.length === 0, p._errors);
      await shot(p, 'client-fixture-' + name + '.png');
      await p.close();
    }

    /* 409: the family already answered by text; the page says so and loads the question again */
    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'client.html?id=Q-1&fixture=1&mock=client_closed');
    await ready(p);
    check('family 409: the question looks open at first', await visible(p, '#buttons'));
    await p.click('[data-action="yes"]');
    await p.waitForSelector('#result:not([hidden])');
    check('family 409: "already closed", calm tone, no buttons', await text(p, '#result') === 'This question is already closed.' &&
      await p.$eval('#result', r => r.classList.contains('neutral')) && !(await visible(p, '#buttons')), await text(p, '#result'));
    check('family 409: loaded again, the thread shows the answer given', await sides(p) === fromOf(q1yes));
    check('family 409 mock: the API\'s refusal', cmocks.closed.body.reason.startsWith('Thanks — this question was already closed'));
    check('family 409: no ids, nothing overflows', await noIds(p) && (await overflow(p)).length === 0, await overflow(p));
    await shot(p, 'client-fixture-closed-409.png');
    await p.close();

    /* live wiring: the answer body, the follow-up GET, a 500 keeps the buttons */
    p = await newPage(browser, 430, 1080);
    let respondBody = null;
    await p.route('**/api/client/Q-1', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(q1) }));
    await p.route('**/api/client/Q-1/respond', r => { respondBody = r.request().postDataJSON(); r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(q1no) }); });
    await p.route('**/api/client/Q-2', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(q2) }));
    await p.route('**/api/client/Q-2/respond', r => r.fulfill({ status: 500, body: '' }));
    await p.goto(base + 'client.html?id=Q-1');
    await ready(p);
    await p.click('[data-action="no"]');
    await p.waitForFunction(t => document.querySelector('[data-slot="question"]').textContent === t, q2.question);
    check('family live: No sends accept false, then the follow-up is loaded', respondBody && respondBody.accept === false, respondBody);
    await p.click('[data-action="yes"]');
    await p.waitForSelector('#loadErr:not([hidden])');
    check('family live: 500 shown, buttons stay', (await text(p, '#loadErr')).includes('HTTP 500') && await visible(p, '#buttons') &&
      !(await p.$eval('[data-action="yes"]', b => b.disabled)));
    await p.close();

    /* load errors in words a parent understands, still without ids */
    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'client.html?fixture=1');
    await p.waitForFunction(() => document.body.dataset.state === 'error');
    check('family: no id -> a line about the link', (await text(p, '#loadErr')).startsWith('This link has no question in it.'));
    await p.goto(base + 'client.html?id=Q-9&fixture=1');
    await p.waitForFunction(() => document.body.dataset.state === 'error');
    check('family: unknown question -> a line about the link, no id', (await text(p, '#loadErr')).startsWith('This link does not open a question') && await noIds(p));
    // must-light: the id and overflow checks fail on a page that shows ids and a box wider than the phone
    await p.goto(base + 'client.html?id=Q-1&fixture=1');
    await ready(p);
    await p.evaluate(() => { const d = document.createElement('div'); d.textContent = 'Q-1 · S07'; d.style.width = '480px'; document.querySelector('.card').appendChild(d); });
    check('family checks can fail (ids and a too-wide box)', !(await noIds(p)) && (await overflow(p)).length > 0, await overflow(p));
    await p.close();

    /* the footer by the server's model mode, outside ?fixture=1: replay, and /health not answering (no claim) */
    for (const [health, want, mode] of [[{ ok: true, model_mode: 'replay' }, 'CoachCover · Replay mode.', 'replay'], [null, 'CoachCover', 'unknown']]) {
      p = await newPage(browser, 1920, 1080);
      await p.route('**/health', r => health ? r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(health) }) : r.fulfill({ status: 503, body: '' }));
      await p.route('**/api/today*', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(today) }));
      await p.goto(base + 'index.html');
      await ready(p);
      await p.waitForFunction(() => document.querySelector('.tagline').dataset.mode);
      check('footer by mode: ' + mode, await text(p, '.tagline') === want && await p.$eval('.tagline', e => e.dataset.mode) === mode, await text(p, '.tagline'));
      await p.close();
    }

    /* a judge scene playing: one calm plaque, no half-filled cards; the morning comes back when the run answers */
    p = await newPage(browser, 1920, 1080);
    let releaseRun;
    const runHeld = new Promise(r => { releaseRun = r; });
    await p.route('**/health', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, model_mode: 'live' }) }));
    await p.route('**/api/today*', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(today) }));
    await p.route('**/api/judge/run', async r => { await runHeld; r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mocks.judge_run.dangerous_morning) }); });
    await p.goto(base + 'index.html');
    await ready(p);
    check('scene: no plaque on a morning with a case', await p.evaluate(() => !document.body.dataset.scene && document.getElementById('scene').hidden));
    await p.click('[data-scenario="dangerous_morning"]');
    await p.waitForFunction(() => document.body.dataset.scene === 'running' && /Vultr/.test(document.querySelector('[data-slot="scene-sub"]').textContent));
    const during = await p.evaluate(() => {
      const seen = sel => { const e = document.querySelector(sel); const st = getComputedStyle(e); return st.visibility !== 'hidden' && st.display !== 'none' && !e.hidden; };
      return { line: document.querySelector('[data-slot="scene-line"]').textContent, sub: document.querySelector('[data-slot="scene-sub"]').textContent,
        plaque: seen('#scene'), cards: ['.card.callout', '.card.staff', '.card.shift', '.card.offers', '.band', '.covered', '.planb', 'h1', '.foot'].filter(seen),
        side: seen('#sideCards'), folder: seen('#folder') };
    });
    check('scene: one plaque while the scene plays, the live models named', during.plaque && during.line === 'Playing the scene “Run the call-out”…' &&
      during.sub.startsWith('Live models on Vultr.'), during);
    check('scene: no half-filled card while it plays', during.cards.length === 0, during.cards);
    check('scene: the side cards and the folder stay', during.side && during.folder);
    if (SHOTS) await p.screenshot({ path: path.join(SHOTS, 'C-scene-playing.png') });
    releaseRun();
    await p.waitForFunction(() => !document.body.dataset.scene);
    check('scene: the morning comes back when the run answers', await p.evaluate(() => document.getElementById('scene').hidden &&
      getComputedStyle(document.querySelector('.card.callout')).visibility === 'visible'));
    await p.close();

    /* another visitor's scene (the server says it is running), and a morning with no case yet */
    const quiet = { ...today, callout: null, shift: null, eligibility: null, timeline: [], offers: [], outcome: null, plan_b: null,
      judge: { running: false, case_id: null, scenario: null } };
    for (const [label, body, state, line] of [
      ['remote', { ...today, judge: { running: true, case_id: null, scenario: 'nobody_accepts' } }, 'running', 'Playing the scene “Nobody accepts”…'],
      ['empty', quiet, 'empty', 'No call-out yet this morning.']]) {
      p = await newPage(browser, 1920, 1080);
      await p.route('**/api/today*', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }));
      await p.goto(base + 'index.html');
      await ready(p);
      const st = await p.evaluate(() => ({ scene: document.body.dataset.scene, line: document.querySelector('[data-slot="scene-line"]').textContent,
        callout: getComputedStyle(document.querySelector('.card.callout')).visibility }));
      check('scene: ' + label, st.scene === state && st.line === line && st.callout === 'hidden', st);
      check('scene: ' + label + ', no label left without its value (· or → or "Needs:" at the end)', await p.evaluate(() => ![...document.querySelectorAll('main *')].some(e => {
        if (e.children.length || !e.textContent.trim()) return false;
        let shown = getComputedStyle(e).visibility === 'visible';
        for (let x = e; x && shown; x = x.parentElement) if (x.hidden || getComputedStyle(x).display === 'none') shown = false;
        return shown && /(·|→)\s*$|^\s*(·|→)|Needs:\s*$/.test(e.textContent);
      })));
      await p.close();
    }

    /* ---------- case page ---------- */
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'case.html?id=' + caseId + '&fixture=1');
    await ready(p);
    const steps = Object.fromEntries(caseFx.steps.map(s => [s.kind, s]));
    check('case: state ready', await p.evaluate(() => document.body.dataset.state) === 'ready');
    check('case: title from data', await text(p, '.case-title') === 'Case ' + caseFx.case_id + ' · ' + caseFx.shift_label);
    check('case: title not cut', await p.$eval('.case-title', t => t.scrollWidth <= t.clientWidth + 1));
    check('case: pill', await text(p, '#pill') === caseFx.status_label + ' · ' + caseFx.covered_time_label);
    check('case: steps in order', JSON.stringify(await texts(p, '[data-slot="step-title"]')) === JSON.stringify(caseFx.steps.map(s => s.title)));
    check('case: local stamps', JSON.stringify(await texts(p, '[data-slot="step-time"]')) === JSON.stringify(caseFx.steps.map(s => s.time_label)));
    check('case: voice note = screen C', await text(p, '[data-slot="callout-original"]') === '“' + today.callout.transcript_original + '”' &&
      await text(p, '[data-slot="callout-translation"]') === '“' + today.callout.transcript_en + '”');
    check('case: blocked line = screen C refusal', await text(p, '[data-slot="blocked-row"]') === e.first_refusal.staff + ' · ' + e.first_refusal.label);
    const caseCounts = await p.$$eval('[data-slot="reason-count"]', es => es.map(x => +x.textContent.replace('×', '')));
    check('case: reasons add up', caseCounts.reduce((a, b) => a + b, 0) === steps.allowed.total - steps.allowed.allowed, caseCounts);
    check('case: every reason chip is one chip-high line', await p.$$eval('[data-slot="reason-chip"]', es => es.every(e => e.getBoundingClientRect().height <= 40)));
    const accRow = (await texts(p, '[data-slot="offer-row"]')).find(t => t.startsWith(accepted.staff));
    check('case: accepted offer with its why line', accRow === accepted.staff + ' — accepted · ' + accepted.why_first, accRow);
    check('case: models line', await text(p, '.facts li:last-child') === 'Models: ' + caseFx.model_calls.length + ' calls · ' + caseFx.model_cost_label, await text(p, '.facts li:last-child'));
    check('case: plan hash shortened from data', await text(p, '[data-slot="plan-hash"]') === caseFx.proof.plan_hash.slice(0, 4) + '…' + caseFx.proof.plan_hash.slice(-4));
    /* Proof: one row per model call (time ET · why · seconds · cents), its model, source and "shared by N cases" below,
       a total that is the Models line's own count and cost; the rows' cents add up to it, their dollars to the case cost */
    const callRows = await p.$$eval('[data-slot="call-row"]', rs => rs.map(r => ({ usd: r.dataset.usd,
      cells: [...r.querySelectorAll('td')].map(td => td.textContent), meta: r.nextElementSibling ? r.nextElementSibling.textContent : '' })));
    check('case: one Proof row per model call', callRows.length === caseFx.model_calls.length && callRows.length > 0, callRows.length);
    check('case: each call row from data (ET time, why, seconds, cents)', callRows.every((r, i) => { const c = caseFx.model_calls[i];
      return JSON.stringify(r.cells) === JSON.stringify([c.time_label, c.purpose_label, c.seconds_label, c.cost_label]) && / ET$/.test(c.time_label); }), callRows.map(r => r.cells));
    check('case: each call names its model and source, and its share', callRows.every((r, i) => { const c = caseFx.model_calls[i];
      return r.meta === [c.model_label, c.source_label, c.shared_label].filter(Boolean).join(' · '); }), callRows.map(r => r.meta));
    check('case: "shared by 2 cases" is on the page', caseFx.model_calls.some(c => c.shared_label === 'shared by 2 cases') &&
      (await p.$eval('.proof', x => x.innerText)).includes('shared by 2 cases'));
    const totalLabel = await text(p, '[data-slot="calls-total-label"]');
    check('case: Proof total = the Models line', totalLabel === caseFx.model_calls_total_label &&
      (await text(p, '.facts li:last-child')) === 'Models: ' + totalLabel, totalLabel);
    const centsOf = t => Math.round(parseFloat(String(t).replace(/[^\d.]/g, '')) * 10000);
    check('case: the rows add up to the total, in cents as shown', callRows.reduce((a, r) => a + centsOf(r.cells[3]), 0) === centsOf(caseFx.model_cost_label),
      [callRows.map(r => r.cells[3]), caseFx.model_cost_label]);
    check('case: the rows add up to the case cost, in dollars', Math.abs(callRows.reduce((a, r) => a + Number(r.usd), 0) - caseFx.model_cost_usd) < 1e-9);
    check('case: no Proof cell cut, one line each', await p.$$eval('.calls td', ts => ts.every(t => t.scrollWidth <= t.clientWidth + 1 && t.getBoundingClientRect().height <= 40)));
    check('case: the Proof card holds its table, clear of the next card', await p.evaluate(() => {
      const proof = document.querySelector('.proof').getBoundingClientRect(), table = document.querySelector('.calls').getBoundingClientRect();
      const risk = document.getElementById('risk'), r = risk.hidden ? null : risk.getBoundingClientRect();
      return table.bottom <= proof.bottom && table.right <= proof.right && (!r || proof.bottom <= r.top);
    }));
    check('case: if-nobody-accepted card from data', await visible(p, '#risk') &&
      await p.$eval('#risk p', x => x.innerText) === caseFx.risk.kids + ' kids · refunds $' + caseFx.risk.refunds_usd + ' · $' + caseFx.risk.total_usd + ' at risk', await p.$eval('#risk p', x => x.innerText));
    check('case: price source under the sums', (await text(p, '[data-slot="risk-source"]')).endsWith(lessonSource));
    check('case: no horizontal scroll', await p.evaluate(() => document.documentElement.scrollWidth <= 1920));
    check('case: footer line says replay in ?fixture=1', await text(p, '.foot') === 'CoachCover · Replay mode.');
    const dl = await p.evaluate(async () => { const a = document.getElementById('download'); return { name: a.download, body: await (await fetch(a.href)).text() }; });
    check('case: download = the case JSON', dl.name === 'case-' + caseId + '.json' && JSON.stringify(JSON.parse(dl.body)) === JSON.stringify(caseFx), dl.name);
    const rail = await p.evaluate(() => { const r = document.getElementById('rail').getBoundingClientRect(), d = [...document.querySelectorAll('.s-dot')].map(x => x.getBoundingClientRect()); return [r.top, r.bottom, d[0].top + 10, d[d.length - 1].top + 10]; });
    check('case: rail from first dot to last', Math.abs(rail[0] - rail[2]) < 1.5 && Math.abs(rail[1] - rail[3]) < 1.5, rail);
    check('case: no console errors', p._errors.length === 0, p._errors);
    await p.close();

    /* case: errors are shown */
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'case.html?id=C-0000&fixture=1');
    await p.waitForFunction(() => document.body.dataset.state === 'error');
    check('case: unknown id → error line', (await text(p, '#loadErr')).includes('HTTP 404'));
    // "Cases" from a page that has no case id in hand: the case of Today's call-out opens
    await p.goto(base + 'case.html?fixture=1');
    await p.waitForURL('**/case.html?id=' + caseId + '&fixture=1');
    await ready(p);
    check('case: no id → the case of Today\'s call-out', (await text(p, '[data-slot="case-id"]')) === caseId);
    // before any call-out there is no case: a plain line says so
    await p.route('**/fixtures/today.json', r => r.fulfill({ contentType: 'application/json', body: JSON.stringify(Object.assign({}, today, { callout: null })) }));
    await p.goto(base + 'case.html?fixture=1');
    await p.waitForFunction(() => document.body.dataset.state === 'error');
    check('case: no id and no call-out → "No case yet" line', (await text(p, '#loadErr')).startsWith('No case yet'));
    await p.close();

    /* screen C opens the case: covered line, timeline, ratio banner */
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    const caseUrl = '**/case.html?id=' + caseId + '&fixture=1';
    await Promise.all([p.waitForURL(caseUrl), p.click('#covered')]);
    check('C → case: covered line opens the case', p.url().endsWith('case.html?id=' + caseId + '&fixture=1'));
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    await Promise.all([p.waitForURL(caseUrl), p.click('.band', { position: { x: 1500, y: 150 } })]);
    check('C → case: timeline opens the case', p.url().endsWith('case.html?id=' + caseId + '&fixture=1'));
    await ready(p);
    await Promise.all([p.waitForURL('**/index.html?fixture=1'), p.click('.nav li[data-href]')]);
    check('case → Today', p.url().endsWith('index.html?fixture=1'));
    await p.goto(base + 'index.html?fixture=1&mock=ratio_open');
    await ready(p);
    await Promise.all([p.waitForURL('**/case.html?id=' + ratioOpen.ratio_alert.case_id + '&fixture=1'), p.click('#alert')]);
    await ready(p);
    check('C → ratio case from the banner', (await text(p, '[data-slot="case-id"]')) === ratioOpen.ratio_alert.case_id);
    await p.close();
    /* ---------- week ahead ---------- */
    const week = fx('week.json');
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'week.html?fixture=1');
    await ready(p);
    const starts = [...new Set(week.days.flatMap(d => d.cells.map(c => c.time_label.split('–')[0])))].sort();
    check('week: one column per day, labels from data', JSON.stringify(await texts(p, '[data-slot="day-label"]')) === JSON.stringify(week.days.map(d => d.label)));
    check('week: one row per start time', JSON.stringify(await texts(p, '[data-slot="slot-time"]')) === JSON.stringify(starts));
    check('week: grid is rows x days', (await p.$$('[data-slot="cell"]')).length === starts.length * week.days.length);
    const wcells = await p.$$eval('[data-slot="cell"]', es => es.map(e => ({ level: e.dataset.level, shift: e.dataset.shift || null, txt: e.innerText.replace(/\s+/g, ' ').trim() })));
    const byId = Object.fromEntries(week.days.flatMap(d => d.cells).map(c => [c.shift_id, c]));
    const LV = { at_risk: 'risk', fragile: 'fragile', safe: 'safe' };
    check('week: every fragile shift is a fragile or at-risk cell with its number and tag', week.fragile.every(id => {
      const c = wcells.find(x => x.shift === id); const d = byId[id];
      return c && c.level === LV[d.level] && c.txt === d.backups + (d.level === 'at_risk' ? ' at risk' : ' fragile') + (d.private ? ' 1:1' : ''); }),
      wcells.filter(c => c.level !== 'safe' && c.level !== 'none'));
    const pastCells = week.days.flatMap(d => d.cells).filter(c => c.past).length;
    check('week: started shifts are grey (when the week has any)', pastCells === 0 || wcells.filter(c => c.level === 'past').length > 0);
    const legend = await texts(p, '[data-slot="legend-item"]');
    check('week: legend from data, threshold included', legend.includes(week.legend.find(l => l.level === 'fragile').label + ' (fragile)') &&
      legend.includes(week.legend.find(l => l.level === 'at_risk').label + ' (at risk)') && legend.includes(week.legend.find(l => l.level === 'safe').label + ' (safe)'), legend);
    check('week: legend inside the card', await p.evaluate(() => Math.max(...[...document.querySelectorAll('.legend .li')].map(x => x.getBoundingClientRect().right)) < document.querySelector('.gcard').getBoundingClientRect().right));
    check('week: fragile count from data', await text(p, '[data-slot="fragile-count"]') === String(week.counts.fragile_total));
    check('week: fragile list rows', (await p.$$('[data-slot="fragile-row"]')).length === Math.min(6, week.fragile.length));
    const wdots = await p.$$eval('.flist .dot', es => es.map(e => e.dataset.level));
    check('week: diamond for at risk, dot for fragile', wdots.every(l => l === 'risk' || l === 'fragile') && wdots.includes('risk'), wdots);
    check('week: tips from data', JSON.stringify(await texts(p, '[data-slot="tip-text"]')) === JSON.stringify([week.fixes.standby[0].text, week.fixes.training.best.text]));
    const folderW = await p.$eval('#folder', a => ({ text: a.textContent.trim(), href: a.href }));
    const pdfW = await p.evaluate(async href => { const r = await fetch(href); const b = new Uint8Array(await r.arrayBuffer()); return { status: r.status, head: String.fromCharCode(...b.slice(0, 5)) }; }, folderW.href);
    check('week: inspector folder button opens a 200 PDF', folderW.text === "Inspector's folder (PDF)" && pdfW.status === 200 && pdfW.head === '%PDF-', [folderW, pdfW]);
    check('week: folder button clear of the title and the board', await p.evaluate(() => {
      // the title's text, not its block (the h1 block runs to the page's right edge)
      const textBox = sel => { const r = document.createRange(); r.selectNodeContents(document.querySelector(sel)); return r.getBoundingClientRect(); };
      const f = document.getElementById('folder').getBoundingClientRect(), h = textBox('h1'), sub = textBox('.sub'), b = document.querySelector('.board').getBoundingClientRect();
      return f.left >= Math.max(h.right, sub.right) + 24 && f.bottom <= b.top && f.right <= 1920;
    }));
    check('week: footer line says replay in ?fixture=1', await text(p, '[data-slot="footer-line"]') === 'CoachCover · Replay mode.');
    check('week: tips fit their boxes', await p.$$eval('.tip', es => es.every(t => t.scrollHeight <= t.clientHeight + 1)));
    const wm = fx('week_mocks.json').standby;
    await p.click('[data-slot="tip-action"]');
    await p.waitForFunction(() => document.querySelector('[data-slot="tip-action"]').textContent.startsWith('Proposed'));
    check('week: Propose standby 201 -> "Proposed · SB-n", button off', await text(p, '[data-slot="tip-action"]') === 'Proposed · ' + wm.created.id &&
      await p.$eval('[data-slot="tip-action"]', b => b.disabled) && (await text(p, '[data-slot="tip-result"]')).startsWith('Nothing is booked'));
    check('week: proposal is for the suggested person and shift', wm.created.shift_id === week.fixes.standby[0].shift_id && wm.created.staff_id === week.fixes.standby[0].staff_id && wm.created.status === 'proposed');
    check('week: menu links', (await p.$eval('[data-nav="network"]', a => a.getAttribute('href'))) === 'network.html?fixture=1' &&
      (await p.$eval('[data-nav="today"]', a => a.getAttribute('href'))) === 'index.html?fixture=1');
    check('week: no console errors', p._errors.length === 0, p._errors);
    await p.close();

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'week.html?fixture=1&mock=standby_conflict');
    await ready(p);
    await p.click('[data-slot="tip-action"]');
    await p.waitForFunction(() => (document.querySelector('[data-slot="tip-result"]') || {}).textContent);
    check('week: Propose standby 409 -> the reason, button back', await text(p, '[data-slot="tip-result"]') === 'Not proposed — ' + wm.not_cleared.error &&
      !(await p.$eval('[data-slot="tip-action"]', b => b.disabled)));
    await p.close();
    /* week: a private lesson's cell carries a 1:1 tag (the API flag "private"): a cell is tagged exactly when the shift it
       shows is a private lesson; then a safe, a fragile and an at-risk group cell are flagged too, to see the tag on each */
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'week.html?fixture=1');
    await ready(p);
    const tagMap = () => p.$$eval('[data-slot="cell"][data-shift]', es => es.map(e => ({ shift: e.dataset.shift, level: e.dataset.level,
      tag: (e.querySelector('[data-slot="private-tag"]') || {}).textContent || null })));
    const shown = await tagMap();
    check('week: 1:1 tags only where the data says private', shown.some(c => c.tag) && shown.every(c => (c.tag === '1:1') === !!(byId[c.shift] || {}).private),
      shown.filter(c => c.tag || (byId[c.shift] || {}).private));
    const privPick = ['safe', 'fragile', 'risk'].map(l => shown.find(c => c.level === l && !c.tag)).filter(Boolean).map(c => c.shift);
    const weekPriv = JSON.parse(JSON.stringify(week));
    weekPriv.days.forEach(d => d.cells.forEach(c => { if (privPick.includes(c.shift_id)) c.private = true; }));
    const privById = Object.fromEntries(weekPriv.days.flatMap(d => d.cells).map(c => [c.shift_id, !!c.private]));
    await p.route('**/fixtures/week.json', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(weekPriv) }));
    await p.reload();
    await ready(p);
    const tagged = await tagMap();
    check('week: 1:1 tag on each private lesson cell (safe, fragile, at risk)', privPick.length === 3 &&
      privPick.every(id => (tagged.find(c => c.shift === id) || {}).tag === '1:1') && tagged.every(c => (c.tag === '1:1') === privById[c.shift]),
      tagged.filter(c => c.tag));
    check('week: the 1:1 tag sits inside its cell, clear of the number and the level', await p.$$eval('[data-slot="private-tag"]', es => es.every(e => {
      const t = e.getBoundingClientRect(), c = e.closest('.cell'), cr = c.getBoundingClientRect();
      return t.left >= cr.left && t.right <= cr.right && t.top >= cr.top && t.bottom <= cr.bottom &&
        [...c.querySelectorAll('.num, .tag')].every(x => { const r = x.getBoundingClientRect(); return r.right <= t.left || r.left >= t.right || r.bottom <= t.top || r.top >= t.bottom; });
    })));
    check('week: no console errors with private lessons', p._errors.length === 0, p._errors);
    await shot(p, 'week-fixture-private.png');
    if (SHOTS) await p.locator('.gcard').screenshot({ path: path.join(SHOTS, 'week-fixture-private-grid.png') });
    await p.close();
    p = await newPage(browser, 1920, 1080);
    let sbBody = null;
    let weekUrl = null;
    await p.route('**/api/today*', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(today) }));
    await p.route('**/api/week*', r => { weekUrl = r.request().url(); r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(week) }); });
    await p.route('**/api/standby', r => { sbBody = r.request().postDataJSON(); r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify(wm.holder) }); });
    await p.goto(base + 'week.html');
    await ready(p);
    await p.click('[data-slot="tip-action"]');
    await p.waitForFunction(() => (document.querySelector('[data-slot="tip-result"]') || {}).textContent);
    check('week live: asks the API for the manager\'s venue', weekUrl && new URL(weekUrl).searchParams.get('venue') === today.venue.id, weekUrl);
    check('week live: standby sends shift and person', sbBody && sbBody.shift_id === week.fixes.standby[0].shift_id && sbBody.staff_id === week.fixes.standby[0].staff_id, sbBody);
    check('week live: 409 holder reason shown', await text(p, '[data-slot="tip-result"]') === 'Not proposed — ' + wm.holder.error);
    await p.close();

    /* ---------- network: the loan step by step, both managers, the 409s ---------- */
    const nm = fx('network_mocks.json');
    const loanOf = st => fx(nm.loan_files[st]);
    const cand = loanOf('candidate'), done = loanOf('assigned');
    const ref = cand.code;
    const shortName = v => v.split(' ')[0];
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'network.html?loan=' + ref + '&fixture=1');
    await ready(p);
    const msgKinds = await p.$$eval('[data-slot="message"]', es => es.map(e => e.dataset.kind));
    check('network: messages are the agents talking', JSON.stringify(msgKinds) === JSON.stringify(cand.steps.filter(s => ['request_sent', 'offered', 'checked_offer'].includes(s.kind)).map(s => s.kind)), msgKinds);
    const heads = await texts(p, '[data-slot="message-head"]');
    check('network: local stamps from the steps, no UTC, no ASCII arrow', heads.every(h => / ET · /.test(h) && !/UTC|->/.test(h)) && heads[0].startsWith(cand.steps.find(s => s.kind === 'request_sent').at_label), heads);
    check('network: two approve buttons, one per manager', JSON.stringify(await texts(p, '[data-slot^="approve-"]')) === JSON.stringify(['Approve as ' + cand.borrower.manager, 'Approve as ' + cand.lender.manager]));
    check('network: waiting for both', await text(p, '#result-text') === cand.status_label);
    await shot(p, 'network-fixture-candidate.png');
    await p.click('[data-slot="approve-borrower"]');
    await p.waitForFunction(m => document.getElementById('result-text').textContent === 'Waiting for ' + m, cand.lender.manager + ' (' + shortName(cand.lender.venue_name) + ')');
    check('network: one approval -> waiting for the other manager', await text(p, '#result-text') === 'Waiting for ' + cand.lender.manager + ' (' + shortName(cand.lender.venue_name) + ')');
    check('network: the approved side shows who and when', (await texts(p, '[data-slot="approval"]'))[0] === loanOf('one_approval').borrower.approved.at_label + ' · ' + cand.borrower.manager + ' (' + shortName(cand.borrower.venue_name) + ') approved');
    await shot(p, 'network-fixture-one-approval.png');
    await p.click('[data-slot="approve-lender"]');
    await p.waitForFunction(() => document.getElementById('result').dataset.state === 'assigned');
    check('network: both -> loan approved, person, case', await text(p, '#result-text') === done.status_label + ' · ' + done.candidate.name + ' covers ' + shortName(done.borrower.venue_name) + ' ' + done.shift.time_label.split(' ').pop().split('–')[0] + ' · case ' + done.case.ref, await text(p, '#result-text'));
    check('network: result opens the case of the loan', (await p.$eval('#result', a => a.getAttribute('href'))) === 'case.html?id=' + done.case.ref + '&mock=network_loan&fixture=1');
    check('network: two approval cards', (await p.$$('[data-slot="approval"]')).length === 2);
    const wrong = await p.evaluate(r => window.CC.post('/api/network/loans/' + r + '/approve', { manager: 'x', venue: 'x' }), ref);
    check('network mock: a wrong manager gets 403', wrong.status === 403);
    check('network: no console errors', p._errors.length === 0, p._errors);
    await p.close();

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'network.html?loan=' + ref + '&fixture=1&mock=one_approval,approve_conflict');
    await ready(p);
    await p.click('[data-slot="approve-lender"]');
    await p.waitForSelector('[data-slot="approve-error"]');
    check('network 409 (already approved): shown in that manager card', await text(p, '[data-slot="approve-error"]') === 'Not approved — ' + nm.approve.repeat.error);
    await p.close();

    p = await newPage(browser, 1920, 1080);
    let loanBody = null;
    await p.route('**/api/network/loans/' + ref, r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(loanOf('one_approval')) }));
    await p.route('**/api/network/loans/' + ref + '/approve', r => { loanBody = r.request().postDataJSON(); r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify(nm.approve.closed) }); });
    await p.goto(base + 'network.html?loan=' + ref);
    await ready(p);
    await p.click('[data-slot="approve-lender"]');
    await p.waitForSelector('[data-slot="approve-error"]');
    check('network live: approve sends that manager and venue', loanBody && loanBody.manager === cand.lender.manager && loanBody.venue === cand.lender.venue_id, loanBody);
    check('network live 409 (loan closed): shown', await text(p, '[data-slot="approve-error"]') === 'Not approved — ' + nm.approve.closed.error);
    await p.close();

    for (const st of ['declined_site_short', 'rejected_rules']) {
      p = await newPage(browser, 1920, 1080);
      await p.goto(base + 'network.html?loan=' + ref + '&fixture=1&mock=' + st);
      await ready(p);
      const l = loanOf(st);
      check('network ' + st + ': refusal banner with the reason', await p.$eval('#result', r => r.classList.contains('fail')) && (await text(p, '#result-text')).startsWith(l.status_label + ' · '));
      check('network ' + st + ': no approve buttons', (await p.$$('[data-slot^="approve-"]')).length === 0);
      await shot(p, 'network-fixture-' + st.replace(/_/g, '-') + '.png');
      await p.close();
    }

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'network.html?fixture=1&mock=network_open');
    await ready(p);
    check('network ask: offered for the open shift', await visible(p, '#ask') && !(await p.$eval('#askBtn', b => b.disabled)));
    await Promise.all([p.waitForURL('**/network.html?loan=' + nm.ask.created.code + '&fixture=1'), p.click('#askBtn')]);
    await ready(p);
    check('network ask: 201 -> the new loan', await text(p, '#result-text') === nm.ask.created.status_label);
    await p.goto(base + 'network.html?fixture=1&mock=network_open,ask_conflict');
    await ready(p);
    await p.click('#askBtn');
    await p.waitForFunction(() => document.querySelector('[data-slot="ask-error"]').textContent);
    check('network ask: 409 shown', await text(p, '[data-slot="ask-error"]') === 'Not asked — ' + nm.ask.conflict.error);
    await p.close();

    /* menus lead to the new screens */
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    await Promise.all([p.waitForURL('**/week.html?fixture=1'), p.click('.nav a[data-nav="week"]')]);
    check('C -> Week ahead', p.url().endsWith('week.html?fixture=1'));
    await ready(p);
    await Promise.all([p.waitForURL('**/network.html?fixture=1'), p.click('[data-nav="network"]')]);
    check('Week -> Network', p.url().endsWith('network.html?fixture=1'));
    await p.goto(base + 'case.html?id=' + caseId + '&fixture=1');
    await ready(p);
    await Promise.all([p.waitForURL('**/network.html?fixture=1'), p.click('.nav li[data-href="network.html"]')]);
    check('case -> Network', p.url().endsWith('network.html?fixture=1'));
    await p.close();
    /* ---------- the menu: the same six items on every page, each one a real link ---------- */
    const MENU = ['Today', 'Week ahead', 'Staff & Certificates', 'Cases', 'Network', 'Reports'];
    const staffFx = fx('staff.json');
    for (const page of ['index', 'week', 'staff', 'network', 'case']) {
      p = await newPage(browser, 1920, 1080);
      await p.goto(base + page + '.html?' + (page === 'case' ? 'id=' + caseId + '&' : '') + 'fixture=1');
      await ready(p);
      const items = await p.$$eval('.nav > li, .nav > a', els => els.map(e => {
        const a = e.tagName === 'A' ? e : e.querySelector('a');
        return { text: e.innerText.replace(/\s+/g, ' ').trim(), href: a ? a.getAttribute('href') : e.getAttribute('data-href') };
      }));
      check(page + ': menu has the six items', JSON.stringify(items.map(i => i.text)) === JSON.stringify(MENU), items.map(i => i.text));
      check(page + ': every menu item is a real link (no "#")', items.every(i => i.href !== null && i.href !== undefined && i.href !== '#' && !String(i.href).startsWith('#')), items);
      check(page + ': Reports opens the inspector\'s folder PDF', items.find(i => i.text === 'Reports').href.endsWith('binder.pdf'), items);
      if (page !== 'index') {
        const cases = items.find(i => i.text === 'Cases');
        check(page + ': Cases leads to the case', page === 'case' || cases.href === 'case.html?id=' + caseId + '&fixture=1', cases);
      }
      await p.close();
    }
    for (const page of ['offer', 'client']) {
      p = await newPage(browser, 1920, 1080);
      await p.goto(base + page + '.html?fixture=1' + (page === 'offer' ? '&id=' + pendingId : ''));
      check(page + ': the logo is not a dead "#" link', (await p.$eval('.brand', a => a.getAttribute('href'))) === 'index.html');
      await p.close();
    }

    /* ---------- Staff & Certificates ---------- */
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    await Promise.all([p.waitForURL('**/staff.html?fixture=1'), p.click('.nav a[data-nav="staff"]')]);
    await ready(p);
    check('staff: state ready', (await p.evaluate(() => document.body.dataset.state)) === 'ready');
    check('staff: one row per person from the API answer', (await p.$$('[data-slot="person"]')).length === staffFx.staff.length && staffFx.staff.length === 40);
    check('staff: the menu marks Staff & Certificates', (await text(p, '.nav-item.active')) === 'Staff & Certificates');
    const sums = await texts(p, '[data-slot="summary"] .chip');
    check('staff: summary counts from totals', sums[0] === staffFx.totals.valid + ' valid' && sums[1].startsWith(staffFx.totals.expiring + ' expire') &&
      sums[2] === (staffFx.totals.expired + staffFx.totals.revoked + staffFx.totals.not_yet_valid) + ' expired or revoked' && sums[3].startsWith(staffFx.totals.review + ' to review'), sums);
    const rowOf = id => '[data-slot="person"][data-staff="' + id + '"]';
    const disputed = staffFx.staff.find(x => x.needs_review);
    check('staff: the disputed card says "Readers disagree — review"', (await text(p, rowOf(disputed.id) + ' .cert[data-state="review"]')).includes('Readers disagree — review'));
    check('staff: the disputed row is labeled as seeded demo data', (await text(p, rowOf(disputed.id) + ' .demo')).startsWith('Seeded demo case'));
    check('staff: an expired card is marked Expired', (await text(p, rowOf('S27') + ' .cert[data-state="expired"]')).includes('Expired'));
    check('staff: a revoked card is marked Revoked by issuer', (await text(p, rowOf('S35') + ' .cert[data-state="revoked"]')).includes('Revoked'));
    check('staff: a card expiring soon says in how many days', /Expires (today|in \d+ days?)/.test(await text(p, rowOf('S20') + ' .cert[data-state="expiring"]')));
    check('staff: no row without name, role and cards', await p.$$eval('[data-slot="person"]', rs => rs.every(r => r.querySelector('.nm').textContent && r.querySelector('.roles').textContent && r.querySelectorAll('.cert').length)));
    check('staff: the note says the data is invented', (await text(p, '[data-slot="note"]')).includes('invented'));
    check('staff: Cases link goes to the case', (await p.$eval('[data-nav="cases"]', a => a.getAttribute('href'))) === 'case.html?id=' + caseId + '&fixture=1');
    check('staff: no horizontal scroll', await p.evaluate(() => document.documentElement.scrollWidth <= 1920));
    check('staff: no cert chip cut off', await p.$$eval('.cert', cs => cs.every(c => c.scrollWidth <= c.clientWidth + 1)));
    check('staff: no console errors', p._errors.length === 0, p._errors);
    await shot(p, 'staff-1920x1080.png');
    await p.close();
    /* a failed load is said out loud */
    p = await newPage(browser, 1920, 1080);
    await p.route('**/fixtures/staff.json', r => r.fulfill({ status: 500, body: '{}' }));
    await p.goto(base + 'staff.html?fixture=1');
    await ready(p);
    check('staff: a failed load shows an error line', (await p.evaluate(() => document.body.dataset.state)) === 'error' && (await text(p, '#loadErr')).includes('Could not load /api/staff'));
    await p.close();
    /* the sample certificate panel on Staff & Certificates: between the summary and the roster, three outcomes */
    for (const [mock, file] of [['', 'agree'], ['&mock=sample_disagree', 'disagree'], ['&mock=sample_failed', 'failed']]) {
      const want = fx('sample-cert/' + file + '.json').body;
      p = await newPage(browser, 1920, 1080);
      await p.goto(base + 'staff.html?fixture=1' + mock);
      await ready(p);
      await p.waitForFunction(() => document.querySelector('#sample-cert [data-slot="sc-runs"]').textContent !== '');
      if (!mock) {
        check('staff: sample check panel sits between the summary and the roster', await p.evaluate(() => {
          const s = document.querySelector('[data-slot="summary"]').getBoundingClientRect(), c = document.getElementById('sample-cert').getBoundingClientRect(),
            r = document.querySelector('.roster').getBoundingClientRect();
          return s.bottom <= c.top && c.bottom <= r.top && Math.abs(c.left - r.left) < 1 && Math.abs(c.right - r.right) < 1;
        }));
        check('staff: sample check button and hint', await text(p, '#sample-cert [data-slot="sc-button"]') === 'Check a sample certificate' &&
          (await text(p, '#sample-cert [data-slot="sc-hint"]')).startsWith('Both models'));
      }
      await p.click('#sample-cert [data-slot="sc-button"]');
      await p.waitForFunction(() => document.getElementById('sample-cert').dataset.state === 'result');
      await p.waitForFunction(() => { const i = document.querySelector('#sample-cert .sc-img'); return i && i.complete && i.naturalWidth > 0; });
      check('staff: sample check ' + file + ' verdict from data', await text(p, '#sample-cert [data-slot="sc-verdict"]') === want.verdict, await text(p, '#sample-cert [data-slot="sc-verdict"]'));
      check('staff: sample check ' + file + ' leaves the roster as it was', (await p.$$('[data-slot="person"]')).length === staffFx.staff.length);
      check('staff: sample check ' + file + ' no horizontal scroll', await p.evaluate(() => document.documentElement.scrollWidth <= 1920));
      check('staff: sample check ' + file + ' no console errors', p._errors.length === 0, p._errors);
      if (SC_SHOTS) await p.screenshot({ path: path.join(SC_SHOTS, 'staff-sc-' + file + '.png') });
      await p.close();
    }

    /* ---------- contrast of every status element, by number ---------- */
    // text 4.5:1 or more against its own background (with hatching: against the darker stripe);
    // dots, rings and dashed borders 3:1 or more against what they sit on
    const CONTRAST_JS = () => {
      const parse = c => {
        c = String(c).trim();
        if (c.startsWith('#')) { const h = c.slice(1); return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: 1 }; }
        const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
        const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
      };
      const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      const lum = c => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
      const over = (t, b) => ({ r: t.r * t.a + b.r * (1 - t.a), g: t.g * t.a + b.g * (1 - t.a), b: t.b * t.a + b.b * (1 - t.a), a: 1 });
      const bgOf = el => {
        const layers = [];
        for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
          const c = parse(getComputedStyle(n).backgroundColor);
          if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; }
        }
        let base = { r: 255, g: 255, b: 255, a: 1 };
        for (let i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
        return base;
      };
      const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
      const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name);
      window.__contrast = (items) => items.flatMap(it => [...document.querySelectorAll(it.sel)].map(el => {
        const cs = getComputedStyle(el);
        let fg, bg;
        if (it.kind === 'text') { fg = parse(cs.color); bg = bgOf(el); }
        else {
          fg = parse(it.prop === 'border' ? cs.borderTopColor : it.prop === 'bg' ? cs.backgroundColor : it.prop === 'fill' ? cs.fill : cssVar(it.prop));
          bg = it.on === 'white' ? { r: 255, g: 255, b: 255, a: 1 } : bgOf(it.on === 'self' ? el : el.parentElement);
        }
        let r = ratio(fg, bg);
        if (it.hatch) r = Math.min(r, ratio(fg, parse(cssVar(it.hatch))));
        return { what: it.what, kind: it.kind, sel: it.sel, ratio: Math.round(r * 100) / 100 };
      }));
    };
    const measured = [];
    async function contrast(page, items) {
      await page.evaluate(CONTRAST_JS);
      const res = await page.evaluate(its => window.__contrast(its), items);
      items.forEach(it => check('contrast present: ' + it.what, res.some(r => r.sel === it.sel), it.sel));
      res.forEach(r => measured.push(r));
    }
    const T = (what, sel, extra) => Object.assign({ what, sel, kind: 'text' }, extra || {});
    const G = (what, sel, prop, on, extra) => Object.assign({ what, sel, kind: 'sign', prop, on }, extra || {});

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    await contrast(p, [
      T('Today: red reason chip', '.chip.expired'), T('Today: amber reason chip', '.chip.review'), T('Today: first refusal (BLOCKED)', '#refusal'),
      G('Today: refusal mark', '#refusal svg circle', 'fill', 'parent'),
      G('Today: declined ring (offers)', '.off-ico.declined', 'border', 'parent'), G('Today: no-reply ring (offers)', '.off-ico.noreply', 'border', 'parent'),
      G('Today: accepted dot (offers)', '.off-ico.accepted', 'bg', 'parent'),
      G('Today: declined ring (timeline)', '.tl-mark.declined', 'border', 'white'), G('Today: no-reply ring (timeline)', '.tl-mark.noreply', 'border', 'white'),
      G('Today: accepted dot (timeline)', '.tl-mark.accepted', 'bg', 'white'),
      T('Today: covered banner (brand gold)', '#covered .txt'), T('Today: judge button (brand teal)', '.jbtn.primary')
    ]);
    for (const [mock, what] of [['ratio_open', 'Today: ratio banner, seat open'], ['ratio_covered', 'Today: ratio banner, covered']]) {
      await p.goto(base + 'index.html?fixture=1&mock=' + mock);
      await ready(p);
      await contrast(p, [T(what, '#alert')]);
    }
    await p.goto(base + 'index.html?fixture=1&mock=plan_changed');
    await ready(p);
    await p.click('[data-scenario="nobody_accepts"]');
    await p.waitForFunction(() => !document.getElementById('approve').disabled);
    await contrast(p, [T('Today: not covered banner', '#covered .txt'), G('Today: plan B mark (timeline)', '.tl-mark.planmark', 'border', 'white')]);
    await p.click('#approve');
    await p.waitForFunction(() => document.getElementById('planb').classList.contains('conflict'));
    await contrast(p, [T('Today: plan changed (409) line', '.planb.conflict .txt b')]);
    await p.click('[data-scenario="nightly_check"]');
    await p.waitForFunction(() => !document.getElementById('alert').hidden);
    await contrast(p, [T('Today: night check banner', '#alert')]);
    await p.close();

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'case.html?id=' + caseId + '&fixture=1');
    await ready(p);
    await contrast(p, [
      T('Case: blocked line', '[data-slot="blocked-row"]'), T('Case: red reason chip', '.reason.tone-risk'), T('Case: amber reason chip', '.reason.tone-fragile'),
      T('Case: accepted offer', '.offer.accepted'), G('Case: accepted mark', '.offer.accepted svg circle', 'fill', 'parent'),
      G('Case: declined bar', '.offer.declined', '--st-risk-dot', 'self'), G('Case: no-reply bar', '.offer.noreply', '--st-fragile-dot', 'self'),
      G('Case: blocked mark', '.blocked svg', 'stroke-color', 'parent', { prop: '--st-risk-dot' }),
      T('Case: covered pill (brand gold)', '#pill')
    ]);
    await p.close();

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'network.html?loan=' + ref + '&fixture=1&mock=assigned');
    await ready(p);
    await contrast(p, [G('Network: approval mark', '.approval .chk circle', 'fill', 'parent'), T('Network: loan approved bar (brand gold)', '#result-text')]);
    for (const st of ['declined_site_short', 'rejected_rules']) {
      await p.goto(base + 'network.html?loan=' + ref + '&fixture=1&mock=' + st);
      await ready(p);
      await contrast(p, [T('Network: ' + st + ' banner', '#result-text')]);
    }
    await p.close();

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'staff.html?fixture=1');
    await ready(p);
    await contrast(p, [
      T('Staff: valid card', '.cert[data-level="safe"]'), T('Staff: expiring or disputed card', '.cert[data-level="fragile"]'),
      T('Staff: expired or revoked card (on hatching)', '.cert[data-level="risk"]', { hatch: '--st-risk-hatch' }),
      T('Staff: summary chip, valid', '.chip[data-level="safe"]'), T('Staff: summary chip, expiring', '.chip[data-level="fragile"]'),
      T('Staff: summary chip, expired (on hatching)', '.chip[data-level="risk"]', { hatch: '--st-risk-hatch' }),
      G('Staff: row marker, at risk', '.who .dot[data-level="risk"]', 'bg', 'parent'), G('Staff: row marker, to check', '.who .dot[data-level="fragile"]', 'bg', 'parent')
    ]);
    await p.close();

    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'week.html?fixture=1');
    await ready(p);
    await contrast(p, [
      T('Week: safe digits', '.cell[data-level="safe"] .num'), T('Week: fragile digits', '.cell[data-level="fragile"] .num'),
      T('Week: fragile tag', '.cell[data-level="fragile"] .tag'),
      T('Week: at-risk digits (on hatching)', '.cell[data-level="risk"] .num', { hatch: '--st-risk-hatch' }),
      T('Week: at-risk tag (on hatching)', '.cell[data-level="risk"] .tag', { hatch: '--st-risk-hatch' }),
      G('Week: fragile dashed border', '.cell[data-level="fragile"]', 'border', 'parent'),
      G('Week: legend dashed border', '.legend .sw[data-level="fragile"]', 'border', 'parent'),
      G('Week: fragile dot (list)', '.flist .dot[data-level="fragile"]', 'bg', 'parent'),
      G('Week: at-risk diamond (list)', '.flist .dot[data-level="risk"]', 'bg', 'parent'),
      T('Week: no-shift dash', '.cell[data-level="none"] .num'), T('Week: no-shift legend', '.legend .sw[data-level="none"]'),
      T('Week: propose standby button (brand teal)', '.tip .btn')
    ]);
    await p.close();

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'offer.html?id=' + pendingId + '&fixture=1');
    await ready(p);
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    await contrast(p, [T('Phone: accepted line', '#result')]);
    await p.goto(base + 'offer.html?id=' + pendingId + '&fixture=1&mock=not_eligible');
    await ready(p);
    await p.click('[data-action="accept"]');
    await p.waitForSelector('#result:not([hidden])');
    await contrast(p, [T('Phone: no longer eligible line', '#result')]);
    await p.close();

    p = await newPage(browser, 1920, 1080);
    await p.route('**/fixtures/week.json', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(weekPriv) }));
    await p.goto(base + 'week.html?fixture=1');
    await ready(p);
    await contrast(p, [T('Week: 1:1 tag (brand teal)', '.cell .one')]);
    await p.close();

    p = await newPage(browser, 430, 1080);
    await p.goto(base + 'client.html?id=Q-1&fixture=1');
    await ready(p);
    await contrast(p, [T('Family: school message', '.bubble.school'), T('Family: message time', '.msg-time'), T('Family: Yes button (brand teal)', '.btn-accept')]);
    await p.click('[data-action="yes"]');
    await p.waitForSelector('#result:not([hidden])');
    await contrast(p, [T('Family: covered line', '#result'), T('Family: own answer (brand teal)', '.bubble.family')]);
    await p.goto(base + 'client.html?id=Q-1&fixture=1');
    await ready(p);
    await p.click('[data-action="no"]');
    await p.waitForSelector('#notice:not([hidden])');
    await contrast(p, [T('Family: new time note', '#notice')]);
    await p.goto(base + 'client.html?id=Q-1&fixture=1&mock=client_credit');
    await ready(p);
    await contrast(p, [T('Family: credit line', '#result')]);
    await p.close();

    /* ---------- laptop windows: the 1920 canvas scales itself to the window width (assets/fit.js) ---------- */
    {
      const fitState = pg => pg.evaluate(() => {
        const b = document.body, r = b.getBoundingClientRect(), root = document.documentElement;
        window.scrollTo(500, 0); const sx = window.scrollX; window.scrollTo(0, 0);
        return { scale: window.CC_SCALE, transform: getComputedStyle(b).transform, inlineW: root.style.width, inlineH: root.style.height,
          fitCss: (document.getElementById('cc-fit') || {}).textContent, bodyW: r.width, scrollW: root.scrollWidth, scrollX: sx,
          hint: getComputedStyle(b, '::after').content };
      });
      const S = 1366 / 1920;
      for (const pageUrl of ['index.html?fixture=1', 'case.html?id=' + caseId + '&fixture=1', 'network.html?fixture=1', 'week.html?fixture=1', 'staff.html?fixture=1']) {
        const name = pageUrl.split('.')[0];
        p = await newPage(browser, 1920, 1080);
        await p.goto(base + pageUrl);
        await ready(p);
        let f = await fitState(p);
        check('fit ' + name + ' 1920: scale exactly 1, page untouched', f.scale === 1 && f.transform === 'none' && f.inlineW === '' && f.inlineH === '' && f.fitCss === '' && f.bodyW === 1920, f);
        await p.close();
        p = await newPage(browser, 1366, 768);
        await p.goto(base + pageUrl);
        await ready(p);
        f = await fitState(p);
        check('fit ' + name + ' 1366: scale 1366/1920, whole width in the window, no horizontal scroll',
          f.scale === S && /^matrix\(([\d.]+), 0, 0, \1, 0, 0\)$/.test(f.transform) && Math.abs(parseFloat(f.transform.slice(7)) - S) < 1e-5 && Math.abs(f.bodyW - 1366) < 0.5 && f.scrollW <= 1366 && f.scrollX === 0 && f.hint === 'none', f);
        // back to a full-size window: the factor returns to exactly 1 and the inline sizes go away
        await p.setViewportSize({ width: 1920, height: 1080 });
        await p.waitForFunction(() => window.CC_SCALE === 1);
        f = await fitState(p);
        check('fit ' + name + ' 1366 → 1920 resize: back to scale 1, untouched', f.transform === 'none' && f.inlineW === '' && f.inlineH === '' && f.bodyW === 1920, f);
        if (name === 'index') {
          await p.setViewportSize({ width: 1366, height: 768 });
          await p.waitForFunction(s => window.CC_SCALE === s, S);
          // must-light: without the fit the same window scrolls sideways (the checks above can fail)
          const off = await p.evaluate(() => { document.getElementById('cc-fit').textContent = ''; document.documentElement.removeAttribute('style'); return document.documentElement.scrollWidth; });
          check('fit must-light: the unscaled canvas is wider than a 1366 window', off > 1366, off);
        }
        await p.close();
      }
      // Today at 1366: judge buttons and Approve plan B are inside the window width and reachable by a real click
      p = await newPage(browser, 1366, 768);
      await p.goto(base + 'index.html?fixture=1');
      await ready(p);
      const reach = await p.evaluate(() => [...document.querySelectorAll('.jbtn'), document.getElementById('approve')].map(b => {
        b.scrollIntoView({ block: 'nearest' });
        const r = b.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2, t = document.elementFromPoint(x, y);
        return { id: b.dataset.scenario || b.id, right: Math.round(r.right), bottom: Math.round(r.bottom), hit: !!t && (t === b || b.contains(t)) };
      }));
      check('fit Today 1366: 5 judge buttons + Approve plan B inside the window and hit at their centres', reach.length === 6 && reach.every(r => r.right <= 1366 && r.bottom <= 768 && r.hit), reach);
      await p.click('[data-scenario="nobody_accepts"]');
      await p.waitForFunction(() => !document.getElementById('approve').disabled);
      await p.click('#approve');
      await p.waitForFunction(() => document.getElementById('planbHead').textContent === 'Plan B approved');
      check('fit Today 1366: nobody accepts → Approve plan B works by a real click', await text(p, '#approve') === 'Approved');
      check('fit Today 1366: no console errors', p._errors.length === 0, p._errors);
      await p.close();
      // measurements that use getBoundingClientRect (banner room, network chevron) give the same layout at 1366 as at 1920.
      // (The chip-stretch line in today.js is divided the same way, but its branch does not run with the current CSS:
      // .chips is a 725-px flex box, so its scrollWidth never falls below 725 and "spare" stays 0.)
      const measure = async (w, h, url, fn) => { const pg = await newPage(browser, w, h); await pg.goto(base + url); await ready(pg); const v = await pg.evaluate(fn); await pg.close(); return v; };
      const alertOf = () => document.getElementById('alert').style.maxWidth;
      const chevOf = () => { const c = document.querySelector('[data-slot="user-label"]').nextElementSibling; return [c.style.left, c.style.transform, ...[...document.querySelector('.topbar').children].map(n => n.style.transform)]; };
      const a1 = await measure(1920, 1080, 'index.html?fixture=1&mock=ratio_open', alertOf), a2 = await measure(1366, 768, 'index.html?fixture=1&mock=ratio_open', alertOf);
      // a range's text width is measured to ~0.03 px run to run even at 1920; the unscaled mistake would be > 1 px
      check('fit Today: banner room at 1366 = at 1920 (±0.25 px)', a1 !== '' && Math.abs(parseFloat(a1) - parseFloat(a2)) <= 0.25, [a1, a2]);
      const n1 = await measure(1920, 1080, 'network.html?fixture=1', chevOf), n2 = await measure(1366, 768, 'network.html?fixture=1', chevOf);
      check('fit Network: user-label chevron at 1366 = at 1920', n1[0] !== '' && JSON.stringify(n1) === JSON.stringify(n2), [n1, n2]);
    }

    /* ---------- sample certificate panel (harness page; answers captured from the API in replay) ---------- */
    {
      const scFx = name => fx('sample-cert/' + name + '.json');
      const scInfo = scFx('info'), scAgree = scFx('agree'), scDis = scFx('disagree'), scFail = scFx('failed');
      const scLimit = scFx('limit'), scBusy = scFx('busy');
      const cells = (pg, row) => pg.$$eval('.sc-table tr[data-row="' + row + '"] td', ts => ts.map(t => t.textContent.trim()));
      const scShot = async (pg, name) => { if (SC_SHOTS) await pg.screenshot({ path: path.join(SC_SHOTS, name), fullPage: true }); };
      async function press(pg) {
        await pg.click('[data-slot="sc-button"]');
        await pg.waitForFunction(() => !['idle', 'waiting'].includes(document.getElementById('sample-cert').dataset.state), null, { timeout: 10000 });
        await pg.waitForFunction(() => { const i = document.querySelector('.sc-img'); return !i || i.complete; });
      }
      p = await newPage(browser, 1320, 900);
      await p.goto(base + 'harness/sample-cert.html?fixture=1');
      await p.waitForFunction(() => document.querySelector('[data-slot="sc-runs"]').textContent !== '');
      await p.evaluate(() => document.fonts.ready);
      check('SC: button label', await text(p, '[data-slot="sc-button"]') === 'Check a sample certificate');
      check('SC: hint from the server mode and limit', await text(p, '[data-slot="sc-hint"]') ===
        'Both models’ recorded readings of a synthetic card (replay mode). Counts as one of ' + scInfo.body.runs_per_hour + ' judge runs an hour.', await text(p, '[data-slot="sc-hint"]'));
      check('SC: runs left before the first press', await text(p, '[data-slot="sc-runs"]') === scInfo.body.runs_left_this_hour + ' of ' + scInfo.body.runs_per_hour + ' judge runs left this hour');
      await p.click('[data-slot="sc-button"]');
      check('SC: waiting indicator while the readers read', await visible(p, '[data-slot="sc-wait"]') &&
        /reading the card… \d+ s$/.test(await text(p, '[data-slot="sc-wait"]')) && await p.$eval('[data-slot="sc-button"]', b => b.disabled));
      await p.waitForFunction(() => document.getElementById('sample-cert').dataset.state === 'result');
      await p.waitForFunction(() => { const i = document.querySelector('.sc-img'); return i && i.complete; });
      const A = scAgree.body;
      check('SC agree: verdict from data', await text(p, '[data-slot="sc-verdict"]') === A.verdict && A.verdict === 'Dates agree: accepted');
      check('SC agree: verdict in the safe colours', await p.$eval('[data-slot="sc-verdict"]', v => v.classList.contains('is-safe')));
      check('SC agree: two reader columns', JSON.stringify(await texts(p, '[data-slot="sc-reader"]')) === JSON.stringify(['Muse Glimmer', 'Nemotron Omni']));
      check('SC agree: issued, expires, card number from data', JSON.stringify([await cells(p, 'issued'), await cells(p, 'expires'), await cells(p, 'card-number')]) ===
        JSON.stringify([A.readings.map(r => r.issued), A.readings.map(r => r.expires), A.readings.map(r => r.card_number)]));
      check('SC agree: seconds and cost from data', JSON.stringify([await cells(p, 'seconds'), await cells(p, 'cost')]) ===
        JSON.stringify([A.readings.map(r => r.seconds_label), A.readings.map(r => r.cost_label)]), [await cells(p, 'seconds'), await cells(p, 'cost')]);
      const proofA = await text(p, '[data-slot="sc-proof"]');
      check('SC agree: Proof line names both models, the time and the price', A.ledger.every(r => proofA.includes(r.model) && proofA.includes(r.time_label) && proofA.includes(r.cost_label)) &&
        proofA.includes('total ' + A.total_cost_label), proofA);
      check('SC agree: card thumbnail loaded', await p.$eval('.sc-img', i => i.naturalWidth > 0 && i.getAttribute('src') === '/media/sample-cards/c3-photo.jpg'));
      check('SC agree: runs left from the answer', await text(p, '[data-slot="sc-runs"]') === A.runs_left_this_hour + ' of ' + A.runs_per_hour + ' judge runs left this hour');
      check('SC agree: no dates marked as differing', (await p.$$('.sc-differs')).length === 0);
      await contrast(p, [T('Sample check: accepted verdict', '[data-slot="sc-verdict"]'), T('Sample check: button (brand teal)', '[data-slot="sc-button"]')]);
      await scShot(p, 'sc-agree.png');
      check('SC agree: no console errors', p._errors.length === 0, p._errors);

      await p.goto(base + 'harness/sample-cert.html?fixture=1&mock=sample_disagree');
      await press(p);
      const D = scDis.body;
      check('SC disagree: verdict from data', await text(p, '[data-slot="sc-verdict"]') === D.verdict && D.verdict === 'Dates disagree: sent to a person');
      check('SC disagree: verdict in the fragile colours', await p.$eval('[data-slot="sc-verdict"]', v => v.classList.contains('is-fragile')));
      check('SC disagree: the two expiry dates as each reader read them', JSON.stringify(await cells(p, 'expires')) === JSON.stringify(D.readings.map(r => r.expires)) &&
        D.readings[0].expires !== D.readings[1].expires);
      const differing = ['issued', 'expires'].filter(k => D.readings[0][k] !== D.readings[1][k]).length * 2;
      check('SC disagree: only the dates the readers read differently are marked', differing > 0 && (await p.$$('.sc-differs')).length === differing &&
        JSON.stringify(await p.$$eval('.sc-differs', ts => ts.map(t => t.parentNode.dataset.row))) === JSON.stringify(['expires', 'expires']));
      check('SC disagree: no price shown where none was recorded', JSON.stringify(await cells(p, 'cost')) === JSON.stringify(['not recorded', 'not recorded']) &&
        (await text(p, '[data-slot="sc-proof"]')).includes('price not recorded'));
      await contrast(p, [T('Sample check: sent to a person verdict', '[data-slot="sc-verdict"]'), T('Sample check: differing date', '.sc-differs')]);
      await scShot(p, 'sc-disagree.png');

      await p.goto(base + 'harness/sample-cert.html?fixture=1&mock=sample_failed');
      await press(p);
      const F = scFail.body;
      check('SC failed: verdict from data', await text(p, '[data-slot="sc-verdict"]') === F.verdict && F.verdict === 'Reader failed: sent to a person');
      check('SC failed: verdict in the risk colours', await p.$eval('[data-slot="sc-verdict"]', v => v.classList.contains('is-risk')));
      check('SC failed: the failed reader says so', JSON.stringify(await cells(p, 'issued')) === JSON.stringify([F.readings[0].issued, 'reader failed']));
      check('SC failed: the error is shown', (await text(p, '[data-slot="sc-failnote"]')).includes(F.readings[1].error));
      check('SC failed: Proof says the call failed, no price claimed', (await text(p, '[data-slot="sc-proof"]')).includes(F.ledger[1].model + ' · ' + F.ledger[1].time_label + ' · failed, no answer'));
      await contrast(p, [T('Sample check: reader failed verdict', '[data-slot="sc-verdict"]')]);
      await scShot(p, 'sc-failed.png');

      await p.goto(base + 'harness/sample-cert.html?fixture=1&mock=sample_limit');
      await press(p);
      check('SC 429: limit line with minutes from Retry-After', await text(p, '[data-slot="sc-error"]') ===
        'Judge run limit reached: try again in ' + Math.ceil(+scLimit.retry_after / 60) + ' min.', await text(p, '[data-slot="sc-error"]'));
      check('SC 429: no result panel', !(await visible(p, '[data-slot="sc-result"]')));
      check('SC 429: runs left 0', await text(p, '[data-slot="sc-runs"]') === '0 of ' + scLimit.body.runs_per_hour + ' judge runs left this hour');
      await contrast(p, [T('Sample check: limit line', '[data-slot="sc-error"]')]);
      await scShot(p, 'sc-limit.png');

      await p.goto(base + 'harness/sample-cert.html?fixture=1&mock=sample_busy');
      await press(p);
      check('SC 503: resetting line from the server', await text(p, '[data-slot="sc-error"]') === scBusy.body.error + ' Try again in ' + scBusy.retry_after + ' s.', await text(p, '[data-slot="sc-error"]'));
      await p.close();

      // live wiring: GET for the limit, one POST per press, 429 with the server's Retry-After
      p = await newPage(browser, 1320, 900);
      const scCalls = [];
      let scNext = { status: 200, body: scAgree.body };
      await p.route('**/api/judge/sample-certificate', r => {
        scCalls.push(r.request().method());
        if (r.request().method() === 'GET') return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...scInfo.body, mode: 'live' }) });
        r.fulfill({ status: scNext.status, contentType: 'application/json', headers: scNext.headers || {}, body: JSON.stringify(scNext.body) });
      });
      await p.goto(base + 'harness/sample-cert.html');
      await p.waitForFunction(() => document.querySelector('[data-slot="sc-runs"]').textContent !== '');
      check('SC live: hint says live on Vultr only when the server says live', await text(p, '[data-slot="sc-hint"]') ===
        'Both models read a synthetic card live on Vultr. Counts as one of ' + scInfo.body.runs_per_hour + ' judge runs an hour.', await text(p, '[data-slot="sc-hint"]'));
      await press(p);
      check('SC live: one GET, then one POST', JSON.stringify(scCalls) === JSON.stringify(['GET', 'POST']), scCalls);
      check('SC live: result shown', await text(p, '[data-slot="sc-verdict"]') === scAgree.body.verdict);
      scNext = { status: 429, headers: { 'Retry-After': '120' }, body: scLimit.body };
      await press(p);
      check('SC live: 429 shown with minutes', await text(p, '[data-slot="sc-error"]') === 'Judge run limit reached: try again in 2 min.', await text(p, '[data-slot="sc-error"]'));
      check('SC live: no console errors besides the 429 itself', p._errors.filter(e => !/429/.test(e)).length === 0, p._errors);
      await p.close();
    }

    // must-light: the same measure on a deliberately pale ink has to fall below 4.5:1
    p = await newPage(browser, 1920, 1080);
    await p.goto(base + 'index.html?fixture=1');
    await ready(p);
    await p.evaluate(CONTRAST_JS);
    const pale = await p.evaluate(() => { document.documentElement.style.setProperty('--st-risk-ink', '#E0A090');
      return window.__contrast([{ what: 'control', kind: 'text', sel: '.chip.expired' }])[0].ratio; });
    check('contrast check can fail (pale ink ' + pale + ':1)', pale < 4.5, pale);
    await p.close();

    const byWhat = {};
    measured.forEach(m => { if (!byWhat[m.what] || m.ratio < byWhat[m.what].ratio) byWhat[m.what] = m; });
    Object.values(byWhat).forEach(m => check('contrast ' + m.what + ' ' + m.ratio + ':1 (min ' + (m.kind === 'text' ? 4.5 : 3) + ')', m.ratio >= (m.kind === 'text' ? 4.5 : 3), m));
    const minOf = kind => Object.values(byWhat).filter(m => m.kind === kind).sort((a, b) => a.ratio - b.ratio)[0];
    console.log('CONTRAST text min ' + JSON.stringify(minOf('text')) + ' · signs min ' + JSON.stringify(minOf('sign')));
    if (SHOTS) fs.writeFileSync(path.join(SHOTS, 'contrast-2026-11-07.json'), JSON.stringify(Object.values(byWhat).sort((a, b) => a.ratio - b.ratio), null, 1) + '\n');
  } finally {
    await browser.close();
    server.close();
  }
  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
