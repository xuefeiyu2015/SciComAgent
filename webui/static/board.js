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
// Suggestions only. Provider strings vary by integration (google_genai vs
// google_vertexai, azure_openai, ...), so this is a datalist and never a
// closed <select> — a fixed list would silently rewrite a working config.
const PROVIDERS = ['anthropic', 'openai', 'google_genai', 'google_vertexai', 'azure_openai', 'ollama'];
const PLATFORM_LABEL = { news: 'News · 新闻稿', xhs: 'Xiaohongshu · 小红书', wechat: 'WeChat · 公众号' };
const POLL_MS = 1500;

const state = {
  slots: { source: null, source_type: null, platforms: null, language: null, liveliness: null },
  sessionId: null,
  result: null,
  spans: {},          // platform -> {flags, spans, unlocated}
  drafts: {},         // platform -> working copy, carrying human edits
  decisions: {},      // "platform:flagIndex" -> "accepted" | "rewritten"
  settings: null,
  polling: null,
  openKey: null,
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
    chip.onclick = () => { chips.remove(); turn('你 · You', esc(text), 'you'); onPick(value); };
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

/* The empty screen is where the product states what it is for. */
function thesis() {
  const node = el('div', 'thesis');
  node.innerHTML = `<h1>每一句话，要么<b>有据可依</b>，要么不能写。</h1>
    <p>把一篇论文交给我，我会起草面向公众的稿子，然后用另一个模型逐句核对。
       任何超出依据清单的说法都会标红，由你决定保留还是改写。稿子永远不会自动发布。</p>
    <p>Hand me a paper. The agent drafts it for a public audience, then a
       <em>different</em> model audits every sentence against the claim ledger.
       Anything that outruns its source is marked in red for you to accept or
       rewrite. Nothing is ever published automatically.</p>`;
  $('#dialog').append(node);
}

function askNext() {
  const s = state.slots;
  if (!s.source) {
    turn('审稿台 · Board', '把论文链接、DOI 或 PDF 给我。<em>Give me a paper: a link, a DOI, or a PDF.</em>');
    return;
  }
  if (!s.platforms) {
    return ask('审稿台 · Board', '要写成哪种稿子？<em>Which kind of piece?</em>', [
      [['news'], '新闻稿', 'News'],
      [['xhs'], '小红书', 'Xiaohongshu'],
      [['news', 'xhs'], '两者都要', 'Both'],
    ], (v) => { s.platforms = v; askNext(); });
  }
  if (!s.language) {
    return ask('审稿台 · Board', '用什么语言？<em>Which language?</em>', [
      ['zh', '中文', 'Chinese'], ['en', 'English', '英文'],
    ], (v) => { s.language = v; askNext(); });
  }
  if (!s.liveliness) {
    return ask('审稿台 · Board', '语气要多活泼？<em>How lively should the tone be? Tone only — never the facts.</em>', [
      [1, '1', '克制 restrained'], [2, '2', ''], [3, '3', '适中 middle'], [4, '4', ''], [5, '5', '活泼 playful'],
    ], (v) => { s.liveliness = v; confirm(); });
  }
  confirm();
}

function confirm() {
  const s = state.slots;
  const node = turn('审稿台 · Board', '准备好了。<em>Ready. Check the slip and start drafting.</em>');
  const slip = el('div', 'slip');
  slip.innerHTML = `<dl>
    <dt>来源 source</dt><dd>${esc(s.source)}</dd>
    <dt>类型 type</dt><dd>${esc(s.source_type)}</dd>
    <dt>平台 platform</dt><dd>${s.platforms.map((p) => esc(PLATFORM_LABEL[p] || p)).join(' · ')}</dd>
    <dt>语言 language</dt><dd>${s.language === 'zh' ? '中文 Chinese' : 'English'}</dd>
    <dt>活泼度 liveliness</dt><dd>${s.liveliness}/5</dd>
  </dl>`;
  const start = el('button', 'btn btn-solid');
  start.textContent = '开始起草 Start drafting';
  start.onclick = () => { slip.remove(); startRun(); };
  const change = el('button', 'btn btn-quiet');
  change.textContent = '重来 Start over';
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
  thesis();
}

/* ── the run ────────────────────────────────────────────────────────── */

async function startRun() {
  const s = state.slots;
  const node = turn('审稿台 · Board', '<div class="progress">正在起草… starting</div><div class="progress-bar"><i style="width:4%"></i></div>');
  try {
    const { session_id } = await postJSON('/api/generate', {
      source: s.source, source_type: s.source_type, platforms: s.platforms,
      language: s.language, liveliness: s.liveliness,
    });
    state.sessionId = session_id;
    poll(node);
  } catch (err) {
    node.remove();
    turn('审稿台 · Board', `<span style="color:var(--flag)">${esc(err.message)}</span>`);
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
}

/* A blocked source is a question, not an error dump: say what to do next. */
function reportTerminal(result) {
  const notice = (result.notices || [])[0];
  const message = notice ? notice.message : '这一篇没能取到可引用的来源。';
  turn('审稿台 · Board', `${esc(message)}`);
  if (notice && (notice.code === 'need_pdf' || notice.code === 'too_short')) {
    turn('审稿台 · Board', '把 PDF 拖进下面的输入框，我再试一次。<em>Drop the PDF into the box below and I will try again.</em>');
    state.slots.source = null;
    state.slots.source_type = null;
  }
}

/* ── manuscript ─────────────────────────────────────────────────────── */

function flagKey(platform, index) { return `${platform}:${index}`; }

function renderBoard() {
  const board = $('#board');
  board.innerHTML = '';

  state.result.platform_outputs.forEach((original) => {
    const platform = original.platform;
    const draft = state.drafts[platform];
    const pack = state.spans[platform] || { flags: [], spans: [], unlocated: [] };

    const wrap = el('article', 'manuscript');
    wrap.dataset.platform = platform;

    const head = el('header', 'manuscript-head');
    const open = pack.flags.length - Object.keys(state.decisions).filter((k) => k.startsWith(`${platform}:`)).length;
    head.innerHTML = `<h2>${esc(PLATFORM_LABEL[platform] || platform)}</h2>
      <span class="count">${pack.flags.length} 处存疑 · ${open} left</span>`;
    wrap.append(head);

    if (pack.unlocated.length) wrap.append(orphanStrip(pack));

    if (draft.title_options.length) {
      wrap.append(fieldLabel('标题选项 · Title options'));
      const list = el('ol', 'titles');
      draft.title_options.forEach((title, i) => {
        const li = el('li');
        li.innerHTML = paint(title, pack, platform, `title:${i}`);
        list.append(li);
      });
      wrap.append(list);
    }
    if (draft.cover_copy) {
      wrap.append(fieldLabel('封面 · Cover'));
      const cover = el('p', 'cover');
      cover.innerHTML = paint(draft.cover_copy, pack, platform, 'cover_copy');
      wrap.append(cover);
    }
    wrap.append(fieldLabel('正文 · Body'));
    const prose = el('div', 'prose');
    prose.innerHTML = paint(draft.body, pack, platform, 'body');
    wrap.append(prose);

    if (draft.hashtags.length) {
      wrap.append(fieldLabel('标签 · Tags'));
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
}

function fieldLabel(text) {
  const node = el('div', 'field-label');
  node.textContent = text;
  return node;
}

function orphanStrip(pack) {
  const node = el('div', 'orphans');
  const items = pack.unlocated
    .map((i) => `<li>${esc(pack.flags[i].text || '(未引用原句)')} — ${esc(pack.flags[i].reason)}</li>`).join('');
  node.innerHTML = `<h3>未定位的存疑 · flags without a matching sentence</h3>
    <p>审校标出了这些问题，但对应的句子已经不在稿子里了，请自行核对。
       <em>The reviewer raised these, but their sentence is no longer in the draft — check them by hand.</em></p>
    <ul>${items}</ul>`;
  return node;
}

/* Raise "(c1)" / "(c77, c78)" into real superscripts, on already-escaped text.
   This is the reader-facing form of the citation the drafter wrote; the
   parentheses are the wire format and are never shown. */
const CITE_RE = /\s*[（(]\s*(c\d+(?:\s*[,，]\s*c\d+)*)\s*[）)]/g;

function raiseCitations(escaped) {
  return escaped.replace(CITE_RE, (_match, group) => {
    const ids = group.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    return ids.map((id) =>
      `<sup class="cite" role="button" tabindex="0" data-claim="${id}"`
      + ` aria-label="依据 ledger ${id}" title="依据 ledger · ${id}">${id}</sup>`).join('');
  });
}

/* Paint one field: escape everything, then wrap the server-supplied spans. */
function paint(text, pack, platform, field) {
  const spans = pack.spans
    .filter((s) => s.field === field)
    .sort((a, b) => a.start - b.start);

  let html = '';
  let cursor = 0;
  spans.forEach((span) => {
    if (span.start < cursor) return;   // never nest a mark inside another
    const flag = pack.flags[span.flag_index];
    const key = flagKey(platform, span.flag_index);
    const decision = state.decisions[key] || '';
    html += raiseCitations(esc(text.slice(cursor, span.start)));
    html += `<span class="mark" tabindex="0" role="button" data-key="${esc(key)}"`
         + ` data-state="${esc(decision)}" aria-label="存疑 flagged: ${esc(flag.reason)}">`
         + raiseCitations(esc(text.slice(span.start, span.end)))
         + '<sup class="query" aria-hidden="true">?</sup></span>';
    cursor = span.end;
  });
  return html + raiseCitations(esc(text.slice(cursor)));
}

function tallyBar() {
  const total = Object.values(state.spans).reduce((n, p) => n + p.flags.length, 0);
  const done = Object.keys(state.decisions).length;
  const bar = el('div', 'tally');
  const count = el('span', 'tally-count');
  count.textContent = `${done} / ${total} 处已处理 · reviewed`;

  const complete = el('button', 'btn btn-solid');
  complete.textContent = '完成审阅 Complete review';
  complete.onclick = completeReview;
  if (done < total) {
    complete.disabled = true;
    complete.title = '还有存疑没有处理 · every flag needs a decision first';
  }
  const restart = el('button', 'btn btn-quiet');
  restart.textContent = '换一篇 New paper';
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
      toast(`正文里没有引用 ${claimId} · nothing in the draft cites ${claimId}`);
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
  pop.innerHTML = `<h3>存疑 · overstatement</h3>
    <p class="reason">${esc(flag.reason)}</p>
    <p class="cite">${claimId ? `依据 ledger · ${esc(claimId)}` : '没有对应的依据条目 · no matching ledger entry'}</p>`;

  const actions = el('div', 'pop-actions');
  const accept = el('button', 'btn');
  accept.textContent = '保留原句 Accept as written';
  accept.onclick = () => decide(key, 'accepted');
  const rewrite = el('button', 'btn btn-solid');
  rewrite.textContent = '改写 Rewrite';
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
  button.textContent = '改写中… Rewriting';
  let proposal;
  try {
    const body = await postJSON('/api/revise', {
      sentence: currentText(mark),
      flag,
      ledger: state.result.claim_ledger,
      platform,
      language: state.slots.language || 'zh',
      liveliness: state.slots.liveliness || 3,
      context: state.drafts[platform].body.slice(0, 1200),
    });
    proposal = body.sentence;
  } catch (err) {
    button.disabled = false;
    button.textContent = '改写 Rewrite';
    toast(err.message, 'error');
    return;
  }

  const pop = $('#popover');
  pop.querySelector('.pop-actions').remove();
  const area = el('textarea');
  area.value = proposal;
  area.setAttribute('aria-label', '改写后的句子 revised sentence');
  const actions = el('div', 'pop-actions');
  const apply = el('button', 'btn btn-solid');
  apply.textContent = '应用 Apply';
  apply.onclick = () => applyRewrite(key, area.value.trim());
  const cancel = el('button', 'btn');
  cancel.textContent = '取消 Cancel';
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

/* Splice the new sentence in and shift every later span in the same field.
   Doing the arithmetic here avoids a round trip, and the offsets stay exactly
   what the server computed for everything the edit did not touch. */
function applyRewrite(key, replacement) {
  if (!replacement) { toast('改写不能是空的 · the rewrite cannot be empty', 'error'); return; }
  const [platform, indexText] = key.split(':');
  const index = Number(indexText);
  const pack = state.spans[platform];
  const span = pack.spans.find((s) => s.flag_index === index);
  if (!span) { toast('这处存疑没有位置，无法自动替换 · no position for this flag', 'error'); return; }

  const draft = state.drafts[platform];
  const field = span.field;
  const read = () => (field === 'body' ? draft.body
    : field === 'cover_copy' ? draft.cover_copy
    : draft.title_options[Number(field.split(':')[1])]);
  const write = (value) => {
    if (field === 'body') draft.body = value;
    else if (field === 'cover_copy') draft.cover_copy = value;
    else draft.title_options[Number(field.split(':')[1])] = value;
  };

  const text = read();
  write(text.slice(0, span.start) + replacement + text.slice(span.end));

  const delta = replacement.length - (span.end - span.start);
  pack.spans.forEach((other) => {
    if (other.field !== field) return;
    if (other.start > span.start) { other.start += delta; other.end += delta; }
  });
  span.end = span.start + replacement.length;
  pack.flags[index].text = replacement;

  decide(key, 'rewritten');
}

function decide(key, outcome) {
  state.decisions[key] = outcome;
  closeFlag();
  renderBoard();
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
  ledger.innerHTML = `<h2>依据清单 <em>Claim ledger</em></h2>
    <p class="note">点一条，看正文里哪句引用了它 · click one to find the sentences citing it</p>`;
  result.claim_ledger.forEach((claim) => {
    const node = el('div', 'claim');
    node.dataset.id = claim.id;
    node.innerHTML = `<span class="claim-id">${esc(claim.id)}</span>${esc(claim.claim)}
      <span class="claim-meta">${esc(claim.confidence)}${claim.qualifier ? ` · ${esc(claim.qualifier)}` : ''}</span>
      <details><summary>原文依据 evidence</summary><blockquote>${esc(claim.source_evidence)}</blockquote></details>`;
    node.tabIndex = 0;
    bindClaim(node);
    ledger.append(node);
  });
  rail.append(ledger);

  if ((result.background_materials || []).length) {
    const sources = el('section');
    sources.innerHTML = `<h2>背景来源 <em>Background</em></h2>
      <p class="note">只用于行文，不是事实来源 · framing only, never facts</p>`;
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
  toast('正在复核改动… re-checking your edits');

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
    toast(`复核又发现 ${reopened} 处存疑，请继续 · the re-check found ${reopened} more`, 'error');
    return;
  }
  offerSave();
}

function offerSave() {
  const board = $('#board');
  const panel = el('div', 'manuscript');
  panel.innerHTML = `<div class="manuscript-head"><h2>保存 · Save</h2></div>
    <p>复核通过，没有新的存疑。要存到本机吗？
       <em>The re-check came back clean. Save it to this machine?</em></p>`;

  const slip = el('div', 'slip');
  slip.innerHTML = `<dl><dt>目录 folder</dt><dd><code>outputs/reviews/</code></dd></dl>`;
  const name = el('input');
  name.type = 'text';
  name.value = suggestName();
  name.setAttribute('aria-label', '文件名 filename');
  name.style.cssText = 'width:100%;padding:7px 9px;border:1px solid var(--rule);font:400 13px var(--mono)';

  const actions = el('div', 'chips');
  const save = el('button', 'btn btn-solid');
  save.textContent = '保存 Save';
  save.onclick = async () => {
    save.disabled = true;
    try {
      const out = structuredClone(state.result);
      out.platform_outputs = Object.values(state.drafts);
      const body = await postJSON('/api/save', { filename: name.value, result: out });
      slip.innerHTML = `<dl><dt>已写入 written</dt><dd>${body.written.map((p) => esc(p)).join('<br>')}</dd></dl>`;
    } catch (err) {
      save.disabled = false;
      toast(err.message, 'error');
    }
  };
  const skip = el('button', 'btn btn-quiet');
  skip.textContent = '暂不保存 Not now';
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

/* ── sidebar ────────────────────────────────────────────────────────── */

async function loadSettings() {
  state.settings = await api('/api/settings');
  renderRoles();
  renderKeys();
  renderSources();
  $('#rule3').hidden = state.settings.drafter_reviewer_distinct;
}

function renderRoles() {
  const host = $('#roles');
  host.innerHTML = '';
  const options = el('datalist');
  options.id = 'provider-options';
  PROVIDERS.forEach((p) => { const o = el('option'); o.value = p; options.append(o); });
  host.append(options);
  state.settings.roles.forEach((role) => {
    const spec = state.settings.models[role];
    const node = el('div', 'role');
    node.innerHTML = `<div class="role-head">
        <span class="dot" data-state="${spec.resolved ? 'resolved' : ''}" id="dot-${role}"></span>
        <span class="role-name">${role}</span>
        <span class="role-hint">${esc(ROLE_HINTS[role] || '')}</span>
      </div>`;
    const inputs = el('div', 'role-inputs');
    const provider = el('input');
    provider.id = `provider-${role}`;
    provider.value = spec.provider;
    provider.placeholder = 'provider';
    provider.setAttribute('list', 'provider-options');
    provider.setAttribute('aria-label', `${role} provider`);
    const model = el('input');
    model.id = `model-${role}`;
    model.value = spec.model;
    model.placeholder = 'model id';
    model.setAttribute('aria-label', `${role} model`);
    inputs.append(provider, model);
    node.append(inputs);
    host.append(node);
  });
}

function renderKeys() {
  const host = $('#keys');
  host.innerHTML = '';
  state.settings.key_names.forEach((name) => {
    const present = state.settings.keys[name];
    const row = el('div', 'keyrow');
    const label = el('label');
    label.setAttribute('for', `key-${name}`);
    label.textContent = name.replace(/_API_KEY$/, '');
    const input = el('input');
    input.id = `key-${name}`;
    input.type = 'password';
    input.placeholder = present ? '已设置 set · 输入以替换' : '未设置 not set';
    input.onchange = async () => {
      try {
        const body = await postJSON('/api/settings/keys', { keys: { [name]: input.value } });
        state.settings.keys = body.keys;
        input.value = '';
        renderKeys();
        toast(`${name} 已保存 · saved`);
      } catch (err) { toast(err.message, 'error'); }
    };
    row.append(label, input);
    if (present) { const tick = el('span', 'set'); tick.textContent = '✓'; row.append(tick); }
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
    models[role] = {
      provider: $(`#provider-${role}`).value,
      model: $(`#model-${role}`).value.trim(),
    };
  });
  try {
    const body = await postJSON('/api/settings/models', { models });
    state.settings.models = body.models;
    state.settings.drafter_reviewer_distinct = body.drafter_reviewer_distinct;
    $('#rule3').hidden = body.drafter_reviewer_distinct;
    renderRoles();
    toast('模型已保存 · models saved');
  } catch (err) { toast(err.message, 'error'); }
}

async function verifyModels() {
  toast('正在验证… verifying');
  try {
    const body = await postJSON('/api/settings/verify', {});
    Object.entries(body.verified).forEach(([role, outcome]) => {
      const dot = $(`#dot-${role}`);
      if (dot) { dot.dataset.state = outcome.ok ? 'verified' : 'failed'; dot.title = outcome.detail; }
    });
    const failed = Object.entries(body.verified).filter(([, o]) => !o.ok);
    toast(failed.length ? `${failed.length} 个角色没通过 · hover a dot for why` : '全部通过 · all roles verified',
      failed.length ? 'error' : 'info');
  } catch (err) { toast(err.message, 'error'); }
}

/* ── input handling ─────────────────────────────────────────────────── */

async function acceptFile(file) {
  turn('你 · You', esc(file.name), 'you');
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
    turn('审稿台 · Board', `<span style="color:var(--flag)">${esc(err.message)}</span>`);
  }
}

function submitEntry(event) {
  event.preventDefault();
  const input = $('#entry');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  turn('你 · You', esc(text), 'you');

  const detected = detectSource(text);
  if (detected) {
    Object.assign(state.slots, detected);
    askNext();
    return;
  }
  turn('审稿台 · Board', '没认出这是链接、DOI 还是 PDF。<em>That is not a link, a DOI, or a PDF — try pasting the paper URL.</em>');
}

function init() {
  $('#composer').addEventListener('submit', submitEntry);
  $('#save-models').onclick = saveModels;
  $('#verify-models').onclick = verifyModels;

  ['dragover', 'dragenter'].forEach((type) =>
    document.addEventListener(type, (e) => { e.preventDefault(); document.body.classList.add('dropping'); }));
  ['dragleave', 'drop'].forEach((type) =>
    document.addEventListener(type, () => document.body.classList.remove('dropping')));
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) acceptFile(file);
  });

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeFlag(); });
  document.addEventListener('click', (e) => {
    if (!state.openKey) return;
    if (e.target.closest('#popover') || e.target.closest('.mark')) return;
    if (e.target.closest('.cite') || e.target.closest('.claim')) return;
    closeFlag();
  });
  window.addEventListener('resize', closeFlag);
  // the tether is drawn in viewport coordinates, so any scroll invalidates it
  $('#board').addEventListener('scroll', closeFlag, { passive: true });
  $('#apparatus').addEventListener('scroll', closeFlag, { passive: true });

  loadSettings().catch((err) => toast(err.message, 'error'));
  thesis();
  askNext();
}

init();
