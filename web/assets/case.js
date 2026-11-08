/* Case page: GET /api/cases/{id} → slots of case.html. "Download case (JSON)" saves exactly the JSON shown.
 * Data strings go in through textContent; only fixed icons use innerHTML. */
(function () {
  'use strict';
  const CC = window.CC;
  const $ = sel => document.querySelector(sel);
  const setText = (name, value) => { const el = document.querySelector('[data-slot="' + name + '"]'); if (el) el.textContent = value == null ? '' : String(value); };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const slotted = (e, name) => { e.dataset.slot = name; return e; };

  const ICON_BLOCK = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9.6"/><path d="M5.3 5.3l13.4 13.4"/></svg>';
  const ICON_OK = '<svg viewBox="0 0 27 27" aria-hidden="true"><circle cx="13.5" cy="13.5" r="13" fill="#9A6A1E"/><circle cx="13.5" cy="13.5" r="11.6" fill="#A87A2A"/><path d="M8 14l3.8 3.6L19.2 10" fill="none" stroke="#fff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const DEFAULT_TITLE = { callout: 'Voice call-out', shift: 'Shift identified', allowed: 'Who is allowed', offers: 'Offers in order', covered: 'Schedule covered', plan_b: 'Plan B' };

  const caseId = CC.params.get('id') || '';
  const present = v => typeof v === 'number' && isFinite(v) && v > 0;
  const fmtUsd = v => v >= 1 ? '$' + (Number.isInteger(v) || v >= 100 ? Math.round(v) : v.toFixed(2)) : '$' + Number(v.toPrecision(2));
  const shortHash = h => (h && h.length > 12) ? h.slice(0, 4) + '…' + h.slice(-4) : (h || '');
  const humanResult = r => String(r || '').replace(/_/g, ' ');

  /* ---------- steps ---------- */
  function line(cls, text, slot) { return slotted(el('div', 's-line ' + cls, text), slot); }

  // a chip's status: the answer's tone (risk / fragile) when it has one; the code groups below are only a fallback
  const RISK_CODES = new Set(['CERT_EXPIRED', 'CERT_EXPIRES_DURING_SHIFT', 'CERT_NOT_YET_VALID', 'CERT_REVOKED', 'RATIO_EXCEEDED']);
  const FRAGILE_CODES = new Set(['CERT_NEEDS_REVIEW', 'PROGRAM_RENEWAL_OVERDUE', 'REST_TOO_SHORT']);
  function toneClass(r) {
    const t = String(r.tone || '').toLowerCase();
    if (t === 'risk' || t === 'red' || (!t && RISK_CODES.has(r.code))) return ' tone-risk';
    if (t === 'fragile' || t === 'amber' || (!t && FRAGILE_CODES.has(r.code))) return ' tone-fragile';
    return '';
  }

  const BODY = {
    callout(s, body) {
      if (s.transcript_original) body.appendChild(line('first wrap', '“' + s.transcript_original + '”', 'callout-original'));
      if (s.transcript_en) body.appendChild(line('tr wrap', '“' + s.transcript_en + '”', 'callout-translation'));
      if (s.model_tag) body.appendChild(slotted(el('span', 'tag', s.model_tag), 'model-tag'));
    },
    shift(s, body) {
      if (s.summary) body.appendChild(line('first', s.summary, 'shift-summary'));
      if (s.model_tag) body.appendChild(slotted(el('span', 'tag', s.model_tag), 'model-tag'));
    },
    allowed(s, body) {
      const summary = s.summary || (s.total != null && s.allowed != null ? s.total + ' staff → ' + s.allowed + ' allowed' : '');
      if (summary) body.appendChild(line('sum', summary, 'allowed-summary'));
      if (s.blocked && (s.blocked.staff || s.blocked.label)) {
        const row = slotted(el('div', 'blocked'), 'blocked-row');
        row.innerHTML = ICON_BLOCK;
        const t = el('span');
        if (s.blocked.staff) { t.appendChild(slotted(el('span', null, s.blocked.staff), 'blocked-name')); t.appendChild(document.createTextNode(' · ')); }
        t.appendChild(slotted(el('span', null, s.blocked.label || ''), 'blocked-reason'));
        row.appendChild(t);
        body.appendChild(row);
      }
      const chips = el('div', 'reasons');
      (s.reasons || []).forEach(r => {
        const c = slotted(el('span', 'reason' + toneClass(r), r.label + ' '), 'reason-chip');
        c.appendChild(slotted(el('span', null, '×' + r.count), 'reason-count'));
        chips.appendChild(c);
      });
      if (chips.children.length) body.appendChild(chips);
    },
    offers(s, body) {
      (s.offers || []).forEach(o => {
        const accepted = o.result === 'accepted';
        const status = accepted ? ' accepted' : o.result === 'declined' ? ' declined' : (o.result === 'no_reply' || o.result === 'expired') ? ' noreply' : '';
        const row = slotted(el('div', 'offer' + status), 'offer-row');
        row.dataset.offerId = o.id || '';
        if (accepted) row.innerHTML = ICON_OK;
        const t = el('span');
        t.appendChild(slotted(el('span', null, o.staff), 'offer-name'));
        t.appendChild(document.createTextNode(' — '));
        t.appendChild(slotted(el('span', null, o.result_label || humanResult(o.result)), 'offer-result'));
        if (accepted && o.why_first) {
          t.appendChild(document.createTextNode(' · '));
          t.appendChild(slotted(el('span', null, o.why_first), 'offer-why'));
        }
        row.appendChild(t);
        body.appendChild(row);
      });
    },
    covered(s, body) { if (s.summary) body.appendChild(line('first wrap', s.summary, 'covered-summary')); },
    plan_b(s, body) { if (s.summary) body.appendChild(line('first wrap', s.summary, 'plan-summary')); }
  };

  function renderSteps(steps) {
    const box = $('#steps');
    box.textContent = '';
    steps.forEach(s => {
      const kind = BODY[s.kind] ? s.kind : 'covered';
      const step = slotted(el('div', 'step'), 'step');
      step.dataset.kind = s.kind || '';
      step.appendChild(slotted(el('div', 's-time', s.time_label || ''), 'step-time'));
      step.appendChild(el('span', 's-dot'));
      const body = el('div', 's-body');
      body.appendChild(slotted(el('div', 's-title', s.title || DEFAULT_TITLE[s.kind] || humanResult(s.kind)), 'step-title'));
      BODY[kind](s, body);
      step.appendChild(body);
      box.appendChild(step);
    });
    placeRail();
  }

  // the rail runs from the first dot to the last; placed again once the fonts are in (text can wrap differently)
  function placeRail() {
    const dots = $('#steps').querySelectorAll('.s-dot');
    const rail = $('#rail');
    if (dots.length > 1) {
      const centre = d => d.parentElement.offsetTop + d.offsetTop + 10;
      rail.style.top = centre(dots[0]) + 'px';
      rail.style.height = (centre(dots[dots.length - 1]) - centre(dots[0])) + 'px';
      rail.hidden = false;
    } else rail.hidden = true;
  }

  /* ---------- proof + money ---------- */
  function renderProof(d) {
    const p = d.proof || {};
    setText('plan-hash', shortHash(p.plan_hash));
    $('#factHash').hidden = !p.plan_hash;
    if (p.plan_hash) $('#factHash').title = p.plan_hash;
    setText('policy', p.policy_label);
    $('#factPolicy').hidden = !p.policy_label;
    const calls = d.model_calls || [];
    setText('models-calls', calls.length + (calls.length === 1 ? ' call' : ' calls'));
    const cost = d.model_cost_label || (present(d.model_cost_usd) ? fmtUsd(d.model_cost_usd) : '');
    setText('models-cost', cost);
    $('#costSep').hidden = !cost;
    // one row per model call: the step's time, why, seconds and cents; below it the model, where the answer came
    // from and "shared by N cases" (a call-out's call split between the cases it opened); the last line adds up
    const tbody = $('#calls');
    tbody.textContent = '';
    calls.forEach(c => {
      const tr = slotted(el('tr', 'call'), 'call-row');
      tr.dataset.usd = present(c.cost_usd) ? String(c.cost_usd) : '';
      tr.appendChild(slotted(el('td', 'c-time', c.time_label || ''), 'call-time'));
      tr.appendChild(slotted(el('td', 'c-what', c.purpose_label || c.purpose || ''), 'call-purpose'));
      tr.appendChild(slotted(el('td', 'c-sec', c.seconds_label || ''), 'call-seconds'));
      tr.appendChild(slotted(el('td', 'c-cost', c.cost_label || ''), 'call-cost'));
      tbody.appendChild(tr);
      const meta = slotted(el('tr', 'call-meta'), 'call-meta');
      const td = el('td', null, [c.model_label || c.model, c.source_label, c.shared_label].filter(Boolean).join(' · '));
      td.colSpan = 4;
      meta.appendChild(td);
      tbody.appendChild(meta);
    });
    if (calls.length) {
      const total = slotted(el('tr', 'call-total'), 'calls-total');
      const head = el('td', null, 'Total');
      const sum = slotted(el('td', 'c-cost', d.model_calls_total_label || ''), 'calls-total-label');
      sum.colSpan = 3;
      total.appendChild(head);
      total.appendChild(sum);
      tbody.appendChild(total);
    }
    tbody.parentElement.hidden = calls.length === 0;
  }

  function renderRisk(d) {
    const r = d.risk || {};
    const money = present(r.refunds_usd) || present(r.overtime_usd) || present(r.total_usd);
    $('#risk').hidden = !money;
    if (!money) return;
    $('#riskKids').hidden = !present(r.kids);
    $('#riskRefunds').hidden = !present(r.refunds_usd);
    $('#riskOvertime').hidden = !present(r.overtime_usd);
    $('#riskTotal').hidden = !present(r.total_usd);
    setText('risk-kids', present(r.kids) ? r.kids : '');
    setText('risk-refunds', present(r.refunds_usd) ? fmtUsd(r.refunds_usd) : '');
    setText('risk-overtime', present(r.overtime_usd) ? fmtUsd(r.overtime_usd) : '');
    setText('risk-total', present(r.total_usd) ? fmtUsd(r.total_usd) : '');
    // the first part shown has no separator in front (a private lesson shows no child count)
    const parts = ['#riskKids', '#riskRefunds', '#riskOvertime', '#riskTotal'].map(sel => $(sel));
    const first = parts.find(x => !x.hidden);
    parts.forEach(x => { const sep = x.querySelector('.rsep'); if (sep) sep.hidden = x === first; });
    // what the sums rest on: the cost lines and where the lesson price comes from (internal message refs dropped)
    const lines = (r.lines || []).map(l => l.label).filter(Boolean);
    // a private lesson's price has its own source
    const lp = r.assumptions && ((r.private && r.assumptions.private_lesson_price) || r.assumptions.lesson_price);
    const src = lp && lp.source ? String(lp.source).replace(/\s*\(TG \d+\)\s*$/, '') : '';
    const note = lines.concat(src ? [src] : []).join(' · ');
    setText('risk-source', note);
    document.querySelector('[data-slot="risk-source"]').hidden = !note;
  }

  // a long shift name: the title steps down in size (to 26 px) before it would be cut
  function fitTitle() {
    const t = $('.case-title');
    t.style.fontSize = '';
    for (let size = 34; size >= 26 && t.scrollWidth > t.clientWidth + 1; size -= 1) t.style.fontSize = size + 'px';
  }

  function render(d) {
    setText('facility', d.venue && d.venue.name);
    setText('date', d.date_label);
    setText('user', d.user_label);
    setText('case-id', d.case_id);
    setText('case-shift', d.shift_label);
    document.title = 'CoachCover — Cover case ' + (d.case_id || '');
    setText('status', d.status_label || humanResult(d.status));
    setText('covered-time', d.covered_time_label);
    $('#pillSep').hidden = !d.covered_time_label;
    $('#pill').classList.toggle('open', d.status !== 'covered');
    renderSteps(d.steps || []);
    fitTitle();
    renderProof(d);
    renderRisk(d);
    const dl = $('#download');
    const text = JSON.stringify(d, null, 2) + '\n';
    if (dl.dataset.url) URL.revokeObjectURL(dl.dataset.url);
    dl.dataset.url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    dl.href = dl.dataset.url;
    dl.download = 'case-' + (d.case_id || caseId) + '.json';
    dl.setAttribute('aria-disabled', 'false');
  }

  async function load() {
    const err = $('#loadErr');
    if (!caseId) {
      err.textContent = 'No case id in the link (case.html?id=…).';
      err.hidden = false;
      document.body.dataset.state = 'error';
      return;
    }
    const r = await CC.get('/api/cases/' + encodeURIComponent(caseId));
    if (r.status === 200 && r.body && typeof r.body === 'object') {
      render(r.body);
      err.hidden = true;
      document.body.dataset.state = 'ready';
    } else {
      err.textContent = 'Could not load case ' + caseId + ' — ' + CC.errorText(r);
      err.hidden = false;
      document.body.dataset.state = 'error';
    }
  }

  document.fonts.ready.then(() => { if (document.body.dataset.state === 'ready') { fitTitle(); placeRail(); } });
  document.querySelectorAll('.nav li[data-href]').forEach(li => li.addEventListener('click', () => {
    location.href = li.dataset.href + (CC.FIXTURE ? '?fixture=1' : '');
  }));
  load();
  CC.fillFooter(document.querySelector('[data-slot="footer-line"]'));
})();
