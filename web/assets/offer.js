/* Screen P2: GET /api/offers/{id} → slots of offer.html; Accept / Decline → POST, 409 shows why the offer no longer fits. */
(function () {
  'use strict';
  const CC = window.CC;
  const $ = sel => document.querySelector(sel);
  const setText = (name, value) => { const el = document.querySelector('[data-slot="' + name + '"]'); if (el) el.textContent = value == null ? '' : String(value); };

  const offerId = CC.params.get('id') || '';
  const state = { offer: null, timer: null, answered: false };

  const CHECK = '<svg viewBox="-22.5 -22.5 45 45" aria-hidden="true"><circle r="22.5" fill="var(--gold)"/>' +
    '<polyline points="-9.2,0.3 -3,6.2 9,-6" fill="none" stroke="#fff" stroke-width="6.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const RESULT_ICON = {
    ok: '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#06545A" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 10.5l4 4L16.5 5"/></svg>',
    neutral: '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#5f6164" stroke-width="2.4" stroke-linecap="round"><circle cx="10" cy="10" r="7.5"/><path d="M6.5 10h7"/></svg>',
    bad: '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="9.5" fill="#A83A2F"/><path d="M6.6 6.6l6.8 6.8M13.4 6.6l-6.8 6.8" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg>'
  };

  function showError(text) {
    const e = $('#loadErr');
    e.textContent = text;
    e.hidden = !text;
  }

  function showResult(tone, text) {
    const r = $('#result');
    r.className = 'result ' + tone;
    r.innerHTML = RESULT_ICON[tone];
    const t = document.createElement('span');
    t.dataset.slot = 'offer-result';
    t.textContent = text;
    r.appendChild(t);
    r.hidden = false;
    $('#buttons').hidden = true;
  }

  const RING = 2 * Math.PI * 137.3;
  function paint(left, windowS) {
    setText('expires-in', Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0'));
    const ring = document.querySelector('[data-slot="expires-ring"]');
    const share = windowS > 0 ? Math.max(0, Math.min(1, left / windowS)) : 0;
    ring.setAttribute('stroke-dasharray', (RING * share).toFixed(1) + ' ' + RING.toFixed(1));
  }

  function startTimer(o) {
    const start = Math.max(0, Math.floor(Number(o.expires_in_s) || 0));
    const windowS = Number(o.window_s) || start || 1;
    paint(start, windowS);
    // Under browser automation (visual diff, DOM check) the clock holds its start value so snapshots are stable;
    // add &tick to the URL to run it there too.
    const hold = navigator.webdriver && !/[?&]tick\b/.test(location.search);
    if (hold) return;
    const t0 = performance.now();
    clearInterval(state.timer);
    state.timer = setInterval(() => {
      const left = Math.max(0, start - Math.floor((performance.now() - t0) / 1000));
      paint(left, windowS);
      if (left === 0) {
        clearInterval(state.timer);
        document.querySelector('.screen').dataset.state = 'expired';
        if (!state.answered) showResult('neutral', 'This offer has expired.');
      }
    }, 250);
  }

  function render(o) {
    state.offer = o;
    setText('shift-start', o.shift_start);
    setText('shift-end', o.shift_end);
    setText('shift-day', o.shift_day);
    setText('venue', o.venue);
    setText('shift-detail', o.shift_detail);
    const list = document.querySelector('[data-slot="why-list"]');
    list.textContent = '';
    (o.why || []).forEach(text => {
      const li = document.createElement('li');
      li.className = 'why-row';
      li.dataset.slot = 'why-row';
      li.innerHTML = CHECK;
      const span = document.createElement('span');
      span.className = 'why-text';
      span.dataset.slot = 'why-text';
      span.textContent = text;
      li.appendChild(span);
      list.appendChild(li);
    });
    setText('manager-name', o.manager && o.manager.name);
    setText('manager-role', o.manager && o.manager.role);
    startTimer(o);
  }

  async function load() {
    if (!offerId) {
      showError('No offer id in the link (offer.html?id=…).');
      document.body.dataset.state = 'error';
      return;
    }
    const r = await CC.get('/api/offers/' + encodeURIComponent(offerId));
    if (r.status === 200 && r.body && typeof r.body === 'object') {
      render(r.body);
      showError('');
      document.body.dataset.state = 'ready';
    } else {
      showError('Could not load this offer — ' + CC.errorText(r));
      document.body.dataset.state = 'error';
    }
  }

  async function answer(action) {
    const btns = document.querySelectorAll('[data-action]');
    btns.forEach(b => { b.disabled = true; });
    const r = await CC.post('/api/offers/' + encodeURIComponent(offerId) + '/' + action, {});
    const o = state.offer || {};
    const when = (o.shift_start || '') + '–' + (o.shift_end || '') + (o.shift_day ? ' ' + o.shift_day : '');
    if (r.status === 200) {
      state.answered = true;
      clearInterval(state.timer);
      const st = r.body && r.body.state;
      if (st === 'accepted' || (action === 'accept' && !st)) showResult('ok', 'Accepted. You cover ' + when + '.');
      else if (st === 'declined' || (action === 'decline' && !st)) showResult('neutral', 'Declined. Thank you for answering.');
      else showResult('neutral', 'Offer is ' + String(st).replace(/_/g, ' ') + '.');
      showError('');
    } else if (r.status === 409) {
      state.answered = true;
      clearInterval(state.timer);
      const reason = (r.body && (r.body.reason || r.body.detail)) || 'the check at your answer failed';
      showResult('bad', 'You are no longer eligible for this shift: ' + reason);
      showError('');
    } else {
      showError('Your answer was not sent — ' + CC.errorText(r) + '. Please try again.');
      btns.forEach(b => { b.disabled = false; });
    }
  }

  document.querySelectorAll('[data-action]').forEach(b => b.addEventListener('click', () => answer(b.dataset.action)));
  load();
})();
