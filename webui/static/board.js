/* SciComm review board — dialog, manuscript, apparatus.
 *
 * All judgement lives on the server (/api). This file collects parameters,
 * polls a run, paints where the server said the flags are, and sends a human's
 * decision back. It never decides whether a sentence is faithful.
 */
'use strict';

const ROLE_HINTS = {
  extractor: 'cheap',
  drafter:   'writes',
  reviewer:  'audits · must differ',
  researcher:'optional',
  stylist:   'optional',
};
const PLATFORM_KEY = { news: 'dialog.platformNews', xhs: 'dialog.platformXhs', wechat: 'dialog.platformXhs' };
const platformLabel = (p) => t(PLATFORM_KEY[p] || 'dialog.platformNews');
const POLL_MS = 1500;

const state = {
  slots: { source: null, source_type: null, platforms: null, language: null, liveliness: null },
  sessionId: null,
  result: null,
  spans: {},          // platform -> {flags, spans, unlocated}
  drafts: {},         // platform -> working copy, carrying human edits
  decisions: {},      // "platform:flagIndex" -> "accepted" | "rewritten"
  settings: null,
  providers: [],
  history: [],
  polling: null,
  openKey: null,
  rewriter: null,     // the passage currently being rewritten by hand
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls) => { const n = document.createElement(tag); if (cls) n.className = cls; return n; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ── transport ──────────────────────────────────────────────────────── */

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let body = {};
  try { body = await resp.json(); } catch { /* an empty body is still an outcome */ }
  if (!resp.ok) throw new Error(body.error || `${resp.status} ${resp.statusText}`);
  return body;
}

const postJSON = (path, payload) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });

let toastTimer = null;
function toast(message, kind = 'info') {
  const node = $('#toast');
  node.textContent = message;
  node.dataset.kind = kind;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, kind === 'error' ? 8000 : 3500);
}

/* ── dialog ─────────────────────────────────────────────────────────── */

function turn(label, html, who = 'agent') {
  const node = el('div', `turn ${who}`);
  node.innerHTML = `<div class="turn-label">${esc(label)}</div><div class="turn-body">${html}</div>`;
  $('#dialog').append(node);
  $('#dialog').scrollTop = $('#dialog').scrollHeight;
  return node;
}

function ask(label, prompt, options, onPick) {
  const node = turn(label, prompt);
  const chips = el('div', 'chips');
  options.forEach(([value, text, sub]) => {
    const chip = el('button', 'chip');
    chip.type = 'button';
    chip.innerHTML = esc(text) + (sub ? `<small>${esc(sub)}</small>` : '');
    chip.onclick = () => { chips.remove(); turn(t('board.you'), esc(text), 'you'); onPick(value); };
    chips.append(chip);
  });
  node.append(chips);
  $('#dialog').scrollTop = $('#dialog').scrollHeight;
}

/* Detect what kind of source the human just handed us. Deterministic on
   purpose: a parsing model here would add a failure mode before the pipeline
   even starts, and get the cheap cases wrong occasionally. */
