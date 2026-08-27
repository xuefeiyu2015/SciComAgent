/* Overview page: render the agent's tools and roles from its own manifest.
   Nothing here is written twice — agent.yaml is the source, so the page cannot
   drift from the contract the platform reads. */
'use strict';

const escape_ = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const node_ = (tag, cls) => { const n = document.createElement(tag); if (cls) n.className = cls; return n; };

let manifest = null;

/* Inline code written in the manifest's own backticks. Escape, then lift. */
const code_ = (s) => escape_(s).replace(/`([^`]+)`/g, '<code>$1</code>');

function paramNote(param) {
  const bits = [];
  if (param.allowed && param.allowed.length) {
    bits.push(param.allowed.map((v) => `<b>${escape_(v)}</b>`).join(' · '));
  }
  if (param.default !== null && param.default !== undefined) {
    const shown = JSON.stringify(param.default).replace(/^"|"$/g, '');
    bits.push(`${escape_(t('front.tools.default'))} <b>${escape_(shown)}</b>`);
  }
  if (param.description) bits.push(code_(param.description));
  return bits.join(' — ');
}

function toolCard(tool) {
  const card = node_('article', 'tool');
  card.innerHTML = `<div class="tool-head"><span class="tool-name">${escape_(tool.name)}</span></div>`;

  const desc = node_('p', 'tool-desc');
  desc.innerHTML = code_(tool.description);
  card.append(desc);

  if (tool.parameters.length) {
    const list = node_('div', 'tool-params');
    tool.parameters.forEach((param) => {
      const row = node_('div', 'param');
      row.innerHTML = `<span class="param-name">${escape_(param.name)}`
        + `${param.required ? `<span class="req" title="${escape_(t('front.tools.required'))}">*</span>` : ''}</span>`
        + `<span class="param-note">${paramNote(param)}</span>`;
      list.append(row);
    });
    card.append(list);
  } else {
    const none = node_('div', 'tool-none');
    none.textContent = t('front.tools.noParams');
    card.append(none);
  }

  const returns = Object.keys(tool.output || {});
  if (returns.length) {
    const note = node_('p', 'tool-returns');
    note.innerHTML = `${escape_(t('front.tools.returns'))} · `
      + returns.map((key) => `<b>${escape_(key)}</b>`).join(' · ');
    card.append(note);
  }
  return card;
}

function renderManifest() {
  const host = document.querySelector('#tools-list');
  if (!manifest) { host.textContent = t('front.tools.loading'); return; }

  host.innerHTML = '';
  manifest.tools.forEach((tool) => host.append(toolCard(tool)));

  const roles = document.querySelector('#roles-list');
  roles.innerHTML = '';
  manifest.model_requirements.forEach((role) => {
    const card = node_('div', 'role-card');
    card.dataset.tier = role.tier;
    card.innerHTML = `<h3>${escape_(role.role)}</h3><span>${escape_(role.tier)}</span>`;
    roles.append(card);
  });
}

async function start() {
  await loadStrings();
  onLanguageChange(renderManifest);
  document.querySelectorAll('[data-lang-switch]').forEach((b) => { b.onclick = toggleLanguage; });

  renderManifest();
  try {
    const resp = await fetch('/api/agent');
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    manifest = await resp.json();
  } catch (err) {
    document.querySelector('#tools-list').textContent = `${t('front.tools.failed')} (${err.message})`;
    return;
  }
  renderManifest();
}

start();
