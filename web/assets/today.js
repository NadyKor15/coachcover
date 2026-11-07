/* Screen C: GET /api/today → slots of index.html. Judge mode, plan B approval and the voice note player.
 * All data strings go in through textContent; only fixed icons use innerHTML. */
(function () {
  'use strict';
  const CC = window.CC;
  const $ = sel => document.querySelector(sel);
  const slot = name => document.querySelector('[data-slot="' + name + '"]');
  const setText = (name, value) => { const el = slot(name); if (el) el.textContent = value == null ? '' : String(value); };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  const ui = { data: null, planNote: null, busy: false, inflight: false };

  /* ---------- icons (fixed markup) ---------- */
  const PERSON = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="4.6" r="3.2"/><path d="M1.8 14.6c.4-3.4 3-5.2 6.2-5.2s5.8 1.8 6.2 5.2z"/></svg>';
  const TL_ICON = {
    call: { size: 45, svg: '<svg width="25" height="25" viewBox="0 0 24 24" fill="#fff"><path d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1C10.61 21 3 13.39 3 4c0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/></svg>' },
    declined: { size: 44, svg: '<svg width="18" height="18" viewBox="0 0 18 18" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M3 3l12 12M15 3L3 15"/></svg>' },
    noreply: { size: 44, svg: '<svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v8.5l5.5 4"/></svg>' },
    accepted: { size: 49, svg: '<svg width="26" height="20" viewBox="0 0 26 20" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5l6.5 6L23 3.5"/></svg>' },
    planmark: { size: 44, svg: '<svg width="22" height="18" viewBox="0 0 30 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h8.5l15.5 16"/><path d="M19.5 19.5h7v-7"/><path d="M13.5 6.6C9 9.5 6.4 13.5 6 19"/></svg>' },
    family: { size: 44, svg: '<svg width="22" height="20" viewBox="0 0 22 20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3.5h16a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5H9l-4.5 3.5V14.5H3A1.5 1.5 0 0 1 1.5 13V5A1.5 1.5 0 0 1 3 3.5z"/></svg>' },
    network: { size: 44, svg: '<svg width="22" height="20" viewBox="0 0 22 20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="4.5" cy="10" r="3"/><circle cx="17.5" cy="4.5" r="3"/><circle cx="17.5" cy="15.5" r="3"/><path d="M7.3 8.8l7.4-3.1M7.3 11.2l7.4 3.1"/></svg>' },
    other: { size: 44, svg: '' },
    end: { size: 26, svg: '' }
  };
  const OFF_ICON = {
    declined: '<svg width="13" height="13" viewBox="0 0 13 13" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M2.5 2.5l8 8M10.5 2.5l-8 8"/></svg>',
    noreply: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 2.8v6.4l4 3"/></svg>',
    accepted: '<svg width="17" height="13" viewBox="0 0 17 13" fill="none" stroke="#fff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6.8l4.2 4L15 2.2"/></svg>',
    other: ''
  };
  const CARD_ICONS = {
    calendar: '<svg width="36" height="40" viewBox="0 0 36 40" fill="none" stroke="#04625a" stroke-width="2.6" stroke-linecap="round"><rect x="2.3" y="6" width="31.4" height="31" rx="5"/><path d="M2.3 14.5h31.4M10.5 2.5v7M25.5 2.5v7"/><g fill="#04625a" stroke="none"><circle cx="11" cy="21.8" r="1.9"/><circle cx="18" cy="21.8" r="1.9"/><circle cx="25" cy="21.8" r="1.9"/><circle cx="11" cy="29" r="1.9"/><circle cx="18" cy="29" r="1.9"/><circle cx="25" cy="29" r="1.6" opacity=".45"/></g></svg>',
    certificate: '<svg width="36" height="45" viewBox="0 0 36 45" fill="none" stroke="#91660c" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17 36.5H6.5a4 4 0 0 1-4-4V6.5a4 4 0 0 1 4-4h17.5l9 9v12"/><path d="M24 2.5v9h9"/><path d="M8.5 13h11M8.5 19h13M8.5 25h7"/><circle cx="25.5" cy="30" r="5.6"/><path d="M22.2 34.8l-2 7.2 5.3-2.6 5.3 2.6-2-7.2"/></svg>',
    closures: '<svg width="42" height="44" viewBox="0 0 42 44" fill="none" stroke="#035d55" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 26V8.5a5 5 0 0 1 9.8-1.3M26 26V8.5a5 5 0 0 1 9.8-1.3M11 14h15M11 21h15"/><path d="M2 32c3.3 0 3.3-2.5 6.6-2.5S11.9 32 15.2 32s3.3-2.5 6.6-2.5S25.1 32 28.4 32s3.3-2.5 6.6-2.5 3.3 2.5 5 2.5"/><path d="M2 40c3.3 0 3.3-2.5 6.6-2.5S11.9 40 15.2 40s3.3-2.5 6.6-2.5S25.1 40 28.4 40s3.3-2.5 6.6-2.5 3.3 2.5 5 2.5"/></svg>'
  };
  const CHECK_BANNER = '<svg width="20" height="16" viewBox="0 0 20 16" fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 8.5l5 4.8L17.5 3"/></svg>';
  const BANG_BANNER = '<svg width="6" height="22" viewBox="0 0 6 22" fill="#fff"><rect x="0.5" y="0" width="5" height="14" rx="2.5"/><circle cx="3" cy="19" r="2.8"/></svg>';

  // per-card geometry of the right column, taken from the approved layout
  const GEO = [
    { mt: 1.5, h: 128, ix: 25, iy: 32.5, tx: 83.5, tt: 22, lh: 28.5, tfs: 22.5, tls: -0.6 },
    { mt: 19.5, h: 108.5, ix: 25, iy: 30, tx: 84.5, tt: 22.8, lh: 30, tfs: 21.5, tls: -1.4 },
    { mt: 20, h: 108, ix: 24, iy: 29.5, tx: 91.5, tt: 21.6, lh: 30.5, tfs: 22.5, tls: -0.65 }
  ];
  // timeline x positions of the approved layout (4 events + shift start)
  const TL_X = [149, 426.5, 706, 1051.5];
  const TL_END_X = 1531.5;

  /* ---------- small helpers ---------- */
  function kindOf(k) {
    const s = String(k || '').toLowerCase().replace(/-/g, '_');
    if (s === 'callout' || s === 'call_out' || s === 'call') return 'call';
    if (s === 'no_reply' || s === 'noreply' || s === 'expired' || s === 'timeout') return 'noreply';
    if (s === 'declined' || s === 'accepted') return s;
    if (s === 'plan_b' || s === 'planb') return 'planmark';
    if (s === 'family') return 'family';
    if (s === 'network') return 'network';
    if (s === 'shift_start' || s === 'end') return 'end';
    return 'other';
  }
  function humanResult(r) {
    const k = kindOf(r);
    if (k === 'noreply') return 'no reply';
    return String(r || '').replace(/_/g, ' ');
  }
  function toneOf(t) {
    const s = String(t || '').toLowerCase();
    if (s === 'red' || s === 'risk' || s === 'expired') return 'expired';
    if (s === 'amber' || s === 'fragile' || s === 'gold' || s === 'review') return 'review';
    return 'plain';
  }
  // the API's cents format (money.cents_label): 2 decimals from 1¢, else 3, below 0.001¢ 4
  function fmtCents(c) {
    if (c >= 1) return c.toFixed(2) + '¢';
    return c >= 0.001 ? c.toFixed(3) + '¢' : c.toFixed(4) + '¢';
  }
  function fmtUsd(v) {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: v < 100 ? 2 : 0, minimumFractionDigits: 0 }).format(v);
  }
  const present = v => typeof v === 'number' && isFinite(v) && v > 0;
  const presentLabel = s => typeof s === 'string' && s.trim() !== '' && s.trim() !== '…';

  /* ---------- render ---------- */
  function renderHeader(d) {
    setText('title', d.title);
    setText('site-name', d.venue && d.venue.name);
    const dt = slot('date-time');
    dt.textContent = '';
    String(d.now_label || '').split(' · ').forEach((part, i) => {
      if (i) dt.appendChild(el('span', 'sep', ' · '));
      dt.appendChild(document.createTextNode(part));
    });
    setText('user', d.user_label);
    const caseId = d.callout && d.callout.case_id;
    const href = 'case.html' + (caseId ? '?id=' + encodeURIComponent(caseId) + (CC.FIXTURE ? '&' + CC.fixtureQuery() : '') : '');
    $('#navCases').href = href;
    document.querySelectorAll('.nav a[data-nav]').forEach(a => { a.href = a.dataset.nav + '.html' + (CC.FIXTURE ? '?fixture=1' : ''); });
    // the covered line and the timeline open the case of this morning
    ui.caseHref = caseId ? href : null;
    ['#covered', '.band'].forEach(sel => {
      const box = $(sel);
      box.classList.toggle('to-case', !!ui.caseHref);
      if (ui.caseHref) { box.setAttribute('role', 'link'); box.tabIndex = 0; box.title = 'Open case ' + caseId; }
      else { box.removeAttribute('role'); box.removeAttribute('tabindex'); box.removeAttribute('title'); }
    });
  }

  function staffNames(d) {
    const names = new Set();
    (d.offers || []).forEach(o => o.staff && names.add(o.staff));
    ((d.eligibility && d.eligibility.staff) || []).forEach(s => s.name && names.add(s.name));
    return [...names].sort((a, b) => b.length - a.length);
  }

  function renderTimeline(d) {
    const box = $('#timeline');
    box.textContent = '';
    const all = (d.timeline || []).slice();
    const endIdx = all.findIndex(t => kindOf(t.kind) === 'end');
    const end = endIdx >= 0 ? all.splice(endIdx, 1)[0] : null;
    let events = all;
    if (events.length > 6) events = [events[0]].concat(events.slice(-5));
    const n = events.length;
    let xs;
    if (n <= 4) xs = TL_X.slice(0, n);
    else {
      const last = Math.min(1330, 149 + (n - 1) * 240);
      xs = events.map((_, i) => 149 + (last - 149) * i / (n - 1));
    }
    const names = staffNames(d);
    const put = (x, item, kind) => {
      const t = el('div', 'tl-time', item.time_label);
      t.style.left = x + 'px';
      t.dataset.slot = 'tl-time';
      box.appendChild(t);
      const ic = TL_ICON[kind];
      const m = el('div', 'tl-mark ' + kind);
      m.style.cssText = 'left:' + (x - ic.size / 2) + 'px;top:' + (78.5 - ic.size / 2) + 'px;width:' + ic.size + 'px;height:' + ic.size + 'px';
      m.innerHTML = ic.svg;
      box.appendChild(m);
      if (kind === 'end' || !item.label) return;
      const label = String(item.label);
      const name = names.find(nm => label.startsWith(nm + ' ') || label.startsWith(nm + ':'));
      const c = el('div', 'tl-chip');
      c.dataset.slot = 'tl-label';
      c.style.left = x + 'px';
      if (name) {
        c.classList.add('two');
        c.style.top = '104px';
        const b = el(kind === 'accepted' ? 'b' : 'span', null, name);
        c.appendChild(b);
        c.appendChild(document.createElement('br'));
        c.appendChild(document.createTextNode(label.slice(name.length).replace(/^[:\s]+/, '')));
      } else {
        c.classList.add('one');
        c.style.top = '107px';
        c.style.paddingTop = '7.7px';
        c.textContent = label;
      }
      box.appendChild(c);
    };
    events.forEach((ev, i) => put(xs[i], ev, kindOf(ev.kind)));
    if (end) put(TL_END_X, end, 'end');
  }

  function renderCallout(d) {
    const c = d.callout || {};
    // no voice note this time (e.g. the night check opened the case): say so instead of an empty card
    const none = !d.callout;
    document.querySelector('.card.callout').classList.toggle('none', none);
    const n = d.night_check;
    setText('callout-empty', none ? (n ? 'No voice note this time. The night check opened this case: ' + String(n.label).replace(/^Night check \d{1,2}:\d{2}:\s*/, '') + '.' : 'No call-out yet this morning.') : '');
    slot('callout-empty').hidden = !none;
    setText('callout-kind', none && n ? 'Night check' : 'Call-out');
    setText('callout-time', none && n ? (String(n.label).match(/\d{1,2}:\d{2}/) || [''])[0] : c.time_label);
    setText('callout-language', c.language);
    setText('callout-language-h', c.language || 'Original');
    setText('callout-staff', c.staff);
    setText('callout-duration', c.duration_label);
    setText('callout-original', c.transcript_original ? '“' + c.transcript_original + '”' : '');
    setText('callout-transcript', c.transcript_en ? '“' + c.transcript_en + '”' : '');
    // five lines fit the card; a longer note is cut with an ellipsis and stays readable on hover
    slot('callout-original').title = c.transcript_original || '';
    slot('callout-transcript').title = c.transcript_en || '';
    const asr = $('#asr');
    asr.hidden = !presentLabel(c.asr_label);
    setText('callout-asr', c.asr_label);
    const play = $('#play');
    play.dataset.src = c.audio_url || '';
  }

  /* reason chips: chips with the same code and a common first words are one chip ("Certificate expired" ×3);
     the exact labels stay in the chip's tooltip and on the case page */
  const TRAILING_WORD = /\s+(at|on|for|since|until|before|after|in|by|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$/;
  function mergeReasons(reasons) {
    const byCode = new Map();
    reasons.forEach(r => { const k = r.code || r.label; if (!byCode.has(k)) byCode.set(k, []); byCode.get(k).push(r); });
    const out = [];
    byCode.forEach(list => {
      if (list.length === 1) { out.push({ label: list[0].label, count: list[0].count, tone: toneOf(list[0].tone), code: list[0].code, labels: [list[0].label] }); return; }
      const words = list.map(r => String(r.label).split(' '));
      let n = 0;
      while (words.every(w => w[n] !== undefined && w[n] === words[0][n])) n++;
      let common = words[0].slice(0, n).join(' ').replace(TRAILING_WORD, '');
      if (n < 1 || !common) { list.forEach(r => out.push({ label: r.label, count: r.count, tone: toneOf(r.tone), code: r.code, labels: [r.label] })); return; }
      out.push({ label: common, count: list.reduce((a, r) => a + r.count, 0), tone: toneOf(list[0].tone), code: list[0].code,
                 labels: list.map(r => r.label + ' ×' + r.count) });
    });
    return out;
  }
  function chipEl(g) {
    const c = el('div', 'chip ' + g.tone);
    c.dataset.slot = 'reason-chip';
    c.dataset.code = g.code || '';
    c.title = g.labels.join('\n');
    c.appendChild(el('span', 'l', g.label + ' '));
    const count = el('span', null, '×' + g.count);
    count.dataset.slot = 'reason-count';
    c.appendChild(count);
    return c;
  }
  function rowsOf(box) { return new Set([...box.children].map(c => c.offsetTop)).size; }
  function buildChips(box, groups, size, maxRows) {
    box.textContent = '';
    box.style.setProperty('--cfs', size + 'px');
    groups.forEach(g => box.appendChild(chipEl(g)));
    const rows = rowsOf(box);
    if (rows === 1) {  // one row spans the card, as in the approved layout
      const spare = 725 - box.scrollWidth;
      const list = [...box.children];
      if (spare > 0) list.forEach(c => { c.style.width = (c.getBoundingClientRect().width + spare / list.length).toFixed(2) + 'px'; });
    }
    return { fits: rows <= maxRows, rows };
  }
  function foldChips(box, groups, size, maxRows) {
    for (let keep = groups.length - 1; keep > 0; keep--) {
      const rest = groups.slice(keep);
      const more = { label: '+' + rest.length + ' more', count: rest.reduce((a, g) => a + g.count, 0), tone: 'plain', code: 'MORE',
                     labels: rest.map(g => g.labels.join('\n')) };
      const r = buildChips(box, groups.slice(0, keep).concat([more]), size, maxRows);
      if (r.fits) return r;
    }
    return buildChips(box, groups, size, maxRows);
  }

  function renderStaff(d) {
    const e = d.eligibility || {};
    const staff = e.staff || [];
    setText('staff-total', e.total);
    setText('eligible-count', e.eligible);
    const dots = $('#dots');
    dots.textContent = '';
    let gold = -1;
    staff.forEach((s, i) => {
      const dot = el('div', 'dot' + (s.eligible && s.highlight ? ' gold' : s.eligible ? ' on' : ''));
      dot.dataset.slot = 'staff-dot';
      dot.dataset.staffId = s.id || '';
      dot.title = (s.name || s.id || '') + (s.eligible ? ' — Eligible' : s.reason_label ? ' — ' + s.reason_label : '');
      dot.innerHTML = PERSON;
      if (s.eligible && s.highlight && gold < 0) gold = i;
      dots.appendChild(dot);
    });
    const tip = $('#tip');
    if (gold >= 0) {
      const col = gold % 14, row = Math.floor(gold / 14);
      const cx = 16.9 + 50.15 * col + 25.075;
      tip.style.left = (cx - 9) + 'px';
      tip.style.top = (43 + 39 * row) + 'px';
      setText('highlight-name', staff[gold].name || staff[gold].id);
      tip.hidden = false;
    } else tip.hidden = true;

    const chips = $('#chips');
    const groups = mergeReasons(e.reasons || []);
    let layout = null;
    for (const size of [14, 13.5, 13]) {
      layout = buildChips(chips, groups, size, size >= 14 ? 2 : 3);
      if (layout.fits) break;
    }
    if (!layout.fits) layout = foldChips(chips, groups, 13, 3);
    const chipsBottom = 216 + chips.offsetHeight;
    const fr = e.first_refusal;
    const line = $('#refusal'), quote = $('#refusalQuote');
    if (fr && (fr.staff || fr.label)) {
      const t = $('#refusalText');
      t.textContent = '';
      if (fr.staff) { t.appendChild(el('b', null, fr.staff)); t.appendChild(document.createTextNode(' · ')); }
      // "BLOCKED:" in bold when the label carries it
      const label = String(fr.label || '');
      const k = label.indexOf('BLOCKED:');
      if (k >= 0) {
        t.appendChild(document.createTextNode(label.slice(0, k)));
        t.appendChild(el('b', null, 'BLOCKED:'));
        t.appendChild(document.createTextNode(label.slice(k + 8)));
      } else t.appendChild(document.createTextNode(label));
      line.hidden = false;
      line.style.top = (chipsBottom + 7) + 'px';
      quote.textContent = fr.contrast_quote || '';
      quote.hidden = !fr.contrast_quote;
      quote.style.top = (chipsBottom + 7 + line.offsetHeight + 3) + 'px';
    } else { line.hidden = true; quote.hidden = true; }
  }

  function renderShift(d) {
    const s = d.shift || {};
    setText('shift-time', s.time_label);
    setText('shift-lanes', s.lanes_label);
    setText('shift-needs', s.needs_label);
    setText('shift-program', s.program_label);
    slot('shift-program-sep').hidden = !s.program_label;
  }

  function renderOffers(d) {
    const list = $('#offers');
    list.textContent = '';
    const all = d.offers || [];
    // the card reads as a log: newest answers, in time order (the queue position is in the WHY line)
    const rows = all.slice(-3).slice().sort((x, y) => String(x.time_label || '').localeCompare(String(y.time_label || '')));
    // no offers yet: say why instead of an empty card
    const policy = (d.plan_b && d.plan_b.autonomy) || '';
    setText('offers-empty', all.length ? '' : (d.night_check && !d.callout
      ? 'No offers sent yet: the night check proposes a replacement to the manager first' + (policy ? ' (policy: ' + policy + ')' : '') + '.'
      : 'No offers yet.'));
    slot('offers-empty').hidden = !!all.length;
    const names = staffNames(d);
    const timeline = d.timeline || [];
    const accepted = rows.find(o => kindOf(o.result) === 'accepted' && o.why_first);
    list.classList.toggle('compact', !!accepted);
    rows.forEach(o => {
      const k = kindOf(o.result);
      const ico = OFF_ICON[k] != null ? k : 'other';
      // result words: the timeline label for the same person and time, else the result code in words
      const tl = timeline.find(t => t.time_label === o.time_label &&
        (String(t.label || '').startsWith(o.staff + ' ') || String(t.label || '').startsWith(o.staff + ':')));
      const words = tl ? String(tl.label).slice(o.staff.length).replace(/^[:\s]+/, '') : (o.result_label || humanResult(o.result));
      const li = el('li', 'off-row ' + ico);
      li.dataset.slot = 'offer-row';
      li.dataset.offerId = o.id || '';
      const i = el('span', 'off-ico ' + ico); i.innerHTML = OFF_ICON[ico]; li.appendChild(i);
      const t = el('span', 'off-time', o.time_label); t.dataset.slot = 'offer-time'; li.appendChild(t);
      const n = el('span', 'off-name', o.staff + ' — ' + words); n.dataset.slot = 'offer-result'; li.appendChild(n);
      list.appendChild(li);
    });
    const card = list.parentElement;
    card.querySelectorAll('.off-more').forEach(x => x.remove());
    if (all.length > rows.length) card.appendChild(el('div', 'off-more', '+' + (all.length - rows.length) + ' earlier'));
    // rail from the first icon centre to the last
    const rail = $('#offRail');
    const icos = list.querySelectorAll('.off-ico');
    if (icos.length > 1) {
      const top = icos[0].offsetTop + icos[0].offsetParent.offsetTop + 15.5;
      const last = icos[icos.length - 1];
      const bottom = last.offsetTop + last.offsetParent.offsetTop + 15.5;
      rail.style.top = top + 'px';
      rail.style.height = (bottom - top) + 'px';
      rail.hidden = false;
    } else rail.hidden = true;
    const why = $('#whyFirst');
    if (accepted) {
      setText('why-first', accepted.why_first);
      setText('why-name', 'why ' + String(accepted.staff || '').split(' ')[0]);
      // under the last row of the log (rows are in time order; the line names the person, so it needs no neighbour)
      const row = list.lastElementChild;
      why.style.top = (row.offsetTop + row.offsetHeight - 2) + 'px';
      why.hidden = false;
    } else why.hidden = true;
  }

  function renderSide(d) {
    const side = $('#sideCards');
    side.textContent = '';
    (d.summary_cards || []).forEach((c, i) => {
      const g = GEO[i] || GEO[GEO.length - 1];
      const title = String(c.title || '');
      const icon = /expir|certif/i.test(title) ? 'certificate' : /clos/i.test(title) ? 'closures' : 'calendar';
      const card = el('section', 'card sum' + (icon === 'closures' ? ' strong' : ''));
      card.dataset.slot = 'summary-card';
      card.style.cssText = 'margin-top:' + g.mt + 'px;height:' + g.h + 'px;line-height:' + g.lh + 'px';
      const ic = el('span', 'ic'); ic.style.cssText = 'left:' + g.ix + 'px;top:' + g.iy + 'px'; ic.innerHTML = CARD_ICONS[icon];
      card.appendChild(ic);
      const body = el('div'); body.style.padding = g.tt + 'px 0 0 ' + g.tx + 'px';
      const t = el('div', 't', title + (/[:?]$/.test(title) ? '' : ':'));
      t.dataset.slot = 'summary-title';
      t.style.cssText = 'font-size:' + g.tfs + 'px;letter-spacing:' + g.tls + 'px';
      const b = el('div', 'b', c.value); b.dataset.slot = 'summary-value';
      body.appendChild(t); body.appendChild(b); card.appendChild(body);
      side.appendChild(card);
    });
  }

  const PLAN_HEAD = {
    standby: 'Plan B ready if no one accepts',
    proposed: 'Plan B needs your approval',
    pending: 'Plan B needs your approval',
    approved: 'Plan B approved',
    done: 'Plan B done',
    executed: 'Plan B done'
  };

  function renderOutcome(d) {
    const o = d.outcome || {};
    const p = d.plan_b || null;
    const box = $('#covered'), txt = slot('covered'), ico = $('#coveredIco');
    txt.textContent = '';
    const add = (cls, s) => { const x = el('span', cls, s); txt.appendChild(x); return x; };
    // the banner names the shift's start (from the shift); covered_time is when the case closed, shown small
    const shiftStart = (d.shift && String(d.shift.time_label || '').split('–')[0]) || '';
    const priv = !!(d.shift && d.shift.private);
    if (o.covered_by) {
      box.classList.remove('open');
      ico.innerHTML = CHECK_BANNER;
      add(null, (shiftStart || o.covered_time) + ' covered by ' + o.covered_by);
      add('sep', ' · ');
      const n = Number(o.lessons_cancelled || 0);
      // a private lesson: another coach only with the family's yes
      add(null, priv ? 'the family agreed' : n + (n === 1 ? ' lesson cancelled' : ' lessons cancelled'));
      if (o.covered_time && shiftStart && o.covered_time !== shiftStart) {
        const c = add('closed', 'closed ' + o.covered_time);
        c.dataset.slot = 'covered-closed';
      }
    } else if (priv && o.private_line) {
      // a private lesson the family moved to their own coach, or a credit: closed without another coach
      box.classList.remove('open');
      ico.innerHTML = CHECK_BANNER;
      add(null, (shiftStart ? shiftStart + ' private lesson' : 'Private lesson'));
      add('sep', ' · ');
      add(null, o.private_line);
    } else {
      const st = p && p.state;
      const approved = st === 'approved' || st === 'done' || st === 'executed';
      box.classList.toggle('open', !approved);
      ico.innerHTML = approved ? CHECK_BANNER : BANG_BANNER;
      const start = shiftStart || o.covered_time || '';
      add(null, (start ? start + ' ' : '') + 'not covered yet');
      add('sep', ' · ');
      if (priv && o.family_wait_until) { add(null, 'waiting for the family’s answer (by ' + o.family_wait_until + ')'); return; }
      // standby = plan B is ready but opens 20 min before the start; only a proposed plan asks the manager now
      add(null, approved ? 'plan B approved' : p && p.state === 'standby' ? 'plan B ready, opens 20 min before the start'
        : p ? 'plan B needs your approval' : 'offers running');
    }
  }

  function renderPlan(d) {
    const p = d.plan_b;
    const box = $('#planb'), btn = $('#approve');
    box.hidden = !p;
    if (!p) return;
    box.classList.remove('conflict', 'approved');
    const note = ui.planNote && ui.planNote.planId === p.id ? ui.planNote : null;
    let head = PLAN_HEAD[p.state] || ('Plan B ' + String(p.state || '').replace(/_/g, ' '));
    if (note) { head = note.text; box.classList.add('conflict'); }
    else if (p.state === 'approved' || p.state === 'done' || p.state === 'executed') box.classList.add('approved');
    $('#planbHead').textContent = head;
    $('#planbBody').textContent = p.text || '';
    fitPlanText();
    const canApprove = p.state === 'proposed' || p.state === 'pending';
    btn.className = 'btn' + (canApprove ? ' live' : (p.state === 'approved' || p.state === 'done' || p.state === 'executed') ? ' done' : '');
    btn.disabled = !canApprove || ui.busy;
    btn.textContent = (p.state === 'approved' || p.state === 'done' || p.state === 'executed') ? 'Approved' : 'Approve plan B';
    btn.dataset.planId = p.id;
    btn.dataset.hash = p.hash || '';
  }

  // "If nobody had accepted: $240 (Refunds: 8 participants × $30)" → head, amount, detail
  const UNCOVERED = /^(.*?):\s*(\$[\d,]+(?:\.\d+)?)\s*(?:\((.*)\))?\s*$/;
  const stripRef = s => String(s || '').replace(/\s*\(TG \d+\)\s*$/, '');
  function fitPlanText() {
    const t = slot('planb-text');
    t.style.fontSize = ''; t.style.lineHeight = '';
    for (let size = 21; size >= 15; size -= 0.5) {
      t.style.fontSize = size + 'px';
      t.style.lineHeight = Math.round(size * 1.29) + 'px';
      if (t.scrollHeight - parseFloat(getComputedStyle(t).paddingTop) <= Math.round(size * 1.29) * 2 + 2) return;
    }
  }

  function renderMoney(d) {
    const m = d.money || {};
    const box = $('#money');
    const hasCase = present(m.case_cost_cents);
    const hasUnc = present(m.uncovered_morning_usd) || presentLabel(m.uncovered_morning_label);
    box.hidden = !(hasCase || hasUnc);
    $('#moneyCase').hidden = !(hasCase && hasUnc);
    setText('money-case', hasCase ? (m.case_cost_label || fmtCents(m.case_cost_cents)) : '');
    let head = '', big = '', detail = '';
    if (hasUnc) {
      const parts = presentLabel(m.uncovered_morning_label) ? String(m.uncovered_morning_label).match(UNCOVERED) : null;
      head = parts ? parts[1] : 'If nobody had accepted';
      big = parts ? parts[2] : fmtUsd(m.uncovered_morning_usd);
      detail = parts ? (parts[3] || '') : (presentLabel(m.uncovered_morning_label) ? m.uncovered_morning_label : '');
    } else if (hasCase) {
      head = 'Cost of this cover';
      big = m.case_cost_label || fmtCents(m.case_cost_cents);
      detail = 'in model calls';
    }
    setText('money-head', head);
    setText('money-big', big);
    setText('money-detail', detail);
    slot('money-detail').hidden = !detail;
    // where the lesson price comes from (money.source); "assumption" when the school has not set its own price
    const source = hasUnc ? stripRef(m.source || '') : '';
    setText('money-source', source);
    slot('money-source').hidden = !source;
  }

  function renderAlert(d) {
    const box = $('#alert');
    const r = d.ratio_alert, n = d.night_check;
    const a = r ? { tone: r.tone || 'red', text: [r.label, r.action_label].filter(Boolean).join(' · '), caseId: r.case_id }
      : n ? { tone: 'amber', text: n.label, caseId: n.case_id } : null;
    box.hidden = !a || !a.text;
    if (box.hidden) return;
    box.className = 'alert ' + (['red', 'green', 'amber'].includes(a.tone) ? a.tone : 'amber');
    setText('alert-text', a.text);
    $('#alertMark').setAttribute('d', a.tone === 'green' ? 'M6.5 11.3l3.1 3 5.9-6.3' : 'M11 5.5v7M11 16.2v.3');
    box.href = a.caseId ? 'case.html?id=' + encodeURIComponent(a.caseId) + (CC.FIXTURE ? '&' + CC.fixtureQuery() : '') : '#';
    // keep the banner clear of the title
    const range = document.createRange();
    range.selectNodeContents(document.querySelector('h1'));
    const room = 1631 - range.getBoundingClientRect().width - 30;
    box.style.maxWidth = Math.min(1000, room) + 'px';
    // a long banner first gets a smaller font (down to 14 px), then wraps to two lines; never cut off
    box.classList.remove('wrap'); box.style.fontSize = '';
    for (let fs = 17; fs >= 14 && box.scrollWidth > box.clientWidth + 1; fs -= 0.5) box.style.fontSize = fs + 'px';
    if (box.scrollWidth > box.clientWidth + 1) box.classList.add('wrap');
  }

  function render(d) {
    ui.data = d;
    renderHeader(d);
    renderTimeline(d);
    renderCallout(d);
    renderStaff(d);
    renderShift(d);
    renderSide(d);
    renderOffers(d);
    renderOutcome(d);
    renderPlan(d);
    renderMoney(d);
    renderAlert(d);
    setText('model-log', d.model_log && d.model_log.label);
  }

  /* ---------- data ---------- */
  function todayPath() {
    const venue = CC.params.get('venue');
    return '/api/today' + (venue ? '?venue=' + encodeURIComponent(venue) : '');
  }

  async function refresh() {
    if (ui.inflight) return;
    ui.inflight = true;
    try {
      const r = await CC.get(todayPath());
      const err = $('#loadErr');
      if (r.status === 200 && r.body && typeof r.body === 'object') {
        render(r.body);
        err.hidden = true;
        document.body.dataset.state = 'ready';
      } else if (r.status === 503 && r.body && r.body.resetting) {
        // a judge run is rebuilding the training school: a calm note, not an error; the next poll shows the new morning
        err.textContent = 'Resetting the training school…';
        err.classList.add('calm');
        err.hidden = false;
        document.body.dataset.state = ui.data ? 'ready' : 'loading';
      } else {
        err.textContent = 'Could not load /api/today — ' + CC.errorText(r);
        err.classList.remove('calm');
        err.hidden = false;
        document.body.dataset.state = ui.data ? 'ready' : 'error';
      }
    } finally {
      ui.inflight = false;
    }
  }

  function burstRefresh(ms, every) {
    if (CC.FIXTURE) return;
    const until = Date.now() + ms;
    const id = setInterval(() => { if (Date.now() > until) clearInterval(id); else refresh(); }, every);
  }

  /* ---------- judge mode ---------- */
  const SCENARIO_NAME = { dangerous_morning: 'Run the call-out', nobody_accepts: 'Nobody accepts', nightly_check: 'Night check',
    private_lesson: 'Private lesson: yes', private_declined: 'Private lesson: no' };
  async function runScenario(scenario) {
    const btns = document.querySelectorAll('.jbtn');
    const status = $('#judgeStatus');
    btns.forEach(b => { b.disabled = true; });
    status.classList.remove('err');
    status.textContent = 'Running “' + SCENARIO_NAME[scenario] + '”…';
    try {
      const r = await CC.post('/api/judge/run', { scenario });
      if (r.status === 200) {
        ui.planNote = null;
        status.textContent = '“' + SCENARIO_NAME[scenario] + '” done' + (r.body && r.body.case_id ? ' · case ' + r.body.case_id : '');
        await refresh();
        burstRefresh(30000, 1500);
      } else {
        status.classList.add('err');
        status.textContent = (r.status === 429 ? 'Run limit reached for now. ' : 'Run failed. ') + CC.errorText(r);
      }
    } finally {
      btns.forEach(b => { b.disabled = false; });
    }
  }

  /* ---------- plan B ---------- */
  async function approve() {
    const btn = $('#approve');
    const planId = btn.dataset.planId, hash = btn.dataset.hash;
    if (!planId) return;
    ui.busy = true; btn.disabled = true; btn.textContent = 'Sending…';
    try {
      const r = await CC.post('/api/plans/' + encodeURIComponent(planId) + '/approve', { hash });
      if (r.status === 200) ui.planNote = null;
      else if (r.status === 409) ui.planNote = { planId, text: 'Plan changed since you saw it — not approved' };
      else ui.planNote = { planId, text: 'Not approved — ' + CC.errorText(r) };
    } finally {
      ui.busy = false;
      await refresh();
      if (ui.data) renderPlan(ui.data);
    }
  }

  /* ---------- voice note ---------- */
  let audio = null;
  function togglePlay() {
    const btn = $('#play');
    const src = btn.dataset.src;
    if (!src) return;
    if (!audio || audio.dataset_src !== src) {
      if (audio) audio.pause();
      audio = new Audio(src);
      audio.dataset_src = src;
      audio.addEventListener('ended', () => btn.classList.remove('playing'));
      audio.addEventListener('error', () => { btn.classList.remove('playing'); btn.title = 'Voice note is not available'; });
    }
    if (audio.paused) {
      audio.play().then(() => btn.classList.add('playing')).catch(() => { btn.title = 'Voice note is not available'; });
    } else { audio.pause(); btn.classList.remove('playing'); }
  }

  /* ---------- wire up ---------- */
  document.querySelectorAll('.jbtn').forEach(b => b.addEventListener('click', () => runScenario(b.dataset.scenario)));
  $('#approve').addEventListener('click', approve);
  $('#play').addEventListener('click', togglePlay);
  // chip rows and the plan B text are measured: lay out again once the web font is in
  document.fonts.ready.then(() => { if (ui.data) render(ui.data); });
  ['#covered', '.band'].forEach(sel => {
    const box = $(sel);
    const go = () => { if (ui.caseHref) location.href = ui.caseHref; };
    box.addEventListener('click', go);
    box.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  });
  refresh();
  if (!CC.FIXTURE) setInterval(refresh, 5000);
})();