function detectSource(text) {
  const raw = text.trim();
  if (!raw) return null;
  if (/^10\.\d{4,9}\//.test(raw) || /^doi:/i.test(raw)) return { source: raw, source_type: 'doi' };
  if (/^https?:\/\//i.test(raw)) {
    if (/doi\.org\//i.test(raw)) return { source: raw, source_type: 'doi' };
    if (/\.pdf($|\?)/i.test(raw)) return { source: raw, source_type: 'pdf' };
    return { source: raw, source_type: 'url' };
  }
  return null;
}

/* The board opens by asking, not by explaining. The overview page at / is
   where the argument for the agent lives; someone who reached the board has
   already made up their mind and wants to get on with it. */
function greeting() {
  const node = el('div', 'thesis');
  node.innerHTML = `<h1>${esc(t('board.greeting'))}</h1><p>${esc(t('board.greetingSub'))}</p>`;
  $('#dialog').append(node);
}

function askNext() {
  const s = state.slots;
  if (!s.source) return;            // the greeting already asked for one
  if (!s.platforms) {
    return ask(t('board.speaker'), esc(t('dialog.askPlatform')), [
      [['news'], t('dialog.platformNews'), ''],
      [['xhs'], t('dialog.platformXhs'), ''],
      [['news', 'xhs'], t('dialog.platformBoth'), ''],
    ], (v) => { s.platforms = v; askNext(); });
  }
  if (!s.language) {
    // The interface language pre-selects, but never decides: `language` is a
    // real run parameter, so an English draft from a Chinese UI stays possible.
    const preferred = draftLanguage();
    return ask(t('board.speaker'), esc(t('dialog.askLanguage')), [
      ['zh', t('dialog.langZh'), preferred === 'zh' ? '·' : ''],
      ['en', t('dialog.langEn'), preferred === 'en' ? '·' : ''],
    ], (v) => { s.language = v; askNext(); });
  }
  if (!s.liveliness) {
    return ask(t('board.speaker'),
      `${esc(t('dialog.askLiveliness'))}<em>${esc(t('dialog.livelinessNote'))}</em>`, [
      [1, '1', t('dialog.lively1')], [2, '2', ''], [3, '3', t('dialog.lively3')],
      [4, '4', ''], [5, '5', t('dialog.lively5')],
    ], (v) => { s.liveliness = v; confirm(); });
  }
  confirm();
}

function confirm() {
  const s = state.slots;
  const node = turn(t('board.speaker'), esc(t('dialog.ready')));
  const slip = el('div', 'slip');
  slip.innerHTML = `<dl>
    <dt>${esc(t('dialog.slipSource'))}</dt><dd>${esc(s.source)}</dd>
    <dt>${esc(t('dialog.slipType'))}</dt><dd>${esc(s.source_type)}</dd>
    <dt>${esc(t('dialog.slipPlatform'))}</dt><dd>${s.platforms.map((p) => esc(platformLabel(p))).join(' · ')}</dd>
    <dt>${esc(t('dialog.slipLanguage'))}</dt><dd>${s.language === 'zh' ? esc(t('dialog.langZh')) : esc(t('dialog.langEn'))}</dd>
    <dt>${esc(t('dialog.slipLiveliness'))}</dt><dd>${s.liveliness}/5</dd>
  </dl>`;
  const start = el('button', 'btn btn-solid');
  start.textContent = t('dialog.start');
  start.onclick = () => { slip.remove(); startRun(); };
  const change = el('button', 'btn btn-quiet');
  change.textContent = t('dialog.startOver');
  change.onclick = () => { resetRun(); askNext(); };
  const actions = el('div', 'chips');
  actions.append(start, change);
  slip.append(actions);
  node.append(slip);
}

function resetRun() {
  clearInterval(state.polling);
  Object.assign(state, {
    slots: { source: null, source_type: null, platforms: null, language: null, liveliness: null },
    sessionId: null, result: null, spans: {}, drafts: {}, decisions: {}, polling: null, openKey: null,
  });
  $('#board').hidden = true;
  $('#apparatus').hidden = true;
  $('#dialog').hidden = false;
  $('#composer').hidden = false;
  document.body.classList.remove('reviewing');
  $('#dialog').innerHTML = '';
  greeting();
}

/* ── the run ────────────────────────────────────────────────────────── */

async function startRun() {
  const s = state.slots;
  const node = turn(t('board.speaker'),
    `<div class="progress">${esc(t('dialog.starting'))}</div><div class="progress-bar"><i style="width:4%"></i></div>`);
  try {
    const { session_id } = await postJSON('/api/generate', {
      source: s.source, source_type: s.source_type, platforms: s.platforms,
      language: s.language, liveliness: s.liveliness,
    });
    state.sessionId = session_id;
    poll(node);
  } catch (err) {
    node.remove();
    turn(t('board.speaker'), `<span style="color:var(--flag)">${esc(err.message)}</span>`);
  }
}

function poll(node) {
  const bar = node.querySelector('.progress-bar i');
  const line = node.querySelector('.progress');
  state.polling = setInterval(async () => {
    let progress;
    try {
      progress = await api(`/api/job/${state.sessionId}/status`);
    } catch (err) { return; }  // a dropped poll is not a failed run; try again

    const pct = progress.steps_total ? Math.round((progress.steps_done / progress.steps_total) * 100) : 6;
    bar.style.width = `${Math.max(pct, 6)}%`;
    line.textContent = `${progress.stage || progress.state} · ${progress.steps_done}/${progress.steps_total} · ${progress.elapsed_s || 0}s`;

    if (progress.state === 'done' || progress.state === 'failed') {
      clearInterval(state.polling);
      loadResult();
    } else if (progress.state === 'lost') {
      clearInterval(state.polling);
      line.textContent = progress.message;
    }
  }, POLL_MS);
}

async function loadResult() {
  const body = await api(`/api/job/${state.sessionId}/result`);
  state.result = body.result;
  state.spans = body.spans;
  state.result.platform_outputs.forEach((d) => { state.drafts[d.platform] = structuredClone(d); });

  if (state.result.status === 'failed' || state.result.status === 'no_claims') {
    return reportTerminal(state.result);
  }
  $('#dialog').hidden = true;
  $('#composer').hidden = true;
  $('#board').hidden = false;
  $('#apparatus').hidden = false;
  document.body.classList.add('reviewing');
  renderBoard();
  renderApparatus();
  loadHistory();
}

/* A blocked source is a question, not an error dump: say what to do next. */
function reportTerminal(result) {
  const notice = (result.notices || [])[0];
  const message = notice ? notice.message : t('dialog.noClaims');
  turn(t('board.speaker'), `${esc(message)}`);
  if (notice && (notice.code === 'need_pdf' || notice.code === 'too_short')) {
    turn(t('board.speaker'), esc(t('dialog.needPdf')));
    state.slots.source = null;
    state.slots.source_type = null;
  }
}

/* ── manuscript ─────────────────────────────────────────────────────── */

function flagKey(platform, index) { return `${platform}:${index}`; }

const HEDGED = new Set(['medium', 'low']);

/* Confidence by ledger id, so a citation can show how solid its evidence is. */
function confidenceById() {
  const map = {};
  (state.result.claim_ledger || []).forEach((c) => { map[c.id] = c.confidence; });
  return map;
}

function renderBoard() {
  const board = $('#board');
  board.innerHTML = '';
  const confidence = confidenceById();

  state.result.platform_outputs.forEach((original) => {
    const platform = original.platform;
    const draft = state.drafts[platform];
    const pack = state.spans[platform] || { flags: [], spans: [], unlocated: [] };

    const wrap = el('article', 'manuscript');
    wrap.dataset.platform = platform;

    const head = el('header', 'manuscript-head');
    const decided = Object.keys(state.decisions).filter((k) => k.startsWith(`${platform}:`)).length;
    const open = pack.flags.length - decided;
    const hedged = (pack.hedged || []).length;
    // Two different things a reviewer looks for, so the header names both. A
    // draft with no overstatements can still rest on shaky evidence, and a bare
    // "0 处存疑" would read as "nothing to do here".
    const counts = [t('board.flagsCount', { count: pack.flags.length })
      + (open ? ` · ${t('board.flagsLeft', { count: open })}` : '')];
    if (hedged) counts.push(t('board.hedgedCount', { count: hedged }));
    head.innerHTML = `<h2>${esc(platformLabel(platform))}</h2>
      <span class="count">${counts.join('　·　')}</span>`;
    wrap.append(head);

    if (pack.unlocated.length) wrap.append(orphanStrip(pack, platform));

    if (draft.title_options.length) {
      wrap.append(fieldLabel(t('board.titles')));
      const list = el('ol', 'titles');
      draft.title_options.forEach((title, i) => {
        const li = el('li');
        li.dataset.field = `title:${i}`;
        li.dataset.platform = platform;
        li.innerHTML = paint(title, pack, platform, `title:${i}`, confidence);
        list.append(li);
      });
      wrap.append(list);
    }
    if (draft.cover_copy) {
      wrap.append(fieldLabel(t('board.cover')));
      const cover = el('p', 'cover');
      cover.dataset.field = 'cover_copy';
      cover.dataset.platform = platform;
      cover.innerHTML = paint(draft.cover_copy, pack, platform, 'cover_copy', confidence);
      wrap.append(cover);
    }
    wrap.append(fieldLabel(t('board.body')));
    const prose = el('div', 'prose');
    prose.dataset.field = 'body';
    prose.dataset.platform = platform;
    prose.innerHTML = paint(draft.body, pack, platform, 'body', confidence);
    wrap.append(prose);

    if (draft.hashtags.length) {
      wrap.append(fieldLabel(t('board.tags')));
      const tags = el('p', 'tags');
      tags.textContent = draft.hashtags.map((h) => `#${h.replace(/^[#＃]+/, '')}`).join(' ');
      wrap.append(tags);
    }
    board.append(wrap);
  });

  if ((state.result.notices || []).length) {
    const notices = el('div', 'notices');
    notices.innerHTML = state.result.notices
      .map((n) => `<div><code>${esc(n.code)}</code> ${esc(n.message)}</div>`).join('');
    board.append(notices);
  }
  board.append(tallyBar());
  board.querySelectorAll('.mark').forEach(bindMark);
  board.querySelectorAll('.cite').forEach(bindCite);
  board.querySelectorAll('.hedged').forEach((node) => {
    node.addEventListener('mouseenter', () => {
      const first = node.querySelector('sup.cite');
      if (first) showCitation(first);
    });
  });
}

function fieldLabel(text) {
  const node = el('div', 'field-label');
  node.textContent = text;
  return node;
}

/* Flags whose sentence cannot be located — because the reviewer quoted text
   that has since been edited away, or because a hand-rewrite cut across it.
   They are shown in full and each still takes a decision: an answerable flag
   that cannot be clicked in the prose would block the review forever. */
function orphanStrip(pack, platform) {
  const node = el('div', 'orphans');
  node.innerHTML = `<h3>${esc(t('board.orphansHead'))}</h3><p>${esc(t('board.orphansBody'))}</p>`;
  const list = el('ul');
  pack.unlocated.forEach((index) => {
    const flag = pack.flags[index];
    const key = flagKey(platform, index);
    const item = el('li');
    item.innerHTML = `${esc(flag.text || '—')} — ${esc(flag.reason)} `;
    if (state.decisions[key]) {
      const done = el('span', 'orphan-done');
      done.textContent = t('board.orphanDone');
      item.append(done);
    } else {
      const ack = el('button', 'btn orphan-ack');
      ack.type = 'button';
      ack.textContent = t('board.orphanAck');
      ack.onclick = () => decide(key, 'accepted');
      item.append(ack);
    }
    list.append(item);
  });
  node.append(list);
  return node;
}

/* Raise "(c1)" / "(c77, c78)" into real superscripts, on already-escaped text.
   This is the reader-facing form of the citation the drafter wrote; the
   parentheses are the wire format and are never shown. */
const CITE_RE = /\s*[（(]\s*(c\d+(?:\s*[,，]\s*c\d+)*)\s*[）)]/g;

/* Render one run of draft text, tagging every piece with the offset it came
   from in the source field. Those offsets are what let a text selection in the
   rendered page map back to an exact slice of the draft — the superscripts do
   not have the same text as their source `(c1)`, so nothing else would line up. */
function renderRun(text, from, to, confidence) {
  const slice = text.slice(from, to);
  let html = '';
  let cursor = 0;
  CITE_RE.lastIndex = 0;
  let match;
  while ((match = CITE_RE.exec(slice)) !== null) {
    if (match.index > cursor) html += segment(slice.slice(cursor, match.index), from + cursor);
    html += citation(match[1], from + match.index, match[0].length, confidence);
    cursor = match.index + match[0].length;
  }
  if (cursor < slice.length) html += segment(slice.slice(cursor), from + cursor);
  return html;
}

function segment(text, offset) {
  return `<span class="seg" data-off="${offset}">${esc(text)}</span>`;
}

/* Every id in one marker shares that marker's source range: `(c1, c2)` is a
   single stretch of text, so a selection touching either superscript covers
   the whole thing. */
function citation(group, offset, length, confidence) {
  return group.split(/[,，]/).map((s) => s.trim()).filter(Boolean).map((id) => {
    const level = confidence[id] || '';
    const caution = HEDGED.has(level) ? ` · ${t('ledger.hedged')}` : '';
    return `<sup class="cite" role="button" tabindex="0" data-claim="${id}"`
      + ` data-confidence="${esc(level)}" data-off="${offset}" data-len="${length}"`
      + ` aria-label="${esc(t('flag.ledger'))} ${id}${caution}" title="${esc(t('flag.ledger'))} · ${id}${caution}">${id}</sup>`;
  }).join('');
}

/* Paint one field. Two kinds of span can be marked — a flag (this sentence
   outran its source) and a hedged sentence (its source is itself uncertain).
   The server keeps them disjoint, so they paint in one pass without nesting. */
function paint(text, pack, platform, field, confidence) {
  const marks = [
    ...pack.spans.filter((s) => s.field === field).map((s) => ({ ...s, kind: 'flag' })),
    ...(pack.hedged || []).filter((s) => s.field === field).map((s) => ({ ...s, kind: 'hedged' })),
  ].sort((a, b) => a.start - b.start);

  let html = '';
  let cursor = 0;
  marks.forEach((span) => {
    if (span.start < cursor) return;   // never nest one mark inside another
    html += renderRun(text, cursor, span.start, confidence);
    html += span.kind === 'flag'
      ? flagMark(text, span, pack, platform, confidence)
      : hedgedMark(text, span, confidence);
    cursor = span.end;
  });
  return html + renderRun(text, cursor, text.length, confidence);
}

function flagMark(text, span, pack, platform, confidence) {
  const flag = pack.flags[span.flag_index];
  const key = flagKey(platform, span.flag_index);
  const decision = state.decisions[key] || '';
  return `<span class="mark" tabindex="0" role="button" data-key="${esc(key)}"`
    + ` data-state="${esc(decision)}" aria-label="${esc(t('flag.head'))}: ${esc(flag.reason)}">`
    + renderRun(text, span.start, span.end, confidence)
    + '<sup class="query" aria-hidden="true">?</sup></span>';
}

function hedgedMark(text, span, confidence) {
  const ids = span.claim_ids.join(' ');
  return `<span class="hedged" data-claims="${esc(ids)}"`
    + ` title="${esc(t('ledger.hedged'))} (${esc(ids)})"`
    + ` aria-label="${esc(t('ledger.hedged'))}: ${esc(ids)}">`
    + renderRun(text, span.start, span.end, confidence)
    + '</span>';
}

function tallyBar() {
  const total = Object.values(state.spans).reduce((n, p) => n + p.flags.length, 0);
  const done = Object.keys(state.decisions).length;
  const bar = el('div', 'tally');
  const count = el('span', 'tally-count');
  count.textContent = t('board.tally', { done, total });

  const complete = el('button', 'btn btn-solid');
  complete.textContent = t('board.complete');
  complete.onclick = completeReview;
  if (done < total) {
    complete.disabled = true;
    complete.title = t('board.completeBlocked');
  }
  const restart = el('button', 'btn btn-quiet');
  restart.textContent = t('board.newPaper');
  restart.onclick = () => { resetRun(); askNext(); };

  bar.append(count, complete, restart);
  return bar;
}

/* ── flag popover ───────────────────────────────────────────────────── */

/* A citation is the sentence -> ledger direction of the same link. */
function bindCite(cite) {
  const show = () => showCitation(cite);
  cite.addEventListener('click', show);
  cite.addEventListener('mouseenter', show);
  cite.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); show(); }
  });
}

