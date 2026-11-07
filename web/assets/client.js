/* The family's page of a private lesson: GET /api/client/{id} → slots of client.html; Yes / No → POST .../respond.
 * A "no" to another coach makes the API open the next question (the family's own coach at a new time): the page moves
 * to it and keeps the thread. 409: the question closed meanwhile (an answer by text) → say so and load it again.
 * Question ids and staff ids stay in the URL and the requests; the page itself never shows them. */
(function () {
  'use strict';
  const CC = window.CC;
  const $ = sel => document.querySelector(sel);
  const setText = (name, value) => { const el = document.querySelector('[data-slot="' + name + '"]'); if (el) el.textContent = value == null ? '' : String(value); };
  const firstName = full => String(full || '').split(' ')[0];
  const upperFirst = s => { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); };
  const lowerFirst = s => { s = String(s || ''); return s.charAt(0).toLowerCase() + s.slice(1); };

  const state = { id: CC.params.get('id') || '', ask: null };

  const RESULT_ICON = {
    ok: '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 10.5l4 4L16.5 5"/></svg>',
    neutral: '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M6.5 10h7"/></svg>'
  };

  // what the family reads once the case has an outcome; the line itself comes from the API
  // (no-break spaces keep "You have a credit" together and the dash off a line start: the balanced strip breaks between sentences)
  const OUTCOME = {
    covered: ['ok', (o, q) => 'Thank you! ' + o.line + '.'],
    moved: ['ok', (o, q) => 'All set. ' + o.line + '.'],
    credit: ['neutral', (o, q) => q.child + "'s lesson is cancelled. You\u00a0have\u00a0" + lowerFirst(o.line).replace(' ', '\u00a0') + '.']
  };
  // an answered question without an outcome yet (the API states asked / yes / no / silent / withdrawn)
  const STATE_TEXT = {
    yes: 'Thank you! We have your answer.',
    no: 'Got it. Thank you for answering.',
    silent: 'This question has closed: the time to answer has passed.',
    withdrawn: 'The school has withdrawn this question.'
  };

  function showError(text) {
    const e = $('#loadErr');
    e.textContent = text;
    e.hidden = !text;
  }

  function strip(el, tone, text) {
    el.className = el.className.replace(/\b(ok|neutral|bad)\b/g, '').replace(/\s+/g, ' ').trim() + ' ' + tone;
    el.innerHTML = RESULT_ICON[tone];
    const t = document.createElement('span');
    t.dataset.slot = el.id + '-text';
    t.textContent = text;
    el.appendChild(t);
    el.hidden = false;
  }

  function showResult(tone, text) {
    strip($('#result'), tone, text);
    $('#buttons').hidden = true;
    $('#answerBy').hidden = true;
  }

  function showNotice(text) {
    if (text) strip($('#notice'), 'neutral', text);
    else $('#notice').hidden = true;
  }

  function renderThread(thread) {
    const list = document.querySelector('[data-slot="thread"]');
    list.textContent = '';
    (thread || []).forEach(m => {
      const family = m.from === 'family';
      const li = document.createElement('li');
      li.className = 'msg ' + (family ? 'family' : 'school');
      li.dataset.slot = 'message';
      li.dataset.from = family ? 'family' : 'school';
      const b = document.createElement('div');
      b.className = 'bubble ' + (family ? 'family' : 'school');
      b.dataset.slot = 'message-text';
      b.textContent = m.text;
      const t = document.createElement('div');
      t.className = 'msg-time';
      t.dataset.slot = 'message-time';
      t.textContent = family ? 'You · ' + m.time_label : m.time_label;
      li.append(b, t);
      list.appendChild(li);
    });
  }

  // the question card, the thread and the footer; buttons only while the question is open
  function render(q) {
    state.ask = q;
    setText('signed', q.signed);
    setText('question', q.question);
    setText('day', upperFirst(q.day_label));
    setText('time', q.time_label);
    setText('venue', q.venue);
    setText('lesson', q.lesson);
    setText('instead-of', q.instead_of);
    document.querySelector('[data-slot="instead-row"]').hidden = !(q.kind === 'cover' && q.instead_of);
    renderThread(q.thread);
    setText('manager-name', q.manager && q.manager.name);
    setText('manager-role', q.manager && q.manager.role);
    document.querySelector('[data-slot="sent-by"]').hidden = !(q.manager && q.manager.name);
    const open = q.state === 'asked';
    setText('answer-by', q.answer_by_label);
    $('#answerBy').hidden = !(open && q.answer_by_label);
    $('#buttons').hidden = !open;
    document.querySelectorAll('[data-action]').forEach(b => { b.disabled = false; });
    if (open) $('#result').hidden = true;
  }

  function showFinal(q) {
    const done = q.outcome && OUTCOME[q.outcome.kind];
    if (done) showResult(done[0], done[1](q.outcome, q));
    else showResult(q.state === 'yes' ? 'ok' : 'neutral', STATE_TEXT[q.state] || 'This question is closed.');
  }

  // what to say when the page moves on to the next question of the same lesson
  function nextNote(next) {
    if (next.kind === 'reschedule') return 'Got it. ' + next.child + ' stays with ' + firstName(next.coach) + '\u00a0— see the new time below.';
    return 'Thanks for answering — there is one more question below.';
  }

  async function fetchAsk(id) {
    const r = await CC.get('/api/client/' + encodeURIComponent(id));
    if (r.status === 200 && r.body && typeof r.body === 'object') return r.body;
    showError(r.status === 404 ? 'This link does not open a question any more. Please use the newest link the school sent you.'
      : 'Could not load this page — ' + CC.errorText(r) + '. Please try again.');
    return null;
  }

  // load a question; an answered one whose case asks something new opens that instead (the thread stays the same story)
  async function load(id) {
    const q = await fetchAsk(id);
    if (!q) { if (!state.ask) document.body.dataset.state = 'error'; return false; }
    let shown = q;
    if (q.state !== 'asked' && q.current && q.current !== q.ask_id) {
      const next = await fetchAsk(q.current);
      if (next) shown = next;
    }
    state.id = shown.ask_id || id;
    render(shown);
    showError('');
    if (shown.state !== 'asked') showFinal(shown);
    document.body.dataset.state = 'ready';
    return shown;
  }

  async function answer(accept) {
    const btns = document.querySelectorAll('[data-action]');
    btns.forEach(b => { b.disabled = true; });
    const r = await CC.post('/api/client/' + encodeURIComponent(state.id) + '/respond', { accept });
    if (r.status === 200 && r.body && typeof r.body === 'object') {
      const q = r.body;
      showError('');
      if (q.state !== 'asked' && q.current && q.current !== q.ask_id) {  // a "no": the next question is open
        const next = await fetchAsk(q.current);
        if (next) {
          state.id = next.ask_id || q.current;
          render(next);
          showNotice(nextNote(next));
          if (next.state !== 'asked') showFinal(next);
          window.scrollTo(0, 0);
          return;
        }
      }
      render(q);
      showNotice('');
      showFinal(q);
    } else if (r.status === 409) {
      showNotice('');
      const shown = await load(state.id);
      if (shown && shown.state === 'asked') showNotice('This question is already closed. Here is the next one.');
      else showResult('neutral', 'This question is already closed.');
    } else {
      showError('Your answer was not sent — ' + CC.errorText(r) + '. Please try again.');
      btns.forEach(b => { b.disabled = false; });
    }
  }

  document.querySelectorAll('[data-action]').forEach(b => b.addEventListener('click', () => answer(b.dataset.action === 'yes')));
  if (!state.id) {
    showError("This link has no question in it. Please open the link from the school's message again.");
    document.body.dataset.state = 'error';
  } else {
    load(state.id);
  }
})();
