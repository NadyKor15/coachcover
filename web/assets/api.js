/* CoachCover web: one small layer between the screens and the REST API.
 *
 * Live mode (default): GET/POST go to /api/... on the same origin.
 * Fixture mode (?fixture=1): answers come from web/fixtures/*.json, nothing leaves the browser.
 *   &mock=plan_changed   POST /api/plans/{id}/approve answers 409 {"reason":"plan changed", ...}
 *   &mock=not_eligible   POST /api/offers/{id}/accept answers 409 {"reason": ...}
 *   &mock=ratio_open | ratio_covered   GET /api/today with the children-per-instructor banner open or covered
 *   &mock=client_declined | client_credit   GET /api/client/{id}: the family's case after its last answer (moved / credit)
 *   &mock=client_closed  POST /api/client/{id}/respond answers 409 (the family already answered by text)
 * Every call resolves to {status, body}; network and parse failures resolve to status 0 with body.error,
 * so a screen always has something to show instead of failing silently.
 */
(function (global) {
  'use strict';

  const params = new URLSearchParams(global.location.search);
  const FIXTURE = params.get('fixture') === '1';
  const MOCKS = new Set((params.get('mock') || '').split(',').map(s => s.trim()).filter(Boolean));
  const FIXTURE_BASE = new URL('fixtures/', document.currentScript ? new URL('..', document.currentScript.src) : global.location.href).href;

  // in-memory state of the fixture "server" (lives as long as the page)
  const fx = { scenario: null, approved: {}, mocks: null };

  async function http(method, path, body) {
    const init = { method, headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      return { status: 0, body: { error: 'network: ' + (e && e.message ? e.message : String(e)) } };
    }
    const text = await res.text();
    if (!text) return { status: res.status, body: {} };
    try {
      return { status: res.status, body: JSON.parse(text) };
    } catch (e) {
      return { status: res.status, body: { error: 'not JSON: ' + text.slice(0, 120) } };
    }
  }

  async function fixtureFile(name) {
    const r = await http('GET', FIXTURE_BASE + name);
    return r;
  }

  async function mocks() {
    if (!fx.mocks) {
      const r = await fixtureFile('mocks.json');
      if (r.status !== 200) throw new Error('fixtures/mocks.json: ' + r.status);
      fx.mocks = r.body;
    }
    return fx.mocks;
  }

  const delay = ms => new Promise(ok => setTimeout(ok, ms));

  // network loans in fixture mode: &mock=<state> opens the loan in that state; approvals move it on
  const LOAN_STATES = ['candidate', 'one_approval', 'one_approval_lender', 'assigned', 'declined_site_short', 'rejected_rules'];
  const initialLoan = () => LOAN_STATES.find(s => MOCKS.has(s)) || 'candidate';
  async function netMocks() {
    if (!fx.net) {
      const r = await fixtureFile('network_mocks.json');
      if (r.status !== 200) throw new Error('fixtures/network_mocks.json: ' + r.status);
      fx.net = r.body;
    }
    return fx.net;
  }
  async function approveLoan(body) {
    const N = await netMocks();
    const state = fx.loan || initialLoan();
    const loan = (await fixtureFile(N.loan_files[state])).body;
    if (MOCKS.has('approve_conflict')) return { status: 409, body: N.approve.repeat };
    const side = ['borrower', 'lender'].find(k => loan[k] && loan[k].venue_id === (body && body.venue));
    if (!side || loan[side].manager !== body.manager) return { status: 403, body: N.approve.forbidden };
    if (loan.status === 'assigned') return { status: 409, body: N.approve.closed };
    if (loan.status !== 'awaiting_approval') return { status: 409, body: N.approve.declined };
    if (loan[side].approved) return { status: 409, body: N.approve.repeat };
    const next = { candidate: { borrower: 'one_approval', lender: 'one_approval_lender' },
                   one_approval: { lender: 'assigned' }, one_approval_lender: { borrower: 'assigned' } }[state];
    fx.loan = next[side];
    return { status: 200, body: state === 'candidate' ? N.approve.first : N.approve.second };
  }

  // the family's page of a private lesson: an answer moves the case on as the API does (fixtures/clients/mocks.json)
  async function clientMocks() {
    if (!fx.clients) {
      const r = await fixtureFile('clients/mocks.json');
      if (r.status !== 200) throw new Error('fixtures/clients/mocks.json: ' + r.status);
      fx.clients = r.body;
      fx.asks = Object.assign({}, ...Object.keys(r.body.opened_by_mock).filter(k => MOCKS.has(k)).map(k => r.body.opened_by_mock[k]));
    }
    return fx.clients;
  }
  const askFile = id => 'clients/' + encodeURIComponent(id) + (fx.asks[id] ? '.' + fx.asks[id] : '') + '.json';

  async function fixtureGet(path) {
    const url = new URL(path, global.location.origin);
    const p = url.pathname;
    if (p === '/api/today') {
      const M = await mocks();
      let file = 'today.json';
      MOCKS.forEach(k => { if (M.today_by_mock && M.today_by_mock[k]) file = M.today_by_mock[k]; });
      if (MOCKS.has('network_open') || MOCKS.has('network_assigned')) file = MOCKS.has('network_open') ? 'today.network_open.json' : 'today.network_assigned.json';
      if (fx.scenario && M.today_by_scenario && M.today_by_scenario[fx.scenario]) file = M.today_by_scenario[fx.scenario];
      const r = await fixtureFile(file);
      if (r.status === 200 && r.body.plan_b && fx.approved[r.body.plan_b.id]) {
        r.body.plan_b.state = 'approved';
      }
      return r;
    }
    if (p === '/api/week') return fixtureFile('week.json');
    if (p === '/api/network') return fixtureFile('network.json');
    let m = p.match(/^\/api\/network\/loans\/([^/]+)$/);
    if (m) return fixtureFile((await netMocks()).loan_files[fx.loan || initialLoan()]);
    m = p.match(/^\/api\/offers\/([^/]+)$/);
    if (m) return fixtureFile('offers/' + encodeURIComponent(decodeURIComponent(m[1])) + '.json');
    m = p.match(/^\/api\/client\/([^/]+)$/);
    if (m) { await clientMocks(); return fixtureFile(askFile(decodeURIComponent(m[1]))); }
    m = p.match(/^\/api\/cases\/([^/]+)$/);
    if (m) return fixtureFile('cases/' + encodeURIComponent(decodeURIComponent(m[1])) + (MOCKS.has('network_loan') ? '.network_loan' : '') + '.json');
    return { status: 404, body: { error: 'no fixture for GET ' + p } };
  }

  async function fixturePost(path, body) {
    const M = await mocks();
    await delay(150);
    const p = new URL(path, global.location.origin).pathname;
    if (p === '/api/judge/run') {
      const run = M.judge_run[body && body.scenario];
      if (!run) return { status: 422, body: { error: 'unknown scenario' } };
      fx.scenario = body.scenario === 'dangerous_morning' ? null : body.scenario;
      fx.approved = {};
      return { status: 200, body: run };
    }
    let m = p.match(/^\/api\/network\/loans\/([^/]+)\/approve$/);
    if (m) return approveLoan(body);
    if (p === '/api/standby') {
      const W = (await fixtureFile('week_mocks.json')).body.standby;
      if (MOCKS.has('standby_conflict')) return { status: 409, body: W.not_cleared };
      const key = (body && body.shift_id) + '|' + (body && body.staff_id);
      if (fx.standby && fx.standby[key]) return { status: 409, body: W.repeat };
      fx.standby = Object.assign(fx.standby || {}, { [key]: true });
      return { status: 201, body: W.created };
    }
    if (p === '/api/network/loans') {
      const N = await netMocks();
      if (MOCKS.has('ask_conflict')) return { status: 409, body: N.ask.conflict };
      fx.loan = 'candidate';
      return { status: 201, body: N.ask.created };
    }
    m = p.match(/^\/api\/offers\/([^/]+)\/(accept|decline)$/);
    if (m) {
      const o = M.offers[decodeURIComponent(m[1])];
      if (!o) return { status: 404, body: { error: 'no such offer' } };
      if (m[2] === 'accept' && MOCKS.has('not_eligible')) return { status: 409, body: o.accept_conflict };
      return { status: 200, body: o[m[2]] };
    }
    m = p.match(/^\/api\/client\/([^/]+)\/respond$/);
    if (m) {
      const C = await clientMocks();
      const id = decodeURIComponent(m[1]);
      if (!C.answers[id]) return { status: 404, body: { error: 'unknown question ' + id } };
      if (fx.asks[id]) return { status: 409, body: C.closed.body };  // answered already
      if (MOCKS.has('client_closed')) { Object.assign(fx.asks, C.closed.then); return { status: 409, body: C.closed.body }; }
      Object.assign(fx.asks, C.answers[id][body && body.accept ? 'yes' : 'no']);
      return fixtureFile(askFile(id));
    }
    m = p.match(/^\/api\/plans\/([^/]+)\/approve$/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      const plan = M.plans[id];
      if (!plan) return { status: 404, body: { error: 'no such plan' } };
      if (MOCKS.has('plan_changed') || !body || body.hash !== plan.current_hash) return { status: 409, body: plan.conflict };
      fx.approved[id] = true;
      return { status: 200, body: plan.approve };
    }
    return { status: 404, body: { error: 'no mock for POST ' + p } };
  }

  async function get(path) {
    if (!FIXTURE) return http('GET', path);
    try { return await fixtureGet(path); } catch (e) { return { status: 0, body: { error: String(e.message || e) } }; }
  }

  async function post(path, body) {
    if (!FIXTURE) return http('POST', path, body || {});
    try { return await fixturePost(path, body); } catch (e) { return { status: 0, body: { error: String(e.message || e) } }; }
  }

  function errorText(r) {
    if (!r) return 'no answer';
    const b = r.body || {};
    return (r.status ? 'HTTP ' + r.status : 'no connection') + (b.error || b.detail || b.reason ? ': ' + (b.error || b.detail || b.reason) : '');
  }

  global.CC = { FIXTURE, MOCKS, get, post, errorText, params };
})(window);
