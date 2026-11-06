/* CoachCover web: one small layer between the screens and the REST API.
 *
 * Live mode (default): GET/POST go to /api/... on the same origin.
 * Fixture mode (?fixture=1): answers come from web/fixtures/*.json, nothing leaves the browser.
 *   &mock=plan_changed   POST /api/plans/{id}/approve answers 409 {"reason":"plan changed", ...}
 *   &mock=not_eligible   POST /api/offers/{id}/accept answers 409 {"reason": ...}
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

  async function fixtureGet(path) {
    const url = new URL(path, global.location.origin);
    const p = url.pathname;
    if (p === '/api/today') {
      const M = await mocks();
      const file = (fx.scenario && M.today_by_scenario && M.today_by_scenario[fx.scenario]) || 'today.json';
      const r = await fixtureFile(file);
      if (r.status === 200 && r.body.plan_b && fx.approved[r.body.plan_b.id]) {
        r.body.plan_b.state = 'approved';
      }
      return r;
    }
    let m = p.match(/^\/api\/offers\/([^/]+)$/);
    if (m) return fixtureFile('offers/' + encodeURIComponent(decodeURIComponent(m[1])) + '.json');
    m = p.match(/^\/api\/cases\/([^/]+)$/);
    if (m) return fixtureFile('cases/' + encodeURIComponent(decodeURIComponent(m[1])) + '.json');
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
    let m = p.match(/^\/api\/offers\/([^/]+)\/(accept|decline)$/);
    if (m) {
      const o = M.offers[decodeURIComponent(m[1])];
      if (!o) return { status: 404, body: { error: 'no such offer' } };
      if (m[2] === 'accept' && MOCKS.has('not_eligible')) return { status: 409, body: o.accept_conflict };
      return { status: 200, body: o[m[2]] };
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