function showCitation(cite) {
  closeFlag();
  const claimId = cite.dataset.claim;
  state.openKey = `cite:${claimId}`;
  cite.dataset.open = '1';
  drawTether(cite, claimId);
}

/* And the ledger -> sentence direction: every sentence resting on this claim. */
function bindClaim(row) {
  row.addEventListener('click', (event) => {
    if (event.target.closest('details')) return;   // the evidence toggle is its own control
    const claimId = row.dataset.id;
    const citing = [...document.querySelectorAll(`.cite[data-claim="${CSS.escape(claimId)}"]`)];
    document.querySelectorAll('.cite[data-open="1"]').forEach((c) => delete c.dataset.open);
    if (!citing.length) {
      toast(t('ledger.notCited', { id: claimId }));
      return;
    }
    closeFlag();
    citing.forEach((c) => { c.dataset.open = '1'; });
    citing[0].scrollIntoView({ block: 'center', behavior: 'smooth' });
    state.openKey = `cite:${claimId}`;
    drawTether(citing[0], claimId);
  });
}

function bindMark(mark) {
  const open = () => openFlag(mark);
  mark.addEventListener('click', open);
  mark.addEventListener('mouseenter', open);
  mark.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
}

function closeFlag() {
  $('#popover').hidden = true;
  $('#tether').innerHTML = '';
  document.querySelectorAll('.mark[data-open="1"]').forEach((m) => delete m.dataset.open);
  document.querySelectorAll('.cite[data-open="1"]').forEach((c) => delete c.dataset.open);
  document.querySelectorAll('.claim[data-lit="1"]').forEach((c) => delete c.dataset.lit);
  state.openKey = null;
}

