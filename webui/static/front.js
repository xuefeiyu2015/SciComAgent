/* Overview page: render the agent's tools and roles from its own manifest.
   Nothing here is written twice — agent.yaml is the source, so the page cannot
   drift from the contract the platform reads. */
'use strict';

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const el = (tag, cls) => { const n = document.createElement(tag); if (cls) n.className = cls; return n; };

/* The manifest writes inline code in backticks, the way its own comments do.
   Escape first, then lift the backticks — never the other way round. */
const code = (s) => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>');

/* What a parameter accepts, in the order a reader cares about. */
function paramNote(param) {
  const bits = [];
  if (param.allowed && param.allowed.length) {
    bits.push(param.allowed.map((v) => `<b>${esc(v)}</b>`).join(' · '));
  }
  if (param.default !== null && param.default !== undefined) {
    bits.push(`默认 default <b>${esc(JSON.stringify(param.default).replace(/^"|"$/g, ''))}</b>`);
  }
  if (param.description) bits.push(code(param.description));
  return bits.join(' — ');
}

function toolCard(tool) {
  const card = el('article', 'tool');
  const head = el('div', 'tool-head');
  head.innerHTML = `<span class="tool-name">${esc(tool.name)}</span>`;
  card.append(head);

  const desc = el('p', 'tool-desc');
  desc.innerHTML = code(tool.description);
  card.append(desc);

  if (tool.parameters.length) {
    const list = el('div', 'tool-params');
    tool.parameters.forEach((param) => {
      const row = el('div', 'param');
      row.innerHTML = `<span class="param-name">${esc(param.name)}`
        + `${param.required ? '<span class="req" title="必填 required">*</span>' : ''}</span>`
        + `<span class="param-note">${paramNote(param)}</span>`;
      list.append(row);
    });
    card.append(list);
  } else {
    const none = el('div', 'tool-none');
    none.textContent = '无参数 · no parameters';
    card.append(none);
  }

  const returns = Object.entries(tool.output || {});
  if (returns.length) {
    const note = el('p', 'tool-returns');
    note.innerHTML = '返回 returns · ' + returns
      .map(([key]) => `<b>${esc(key)}</b>`).join(' · ');
    card.append(note);
  }
  return card;
}

async function render() {
  const host = document.querySelector('#tools-list');
  let manifest;
  try {
    const resp = await fetch('/api/agent');
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    manifest = await resp.json();
  } catch (err) {
    host.textContent = `读不到 agent.yaml · could not read the manifest (${err.message})`;
    return;
  }

  host.innerHTML = '';
  manifest.tools.forEach((tool) => host.append(toolCard(tool)));

  const roles = document.querySelector('#roles-list');
  roles.innerHTML = '';
  manifest.model_requirements.forEach((role) => {
    const card = el('div', 'role-card');
    card.dataset.tier = role.tier;
    card.innerHTML = `<h3>${esc(role.role)}</h3><span>${esc(role.tier)}</span>`;
    roles.append(card);
  });

  if (manifest.version) {
    document.querySelector('#version').textContent =
      `科普写作智能体 · v${manifest.version}`;
  }
}

render();
