/* Interface language: one table, two languages, switched without a reload.
 *
 * Shared by both pages. The draft language is a SEPARATE thing — it is a real
 * AgentInput field — so this only pre-fills it; the dialog still confirms it.
 */
'use strict';

const I18N = {
  strings: null,
  lang: 'zh',
  listeners: [],
};

const LANG_KEY = 'scicom.lang';

/* Look up one string, filling {placeholders}. A missing key returns the key
   itself rather than empty text, so a gap is visible instead of invisible. */
function t(key, vars) {
  const table = (I18N.strings && I18N.strings[I18N.lang]) || {};
  let text = table[key];
  if (text === undefined) return key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.replaceAll(`{${name}}`, String(value));
    }
  }
  return text;
}

/* Backtick spans in the table become inline code, after escaping. */
function tCode(key, vars) {
  return t(key, vars)
    .replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function currentLang() { return I18N.lang; }

/* The interface language also decides which language a draft defaults to. */
function draftLanguage() { return I18N.lang === 'en' ? 'en' : 'zh'; }

function onLanguageChange(fn) { I18N.listeners.push(fn); }

async function loadStrings() {
  let stored = null;
  try { stored = localStorage.getItem(LANG_KEY); } catch { /* private window */ }
  if (stored === 'zh' || stored === 'en') {
    I18N.lang = stored;
  } else {
    I18N.lang = (navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en';
  }

  const resp = await fetch('/api/strings');
  I18N.strings = await resp.json();
  applyStrings();
}

function setLanguage(lang) {
  if (lang !== 'zh' && lang !== 'en') return;
  I18N.lang = lang;
  try { localStorage.setItem(LANG_KEY, lang); } catch { /* private window: this session only */ }
  applyStrings();
  I18N.listeners.forEach((fn) => fn(lang));
}

function toggleLanguage() { setLanguage(I18N.lang === 'zh' ? 'en' : 'zh'); }

/* Fill every element that names a key. Attribute variants cover the places
   text is not a child node: placeholders, titles, and the document title. */
function applyStrings() {
  document.documentElement.lang = I18N.lang === 'zh' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-html]').forEach((node) => {
    node.innerHTML = tCode(node.dataset.i18nHtml);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
  document.querySelectorAll('[data-i18n-aria]').forEach((node) => {
    node.setAttribute('aria-label', t(node.dataset.i18nAria));
  });
  const title = document.querySelector('[data-i18n-doctitle]');
  if (title) document.title = t(title.dataset.i18nDoctitle);
  document.querySelectorAll('[data-lang-switch]').forEach((node) => {
    node.textContent = t('lang.switch');
  });
}