function openFlag(mark) {
  const key = mark.dataset.key;
  if (state.openKey === key) return;
  closeFlag();
  state.openKey = key;
  mark.dataset.open = '1';

  const [platform, indexText] = key.split(':');
  const flag = state.spans[platform].flags[Number(indexText)];
  const claimId = (flag.reason.match(/^\[(c\d+)\]/) || [])[1] || null;

  const pop = $('#popover');
  pop.innerHTML = `<h3>${esc(t('flag.head'))}</h3>
    <p class="reason">${esc(flag.reason)}</p>
    <p class="cite">${claimId ? `${esc(t('flag.ledger'))} · ${esc(claimId)}` : esc(t('flag.noLedger'))}</p>`;

  const actions = el('div', 'pop-actions');
  const accept = el('button', 'btn');
  accept.textContent = t('flag.accept');
  accept.onclick = () => decide(key, 'accepted');
  const rewrite = el('button', 'btn btn-solid');
  rewrite.textContent = t('flag.rewrite');
  rewrite.onclick = () => proposeRewrite(key, mark, rewrite);
  actions.append(accept, rewrite);
  pop.append(actions);

  pop.hidden = false;
  placePopover(pop, mark);
  drawTether(mark, claimId);
}

function placePopover(pop, mark) {
  const box = mark.getBoundingClientRect();
  const width = pop.offsetWidth;
  const left = Math.min(Math.max(8, box.left), window.innerWidth - width - 8);
  const below = box.bottom + 10;
  const fitsBelow = below + pop.offsetHeight < window.innerHeight - 8;
  pop.style.left = `${left}px`;
  pop.style.top = fitsBelow ? `${below}px` : `${Math.max(8, box.top - pop.offsetHeight - 10)}px`;
}

async function proposeRewrite(key, mark, button) {
  const [platform, indexText] = key.split(':');
  const index = Number(indexText);
  const flag = state.spans[platform].flags[index];

  button.disabled = true;
  button.textContent = t('flag.rewriting');
  let proposal;
  try {
    const body = await postJSON('/api/revise', {
      sentence: currentText(mark),
      instruction: flag.reason,
      ledger: state.result.claim_ledger,
      platform,
      language: state.slots.language || 'zh',
      liveliness: state.slots.liveliness || 3,
      context: state.drafts[platform].body.slice(0, 1200),
    });
    proposal = body.sentence;
  } catch (err) {
    button.disabled = false;
    button.textContent = t('flag.rewrite');
    toast(err.message, 'error');
    return;
  }

  const pop = $('#popover');
  pop.querySelector('.pop-actions').remove();
  const area = el('textarea');
  area.value = proposal;
  area.setAttribute('aria-label', t('flag.rewrite'));
  const actions = el('div', 'pop-actions');
  const apply = el('button', 'btn btn-solid');
  apply.textContent = t('flag.apply');
  apply.onclick = () => applyRewrite(key, area.value.trim());
  const cancel = el('button', 'btn');
  cancel.textContent = t('flag.cancel');
  cancel.onclick = closeFlag;
  actions.append(apply, cancel);
  pop.append(area, actions);
  placePopover(pop, mark);
  area.focus();
}

function currentText(mark) {
  const clone = mark.cloneNode(true);
  clone.querySelectorAll('sup').forEach((s) => s.remove());
  return clone.textContent;
}

/* ── editing a draft field ───────────────────────────────────────────── */

function readField(platform, field) {
  const draft = state.drafts[platform];
  if (field === 'body') return draft.body;
  if (field === 'cover_copy') return draft.cover_copy;
  return draft.title_options[Number(field.split(':')[1])];
}

function writeField(platform, field, value) {
  const draft = state.drafts[platform];
  if (field === 'body') draft.body = value;
  else if (field === 'cover_copy') draft.cover_copy = value;
  else draft.title_options[Number(field.split(':')[1])] = value;
}

/* Replace [start, end) and move every span the edit displaced.
   A span the edit CUTS INTO is not moved — it is retired to the unlocated
   list, because after an arbitrary edit we can no longer honestly say where
   that flag's sentence is. Silently keeping a stale offset would paint the
   red mark over the wrong words. */
