/* Week ahead: GET /api/week (+ /api/today for the date and the manager) → slots of week.html.
 * Rows are the start times of the week's shifts, columns its days; a cell with several shifts shows the weakest.
 * Levels, the threshold and the legend come from the answer, never from constants here. */
(function () {
  'use strict';
  const CC = window.CC;
  const $ = s => document.querySelector(s);
  const slot = name => document.querySelector('[data-slot="' + name + '"]');
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const fx = CC.FIXTURE ? 'fixture=1' : '';
  const withFx = href => href + (fx ? (href.includes('?') ? '&' : '?') + fx : '');

  // the screen's level names; the API says at_risk where the approved layout says risk
  const LEVEL = { at_risk: 'risk', fragile: 'fragile', safe: 'safe', past: 'past' };
  const LEVEL_WORD = { at_risk: 'at risk', fragile: 'fragile', safe: 'safe' };
  const backupsLabel = n => n === 1 ? '1 backup' : n + ' backups';
  const startOf = c => String(c.time_label || '').split('–')[0];
  const dayOf = c => String(c.label || '').split(' ')[0];

  // without ?venue= the API answers for every venue of the school; the screen is one venue: the manager's
  function weekPath(today) {
    const q = new URLSearchParams();
    const venue = CC.params.get('venue') || (today && today.venue && today.venue.id);
    if (venue) q.set('venue', venue);
    const start = CC.params.get('start');
    if (start) q.set('start', start);
    const s = q.toString();
    return '/api/week' + (s ? '?' + s : '');
  }

  function renderHeader(w, today) {
    slot('venue-name').textContent = (w.venue && w.venue.name) || '';
    slot('today-date').textContent = today && today.now_label ? String(today.now_label).split(' · ')[0] : (w.week && w.week.label) || '';
    slot('user').textContent = (today && today.user_label) || '';
    document.querySelectorAll('[data-nav]').forEach(a => {
      const base = a.getAttribute('href').split('?')[0];
      const caseId = today && today.callout && today.callout.case_id;
      a.href = withFx(base === 'case.html' && caseId ? 'case.html?id=' + encodeURIComponent(caseId) : base);
    });
  }

  function renderGrid(w) {
    const days = w.days || [];
    const head = slot('day-headers');
    head.querySelectorAll('.dayh').forEach(n => n.remove());
    days.forEach(d => { const h = el('div', 'dayh', d.label); h.dataset.slot = 'day-label'; head.appendChild(h); });

    const starts = [...new Set(days.flatMap(d => (d.cells || []).map(startOf)))].sort();
    const body = slot('cells');
    body.textContent = '';
    // the approved layout: 6 rows in 527 px; more rows share the same height
    const gap = starts.length > 6 ? 6 : 8.5;
    const rowH = Math.min(84, (527 - gap * (starts.length - 1)) / Math.max(1, starts.length));
    body.style.gridTemplateRows = starts.map(() => rowH.toFixed(2) + 'px').join(' ' + gap + 'px ');
    body.style.setProperty('--num-fs', Math.min(41.5, rowH * 0.6).toFixed(1) + 'px');
    body.style.setProperty('--tag-fs', Math.min(15, Math.max(11, rowH * 0.25)).toFixed(1) + 'px');
    const labelFs = Math.min(29.2, rowH * 0.62);

    starts.forEach((t, ri) => {
      const tl = el('div', 'tlabel' + (ri === starts.length - 1 ? ' last' : ''));
      tl.style.gridRow = String(ri * 2 + 1);
      tl.style.fontSize = labelFs.toFixed(1) + 'px';
      const ts = el('span', null, t); ts.dataset.slot = 'slot-time'; tl.appendChild(ts);
      body.appendChild(tl);
      days.forEach((d, ci) => {
        const cells = (d.cells || []).filter(c => startOf(c) === t);
        const live = cells.filter(c => !c.past && typeof c.backups === 'number');
        const weakest = live.slice().sort((a, b) => a.backups - b.backups)[0];
        const c = el('div', 'cell');
        c.style.gridRow = String(ri * 2 + 1);
        c.style.gridColumn = String(ci * 2 + 2);
        c.dataset.slot = 'cell';
        c.dataset.day = d.date;
        c.dataset.time = t;
        if (weakest) {
          c.dataset.level = LEVEL[weakest.level] || 'safe';
          c.dataset.shift = weakest.shift_id;
          c.title = cells.map(x => x.label + ' · ' + (x.past ? 'started' : backupsLabel(x.backups)) +
            (x.holder ? ' · ' + x.holder.name : ' · open')).join('\n');
          const v = el('span', 'num', String(weakest.backups)); v.dataset.slot = 'backups'; c.appendChild(v);
          if (c.dataset.level === 'risk' || c.dataset.level === 'fragile') {
            const tg = el('span', 'tag', c.dataset.level === 'risk' ? 'at risk' : 'fragile'); tg.dataset.slot = 'level-tag'; c.appendChild(tg);
          }
        } else if (cells.length) {
          c.dataset.level = 'past';
          c.title = cells.map(x => x.label + ' · started').join('\n');
          c.appendChild(el('span', 'num', '—'));
        } else {
          c.dataset.level = 'none';
          c.title = 'No shift';
          c.appendChild(el('span', 'num', '—'));
        }
        body.appendChild(c);
      });
    });
  }

  function renderLegend(w) {
    const lg = slot('legend');
    lg.textContent = '';
    // order of the approved layout: safe, fragile, at risk; then the started shifts if the week has any
    const items = (w.legend || []).slice().sort((a, b) => (b.min || 0) - (a.min || 0));
    items.forEach(it => {
      const li = el('div', 'li'); li.dataset.slot = 'legend-item'; li.dataset.level = LEVEL[it.level] || it.level;
      const sw = el('span', 'sw'); sw.dataset.level = LEVEL[it.level] || it.level; li.appendChild(sw);
      li.appendChild(el('span', 'lt', it.label + (LEVEL_WORD[it.level] ? ' (' + LEVEL_WORD[it.level] + ')' : '')));
      lg.appendChild(li);
    });
    if (slot('cells').querySelector('.cell[data-level="none"]')) {
      const li = el('div', 'li'); li.dataset.slot = 'legend-item'; li.dataset.level = 'none';
      const sw = el('span', 'sw', '—'); sw.dataset.level = 'none'; li.appendChild(sw);
      li.appendChild(el('span', 'lt', 'No shift'));
      lg.appendChild(li);
    }
    const hasPast = (w.days || []).some(d => (d.cells || []).some(c => c.past));
    if (hasPast && w.past_legend) {
      const li = el('div', 'li'); li.dataset.slot = 'legend-item'; li.dataset.level = 'past';
      const sw = el('span', 'sw'); sw.dataset.level = 'past'; li.appendChild(sw);
      li.appendChild(el('span', 'lt', w.past_legend.label));
      lg.appendChild(li);
    }
  }

  function renderFragile(w) {
    const ids = new Set(w.fragile || []);
    const cells = (w.days || []).flatMap(d => d.cells || []).filter(c => ids.has(c.shift_id));
    cells.sort((a, b) => a.backups - b.backups || String(a.start).localeCompare(String(b.start)));
    const n = w.counts && typeof w.counts.fragile_total === 'number' ? w.counts.fragile_total : cells.length;
    slot('fragile-count').textContent = n;
    $('.c1 h2').lastChild.textContent = n === 1 ? ' fragile shift' : ' fragile shifts';
    const fl = slot('fragile-list');
    fl.textContent = '';
    const shown = cells.slice(0, 6);
    shown.forEach(c => {
      const li = el('li'); li.dataset.slot = 'fragile-row'; li.dataset.shift = c.shift_id;
      const dot = el('span', 'dot'); dot.dataset.level = LEVEL[c.level] || 'fragile'; li.appendChild(dot);
      const parts = String(c.label || '').split(' · ');
      const row = el('span', 'row');
      const a = el('span', null, dayOf(c) + ' ' + startOf(c)); a.dataset.slot = 'shift';
      const p = el('span', null, parts[2] || c.role_label || parts[1] || ''); p.dataset.slot = 'program';
      const b = el('span', null, backupsLabel(c.backups)); b.dataset.slot = 'backups-label';
      row.append(a, ' · ', p, ' · ', b);
      row.title = c.label + (c.backup_names && c.backup_names.length ? ' · backup: ' + c.backup_names.join(', ') : '');
      li.appendChild(row);
      fl.appendChild(li);
    });
    if (cells.length > shown.length) fl.appendChild(el('li', 'more', '+' + (cells.length - shown.length) + ' more'));
  }

  // a tip's text gets smaller until it fits its box (long shift names come from the data)
  function fitTip(box) {
    const tx = box.querySelector('.tx');
    for (let size = 22.7; size >= 15; size -= 0.5) {
      tx.style.fontSize = size + 'px';
      tx.style.lineHeight = Math.round(size * 1.38) + 'px';
      if (tx.scrollHeight <= box.clientHeight - 14) return;
    }
  }

  function renderTips(w) {
    const tp = slot('tips');
    tp.textContent = '';
    const fixes = w.fixes || {};
    const tips = [];
    const standby = (fixes.standby || [])[0];
    if (standby) tips.push({ text: standby.text, action: 'Propose standby', standby });
    const best = fixes.training && fixes.training.best;
    if (best) tips.push({ text: best.text, note: /^assumption/.test(best.cost_source || '') ? 'course cost is an assumption' : null });
    tips.forEach(t => {
      const box = el('div', 'tip'); box.dataset.slot = 'tip';
      const tx = el('div', 'tx');
      const s = el('span', null, t.text); s.dataset.slot = 'tip-text'; tx.appendChild(s);
      if (t.note) { const n = el('span', 'note', t.note); n.dataset.slot = 'tip-note'; tx.appendChild(n); }
      box.appendChild(tx);
      if (t.action) {
        const btn = el('button', 'btn', t.action); btn.type = 'button'; btn.dataset.slot = 'tip-action';
        btn.dataset.shift = t.standby.shift_id; btn.dataset.staff = t.standby.staff_id;
        // only a proposal: POST /api/standby books nothing; 201 → "Proposed · SB-n", 409 / 404 → the reason
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          const r = await CC.post('/api/standby', { shift_id: t.standby.shift_id, staff_id: t.standby.staff_id });
          let done = tx.querySelector('.done');
          if (!done) { done = el('span', 'done'); done.dataset.slot = 'tip-result'; tx.appendChild(done); }
          if (r.status === 201 && r.body && r.body.id) {
            btn.textContent = 'Proposed · ' + r.body.id;
            done.textContent = 'Nothing is booked until the manager approves.';
          } else {
            btn.disabled = false;
            done.textContent = 'Not proposed — ' + ((r.body && (r.body.error || r.body.detail)) || CC.errorText(r));
          }
          fitTip(box);
        });
        box.appendChild(btn);
      }
      tp.appendChild(box);
      fitTip(box);
    });
  }

  let last = null;
  function render(w, today) {
    last = [w, today];
    renderHeader(w, today);
    renderGrid(w);
    renderLegend(w);
    renderFragile(w);
    renderTips(w);
  }

  async function load() {
    const t = await CC.get('/api/today');
    const today = t.status === 200 ? t.body : null;
    const r = await CC.get(weekPath(today));  // without start: the API's own Monday to Sunday
    const err = $('#loadErr');
    if (r.status === 200 && r.body && typeof r.body === 'object') {
      render(r.body, today);
      err.hidden = true;
      document.body.dataset.state = 'ready';
    } else {
      err.textContent = 'Could not load ' + weekPath(today) + ' — ' + CC.errorText(r);
      err.hidden = false;
      document.body.dataset.state = 'error';
    }
  }

  document.fonts.ready.then(() => { if (last) render(last[0], last[1]); });
  load();
})();
