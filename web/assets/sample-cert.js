/* CoachCover: "Check a sample certificate" for the judges.
 *
 * Mounts into <section id="sample-cert"></section> when the page has one (or call CoachSampleCert.mount(el)).
 * One press = POST /api/judge/sample-certificate: the server picks a bundled synthetic card and both readers
 * (Muse Glimmer, then Nemotron Omni) read it; the panel shows both readings, the verdict and the Proof line.
 * GET /api/judge/sample-certificate first gives the model mode and the hourly judge limit (counts nothing).
 * Every number on the panel comes from those answers; strings go in as text, never as HTML.
 * Fixture mode (?fixture=1): answers come from fixtures/sample-cert/*.json, captured from the API in replay mode
 *   (tests/web/make_sample_cert_fixtures.py); &mock=sample_disagree | sample_failed | sample_limit | sample_busy.
 */
(function (global) {
  'use strict';

  const URL_API = '/api/judge/sample-certificate';
  const script = document.currentScript;
  const FIXTURE_BASE = new URL('../fixtures/sample-cert/', script ? script.src : global.location.href).href;
  const params = new URLSearchParams(global.location.search);
  const FIXTURE = params.get('fixture') === '1';
  const MOCKS = new Set((params.get('mock') || '').split(',').map(s => s.trim()).filter(Boolean));
  const FIXTURE_POST = ['sample_disagree', 'sample_failed', 'sample_limit', 'sample_busy'].find(m => MOCKS.has(m));

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  async function http(method, path, body) {
    const init = { method, headers: { Accept: 'application/json' } };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, init); } catch (e) {
      return { status: 0, retryAfter: null, body: { error: 'network: ' + (e && e.message ? e.message : String(e)) } };
    }
    const text = await res.text();
    let parsed = {};
    if (text) { try { parsed = JSON.parse(text); } catch (e) { parsed = { error: 'not JSON: ' + text.slice(0, 120) }; } }
    return { status: res.status, retryAfter: res.headers.get('Retry-After'), body: parsed };
  }

  async function fixture(name) {
    const r = await http('GET', FIXTURE_BASE + name + '.json');
    if (r.status !== 200) return { status: 0, retryAfter: null, body: { error: 'fixture ' + name + ': HTTP ' + r.status } };
    return { status: r.body.status, retryAfter: r.body.retry_after, body: r.body.body };
  }

  const info = () => FIXTURE ? fixture('info') : http('GET', URL_API);
  async function run() {
    if (!FIXTURE) return http('POST', URL_API, {});
    await new Promise(ok => setTimeout(ok, 400));  // the waiting state shows in fixture mode too
    return fixture(FIXTURE_POST ? FIXTURE_POST.replace('sample_', '') : 'agree');
  }

  function hintText(meta) {
    const runs = meta && meta.runs_per_hour
      ? 'Counts as one of ' + meta.runs_per_hour + ' judge runs an hour.'
      : 'Counts as one judge run (the hourly limit is shared with the scenes).';
    const how = meta && meta.mode === 'live' ? 'Both models read a synthetic card live on Vultr.'
      : meta && meta.mode === 'replay' ? 'Both models’ recorded readings of a synthetic card (replay mode).'
      : 'Both models read a synthetic card.';
    return how + ' ' + runs;
  }

  function runsText(left, perHour) {
    if (left === undefined || left === null) return '';
    return left + ' of ' + perHour + ' judge runs left this hour';
  }

  function dash(v) { return v === null || v === undefined || v === '' ? '—' : String(v); }

  function mount(root) {
    if (!root || root.dataset.mounted) return;
    root.dataset.mounted = '1';
    root.classList.add('sc');
    root.setAttribute('aria-label', 'Check a sample certificate');

    const head = el('div', 'sc-head');
    const btn = el('button', 'sc-btn', 'Check a sample certificate');
    btn.type = 'button';
    btn.dataset.slot = 'sc-button';
    const hint = el('p', 'sc-hint', hintText(null));
    hint.dataset.slot = 'sc-hint';
    const runs = el('p', 'sc-runs');
    runs.dataset.slot = 'sc-runs';
    const words = el('div', 'sc-words');
    words.append(hint, runs);
    head.append(btn, words);

    const wait = el('div', 'sc-wait');
    wait.hidden = true;
    wait.dataset.slot = 'sc-wait';
    wait.setAttribute('role', 'status');
    const spin = el('span', 'sc-spin');
    spin.setAttribute('aria-hidden', 'true');
    const waitText = el('span', 'sc-wait-text');
    wait.append(spin, waitText);

    const err = el('p', 'sc-error');
    err.hidden = true;
    err.dataset.slot = 'sc-error';
    err.setAttribute('role', 'alert');

    const result = el('div', 'sc-result');
    result.hidden = true;
    result.dataset.slot = 'sc-result';

    root.replaceChildren(head, wait, err, result);
    root.dataset.state = 'idle';

    info().then(r => {
      if (r.status !== 200) return;
      hint.textContent = hintText(r.body);
      runs.textContent = runsText(r.body.runs_left_this_hour, r.body.runs_per_hour);
    });

    let timer = null;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      err.hidden = true;
      result.hidden = true;
      wait.hidden = false;
      root.dataset.state = 'waiting';
      const started = Date.now();
      const tick = () => { waitText.textContent = 'Muse Glimmer, then Nemotron Omni, are reading the card… ' + Math.floor((Date.now() - started) / 1000) + ' s'; };
      tick();
      timer = setInterval(tick, 1000);
      const r = await run();
      clearInterval(timer);
      wait.hidden = true;
      btn.disabled = false;
      if (r.status === 200) {
        render(result, r.body);
        result.hidden = false;
        root.dataset.state = 'result';
        root.dataset.outcome = r.body.outcome || '';
        if (r.body.runs_left_this_hour !== undefined) runs.textContent = runsText(r.body.runs_left_this_hour, r.body.runs_per_hour);
        if (r.body.runs_per_hour) hint.textContent = hintText({ mode: r.body.mode, runs_per_hour: r.body.runs_per_hour });
        return;
      }
      const secs = parseInt(r.retryAfter, 10);
      if (r.status === 429) {
        err.textContent = 'Judge run limit reached: try again in ' + (secs > 0 ? Math.ceil(secs / 60) + ' min' : 'a while') + '.';
        if (r.body && r.body.runs_per_hour) runs.textContent = runsText(0, r.body.runs_per_hour);
        root.dataset.state = 'limit';
      } else if (r.status === 503) {
        err.textContent = (r.body && r.body.error ? r.body.error + ' ' : 'The server is busy. ') +
          'Try again in ' + (secs > 0 ? secs + ' s' : 'a few seconds') + '.';
        root.dataset.state = 'busy';
      } else {
        err.textContent = 'The check failed: ' + (r.status ? 'HTTP ' + r.status : 'no connection') +
          (r.body && (r.body.error || r.body.detail) ? ' — ' + (r.body.error || JSON.stringify(r.body.detail)) : '') + '.';
        root.dataset.state = 'error';
      }
      err.hidden = false;
    });
  }

  function render(box, d) {
    const sample = d.sample || {};
    const fig = el('figure', 'sc-card');
    const img = el('img', 'sc-img');
    img.alt = 'Synthetic certificate card: ' + (sample.label || sample.id || '');
    img.src = sample.image_url || '';
    img.addEventListener('error', () => { img.hidden = true; fig.classList.add('sc-noimg'); });
    const cap = el('figcaption', 'sc-cap', (sample.label || sample.id || '') + ' · synthetic card, fictional person');
    cap.dataset.slot = 'sc-sample';
    fig.append(img, cap);

    const readings = d.readings || [];
    const table = el('table', 'sc-table');
    const thead = el('thead');
    const hr = el('tr');
    hr.append(el('th', null, ''));
    readings.forEach(r => {
      const th = el('th', null, r.reader || r.model);
      th.dataset.slot = 'sc-reader';
      th.title = r.model || '';
      hr.append(th);
    });
    thead.append(hr);
    const tbody = el('tbody');
    const rows = [
      ['Name', r => r.name],
      ['Issued', r => r.issued || r.issued_raw],
      ['Expires', r => r.expires || r.expires_raw],
      ['Card number', r => r.card_number],
      ['Seconds', r => r.seconds_label],
      ['Cost', r => r.cost_label || (r.ok ? 'not recorded' : null)],
    ];
    rows.forEach(([label, get]) => {
      const tr = el('tr');
      tr.dataset.row = label.toLowerCase().replace(/\s+/g, '-');
      tr.append(el('th', null, label));
      readings.forEach(r => {
        const td = el('td', null, r.ok ? dash(get(r)) : (label === 'Issued' ? 'reader failed' : '—'));
        if (!r.ok) td.classList.add('sc-failed');
        if ((label === 'Issued' || label === 'Expires') && d.dates_agree === false &&
            readings.every(x => x.ok) && new Set(readings.map(get)).size > 1) td.classList.add('sc-differs');
        tr.append(td);
      });
      tbody.append(tr);
    });
    table.append(thead, tbody);
    const failures = readings.filter(r => !r.ok && r.error);
    const failNote = failures.length ? el('p', 'sc-failnote', failures.map(r => (r.reader || r.model) + ': ' + r.error).join(' · ')) : null;
    if (failNote) failNote.dataset.slot = 'sc-failnote';

    const verdict = el('p', 'sc-verdict', d.verdict);
    verdict.dataset.slot = 'sc-verdict';
    verdict.dataset.outcome = d.outcome || '';
    const failed = (d.flags || []).some(f => f.indexOf('reader_failed:') === 0);
    verdict.classList.add(d.outcome === 'accepted' ? 'is-safe' : failed ? 'is-risk' : 'is-fragile');

    const notes = [];
    if (d.card && d.card.expired) notes.push('The card itself ran out on ' + d.card.expires_on + ': the rules would not put this person on a shift.');
    if ((d.notes || []).indexOf('check_id') >= 0) notes.push('The card numbers differ: noted for a person, not a block.');
    if (d.applicant) notes.push('Read for ' + d.applicant.name + ', a fictional applicant who is not on the roster.');
    const noteBox = el('p', 'sc-note', notes.join(' '));
    noteBox.dataset.slot = 'sc-note';

    const proof = el('p', 'sc-proof');
    proof.dataset.slot = 'sc-proof';
    proof.append(el('strong', null, 'Proof: '));
    const parts = (d.ledger || []).map(row => (row.ok
      ? [row.model, row.time_label, row.seconds_label, row.cost_label || 'price not recorded', row.source_label]
      : [row.model, row.time_label, 'failed, no answer']).filter(Boolean).join(' · '));
    proof.append(document.createTextNode(parts.join('  |  ') +
      (d.total_cost_label ? '  |  total ' + d.total_cost_label : '')));

    const right = el('div', 'sc-right');
    right.append(table);
    if (failNote) right.append(failNote);
    right.append(verdict, noteBox, proof);
    box.replaceChildren(fig, right);
  }

  function auto() {
    const root = document.getElementById('sample-cert');
    if (root) mount(root);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', auto);
  else auto();

  global.CoachSampleCert = { mount };
})(window);