function spliceField(platform, field, start, end, replacement) {
  const text = readField(platform, field);
  writeField(platform, field, text.slice(0, start) + replacement + text.slice(end));

  const delta = replacement.length - (end - start);
  const pack = state.spans[platform];
  const kept = [];
  pack.spans.forEach((span) => {
    if (span.field !== field) { kept.push(span); return; }
    if (span.start >= end) { span.start += delta; span.end += delta; kept.push(span); return; }
    if (span.end <= start) { kept.push(span); return; }

    if (span.start === start && span.end === end) {
      // The edit replaced exactly the flagged sentence, so it IS that flag's
      // rewrite — however the human got here. The flag has to follow its text,
      // or the board keeps a red mark and a reason about words that are gone.
      span.end = start + replacement.length;
      pack.flags[span.flag_index].text = replacement;
      state.decisions[flagKey(platform, span.flag_index)] = 'rewritten';
      kept.push(span);
      return;
    }
    if (!pack.unlocated.includes(span.flag_index)) pack.unlocated.push(span.flag_index);
  });
  pack.spans = kept;
}

function applyRewrite(key, replacement) {
  if (!replacement) { toast(t('rewrite.empty'), 'error'); return; }
  const [platform, indexText] = key.split(':');
  const index = Number(indexText);
  const pack = state.spans[platform];
  const span = pack.spans.find((s) => s.flag_index === index);
  if (!span) { toast(t('board.orphansHead'), 'error'); return; }

  spliceField(platform, span.field, span.start, span.end, replacement);
  pack.flags[index].text = replacement;
  decide(key, 'rewritten');
}

function decide(key, outcome) {
  state.decisions[key] = outcome;
  closeFlag();
  renderBoard();
}

/* ── selecting a passage to rewrite ──────────────────────────────────── */

/* Map one edge of a DOM selection back to an offset in the source field.
   `.seg` nodes carry the offset their text starts at; a citation superscript
   is atomic, because its rendered text ("c1") is not its source text ("(c1)"). */
function edgeOffset(node, offset, edge) {
  const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  if (!element) return null;
  const holder = element.closest('.seg, sup.cite');
  if (!holder) return null;
  const host = holder.closest('[data-field]');
  if (!host) return null;

  let position;
  if (holder.classList.contains('cite')) {
    position = Number(holder.dataset.off) + (edge === 'end' ? Number(holder.dataset.len) : 0);
  } else {
    position = Number(holder.dataset.off)
      + (node.nodeType === Node.TEXT_NODE ? offset : (edge === 'end' ? holder.textContent.length : 0));
  }
  return { position, field: host.dataset.field, platform: host.dataset.platform };
}

/* The current selection as an exact slice of one draft field, or null. */
function selectedPassage() {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!range.commonAncestorContainer.parentElement?.closest('#board')) return null;

  const from = edgeOffset(range.startContainer, range.startOffset, 'start');
  const to = edgeOffset(range.endContainer, range.endOffset, 'end');
  if (!from || !to) return null;
  // A selection spanning two fields has no single passage to replace.
  if (from.field !== to.field || from.platform !== to.platform) return null;

  const start = Math.min(from.position, to.position);
  const end = Math.max(from.position, to.position);
  if (end - start < 2) return null;

  const text = readField(from.platform, from.field);
  return { platform: from.platform, field: from.field, start, end, text: text.slice(start, end) };
}

