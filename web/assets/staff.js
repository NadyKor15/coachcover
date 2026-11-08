/* Staff & Certificates: GET /api/staff (+ /api/today for the header and the link to the case) → slots of staff.html.
 * Read-only. The state of every card comes from the server, computed as the rules compute it; nothing is decided here. */
(function () {
  'use strict';
  const CC = window.CC;
  const $ = s => document.querySelector(s);
  const slot = name => document.querySelector('[data-slot="' + name + '"]');
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const withFx = href => href + (CC.FIXTURE ? (href.includes('?') ? '&' : '?') + 'fixture=1' : '');

  // the screen's levels for a card state
  const LEVEL = { valid: 'safe', expiring: 'fragile', review: 'fragile', expired: 'risk', revoked: 'risk', not_yet_valid: 'risk', replaced: 'past' };
  const day = iso => new Date(iso + 'T00:00:00Z').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  const plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');

  function stateLine(c) {
    if (c.state === 'expiring') return c.days_left === 0 ? 'Expires today' : 'Expires in ' + plural(c.days_left, 'day');
    if (c.state === 'revoked') return 'Revoked ' + (c.revoked_on ? day(c.revoked_on) : '') + ' by issuer';
    return c.state_label;
  }

  function renderHeader(t) {
    slot('venue-name').textContent = (t && t.venue && t.venue.name) || '';
    slot('today-date').textContent = t && t.now_label ? String(t.now_label).split(' · ')[0] : '';
    slot('user').textContent = (t && t.user_label) || '';
    const caseId = t && t.callout && t.callout.case_id;
    document.querySelectorAll('[data-nav]').forEach(a => {
      const base = a.getAttribute('href').split('?')[0];
      a.href = withFx(base === 'case.html' && caseId ? 'case.html?id=' + encodeURIComponent(caseId) : base);
    });
    $('#navReports').href = CC.folderHref(t && t.venue && t.venue.id);
  }

  function renderSummary(d) {
    const T = d.totals, box = slot('summary');
    box.textContent = '';
    [['safe', T.valid, 'valid'],
     ['fragile', T.expiring, 'expire within ' + d.expiring_days + ' days'],
     ['risk', T.expired + T.revoked + T.not_yet_valid, 'expired or revoked'],
     ['fragile', T.review, 'to review: readers disagree']].forEach(([level, n, word]) => {
      const chip = el('div', 'chip'); chip.dataset.level = level;
      chip.appendChild(el('b', null, String(n))); chip.appendChild(document.createTextNode(word));
      box.appendChild(chip);
    });
  }

  function renderRoster(d) {
    const body = slot('roster');
    body.textContent = '';
    d.staff.forEach(p => {
      const row = el('div', 'r-row'); row.dataset.slot = 'person'; row.dataset.staff = p.id; row.dataset.state = p.state;
      const who = el('div', 'who');
      const dot = el('span', 'dot'); dot.dataset.level = LEVEL[p.state] || 'safe'; who.appendChild(dot);
      const wt = el('div');
      wt.appendChild(el('div', 'nm', p.name));
      wt.appendChild(el('div', 'id', p.id + (p.home_venue ? ' · ' + p.home_venue : '')));
      if (p.needs_review) wt.appendChild(el('span', 'demo', 'Seeded demo case: the two readings of this card differ, and nobody has confirmed it yet.'));
      who.appendChild(wt); row.appendChild(who);

      const roles = el('div', 'roles'); p.roles.forEach(r => roles.appendChild(el('div', null, r))); row.appendChild(roles);

      const progs = el('div', 'progs');
      if (!p.programs.length) progs.appendChild(el('span', 'none', 'No program training'));
      p.programs.forEach(g => {
        const lv = g.levels.every(l => /^Level /.test(l)) ? 'Levels ' + g.levels.map(l => l.slice(6)).join(', ') : g.levels.join(', ');
        const line = el('div', null, g.label + (g.levels.length ? ' · ' + lv : ''));
        if (g.renewal_overdue) line.appendChild(el('span', 'od', 'Renewal overdue since ' + day(g.renewal_due)));
        progs.appendChild(line);
      });
      row.appendChild(progs);

      const cards = el('div', 'cards');
      if (!p.certs.length) cards.appendChild(el('span', 'none', 'No certificates on file'));
      p.certs.forEach(c => {
        const card = el('div', 'cert'); card.dataset.slot = 'cert'; card.dataset.state = c.state; card.dataset.level = LEVEL[c.state] || 'safe';
        card.appendChild(el('div', 't', c.short));
        card.appendChild(el('div', 'd', (c.state === 'expired' ? 'Expired ' : c.state === 'revoked' ? 'Card to ' : 'Expires ') + day(c.expires_on)));
        if (c.state !== 'valid') card.appendChild(el('div', 's', stateLine(c)));
        card.title = c.label + ' · issued ' + day(c.issued_on) + ' · ' + c.state_label;
        cards.appendChild(card);
      });
      row.appendChild(cards);
      body.appendChild(row);
    });
  }

  function render(d, t) {
    renderHeader(t);
    slot('sub').textContent = d.staff.length + ' people at ' + plural(d.venue_count, 'venue') + ' · certificates checked as of ' + day(d.as_of) +
      ' by the same rules that decide who may teach';
    renderSummary(d);
    renderRoster(d);
    slot('note').textContent = d.note || '';
  }

  async function load() {
    const [s, t] = await Promise.all([CC.get('/api/staff'), CC.get('/api/today')]);
    const err = $('#loadErr');
    if (s.status === 200 && s.body && Array.isArray(s.body.staff)) {
      render(s.body, t.status === 200 ? t.body : null);
      err.hidden = true;
      document.body.dataset.state = 'ready';
    } else {
      err.textContent = 'Could not load /api/staff — ' + CC.errorText(s);
      err.hidden = false;
      document.body.dataset.state = 'error';
    }
  }

  load();
  CC.fillFooter(document.querySelector('[data-slot="footer-line"]'));
})();
