/* Network · borrow from another venue: GET /api/network/loans/{ref} (+ /api/network for its name, /api/today for the
 * date and manager) → slots of network.html. Each manager approves for their own venue: POST …/approve
 * {manager, venue}; 403 and 409 are shown in that manager's card. Without ?loan= the page offers to ask the network
 * for today's open shift: POST /api/network/loans {shift_id}. Data strings go in as text only. */
(function () {
  'use strict';
  const CC = window.CC;
  const $ = s => document.querySelector(s);
  const slot = name => document.querySelector('[data-slot="' + name + '"]');
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const withFx = href => href + (CC.FIXTURE ? (href.includes('?') ? '&' : '?') + 'fixture=1' : '');
  const loanRef = CC.params.get('loan') || '';

  const WAVES = '<svg width="61" height="39" viewBox="0 0 61 39" fill="none" stroke="#0B6467" stroke-width="4.4" stroke-linecap="round"><path d="M2.2 5q5.7-5.6 11.32 0t11.32 0 11.32 0 11.32 0 11.32 0"/><path d="M2.2 19.5q5.7-5.6 11.32 0t11.32 0 11.32 0 11.32 0 11.32 0"/><path d="M2.2 34q5.7-5.6 11.32 0t11.32 0 11.32 0 11.32 0 11.32 0"/></svg>';
  const CHECK = '<svg class="chk" width="36" height="36" viewBox="0 0 36 36"><circle cx="18" cy="18" r="18" fill="#A47A25"/><path d="M10.5 18.6 15.6 23.6 25.6 13.2" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const arrowRight = y => '<svg class="arrow" width="120" height="' + (y * 2 + 2) + '" aria-hidden="true"><g fill="none" stroke="#156C6B" stroke-linecap="round" stroke-linejoin="round"><path d="M1 ' + y + 'H101" stroke-width="2.2"/><path d="M93 ' + (y - 8) + ' 101.3 ' + y + ' 93 ' + (y + 8) + '" stroke-width="3"/></g></svg>';
  const arrowLeft = y => '<svg class="arrow wide" width="789" height="' + (y * 2 + 2) + '" aria-hidden="true"><g fill="none" stroke="#156C6B" stroke-linecap="round" stroke-linejoin="round"><path d="M450 ' + y + 'H5" stroke-width="1.9"/><path d="M13 ' + (y - 8) + ' 4.7 ' + y + ' 13 ' + (y + 8) + '" stroke-width="3"/><path d="M450 ' + (y - 6) + 'V' + (y + 6) + '" stroke-width="2"/></g></svg>';
  const RESULT_ICON = { ok: '<path d="M11.5 20 17 25.5 28 14.5" fill="none" stroke="#A47A25" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>',
                        wait: '<path d="M19.5 10v10l6 4" fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>',
                        fail: '<path d="M13 13l13 13M26 13 13 26" fill="none" stroke="#fff" stroke-width="3.6" stroke-linecap="round"/>' };

  // steps that are a message between the two agents; the rest stay in the record (and in the case)
  const MESSAGE_KINDS = new Set(['request_sent', 'offered', 'checked_offer', 'declined', 'offer_withdrawn', 'confirm_refused',
                                 'lender_unreachable', 'failed']);
  const short = name => String(name || '').split(' ')[0];
  const tail = title => String(title || '').replace(/^\S+ agent:?\s*/, '');
  const ui = { loan: null, network: null, today: null, errors: {} };

  // The manager's name keeps its chevron 11 px after it (as in the design) for any name length; when the chevron
  // would come closer than 27 px to the bar's edge, the date, divider, name and chevron move left together.
  function fitUserLabel() {
    const bar = $('.topbar'), label = slot('user-label'), chevron = label.nextElementSibling;
    const kids = [...bar.children], group = kids.slice(kids.indexOf(slot('today-date')) - 1);
    group.forEach(n => { n.style.transform = ''; });
    chevron.style.left = (label.offsetLeft + label.offsetWidth + 11) + 'px';
    const over = parseFloat(chevron.style.left) + chevron.getBoundingClientRect().width + 27 - bar.clientWidth;
    if (over > 0) group.forEach(n => { n.style.transform = 'translateX(' + (-Math.ceil(over)) + 'px)'; });
  }

  function renderHeader() {
    const t = ui.today, l = ui.loan;
    slot('pool-name').textContent = (l && l.borrower && l.borrower.venue_name) || (t && t.venue && t.venue.name) || '';
    slot('today-date').textContent = t && t.now_label ? String(t.now_label).split(' · ')[0] : '';
    slot('user-label').textContent = (t && t.user_label) || '';
    fitUserLabel();
    const net = ui.network && (ui.network.networks || []).find(n => !l || n.id === l.network_id);
    slot('network-name').textContent = net ? net.name : '';
    const role = l && l.shift ? String(l.shift.label || '').split(' · ')[1] : null;
    slot('crumb').textContent = 'Network · borrow ' + (role ? 'a ' + role.toLowerCase() : 'from another venue');
    document.querySelectorAll('[data-nav]').forEach(a => {
      const base = a.getAttribute('href').split('?')[0];
      const caseId = (l && l.case && l.case.ref) || (t && t.callout && t.callout.case_id);
      a.href = withFx(base === 'case.html' && caseId ? 'case.html?id=' + encodeURIComponent(caseId) : base);
    });
    slot('policy-note').textContent = "Each agent only acts within its own pool's policy. Nothing moves without both managers.";
    slot('footer-line').textContent = "CoachCover · Running on Vultr.";
  }

  function agentCard(lane, name, facts, status) {
    const a = el('article', 'agent lane-' + lane + (status ? ' has-status' : ''));
    a.dataset.slot = 'agent-card';
    const tile = el('div', 'tile'); tile.innerHTML = WAVES; a.appendChild(tile);
    const txt = el('div', 'txt');
    txt.appendChild(Object.assign(el('h2', null, name), { title: name })).dataset.slot = 'agent-name';
    const meta = el('div', 'meta');
    facts.filter(Boolean).forEach((f, i, all) => {
      const s = el('span', 'fact', f + (status || i < all.length - 1 ? ' ·' : '')); s.dataset.slot = 'agent-fact';
      meta.appendChild(s); meta.appendChild(document.createTextNode(' '));
    });
    txt.appendChild(meta);
    if (status) { const st = el('div', 'status', status); st.dataset.slot = 'agent-status'; txt.appendChild(st); }
    a.appendChild(txt);
    return a;
  }

  function renderAgents() {
    const box = $('#agents');
    box.textContent = '';
    const l = ui.loan, t = ui.today;
    const steps = (l && l.steps) || [];
    const asked = steps.find(s => s.kind === 'asked_network');
    const own = steps.find(s => s.kind === 'checked_own_people');
    const bName = short((l && l.borrower && l.borrower.venue_name) || (t && t.venue && t.venue.name));
    const members = (ui.network && ui.network.members) || [];
    const lenderMember = members.find(m => !l || !l.borrower || m.venue_id !== l.borrower.venue_id);
    const lName = short((l && l.lender && l.lender.venue_name) || (lenderMember && lenderMember.venue_name));
    const shift = l && l.shift ? l.shift : null;
    const left = shift ? [shift.time_label, 'Needs: ' + (String(shift.label).split(' · ')[1] || '')]
      : t && t.shift ? [t.shift.time_label, 'Needs: ' + t.shift.needs_label] : [];
    box.appendChild(agentCard('left', bName + ' agent', left, asked ? tail(asked.title) : null));
    box.appendChild(agentCard('right', lName + ' agent', [own ? tail(own.title) : 'Checks its own rules before offering anyone'], null));
  }

  function messageCard(s, l) {
    const lane = s.side === 'lender' ? 'right' : 'left';
    const from = s.venue || (lane === 'left' ? short(l.borrower.venue_name) : short(l.lender && l.lender.venue_name));
    const to = s.kind === 'request_sent' || s.kind === 'confirm_sent' ? short(l.lender && l.lender.venue_name) : null;
    const card = el('article', 'msg c-' + lane);
    card.dataset.slot = 'message';
    card.dataset.kind = s.kind;
    card.appendChild(Object.assign(el('div', 'head', s.at_label + ' · ' + from + (to ? ' → ' + to : '') + ':'), { title: s.at }))
      .dataset.slot = 'message-head';
    let facts;
    if (s.kind === 'request_sent') {
      const asked = (l.steps || []).find(x => x.kind === 'asked_network');
      facts = ['request cover'].concat(asked ? String(asked.detail).split(' · ').slice(0, 3) : []);
    } else {
      facts = [tail(s.title)].concat(String(s.detail || '').split(' · ').filter(Boolean));
    }
    facts = facts.filter((f, i) => f && facts.indexOf(f) === i);
    const body = el('div', 'body'); body.dataset.slot = 'message-body';
    facts.forEach((f, i) => { body.appendChild(el('span', 'fact', f + (i < facts.length - 1 ? ' ·' : ''))); body.appendChild(document.createTextNode(' ')); });
    card.appendChild(body);
    return { lane, card };
  }

  function approvalCard(lane, who, l) {
    const venue = who.venue_name;
    if (who.approved) {
      const a = el('article', 'approval c-' + lane);
      a.dataset.slot = 'approval';
      a.innerHTML = CHECK;
      const t = el('span', 't');
      const b = el('b', null, who.approved.at_label); b.dataset.slot = 'approval-time';
      const rest = el('span', null, ' · ' + who.approved.by + ' (' + short(venue) + ') approved'); rest.dataset.slot = 'approval-manager';
      t.append(b, rest);
      a.appendChild(t);
      return a;
    }
    const a = el('article', 'approval wait c-' + lane);
    a.dataset.slot = 'approval-wait';
    const other = lane === 'left' ? l.lender : l.borrower;
    const wait = el('span', 'who');
    wait.append('waiting for ', el('b', null, who.manager), ' (' + short(venue) + ')');
    a.appendChild(wait);
    const btn = el('button', 'abtn', 'Approve as ' + who.manager);
    btn.type = 'button';
    btn.dataset.slot = 'approve-' + (lane === 'left' ? 'borrower' : 'lender');
    btn.addEventListener('click', () => approve(who, btn));
    a.appendChild(btn);
    const err = ui.errors[who.venue_id];
    if (err) { a.classList.add('has-err'); const e = el('span', 'err', err); e.dataset.slot = 'approve-error'; a.appendChild(e); }
    a.title = other && other.approved ? 'One approval changes nothing: the loan waits for ' + who.manager : '';
    return a;
  }

  function renderTimeline() {
    const box = $('#timeline');
    box.textContent = '';
    const l = ui.loan;
    if (!l) return;
    const cards = (l.steps || []).filter(s => MESSAGE_KINDS.has(s.kind)).map(s => messageCard(s, l));
    cards.forEach((c, i) => {
      const row = el('div', 'row r-' + c.lane);
      row.appendChild(c.card);
      const next = cards[i + 1], prev = cards[i - 1];
      if (c.lane === 'left' && next && next.lane === 'right') row.insertAdjacentHTML('beforeend', arrowRight(41.5));
      else if (c.lane === 'left' && prev && prev.lane === 'right') row.insertAdjacentHTML('beforeend', arrowLeft(40.5));
      box.appendChild(row);
    });
    if (l.lender && (l.status === 'awaiting_approval' || l.status === 'assigned')) {
      const row = el('div', 'row r-approvals');
      row.appendChild(approvalCard('left', l.borrower, l));
      row.appendChild(approvalCard('right', l.lender, l));
      if (l.borrower.approved && l.lender.approved) row.insertAdjacentHTML('beforeend', arrowRight(35.5));
      box.appendChild(row);
    }
  }

  function renderResult() {
    const r = $('#result'), l = ui.loan;
    r.hidden = !l;
    if (!l) return;
    const t = $('#result-text');
    t.textContent = '';
    let tone;
    if (l.status === 'assigned') {
      tone = 'ok';
      const start = String((l.shift && l.shift.time_label) || '').split(' ').pop().split('–')[0];
      t.append(el('span', null, l.status_label), ' · ', el('span', null, (l.candidate && l.candidate.name) || ''),
        ' covers ' + short(l.borrower.venue_name) + ' ' + start, ' · case ', el('span', null, (l.case && l.case.ref) || ''));
      r.href = l.case && l.case.ref ? withFx('case.html?id=' + encodeURIComponent(l.case.ref) + (CC.FIXTURE ? '&mock=network_loan' : '')) : '#';
    } else if (l.status === 'awaiting_approval') {
      tone = 'wait';
      const waiting = [l.borrower, l.lender].filter(x => x && !x.approved);
      t.textContent = waiting.length === 1 ? 'Waiting for ' + waiting[0].manager + ' (' + short(waiting[0].venue_name) + ')' : l.status_label;
      r.removeAttribute('href');
    } else {
      tone = 'fail';
      const why = (l.steps || []).slice().reverse().find(s => s.kind === 'lender_declined' || s.kind === 'checked_offer' || s.kind === 'lender_unreachable');
      t.textContent = l.status_label + (why && why.detail ? ' · ' + why.detail : '');
      r.removeAttribute('href');
    }
    r.className = 'result' + (tone === 'ok' ? '' : ' ' + tone);
    r.dataset.state = l.status;
    r.querySelector('.rchk').innerHTML = '<circle cx="19.5" cy="19.5" r="19.5" fill="#fff"/>' + RESULT_ICON[tone];
    t.title = t.textContent;
  }

  function renderAsk() {
    const ask = $('#ask');
    ask.hidden = !!ui.loan;
    if (ui.loan) return;
    const t = ui.today;
    const s = t && t.shift;
    const open = t && t.outcome && !t.outcome.covered_by;
    slot('ask-text').textContent = s ? (open ? 'Nobody is covering ' + s.time_label + ' at ' + ((t.venue && t.venue.name) || '') + ' yet.' :
      s.time_label + ' is covered; the network is asked only when nobody here is left.') : 'No shift to ask about.';
    $('#askBtn').disabled = !s || !open;
  }

  function lanesHeight() {
    // the dashed lane lines run from the agent cards to the result
    const lanes = $('.lanes'), svg = $('.lane-lines');
    const h = lanes.offsetHeight;
    svg.setAttribute('height', h);
    const bottom = ($('#result').hidden ? $('#ask') : $('#result')).offsetTop - 10;
    svg.querySelectorAll('path').forEach((p, i) => p.setAttribute('d', 'M' + (i ? 1205.5 : 401) + ' 123V' + Math.max(123, bottom)));
  }

  function render() {
    renderHeader();
    renderAgents();
    renderTimeline();
    renderResult();
    renderAsk();
    lanesHeight();
  }

  async function approve(who, btn) {
    btn.disabled = true;
    const r = await CC.post('/api/network/loans/' + encodeURIComponent(ui.loan.code) + '/approve', { manager: who.manager, venue: who.venue_id });
    if (r.status === 200) delete ui.errors[who.venue_id];
    else ui.errors[who.venue_id] = 'Not approved — ' + ((r.body && (r.body.error || r.body.detail)) || CC.errorText(r));
    await loadLoan(ui.loan.code);
  }

  async function ask() {
    const btn = $('#askBtn');
    btn.disabled = true;
    const shift = ui.today && ui.today.shift && ui.today.shift.id;
    const r = await CC.post('/api/network/loans', { shift_id: shift });
    if (r.status === 201 && r.body && r.body.code) {
      location.href = withFx('network.html?loan=' + encodeURIComponent(r.body.code));
      return;
    }
    slot('ask-error').textContent = 'Not asked — ' + ((r.body && r.body.error) || CC.errorText(r));
    btn.disabled = false;
  }

  async function loadLoan(ref) {
    const r = await CC.get('/api/network/loans/' + encodeURIComponent(ref));
    const err = $('#loadErr');
    if (r.status === 200 && r.body && typeof r.body === 'object') {
      ui.loan = r.body;
      err.hidden = true;
      document.body.dataset.state = 'ready';
      render();
    } else {
      err.textContent = 'Could not load loan ' + ref + ' — ' + CC.errorText(r);
      err.hidden = false;
      document.body.dataset.state = ui.loan ? 'ready' : 'error';
    }
  }

  async function load() {
    const [n, t] = await Promise.all([CC.get('/api/network'), CC.get('/api/today')]);
    ui.network = n.status === 200 ? n.body : null;
    ui.today = t.status === 200 ? t.body : null;
    // without ?loan= the page opens the loan of the morning's case, if there is one (the judge's network scene)
    const ref = loanRef || (ui.today && ui.today.network_loan && ui.today.network_loan.code) || '';
    if (ref) return loadLoan(ref);
    ui.loan = null;
    document.body.dataset.state = 'ready';
    render();
  }

  $('#askBtn').addEventListener('click', ask);
  document.fonts.ready.then(() => { if (document.body.dataset.state === 'ready') { fitUserLabel(); lanesHeight(); } });
  load();
})();