function offerRewrite() {
  const passage = selectedPassage();
  if (!passage) { $('#pill').hidden = true; return; }

  const rect = window.getSelection().getRangeAt(0).getBoundingClientRect();
  const pill = $('#pill');
  pill.hidden = false;
  pill.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - pill.offsetWidth - 8))}px`;
  pill.style.top = `${Math.max(8, rect.top - pill.offsetHeight - 8)}px`;
  pill.onclick = () => openRewriter(passage);
}

/* ── the rewrite conversation ────────────────────────────────────────── */

function openRewriter(passage) {
  closeFlag();
  $('#pill').hidden = true;
  state.rewriter = { ...passage, proposal: '', turns: [] };
  const panel = $('#rewriter');
  panel.hidden = false;
  panel.querySelector('.rw-target').textContent = passage.text;
  panel.querySelector('.rw-thread').innerHTML = '';
  const ask = panel.querySelector('textarea');
  ask.value = '';
  panel.querySelector('.rw-apply').disabled = true;
  placeRewriter();
  ask.focus();
}

function placeRewriter() {
  const panel = $('#rewriter');
  const rect = window.getSelection().rangeCount
    ? window.getSelection().getRangeAt(0).getBoundingClientRect()
    : { left: 120, bottom: 160 };
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - panel.offsetWidth - 8));
  const top = Math.min(rect.bottom + 12, window.innerHeight - panel.offsetHeight - 12);
  panel.style.left = `${left}px`;
  panel.style.top = `${Math.max(8, top)}px`;
}

function rewriterTurn(who, text, editable = false) {
  const thread = $('#rewriter .rw-thread');
  const turn = el('div', `rw-turn rw-${who}`);
  turn.innerHTML = `<span class="rw-who">${esc(who === 'you' ? t('rewrite.you') : t('rewrite.agent'))}</span>`;
  if (editable) {
    const area = el('textarea', 'rw-proposal');
    area.value = text;
    area.setAttribute('aria-label', t('rewrite.head'));
    turn.append(area);
  } else {
    const body = el('div', 'rw-text');
    body.textContent = text;
    turn.append(body);
  }
  thread.append(turn);
  thread.scrollTop = thread.scrollHeight;
  return turn;
}

async function sendRewrite() {
  const panel = $('#rewriter');
  const ask = panel.querySelector('textarea.rw-ask');
  const instruction = ask.value.trim();
  if (!instruction) { toast(t('rewrite.needInstruction')); return; }

  const rw = state.rewriter;
  // an earlier proposal the editor kept tweaking is what the next pass refines
  const standing = panel.querySelector('.rw-proposal');
  if (standing) rw.proposal = standing.value.trim();

  rewriterTurn('you', instruction);
  ask.value = '';
  const send = panel.querySelector('.rw-send');
  send.disabled = true;
  send.textContent = t('flag.rewriting');

  try {
    const body = await postJSON('/api/revise', {
      sentence: rw.text,
      instruction,
      previous: rw.proposal,
      ledger: state.result.claim_ledger,
      platform: rw.platform,
      language: state.slots.language || 'zh',
      liveliness: state.slots.liveliness || 3,
      context: readField(rw.platform, rw.field).slice(0, 1200),
    });
    panel.querySelectorAll('.rw-proposal').forEach((a) => {
      const frozen = el('div', 'rw-text');
      frozen.textContent = a.value;
      a.replaceWith(frozen);
    });
    rw.proposal = body.sentence;
    rewriterTurn('agent', body.sentence, true);
    panel.querySelector('.rw-apply').disabled = false;
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    send.disabled = false;
    send.textContent = t('rewrite.send');
  }
}

function applyRewriter() {
  const panel = $('#rewriter');
  const standing = panel.querySelector('.rw-proposal');
  const replacement = (standing ? standing.value : state.rewriter.proposal).trim();
  if (!replacement) { toast(t('rewrite.empty'), 'error'); return; }

  const rw = state.rewriter;
  spliceField(rw.platform, rw.field, rw.start, rw.end, replacement);
  closeRewriter();
  renderBoard();
  toast(t('rewrite.applied'));
}

function closeRewriter() {
  $('#rewriter').hidden = true;
  $('#pill').hidden = true;
  state.rewriter = null;
  window.getSelection().removeAllRanges();
}

/* ── the tether: draw the provenance link ───────────────────────────── */

function drawTether(anchor, claimId) {
  const svg = $('#tether');
  svg.innerHTML = '';
  svg.classList.toggle('untethered', !claimId);
  svg.classList.toggle('citation', anchor.classList.contains('cite'));
  if (window.innerWidth <= 1180) return;   // the apparatus is stacked, not beside

  const from = anchor.getBoundingClientRect();
  const column = anchor.closest('.manuscript').getBoundingClientRect();
  const target = claimId ? document.querySelector(`.claim[data-id="${CSS.escape(claimId)}"]`) : null;
  const rail = $('#apparatus').getBoundingClientRect();
  if (target) target.dataset.lit = '1';

  // The line leaves from the GUTTER, level with the flagged line, never from
  // the sentence itself — a tether drawn across the prose strikes through the
  // words the reader is trying to judge.
  const x1 = column.right + 14;
  const y1 = from.top + from.height / 2;
  const x2 = rail.left + 12;
  const y2 = target ? target.getBoundingClientRect().top + 14 : y1;
  const mid = x1 + (x2 - x1) / 2;

  svg.innerHTML = `<path d="M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}"/>`
    + `<circle cx="${x1}" cy="${y1}" r="2"/>`
    + `<circle cx="${x2}" cy="${y2}" r="${target ? 2.5 : 4}"/>`;
  if (target) target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/* ── apparatus ──────────────────────────────────────────────────────── */

function renderApparatus() {
  const rail = $('#apparatus');
  const result = state.result;
  rail.innerHTML = '';

  const ledger = el('section');
  ledger.innerHTML = `<h2>${esc(t('ledger.head'))}</h2>
    <p class="note">${esc(t('ledger.note'))}</p>`;
  const hedged = result.claim_ledger.filter((c) => HEDGED.has(c.confidence));
  if (hedged.length) {
    const banner = el('div', 'hedged-count');
    banner.textContent = t('ledger.hedgedBanner', {
      count: hedged.length, ids: hedged.map((c) => c.id).join(' '),
    });
    ledger.append(banner);
  }
  result.claim_ledger.forEach((claim) => {
    const node = el('div', 'claim');
    node.dataset.id = claim.id;
    node.dataset.confidence = claim.confidence;
    const caution = HEDGED.has(claim.confidence)
      ? `<span class="claim-caution">${esc(t('ledger.hedged'))}</span> · ` : '';
    node.innerHTML = `<span class="claim-id">${esc(claim.id)}</span>${esc(claim.claim)}
      <span class="claim-meta">${caution}${esc(claim.confidence)}${claim.qualifier ? ` · ${esc(claim.qualifier)}` : ''}</span>
      <details><summary>${esc(t('ledger.evidence'))}</summary><blockquote>${esc(claim.source_evidence)}</blockquote></details>`;
    node.tabIndex = 0;
    bindClaim(node);
    ledger.append(node);
  });
  rail.append(ledger);

  if ((result.background_materials || []).length) {
    const sources = el('section');
    sources.innerHTML = `<h2>${esc(t('ledger.background'))}</h2>
      <p class="note">${esc(t('ledger.backgroundNote'))}</p>`;
    result.background_materials.forEach((m) => {
      const node = el('div', 'source');
      const title = esc(m.source_title || m.source_url);
      node.innerHTML = m.source_url
        ? `<a href="${esc(m.source_url)}" target="_blank" rel="noopener">${title}</a>`
        : title;
      if (m.relation) node.innerHTML += `<span>${esc(m.relation)}</span>`;
      sources.append(node);
    });
    rail.append(sources);
  }

}

/* ── completing the review ──────────────────────────────────────────── */

async function completeReview() {
  const platforms = Object.keys(state.drafts);
  toast(t('save.rechecking'));

  let reopened = 0;
  for (const platform of platforms) {
    const body = await postJSON('/api/recheck', {
      draft: state.drafts[platform],
      ledger: state.result.claim_ledger,
      language: state.slots.language || 'zh',
    });
    if (body.flags.length) {
      reopened += body.flags.length;
      state.spans[platform] = { flags: body.flags, spans: body.spans, unlocated: body.unlocated };
      Object.keys(state.decisions)
        .filter((k) => k.startsWith(`${platform}:`))
        .forEach((k) => delete state.decisions[k]);
    }
  }

  if (reopened) {
    renderBoard();
    toast(t('save.reopened', { count: reopened }), 'error');
    return;
  }
  offerSave();
}

function offerSave() {
  const board = $('#board');
  const panel = el('div', 'manuscript');
  panel.innerHTML = `<div class="manuscript-head"><h2>${esc(t('save.head'))}</h2></div>
    <p>${esc(t('save.clean'))}</p>`;

  const slip = el('div', 'slip');
  slip.innerHTML = `<dl><dt>${esc(t('save.folder'))}</dt><dd><code>outputs/reviews/</code></dd></dl>`;
  const name = el('input');
  name.type = 'text';
  name.value = suggestName();
  name.setAttribute('aria-label', t('save.filename'));
  name.style.cssText = 'width:100%;padding:7px 9px;border:1px solid var(--rule);font:400 13px var(--mono)';

  const actions = el('div', 'chips');
  const save = el('button', 'btn btn-solid');
  save.textContent = t('save.do');
  save.onclick = async () => {
    save.disabled = true;
    try {
      const out = structuredClone(state.result);
      out.platform_outputs = Object.values(state.drafts);
      const body = await postJSON('/api/save', { filename: name.value, result: out });
      slip.innerHTML = `<dl><dt>${esc(t('save.written'))}</dt><dd>${body.written.map((p) => esc(p)).join('<br>')}</dd></dl>`;
    } catch (err) {
      save.disabled = false;
      toast(err.message, 'error');
    }
  };
  const skip = el('button', 'btn btn-quiet');
  skip.textContent = t('save.skip');
  skip.onclick = () => panel.remove();
  actions.append(save, skip);
  slip.append(name, actions);
  panel.append(slip);
  board.prepend(panel);
  board.scrollTop = 0;
}

function suggestName() {
  const source = state.slots.source || 'review';
  const stem = source.split(/[/\\]/).pop().replace(/\.pdf$/i, '').replace(/[^0-9A-Za-z._-]+/g, '-');
  return (stem || 'review').slice(0, 60);
}

/* ── history: the way back to earlier work ──────────────────────────── */

async function loadHistory() {
  try {
    const body = await api('/api/history?limit=50');
    state.history = body.runs;
  } catch (err) {
    state.history = [];
  }
  renderHistory();
}

function renderHistory() {
  const host = $('#history');
  if (!host) return;
  host.innerHTML = '';

  if (!state.history.length) {
    const empty = el('p', 'note');
    empty.textContent = t('history.empty');
    host.append(empty);
    return;
  }

  state.history.forEach((run) => {
    const row = el('button', 'histrow');
    row.type = 'button';
    if (run.session_id === state.sessionId) row.dataset.open = '1';
    row.innerHTML = `<span class="hist-title">${esc(run.title)}</span>`
      + `<span class="hist-meta">${esc(stamp(run.created_at))} · ${esc(run.platforms.join(' · '))}`
      + ` · ${esc(t('history.claims', { count: run.claims }))}</span>`;
    row.onclick = () => reopen(run.session_id);
    host.append(row);
  });
}

function stamp(seconds) {
  const d = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* Reopening gives the ORIGINAL draft: review decisions are not persisted, so
   pretending to resume one would quietly lose the earlier pass. */
async function reopen(sessionId) {
  clearInterval(state.polling);
  state.sessionId = sessionId;
  state.decisions = {};
  state.drafts = {};
  try {
    await loadResult();
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  renderHistory();
  toast(t('history.reopenNote'));
}

/* ── settings dialog ────────────────────────────────────────────────── */

function openSettings(tab = 'models') {
  $('#settings').hidden = false;
  $('#settings-scrim').hidden = false;
  showTab(tab);
  const first = $('#settings .dlg-tab');
  if (first) first.focus();
}

function closeSettings() {
  if ($('#settings').hidden) return;
  $('#settings').hidden = true;
  $('#settings-scrim').hidden = true;
  $('#open-settings').focus();
}

function showTab(name) {
  document.querySelectorAll('#settings .dlg-tab').forEach((tab) => {
    const on = tab.dataset.tab === name;
    tab.dataset.on = on ? '1' : '';
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('#settings .dlg-panel').forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
}

/* ── sidebar ────────────────────────────────────────────────────────── */

async function loadSettings() {
  const [settings, providers] = await Promise.all([
    api('/api/settings'),
    api('/api/providers'),
  ]);
  state.settings = settings;
  state.providers = providers.providers;
  renderSettings();
}

function renderSettings() {
  if (!state.settings) return;
  renderRoles();
  renderKeys();
  renderSources();
  $('#rule3').hidden = state.settings.drafter_reviewer_distinct;
}

/* One role row: which provider, which model, and how sure we are it works.
   Both are closed <select>s now, built from the keys this machine actually
   holds — a free-text field let you save a provider with no key and only find
   out mid-run. A configured value that is no longer available is KEPT as a
   disabled option rather than dropped, so opening settings can never silently
   rewrite a working config. */
function renderRoles() {
  const host = $('#roles');
  host.innerHTML = '';

  const usable = state.providers.filter((p) => p.available);
  if (!usable.length) {
    const empty = el('p', 'note note-warn');
    empty.textContent = t('settings.noKeys');
    host.append(empty);
    return;
  }

  state.settings.roles.forEach((role) => {
    const spec = state.settings.models[role];
    const node = el('div', 'role');
    node.innerHTML = `<div class="role-head">
        <span class="dot" data-state="${spec.resolved ? 'resolved' : ''}" id="dot-${role}"></span>
        <span class="role-name">${role}</span>
        <span class="role-hint">${esc(ROLE_HINTS[role] || '')}</span>
      </div>`;

    const inputs = el('div', 'role-inputs');
    const provider = el('select');
    provider.id = `provider-${role}`;
    provider.setAttribute('aria-label', `${role} provider`);
    provider.append(option('', t('settings.pickProvider')));
    usable.forEach((p) => provider.append(option(p.id, p.label)));
    if (spec.provider && !usable.some((p) => p.id === spec.provider)) {
      provider.append(option(spec.provider, `${spec.provider} ${t('settings.modelUnavailable')}`, true));
    }
    provider.value = spec.provider || '';

    const model = el('select');
    model.id = `model-${role}`;
    model.setAttribute('aria-label', `${role} model`);
    provider.onchange = () => fillModels(model, provider.value, '');
    inputs.append(provider, model);
    node.append(inputs);
    host.append(node);
    fillModels(model, spec.provider, spec.model);
  });

  const live = usable.filter((p) => p.source === 'live');
  const note = el('p', 'note');
  note.textContent = live.length === usable.length
    ? t('settings.modelsLive')
    : t('settings.modelsFallback');
  host.append(note);
}

function option(value, label, disabled = false) {
  const node = el('option');
  node.value = value;
  node.textContent = label;
  node.disabled = disabled;
  return node;
}

/* Fill a model <select> from what that provider's key allows, keeping a
   configured-but-missing model visible so a save cannot quietly drop it. */
function fillModels(select, providerId, selected) {
  select.innerHTML = '';
  const provider = state.providers.find((p) => p.id === providerId);
  select.append(option('', t('settings.pickModel')));
  (provider ? provider.models : []).forEach((m) => select.append(option(m.id, m.label || m.id)));
  if (selected && !(provider ? provider.models : []).some((m) => m.id === selected)) {
    select.append(option(selected, `${selected} ${t('settings.modelUnavailable')}`));
  }
  select.value = selected || '';
}

/* Keys are read-only here: they are set in .env, and this only reports which
   ones the process can see, so a missing key explains itself instead of
   surfacing later as a failed run. */
function renderKeys() {
  const host = $('#keys');
  host.innerHTML = '';
  state.providers.forEach((provider) => {
    const row = el('div', 'keyrow');
    row.innerHTML = `<span class="key-name">${esc(provider.label)}</span>`
      + `<span class="key-state" data-present="${provider.available ? '1' : '0'}">`
      + `${esc(provider.available ? t('settings.keyPresent') : t('settings.keyMissing'))}</span>`
      + `<code class="key-var">${esc(provider.env_var)}</code>`;
    host.append(row);
  });
}

function renderSources() {
  const host = $('#sources');
  host.innerHTML = '';
  const all = ['arxiv', 'semantic_scholar', 'pubmed', 'crossref', 'wikipedia', 'ddgs', 'tavily'];
  all.forEach((source) => {
    const label = el('label');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = state.settings.search_sources.includes(source);
    box.onchange = async () => {
      const chosen = all.filter((s) => host.querySelector(`input[data-source="${s}"]`).checked);
      try {
        const body = await postJSON('/api/settings/sources', { sources: chosen });
        state.settings.search_sources = body.search_sources;
      } catch (err) { toast(err.message, 'error'); }
    };
    box.dataset.source = source;
    label.append(box, document.createTextNode(source));
    host.append(label);
  });
}

async function saveModels() {
  const models = {};
  state.settings.roles.forEach((role) => {
    const provider = $(`#provider-${role}`);
    const model = $(`#model-${role}`);
    if (!provider || !model) return;           // no keys: nothing was rendered
    models[role] = { provider: provider.value, model: model.value };
  });
  try {
    const body = await postJSON('/api/settings/models', { models });
    state.settings.models = body.models;
    state.settings.drafter_reviewer_distinct = body.drafter_reviewer_distinct;
    $('#rule3').hidden = body.drafter_reviewer_distinct;
    renderRoles();
    toast(t('settings.saved'));
  } catch (err) { toast(err.message, 'error'); }
}

async function verifyModels() {
  toast(t('settings.verifying'));
  try {
    const body = await postJSON('/api/settings/verify', {});
    Object.entries(body.verified).forEach(([role, outcome]) => {
      const dot = $(`#dot-${role}`);
      if (dot) { dot.dataset.state = outcome.ok ? 'verified' : 'failed'; dot.title = outcome.detail; }
    });
    const failed = Object.entries(body.verified).filter(([, o]) => !o.ok);
    toast(failed.length ? t('settings.verifyFailed', { count: failed.length }) : t('settings.verifyOk'),
      failed.length ? 'error' : 'info');
  } catch (err) { toast(err.message, 'error'); }
}

/* ── input handling ─────────────────────────────────────────────────── */

async function acceptFile(file) {
  turn(t('board.you'), esc(file.name), 'you');
  try {
    const body = await api('/api/upload', {
      method: 'POST',
      headers: { 'X-Filename': file.name.replace(/[^\x20-\x7E]/g, '') || 'paper.pdf' },
      body: file,
    });
    state.slots.source = body.source;
    state.slots.source_type = body.source_type;
    askNext();
  } catch (err) {
    turn(t('board.speaker'), `<span style="color:var(--flag)">${esc(err.message)}</span>`);
  }
}

function submitEntry(event) {
  event.preventDefault();
  const input = $('#entry');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  turn(t('board.you'), esc(text), 'you');

  const detected = detectSource(text);
  if (detected) {
    Object.assign(state.slots, detected);
    askNext();
    return;
  }
  turn(t('board.speaker'), esc(t('dialog.unrecognised')));
}

async function init() {
  await loadStrings();
  document.querySelectorAll('[data-lang-switch]').forEach((b) => { b.onclick = toggleLanguage; });
  onLanguageChange(() => {
    // Re-render everything the page drew itself; the markup's own data-i18n
    // nodes are already handled by applyStrings().
    renderSettings();
    renderHistory();
    if (state.result) { renderBoard(); renderApparatus(); }
    else { $('#dialog').innerHTML = ''; greeting(); askNext(); }
  });

  $('#composer').addEventListener('submit', submitEntry);
  $('#save-models').onclick = saveModels;
  $('#verify-models').onclick = verifyModels;
  $('#open-settings').onclick = () => openSettings();
  $('#close-settings').onclick = closeSettings;
  $('#new-run').onclick = () => { resetRun(); renderHistory(); };
  document.querySelectorAll('#settings .dlg-tab').forEach((tab) => {
    tab.onclick = () => showTab(tab.dataset.tab);
  });
  $('#settings-scrim').onclick = closeSettings;

  ['dragover', 'dragenter'].forEach((type) =>
    document.addEventListener(type, (e) => { e.preventDefault(); document.body.classList.add('dropping'); }));
  ['dragleave', 'drop'].forEach((type) =>
    document.addEventListener(type, () => document.body.classList.remove('dropping')));
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) acceptFile(file);
  });

  $('#rewriter .rw-send').onclick = sendRewrite;
  $('#rewriter .rw-apply').onclick = applyRewriter;
  $('#rewriter .rw-cancel').onclick = closeRewriter;
  $('#rewriter .rw-ask').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendRewrite(); }
  });

  // a selection anywhere in a draft offers to rewrite it
  ['mouseup', 'keyup'].forEach((type) =>
    $('#board').addEventListener(type, () => {
      if (!$('#rewriter').hidden) return;   // already in a rewrite
      setTimeout(offerRewrite, 0);          // let the selection settle first
    }));

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('#settings').hidden) closeSettings();
    else if ($('#rewriter').hidden) closeFlag();
    else closeRewriter();
  });
  document.addEventListener('click', (e) => {
    if (!state.openKey) return;
    if (e.target.closest('#popover') || e.target.closest('.mark')) return;
    if (e.target.closest('.cite') || e.target.closest('.claim')) return;
    if (e.target.closest('#rewriter') || e.target.closest('#pill')) return;
    closeFlag();
  });
  window.addEventListener('resize', closeFlag);
  // the tether is drawn in viewport coordinates, so any scroll invalidates it
  $('#board').addEventListener('scroll', closeFlag, { passive: true });
  $('#apparatus').addEventListener('scroll', closeFlag, { passive: true });

  loadSettings().catch((err) => toast(err.message, 'error'));
  loadHistory();
  greeting();
  askNext();
}

init();
