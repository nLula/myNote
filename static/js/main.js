// ── marked.js config ───────────────────────────────────────────────────────

marked.use({ breaks: true, gfm: true });

const _MEDIA_VIDEO_EXTS = new Set(['mp4','m4v','3gp','wmv','flv','mov','webm','avi','mkv','mpg','mpeg']);
const _MEDIA_AUDIO_EXTS = new Set(['mp3','m4a','ogg','oga','wav','flac','aac','opus','weba','wma']);

/**
 * Pre-process raw markdown BEFORE marked.parse().
 *
 * Markdown link syntax [text](url) does NOT allow unescaped spaces in the URL.
 * Filenames with spaces, Cyrillic, parentheses etc. cause marked.js to skip the
 * link entirely and emit raw markdown text.  We fix this by URL-encoding the
 * filename segment of every /attachments/… link before handing the text to marked.
 *
 * Uses a balanced-parentheses pattern so filenames like "(SamBelony) (720p).mp4"
 * don't confuse the extractor.
 */
function encodeAttachmentUrls(md) {
  // Each attachment URL has the shape: /attachments/<subfolder>/<filename>
  // The filename may contain spaces, Cyrillic, balanced parens, &, etc.
  // Pattern breakdown:
  //   \]\(                              – closing ] then opening ( of a markdown link
  //   (\/attachments\/[^/\n]+\/)        – capture: /attachments/<subfolder>/
  //   ((?:[^()\n]+|\([^()\n]*\))*)      – capture: filename  (non-paren chars OR balanced (...))
  //   (?=[)\n]|$)                        – lookahead: must be followed by ) or end-of-line
  return md.replace(
    /\]\((\/attachments\/[^/\n]+\/)((?:[^()\n]+|\([^()\n]*\))*)\)(?=[)\n]|$)/g,
    (match, prefix, rawFilename, ...rest) => {
      // Normalise: decode any existing percent-encoding then re-encode cleanly
      let fname = rawFilename;
      try { fname = decodeURIComponent(rawFilename); } catch (_) {}
      return `](${prefix}${encodeURIComponent(fname)})`;
    }
  );
}

/**
 * After marked.parse() produces HTML, replace <a href="/attachments/…video"> and
 * <a href="/attachments/…audio"> links with inline <video>/<audio> players.
 */
function injectMediaPlayers(html) {
  return html.replace(
    /<a\s[^>]*?\bhref="(\/attachments\/[^"]+)"[^>]*>[\s\S]*?<\/a>/gi,
    (match, href) => {
      let decoded = href;
      try { decoded = decodeURIComponent(href); } catch (_) {}
      const ext   = decoded.split('.').pop().toLowerCase();
      const fname = decoded.split('/').pop();

      if (_MEDIA_VIDEO_EXTS.has(ext)) {
        return `<div class="note-media-wrap">` +
                 `<video src="${href}" controls preload="metadata" class="note-media-video"></video>` +
                 `<div class="note-media-label">${fname}</div>` +
               `</div>`;
      }
      if (_MEDIA_AUDIO_EXTS.has(ext)) {
        return `<div class="note-media-wrap note-media-wrap--audio">` +
                 `<div class="note-media-label">${fname}</div>` +
                 `<audio src="${href}" controls preload="metadata" class="note-media-audio"></audio>` +
               `</div>`;
      }
      return match;
    }
  );
}

// ── Theme ──────────────────────────────────────────────────────────────────

const root = document.documentElement;

function applyTheme(t) {
  root.setAttribute('data-theme', t);
  localStorage.setItem('mn-theme', t);
}

applyTheme(localStorage.getItem('mn-theme') || 'dark');

document.getElementById('theme-toggle').addEventListener('click', () =>
  applyTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark')
);

// ── Column resize ──────────────────────────────────────────────────────────

function saveWidth(id, px) {
  const w = JSON.parse(localStorage.getItem('mn-col-widths') || '{}');
  w[id] = px;
  localStorage.setItem('mn-col-widths', JSON.stringify(w));
}

const savedWidths = JSON.parse(localStorage.getItem('mn-col-widths') || '{}');
['col-tags', 'col-notes'].forEach(id => {
  if (savedWidths[id]) document.getElementById(id).style.flexBasis = savedWidths[id] + 'px';
});

let drag = null;

function attachHandle(handleId, colId) {
  document.getElementById(handleId).addEventListener('mousedown', e => {
    e.preventDefault();
    const col = document.getElementById(colId);
    drag = { col, startX: e.clientX, startBasis: col.offsetWidth };
    document.body.classList.add('dragging');
  });
}

attachHandle('handle-1', 'col-tags');
attachHandle('handle-2', 'col-notes');

document.addEventListener('mousemove', e => {
  if (!drag) return;
  const minW = parseInt(getComputedStyle(drag.col).minWidth) || 80;
  drag.col.style.flexBasis = Math.max(minW, drag.startBasis + (e.clientX - drag.startX)) + 'px';
});

document.addEventListener('mouseup', () => {
  if (!drag) return;
  saveWidth(drag.col.id, drag.col.offsetWidth);
  drag = null;
  document.body.classList.remove('dragging');
});

// ── API ────────────────────────────────────────────────────────────────────

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

// ── State ──────────────────────────────────────────────────────────────────

const TRASH_TAG      = 'trash';
// Tags the user cannot create from scratch (only assign if they already exist)
const RESERVED_TAGS  = ['Telegram'];

let activeTag        = '';
let activeNote       = '';
let activeButton     = null;
let searchQuery      = '';
let searchTimer      = null;
let selectedNotes        = new Set();   // Ctrl-multi-selected note names
let multiSelectAnchor    = '';          // last plain/ctrl-click — Shift range starts here
let noteFrontmatter  = '';
let noteBodyContent  = '';       // always plaintext when unlocked; ciphertext when locked
let noteRawCiphertext = '';      // last known ciphertext for the current encrypted note
let noteCtime        = null;
let noteMtime        = null;
let saveTimer        = null;
let editingTags      = [];
let allTagsCache     = [];
let noteEncrypted    = false;
const notePasswords  = new Map(); // noteName → session decryption password
let autoLockTimer    = null;
let toastFilePath    = null;

// ── Toolbar meta display ───────────────────────────────────────────────────

const metaPrimary   = document.getElementById('note-meta-primary');
const metaSecondary = document.getElementById('note-meta-secondary');

function formatDate(ts) {
  const d  = new Date(ts * 1000);
  const mn = ['January','February','March','April','May','June',
               'July','August','September','October','November','December'];
  const p  = n => String(n).padStart(2, '0');
  return `${d.getFullYear()} ${mn[d.getMonth()]} ${d.getDate()} at ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function setMetaDates(ctime, mtime) {
  metaPrimary.textContent   = ctime ? 'Created: '     + formatDate(ctime) : 'Select a note';
  metaSecondary.textContent = mtime ? 'Last change: ' + formatDate(mtime) : '';
}

// ── Button state ───────────────────────────────────────────────────────────

function setActiveButton(id) {
  document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('btn-active'));
  activeButton = id;
  if (id) document.getElementById(`btn-${id}`).classList.add('btn-active');
}

// ── Content pane helpers ───────────────────────────────────────────────────

const colContent = document.getElementById('col-content');
const noteView   = document.getElementById('note-view');
const noteEdit   = document.getElementById('note-edit');

// Adds data-ext attribute to remaining (non-media) attachment links for CSS emoji icons.
function decorateAttachmentLinks(container) {
  container.querySelectorAll('a[href^="/attachments/"]').forEach(a => {
    const ext = a.getAttribute('href').split('.').pop().toLowerCase();
    a.dataset.ext = ext;
  });
}

function enterViewMode() {
  if (noteEncrypted && !notePasswords.has(activeNote)) {
    noteView.innerHTML = `
      <div class="encrypted-placeholder">
        <img class="encrypted-lock-icon" src="/static/images/lock.png" alt="" />
        <p>This note is encrypted.</p>
        <p>Click the lock button to unlock.</p>
      </div>`;
  } else {
    // 1. URL-encode attachment filenames so marked.js can parse links with spaces/parens
    // 2. Parse markdown → HTML string
    // 3. Replace media attachment links with inline <video>/<audio> players
    // 4. Write to DOM, then decorate remaining doc/archive links with emoji icons
    noteView.innerHTML = injectMediaPlayers(marked.parse(encodeAttachmentUrls(noteBodyContent)));
    decorateAttachmentLinks(noteView);
  }
  colContent.classList.remove('editing');
  document.body.classList.remove('editing-active');
  _flushPendingUpdate();
}

function enterEditMode() {
  if (noteEncrypted && !notePasswords.has(activeNote)) return;
  noteEdit.value = noteBodyContent;
  colContent.classList.add('editing');
  document.body.classList.add('editing-active');
  noteEdit.focus();
}

function clearContentPane() {
  cancelAutoLockTimer();
  noteBodyContent   = '';
  noteRawCiphertext = '';
  noteFrontmatter   = '';
  noteCtime       = null;
  noteMtime       = null;
  noteEncrypted   = false;
  noteView.innerHTML = '';
  noteEdit.value  = '';
  colContent.classList.remove('editing');
  document.body.classList.remove('editing-active');
  setMetaDates(null, null);
  document.getElementById('btn-lock').disabled  = true;
  document.getElementById('btn-trash').disabled = true;
  updateExportButton();
  updateShareButton();
}

function startAutoLockTimer() {
  cancelAutoLockTimer();
  autoLockTimer = setTimeout(() => {
    if (noteEncrypted && activeNote) {
      console.debug('[auto-lock] restoring ciphertext before clearing session password');
      noteBodyContent = noteRawCiphertext;
    }
    notePasswords.clear();
    if (noteEncrypted && activeNote) {
      enterViewMode();
      updateExportButton();
    }
  }, 60000);
}

function cancelAutoLockTimer() {
  if (autoLockTimer) { clearTimeout(autoLockTimer); autoLockTimer = null; }
}

function updateExportButton() {
  document.getElementById('btn-export').disabled = !activeNote || noteEncrypted;
}

function updateShareButton() {
  // Share is available for any open non-encrypted note
  const btn = document.getElementById('btn-share');
  btn.disabled = !activeNote || noteEncrypted;
}

// ── Multi-select ────────────────────────────────────────────────────────────

function enterMultiSelectMode() {
  // Blank the content pane without touching activeNote state
  noteBodyContent   = '';
  noteRawCiphertext = '';
  noteFrontmatter   = '';
  noteEncrypted     = false;
  noteView.innerHTML = '';
  noteEdit.value  = '';
  colContent.classList.remove('editing');
  document.body.classList.remove('editing-active');
  cancelAutoLockTimer();

  // Update title strip
  const n = selectedNotes.size;
  const verb = activeTag === TRASH_TAG ? 'Restore' : 'Trash';
  metaPrimary.textContent   = `${n} notes selected — click ${verb} to confirm`;
  metaSecondary.textContent = '';

  // Disable everything except trash
  ['btn-new', 'btn-edit', 'btn-tags', 'btn-lock', 'btn-export', 'btn-share'].forEach(id =>
    document.getElementById(id).disabled = true);
  document.getElementById('btn-trash').disabled = false;

  // Highlight selected items
  document.querySelectorAll('.note-item').forEach(el => {
    el.classList.toggle('multi-selected', selectedNotes.has(el.dataset.name));
    el.classList.remove('active');
  });
}

function clearMultiSelect() {
  if (!selectedNotes.size) return;
  selectedNotes.clear();
  document.querySelectorAll('.note-item.multi-selected').forEach(el =>
    el.classList.remove('multi-selected'));
  // Re-enable buttons to their normal state
  document.getElementById('btn-new').disabled    = false;
  document.getElementById('btn-edit').disabled   = false;
  document.getElementById('btn-tags').disabled   = false;
  document.getElementById('btn-trash').disabled  = !activeNote;
  document.getElementById('btn-lock').disabled   = !activeNote;
  updateExportButton();
  updateShareButton();
}

function extractTitle(body) {
  const m = body.match(/^#\s+(.+)/m);
  return m ? m[1].trim() : (activeNote || '');
}

function sanitizeFilename(name) {
  return name.replace(/[/\\<>:"|?*]/g, '').trim().slice(0, 100);
}

// ── Crypto helpers (AES-GCM + PBKDF2) ─────────────────────────────────────

async function deriveKey(password, salt) {
  const material = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']
  );
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' },
    material, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']
  );
}

async function encryptText(text, password) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv   = crypto.getRandomValues(new Uint8Array(12));
  const key  = await deriveKey(password, salt);
  const ct   = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv }, key, new TextEncoder().encode(text)
  );
  const out = new Uint8Array(28 + ct.byteLength);
  out.set(salt, 0); out.set(iv, 16); out.set(new Uint8Array(ct), 28);
  return btoa(String.fromCharCode(...out));
}

async function decryptText(b64, password) {
  const buf  = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const key  = await deriveKey(password, buf.slice(0, 16));
  const pt   = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: buf.slice(16, 28) }, key, buf.slice(28)
  );
  return new TextDecoder().decode(pt);
}

// ── Frontmatter helpers ────────────────────────────────────────────────────

function setFrontmatterField(fm, key, value) {
  const cleaned = fm.replace(new RegExp(`^${key}:[ \\t]*.*\\n?`, 'm'), '');
  return cleaned.replace('---\n', `---\n${key}: ${value}\n`);
}

function removeFrontmatterField(fm, key) {
  return fm.replace(new RegExp(`^${key}:[ \\t]*.*\\n?`, 'm'), '');
}

function ensureFrontmatter(fm) {
  return fm.trim() ? fm : '---\n---\n';
}

function stripEmptyFrontmatter(fm) {
  // Remove frontmatter block if nothing remains inside it
  return /^---\s*\n\s*---\s*\n?$/.test(fm.trim()) ? '' : fm;
}

// ── Save + rename ──────────────────────────────────────────────────────────

async function saveCurrentNote() {
  if (!activeNote) return;
  let body = noteBodyContent;
  if (noteEncrypted && notePasswords.has(activeNote)) {
    body = await encryptText(noteBodyContent, notePasswords.get(activeNote));
    noteRawCiphertext = body;  // keep in sync so session-lock can restore it
  }
  await api(`/api/note/${encodeURIComponent(activeNote)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: noteFrontmatter + body }),
  });
  noteMtime = Date.now() / 1000;
  metaSecondary.textContent = 'Last change: ' + formatDate(noteMtime);
}

async function saveAndRename(title) {
  if (!activeNote) return;
  await saveCurrentNote();

  const newName = sanitizeFilename(title);
  if (!newName || newName === activeNote) return;

  try {
    const result = await api(`/api/note/${encodeURIComponent(activeNote)}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    });
    const oldName = activeNote;
    if (notePasswords.has(oldName)) {
      notePasswords.set(result.name, notePasswords.get(oldName));
      notePasswords.delete(oldName);
    }
    activeNote = result.name;
    const item = document.querySelector(`.note-item[data-name="${CSS.escape(oldName)}"]`);
    if (item) item.dataset.name = result.name;
  } catch (_) {}
}

// ── Live editor ────────────────────────────────────────────────────────────

noteEdit.addEventListener('input', () => {
  noteBodyContent = noteEdit.value;
  const activeItem = document.querySelector(`.note-item[data-name="${CSS.escape(activeNote)}"]`);
  if (activeItem) {
    const title   = extractTitle(noteBodyContent);
    const textEl  = activeItem.querySelector('.note-item-text');
    if (textEl) textEl.textContent = title || activeNote;
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveAndRename(extractTitle(noteBodyContent)), 300);
});

// ── Attachments ────────────────────────────────────────────────────────────

function clipboardImageFilename(mimeType) {
  const ext = mimeType === 'image/jpeg' ? '.jpg' : '.png';
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return `screenshot-${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}${ext}`;
}

// ── Table paste helpers (Excel / HTML tables / TSV) ───────────────────────

function _tableHtmlToMd(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const tbl = doc.querySelector('table');
  if (!tbl) return null;
  const rows = Array.from(tbl.querySelectorAll('tr'));
  if (!rows.length) return null;

  const escape = s => (s.innerText ?? '').replace(/\|/g, '\\|').replace(/[\r\n]+/g, ' ').trim();
  const grid   = rows.map(r => Array.from(r.querySelectorAll('td,th')).map(escape));
  if (!grid.length || !grid[0].length) return null;

  const cols = Math.max(...grid.map(r => r.length));
  const pad  = r => { const c = [...r]; while (c.length < cols) c.push(''); return c; };
  const line = r => '| ' + pad(r).join(' | ') + ' |';
  const sep  = '| ' + Array(cols).fill('---').join(' | ') + ' |';
  const out  = grid.map(line);
  out.splice(1, 0, sep);
  return out.join('\n');
}

function _tsvToMd(text) {
  const lines = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    .split('\n').filter(l => l.length);
  if (lines.length < 2 || !lines.every(l => l.includes('\t'))) return null;

  const escape = s => s.replace(/\|/g, '\\|').trim();
  const grid   = lines.map(l => l.split('\t').map(escape));
  const cols   = Math.max(...grid.map(r => r.length));
  const pad    = r => { const c = [...r]; while (c.length < cols) c.push(''); return c; };
  const line   = r => '| ' + pad(r).join(' | ') + ' |';
  const sep    = '| ' + Array(cols).fill('---').join(' | ') + ' |';
  const out    = grid.map(line);
  out.splice(1, 0, sep);
  return out.join('\n');
}

function insertAtCursor(text) {
  const s = noteEdit.selectionStart, e = noteEdit.selectionEnd;
  const before = noteEdit.value.substring(0, s);
  const after  = noteEdit.value.substring(e);
  const prefix = (before.length && !before.endsWith('\n')) ? '\n' : '';
  const suffix = (after.length  && !after.startsWith('\n')) ? '\n' : '';
  noteEdit.value = before + prefix + text + suffix + after;
  noteEdit.selectionStart = noteEdit.selectionEnd = s + prefix.length + text.length + suffix.length;
}

async function handleAttachment(file, name) {
  if (!activeNote || activeTag === TRASH_TAG) return;
  const formData = new FormData();
  formData.append('file', file, name);
  let data;
  try {
    const r = await fetch('/api/attachment/upload', { method: 'POST', body: formData });
    if (!r.ok) return;
    data = await r.json();
  } catch (_) { return; }
  // URL-encode the filename segment so markdown links survive spaces / Cyrillic / parens
  const urlParts  = data.url.split('/');
  const safeUrl   = urlParts.slice(0, -1).join('/') + '/' +
                    encodeURIComponent(decodeURIComponent(urlParts[urlParts.length - 1]));
  const dispName  = data.filename.split('/').pop();   // "videos/file.mp4" → "file.mp4"
  const md = file.type.startsWith('image/')
    ? `![${dispName}](${safeUrl})`
    : `[${dispName}](${safeUrl})`;
  insertAtCursor(md);
  noteBodyContent = noteEdit.value;
  clearTimeout(saveTimer);
  await saveAndRename(extractTitle(noteBodyContent));
}

noteEdit.addEventListener('paste', async e => {
  if (activeTag === TRASH_TAG || !activeNote) return;

  // ── Table paste: HTML table (Excel, Google Sheets, browser) or TSV ─────
  const htmlData = e.clipboardData?.getData('text/html') ?? '';
  const txtData  = e.clipboardData?.getData('text/plain') ?? '';
  const mdTable  = (htmlData && _tableHtmlToMd(htmlData)) || _tsvToMd(txtData);
  if (mdTable) {
    e.preventDefault();
    insertAtCursor(mdTable);
    noteBodyContent = noteEdit.value;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveAndRename(extractTitle(noteBodyContent)), 300);
    return;
  }

  const items   = Array.from(e.clipboardData?.items || []);
  const files   = Array.from(e.clipboardData?.files  || []);
  const imgItem = items.find(i => i.kind === 'file' && i.type.startsWith('image/'));
  if (imgItem) {
    e.preventDefault();
    await handleAttachment(imgItem.getAsFile(),
      files.find(f => f.type.startsWith('image/'))?.name || clipboardImageFilename(imgItem.type));
    return;
  }
  if (files.length) { e.preventDefault(); for (const f of files) await handleAttachment(f, f.name); }
});

noteEdit.addEventListener('dragenter', e => {
  if (activeTag === TRASH_TAG || !activeNote) return;
  if (e.dataTransfer?.types.includes('Files')) { e.preventDefault(); noteEdit.classList.add('drag-over'); }
});
noteEdit.addEventListener('dragleave', () => noteEdit.classList.remove('drag-over'));
noteEdit.addEventListener('dragover', e => {
  if (activeTag === TRASH_TAG || !activeNote) return;
  if (e.dataTransfer?.types.includes('Files')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
});
noteEdit.addEventListener('drop', async e => {
  noteEdit.classList.remove('drag-over');
  if (activeTag === TRASH_TAG || !activeNote || !e.dataTransfer?.files.length) return;
  e.preventDefault();
  for (const f of e.dataTransfer.files) await handleAttachment(f, f.name);
});

noteView.addEventListener('click', async e => {
  const a = e.target.closest('a');
  if (!a) return;
  const href = a.getAttribute('href') || '';
  if (!href.startsWith('/attachments/')) return;
  e.preventDefault();
  try {
    await fetch('/api/attachment/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: href.slice('/attachments/'.length) }),
    });
  } catch (_) {}
});

// ── Search ─────────────────────────────────────────────────────────────────

document.getElementById('search-input').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchQuery = e.target.value.trim();
  searchTimer = setTimeout(renderNotes, 250);
});
document.getElementById('search-input').addEventListener('search', e => {
  if (!e.target.value) { searchQuery = ''; renderNotes(); }
});

// ── Tags (col 1) ───────────────────────────────────────────────────────────

async function renderTags() {
  const tags = await api('/api/tags');
  allTagsCache = tags;
  const list = document.getElementById('tag-list');
  list.innerHTML = '';
  [{ label: 'All', value: '', system: true },
   ...tags.map(t => ({ label: t, value: t, system: false })),
   { label: 'Trash', value: TRASH_TAG, system: true }
  ].forEach(({ label, value, system }) => {
    const li = document.createElement('li');
    li.className = 'tag-item'
      + (system        ? ' tag-system' : '')
      + (activeTag === value ? ' active'     : '');
    li.textContent = label;
    li.dataset.tag = value;
    li.addEventListener('click', () => selectTag(value));
    list.appendChild(li);
  });
}

/** Returns true if tagName is reserved and does NOT yet exist in the tag list. */
function _isNewReservedTag(tagName) {
  const lower = tagName.toLowerCase();
  return RESERVED_TAGS.some(r => r.toLowerCase() === lower)
    && !allTagsCache.some(t => t.toLowerCase() === lower);
}

async function selectTag(tag) {
  clearTimeout(saveTimer);
  if (activeNote && noteEncrypted && notePasswords.has(activeNote)) {
    notePasswords.delete(activeNote);
    cancelAutoLockTimer();
  }
  if (activeNote && colContent.classList.contains('editing')) await saveCurrentNote();
  setActiveButton(null);
  activeTag = tag; activeNote = ''; searchQuery = '';
  document.getElementById('search-input').value = '';
  clearContentPane();
  document.querySelectorAll('.tag-item').forEach(el =>
    el.classList.toggle('active', el.dataset.tag === tag));
  updateToolbarState();
  await renderNotes();
}

// ── Notes (col 2) ──────────────────────────────────────────────────────────

async function renderNotes() {
  let url = searchQuery
    ? `/api/search?q=${encodeURIComponent(searchQuery)}` + (activeTag ? `&tag=${encodeURIComponent(activeTag)}` : '')
    : (activeTag ? `/api/notes?tag=${encodeURIComponent(activeTag)}` : '/api/notes');

  const items = await api(url);
  const list  = document.getElementById('note-list');
  list.innerHTML = '';
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'note-item empty';
    li.textContent = searchQuery ? 'No matches' : 'No notes found';
    list.appendChild(li);
    return;
  }
  items.forEach(({ name, title, encrypted }) => {
    const li = document.createElement('li');
    li.className = 'note-item' + (activeNote === name ? ' active' : '');
    li.dataset.name = name;
    const lockImg = document.createElement('img');
    lockImg.className = 'note-lock-icon' + (encrypted ? '' : ' hidden');
    lockImg.src = '/static/images/lock.png';
    lockImg.alt = '';
    const textSpan = document.createElement('span');
    textSpan.className = 'note-item-text';
    textSpan.textContent = title;
    li.appendChild(lockImg);
    li.appendChild(textSpan);
    li.addEventListener('click', e => {
      if (e.shiftKey && multiSelectAnchor) {
        // ── Shift-click: select every note between anchor and here ──
        e.preventDefault();   // don't let the browser text-select the list
        const order = Array.from(
          document.querySelectorAll('#note-list .note-item:not(.empty)')
        ).map(el => el.dataset.name).filter(Boolean);

        const ai = order.indexOf(multiSelectAnchor);
        const bi = order.indexOf(name);
        if (ai !== -1 && bi !== -1) {
          // Seed with the currently open note if nothing is selected yet
          if (selectedNotes.size === 0 && activeNote) selectedNotes.add(activeNote);
          const [from, to] = ai <= bi ? [ai, bi] : [bi, ai];
          for (let i = from; i <= to; i++) selectedNotes.add(order[i]);
        }

        if (selectedNotes.size >= 2) {
          activeNote = '';
          enterMultiSelectMode();
        } else if (selectedNotes.size === 1) {
          clearMultiSelect();
          setActiveButton(null);
          openNote([...selectedNotes][0]);
        }

      } else if (e.ctrlKey) {
        // ── Ctrl-click: toggle this note in the multi-selection ──
        // Seed the set with the currently active note on first ctrl-click
        if (selectedNotes.size === 0 && activeNote) selectedNotes.add(activeNote);
        if (selectedNotes.has(name)) selectedNotes.delete(name);
        else                         selectedNotes.add(name);

        multiSelectAnchor = name;   // update anchor for future Shift-ranges

        if (selectedNotes.size >= 2) {
          activeNote = '';
          enterMultiSelectMode();
        } else if (selectedNotes.size === 1) {
          // Dropped back to 1 — just open that note normally
          const only = [...selectedNotes][0];
          clearMultiSelect();
          setActiveButton(null);
          openNote(only);
        } else {
          // Empty selection — just clear
          clearMultiSelect();
        }

      } else {
        // ── Normal click: clear any multi-select then open ──
        multiSelectAnchor = name;   // reset anchor
        clearMultiSelect();
        setActiveButton(null);
        openNote(name);
      }
    });
    list.appendChild(li);
  });
}

// ── Open note (col 3) ──────────────────────────────────────────────────────

async function openNote(name, startEditing = false) {
  clearMultiSelect();
  clearTimeout(saveTimer);
  // Lock the note we're leaving if it was session-unlocked
  if (activeNote && noteEncrypted && notePasswords.has(activeNote)) {
    notePasswords.delete(activeNote);
    cancelAutoLockTimer();
  }
  if (activeNote && colContent.classList.contains('editing')) await saveCurrentNote();

  activeNote = name;
  document.querySelectorAll('.note-item').forEach(el =>
    el.classList.toggle('active', el.dataset.name === name));

  const data      = await api(`/api/note/${encodeURIComponent(name)}`);
  const fmMatch   = data.content.match(/^(---\s*\n[\s\S]*?\n---\s*\n?)/);
  noteFrontmatter = fmMatch ? fmMatch[1] : '';
  const rawBody   = fmMatch ? data.content.slice(fmMatch[1].length).trimStart() : data.content;

  noteCtime     = data.ctime ?? null;
  noteMtime     = data.mtime ?? null;
  noteEncrypted = /^encrypted:\s*true/m.test(noteFrontmatter);

  // Always save ciphertext so we can restore it after a session-lock
  if (noteEncrypted) noteRawCiphertext = rawBody;
  else noteRawCiphertext = '';

  document.getElementById('btn-lock').disabled  = false;
  document.getElementById('btn-trash').disabled = false;
  updateShareButton();
  setMetaDates(noteCtime, noteMtime);

  if (noteEncrypted && notePasswords.has(name)) {
    try {
      noteBodyContent = await decryptText(rawBody, notePasswords.get(name));
    } catch (_) {
      notePasswords.delete(name);
      noteBodyContent = rawBody;
    }
  } else {
    noteBodyContent = rawBody;
  }

  if (startEditing && activeTag !== TRASH_TAG && !(noteEncrypted && !notePasswords.has(name))) {
    enterEditMode();
  } else {
    enterViewMode();
  }
  if (noteEncrypted && notePasswords.has(name)) startAutoLockTimer();
  updateExportButton();
}

// ── Toolbar state ───────────────────────────────────────────────────────────

function updateToolbarState() {
  const inTrash = activeTag === TRASH_TAG;
  document.getElementById('btn-trash').title =
    inTrash ? 'Restore from Trash' : 'Move to Trash';
  document.querySelector('.content-toolbar').classList.toggle('trash-mode', inTrash);
}

// ── Lock / encrypt dialog ──────────────────────────────────────────────────

const encryptDialog      = document.getElementById('encrypt-dialog');
const encryptDialogTitle = document.getElementById('encrypt-dialog-title');
const encryptPass1       = document.getElementById('encrypt-pass1');
const encryptPass2       = document.getElementById('encrypt-pass2');
const encryptError       = document.getElementById('encrypt-error');

let encryptDialogMode        = 'lock';  // 'lock' | 'unlock' | 'settings-unlock'
let settingsUnlockNoteName   = null;

function openEncryptDialog(mode) {
  encryptDialogMode = mode;
  encryptDialogTitle.textContent =
    mode === 'lock'            ? 'Lock Note'            :
    mode === 'settings-unlock' ? 'Decrypt Note'         :
    mode === 'export'          ? 'Password to Export'   : 'Unlock Note';
  encryptPass2.classList.toggle('hidden', mode !== 'lock');
  encryptPass1.value = '';
  encryptPass2.value = '';
  encryptError.textContent = '';
  encryptDialog.classList.remove('hidden');
  encryptPass1.focus();
}

function closeEncryptDialog() {
  encryptDialog.classList.add('hidden');
  encryptPass1.value = '';
  encryptPass2.value = '';
  encryptError.textContent = '';
}

document.getElementById('encrypt-cancel').addEventListener('click', closeEncryptDialog);

encryptDialog.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); document.getElementById('encrypt-ok').click(); }
});

document.getElementById('encrypt-ok').addEventListener('click', async () => {
  const p1 = encryptPass1.value;
  const p2 = encryptPass2.value;
  encryptError.textContent = '';

  // ── Lock: encrypt and blur immediately ────────────────────────────────────
  if (encryptDialogMode === 'lock') {
    if (!p1) { encryptError.textContent = 'Password cannot be empty.'; return; }
    if (p1 !== p2) { encryptError.textContent = 'Passwords do not match.'; return; }

    const ciphertext = await encryptText(noteBodyContent, p1);
    noteFrontmatter  = setFrontmatterField(ensureFrontmatter(noteFrontmatter), 'encrypted', 'true');
    noteEncrypted    = true;
    noteBodyContent  = ciphertext;   // state now holds ciphertext
    notePasswords.delete(activeNote); // no session password → blurred immediately
    cancelAutoLockTimer();

    await api(`/api/note/${encodeURIComponent(activeNote)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: noteFrontmatter + ciphertext }),
    });
    noteMtime = Date.now() / 1000;
    metaSecondary.textContent = 'Last change: ' + formatDate(noteMtime);

    // Show lock icon in note list
    const item = document.querySelector(`.note-item[data-name="${CSS.escape(activeNote)}"]`);
    item?.querySelector('.note-lock-icon')?.classList.remove('hidden');

    closeEncryptDialog();
    enterViewMode();  // shows blurred placeholder since no session password
    updateExportButton();

  // ── Unlock for session ────────────────────────────────────────────────────
  } else if (encryptDialogMode === 'unlock') {
    if (!p1) { encryptError.textContent = 'Enter the password.'; return; }
    console.debug('[unlock] activeNote:', activeNote);
    console.debug('[unlock] noteBodyContent length:', noteBodyContent.length,
      '| first 40 chars:', JSON.stringify(noteBodyContent.slice(0, 40)));
    console.debug('[unlock] noteRawCiphertext length:', noteRawCiphertext.length,
      '| noteBodyContent === noteRawCiphertext:', noteBodyContent === noteRawCiphertext);
    try {
      const plain = await decryptText(noteBodyContent, p1);
      noteBodyContent = plain;
      notePasswords.set(activeNote, p1);
      closeEncryptDialog();
      enterViewMode();
      startAutoLockTimer();
      updateExportButton();
      console.debug('[unlock] success, plaintext length:', plain.length);
    } catch (err) {
      console.error('[unlock] decryptText threw:', err.name, err.message);
      console.error('[unlock] noteBodyContent was valid base64?',
        /^[A-Za-z0-9+/]+=*$/.test(noteBodyContent.trim()));
      encryptError.textContent = 'Incorrect password.';
    }

  // ── Permanently decrypt (from settings) ───────────────────────────────────
  } else if (encryptDialogMode === 'settings-unlock') {
    if (!p1) { encryptError.textContent = 'Enter the password.'; return; }
    try {
      const data    = await api(`/api/note/${encodeURIComponent(settingsUnlockNoteName)}`);
      const fmMatch = data.content.match(/^(---\s*\n[\s\S]*?\n---\s*\n?)/);
      const fm      = fmMatch ? fmMatch[1] : '';
      const body    = fmMatch ? data.content.slice(fmMatch[1].length).trimStart() : data.content;
      const plain   = await decryptText(body, p1);

      // Strip encrypted: true and drop the frontmatter block if it becomes empty
      const newFm = stripEmptyFrontmatter(removeFrontmatterField(fm, 'encrypted'));

      await api(`/api/note/${encodeURIComponent(settingsUnlockNoteName)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: newFm + plain }),
      });

      notePasswords.delete(settingsUnlockNoteName);

      // If the note is currently open, update live state
      if (activeNote === settingsUnlockNoteName) {
        noteEncrypted   = false;
        noteBodyContent = plain;
        noteFrontmatter = newFm;
        noteMtime       = Date.now() / 1000;
        metaSecondary.textContent = 'Last change: ' + formatDate(noteMtime);
        document.getElementById('btn-lock').disabled = false;
        updateShareButton();
        enterViewMode();
        // Remove lock icon from list item
        const item = document.querySelector(`.note-item[data-name="${CSS.escape(activeNote)}"]`);
        item?.querySelector('.note-lock-icon')?.classList.add('hidden');
      }

      closeEncryptDialog();
      await renderNotes();
      loadLockedNotesList();
    } catch (_) {
      encryptError.textContent = 'Incorrect password.';
    }

  // ── Export encrypted note (re-verify password each time) ─────────────────
  } else if (encryptDialogMode === 'export') {
    if (!p1) { encryptError.textContent = 'Enter the password.'; return; }
    try {
      // Always decrypt fresh from disk so export works even if note is locked
      const data    = await api(`/api/note/${encodeURIComponent(activeNote)}`);
      const fmMatch = data.content.match(/^(---\s*\n[\s\S]*?\n---\s*\n?)/);
      const body    = fmMatch ? data.content.slice(fmMatch[1].length).trimStart() : data.content;
      const plain   = await decryptText(body, p1);
      closeEncryptDialog();
      await doExport(plain);
    } catch (_) {
      encryptError.textContent = 'Incorrect password.';
    }
  }
});

document.getElementById('btn-lock').addEventListener('click', () => {
  if (!activeNote) return;
  if (noteEncrypted) {
    // Locked and no session password → show unlock dialog
    if (!notePasswords.has(activeNote)) {
      openEncryptDialog('unlock');
    } else {
      // Currently unlocked in session → restore ciphertext then lock
      console.debug('[lock] re-locking. noteBodyContent length:', noteBodyContent.length,
        '| noteRawCiphertext length:', noteRawCiphertext.length);
      noteBodyContent = noteRawCiphertext;
      notePasswords.delete(activeNote);
      cancelAutoLockTimer();
      enterViewMode();
      updateExportButton();
    }
  } else {
    openEncryptDialog('lock');
  }
});

// ── Settings ───────────────────────────────────────────────────────────────

const settingsModal = document.getElementById('settings-modal');

async function loadLockedNotesList() {
  const list = document.getElementById('locked-notes-list');
  if (!list) return;
  list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0">Loading…</div>';
  try {
    const notes = await api('/api/locked-notes');
    list.innerHTML = '';
    if (!notes.length) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0">No locked notes.</div>';
      return;
    }
    notes.forEach(({ name, title }) => {
      const item   = document.createElement('div');
      item.className = 'locked-note-item';
      const nameEl = document.createElement('span');
      nameEl.className = 'locked-note-name';
      const lockImg2 = document.createElement('img');
      lockImg2.className = 'note-lock-icon';
      lockImg2.src = '/static/images/lock.png';
      lockImg2.alt = '';
      nameEl.appendChild(lockImg2);
      nameEl.appendChild(document.createTextNode(' ' + title));
      const btn = document.createElement('button');
      btn.className = 'locked-note-remove-btn';
      btn.title = 'Decrypt permanently';
      btn.textContent = '×';
      btn.addEventListener('click', () => {
        settingsUnlockNoteName = name;
        openEncryptDialog('settings-unlock');
      });
      item.appendChild(nameEl);
      item.appendChild(btn);
      list.appendChild(item);
    });
  } catch (_) {
    list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0">Error loading.</div>';
  }
}

// ── Work-folder setup state ────────────────────────────────────────────────
let workfolderConfigured = false;

/** Switch the visible settings panel + highlight the matching nav item. */
function activateSettingsPanel(panelName) {
  document.querySelectorAll('.settings-nav-item').forEach(i =>
    i.classList.toggle('active', i.dataset.panel === panelName));
  document.querySelectorAll('.settings-panel').forEach(p =>
    p.classList.toggle('active', p.id === 'settings-panel-' + panelName));
  if (panelName === 'telegram') loadTelegramSettings();
}

function openSettings(panel) {
  // Pre-fill email
  api('/api/settings').then(s => {
    document.getElementById('settings-email-addr').value = s.email || '';
    document.getElementById('settings-email-pass').value = s.emailPasswordSet ? '••••••••' : '';
  }).catch(() => {});
  // Pre-fill workfolder path
  api('/api/settings/workfolder').then(r => {
    document.getElementById('workfolder-path').value = r.path || '';
  }).catch(() => {});

  settingsModal.classList.remove('hidden');

  if (!workfolderConfigured) {
    // Force workfolder panel; lock everything else
    settingsModal.classList.add('setup-required');
    activateSettingsPanel('workfolder');
  } else {
    settingsModal.classList.remove('setup-required');
    activateSettingsPanel(panel || 'workfolder');
  }
}

function closeSettings() {
  // Block closing if work folder hasn't been set yet
  if (!workfolderConfigured) return;
  settingsModal.classList.add('hidden');
  // Clear every status / warning visible in any panel so stale messages
  // don't show next time the user opens Settings.
  ['settings-tg-status', 'settings-tg-status-verify', 'settings-tg-status-conn']
    .forEach(id => tgSetStatus(id, ''));
  const importStatus = document.getElementById('import-pwd-status');
  if (importStatus) { importStatus.textContent = ''; importStatus.className = 'import-status'; }
  const debugBox = document.getElementById('import-debug-box');
  if (debugBox) { debugBox.textContent = ''; debugBox.classList.add('hidden'); }
  const csvFile = document.getElementById('import-csv-file');
  if (csvFile) csvFile.value = '';
  const dialogsList = document.getElementById('tg-dialogs-list');
  if (dialogsList) dialogsList.classList.add('hidden');
  // Workfolder panel
  const wfStatus = document.getElementById('workfolder-status');
  if (wfStatus) { wfStatus.textContent = ''; wfStatus.className = 'import-status'; }
  // Notable import panel
  const notableStatus = document.getElementById('notable-import-status');
  if (notableStatus) { notableStatus.textContent = ''; notableStatus.className = 'import-status'; }
  const notableLog = document.getElementById('notable-import-log');
  if (notableLog) { notableLog.textContent = ''; notableLog.classList.add('hidden'); }
}

document.getElementById('btn-settings').addEventListener('click', openSettings);
document.getElementById('settings-close').addEventListener('click', closeSettings);
document.getElementById('settings-cancel-btn').addEventListener('click', closeSettings);
settingsModal.addEventListener('click', e => { if (e.target === settingsModal) closeSettings(); });

document.getElementById('settings-save-btn').addEventListener('click', async () => {
  const email = document.getElementById('settings-email-addr').value.trim();
  const pass  = document.getElementById('settings-email-pass').value;
  const body  = { email };
  if (pass && pass !== '••••••••') body.emailPassword = pass;
  try {
    await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    closeSettings();
  } catch (_) {}
});

// Settings left-panel navigation
document.querySelectorAll('.settings-nav-item').forEach(item => {
  item.addEventListener('click', () => {
    activateSettingsPanel(item.dataset.panel);
    if (item.dataset.panel === 'locked') loadLockedNotesList();
  });
});

// ── Telegram settings ───────────────────────────────────────────────────────

const TG_STEPS = ['credentials', 'verify', 'channel', 'connected'];

function tgSetStep(step) {
  TG_STEPS.forEach(s =>
    document.getElementById(`tg-step-${s}`).classList.toggle('hidden', s !== step)
  );
  // Update wizard dots + connector lines
  const idx = TG_STEPS.indexOf(step);
  TG_STEPS.forEach((s, i) => {
    const dot  = document.querySelector(`.tg-wizard-dot[data-step="${s}"]`);
    const line = document.querySelector(`.tg-wizard-line[data-after="${s}"]`);
    if (dot) {
      dot.classList.toggle('active', i === idx);
      dot.classList.toggle('done',   i < idx);
    }
    if (line) line.classList.toggle('done', i < idx);
  });
}

function tgSetStatus(id, msg, type = '') {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.className   = 'tg-status' + (type ? ' ' + type : '');
}

async function loadTelegramSettings() {
  try {
    const s = await api('/api/telegram');
    if (s.authorized) {
      // Pre-fill the chat input (lives in step 3) and interval selector
      document.getElementById('settings-tg-chat').value      = s.chatId || '';
      document.getElementById('settings-tg-enabled').checked = s.enabled || false;
      const ivSel = document.getElementById('settings-tg-interval');
      if (ivSel) {
        const iv  = String(s.pollInterval || 30);
        const opt = ivSel.querySelector(`option[value="${iv}"]`);
        if (opt) ivSel.value = iv; else ivSel.value = '30';
      }
      // Show connected account name
      const label = s.username ? `@${s.username}` : (s.name || 'Connected');
      document.getElementById('tg-connected-name').textContent = label;
      tgSetStatus('settings-tg-status-conn', '');
      if (s.chatId) {
        // Fully configured — jump straight to step 4
        document.getElementById('tg-connected-channel-id').textContent = s.chatId;
        tgSetStep('connected');
      } else {
        // Authorized but no channel selected yet — start at step 3
        tgSetStatus('settings-tg-status-channel', '');
        tgSetStep('channel');
      }
    } else if (s.codeSent) {
      document.getElementById('settings-tg-code').value       = '';
      document.getElementById('settings-tg-api-id').value     = s.apiId   || '';
      document.getElementById('settings-tg-api-hash').value   = s.apiHash || '';
      document.getElementById('settings-tg-phone').value      = s.phone   || '';
      tgSetStep('verify');
    } else {
      document.getElementById('settings-tg-api-id').value     = s.apiId   || '';
      document.getElementById('settings-tg-api-hash').value   = s.apiHash || '';
      document.getElementById('settings-tg-phone').value      = s.phone   || '';
      tgSetStep('credentials');
    }
  } catch (_) {
    tgSetStep('credentials');
  }
}

// Step 1 — open my.telegram.org in system browser
document.getElementById('tg-open-mytelegram').addEventListener('click', () => {
  const url = 'https://my.telegram.org/apps';
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.open_url(url);
  } else {
    window.open(url, '_blank');
  }
});

// Step 1 — send code
document.getElementById('settings-tg-sendcode-btn').addEventListener('click', async () => {
  const apiId   = document.getElementById('settings-tg-api-id').value.trim();
  const apiHash = document.getElementById('settings-tg-api-hash').value.trim();
  const phone   = document.getElementById('settings-tg-phone').value.trim();
  if (!apiId || !apiHash || !phone) {
    tgSetStatus('settings-tg-status', 'Fill in all three fields first', 'err');
    return;
  }
  tgSetStatus('settings-tg-status', 'Sending code…');
  try {
    const r = await api('/api/telegram/auth/code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apiId, apiHash, phone }),
    });
    if (r.ok) {
      tgSetStatus('settings-tg-status', '');
      tgSetStep('verify');
    } else {
      tgSetStatus('settings-tg-status', r.error || 'Failed', 'err');
    }
  } catch (_) { tgSetStatus('settings-tg-status', 'Request failed', 'err'); }
});

// Step 2 — verify code
document.getElementById('settings-tg-verify-btn').addEventListener('click', async () => {
  const code     = document.getElementById('settings-tg-code').value.trim();
  const password = document.getElementById('settings-tg-2fa').value.trim();
  if (!code) { tgSetStatus('settings-tg-status-verify', 'Enter the code first', 'err'); return; }
  tgSetStatus('settings-tg-status-verify', 'Verifying…');
  try {
    const r = await api('/api/telegram/auth/verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, password }),
    });
    if (r.ok) {
      const label = r.username ? `@${r.username}` : (r.name || 'Connected');
      document.getElementById('tg-connected-name').textContent = label;
      document.getElementById('settings-tg-chat').value        = '';
      tgSetStatus('settings-tg-status-channel', '');
      tgSetStep('channel');                        // ← go to channel setup, not directly to step 4
    } else if (r.need2fa) {
      document.getElementById('tg-2fa-field').classList.remove('hidden');
      tgSetStatus('settings-tg-status-verify', 'Enter your 2FA password above', 'err');
    } else {
      tgSetStatus('settings-tg-status-verify', r.error || 'Wrong code', 'err');
    }
  } catch (_) { tgSetStatus('settings-tg-status-verify', 'Request failed', 'err'); }
});

// Step 2 — back to credentials
document.getElementById('settings-tg-back-btn').addEventListener('click', () => {
  tgSetStatus('settings-tg-status', '');
  tgSetStep('credentials');
});

// Step 3 — browse dialogs (channels / groups)
document.getElementById('settings-tg-browse-btn').addEventListener('click', async () => {
  const btn  = document.getElementById('settings-tg-browse-btn');
  const list = document.getElementById('tg-dialogs-list');
  btn.disabled    = true;
  btn.textContent = '…';
  list.classList.add('hidden');
  list.innerHTML  = '';
  tgSetStatus('settings-tg-status-channel', 'Loading your channels…');
  try {
    const r = await api('/api/telegram/dialogs');
    if (!r.dialogs?.length) {
      tgSetStatus('settings-tg-status-channel', 'No channels/groups found — try sending a message to your channel first', 'err');
      return;
    }
    tgSetStatus('settings-tg-status-channel', '');
    r.dialogs.forEach(d => {
      const item       = document.createElement('div');
      item.className   = 'tg-dialog-item';
      item.textContent = `${d.name}  (${d.id})`;
      item.addEventListener('click', () => {
        document.getElementById('settings-tg-chat').value = String(d.id);
        list.classList.add('hidden');
      });
      list.appendChild(item);
    });
    list.classList.remove('hidden');
  } catch (_) {
    tgSetStatus('settings-tg-status-channel', 'Browse failed', 'err');
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Browse';
  }
});

// Step 3 — connect channel → advance to step 4
document.getElementById('settings-tg-connect-btn').addEventListener('click', () => {
  const chatId = document.getElementById('settings-tg-chat').value.trim();
  if (!chatId) {
    tgSetStatus('settings-tg-status-channel', 'Select a channel from the list or paste its ID', 'err');
    return;
  }
  document.getElementById('tg-connected-channel-id').textContent = chatId;
  tgSetStatus('settings-tg-status-conn', '');
  tgSetStep('connected');
});

// Step 4 — change channel (go back to step 3)
document.getElementById('settings-tg-change-channel-btn').addEventListener('click', () => {
  tgSetStatus('settings-tg-status-channel', '');
  tgSetStep('channel');
});

// Step 4 — save polling config
document.getElementById('settings-tg-save-btn').addEventListener('click', async () => {
  const chatId       = document.getElementById('settings-tg-chat').value.trim();
  const enabled      = document.getElementById('settings-tg-enabled').checked;
  const pollInterval = parseInt(document.getElementById('settings-tg-interval')?.value || '30', 10);
  try {
    await api('/api/telegram', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chatId, enabled, pollInterval }),
    });
    // Update channel display in case it changed
    document.getElementById('tg-connected-channel-id').textContent = chatId || '—';
    tgSetStatus('settings-tg-status-conn', '✓ Saved', 'ok');
    setTimeout(() => tgSetStatus('settings-tg-status-conn', ''), 2500);
  } catch (_) { tgSetStatus('settings-tg-status-conn', 'Save failed', 'err'); }
});

// Step 4 — reset sync position
document.getElementById('settings-tg-reset-btn').addEventListener('click', async () => {
  try {
    await api('/api/telegram/reset', { method: 'POST' });
    tgSetStatus('settings-tg-status-conn', 'Sync reset — will re-read all messages', 'ok');
    setTimeout(() => tgSetStatus('settings-tg-status-conn', ''), 3000);
  } catch (_) {
    tgSetStatus('settings-tg-status-conn', 'Reset failed', 'err');
  }
});

// Step 4 — log out
document.getElementById('settings-tg-logout-btn').addEventListener('click', async () => {
  try {
    await fetch('/api/telegram/session', { method: 'DELETE' });
    tgSetStep('credentials');
  } catch (_) {}
});

// ── Browser password import ─────────────────────────────────────────────────

(function () {
  // Browser radio buttons — toggle selected
  document.querySelectorAll('.browser-radio-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.browser-radio-btn').forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
      const titleEl    = document.getElementById('import-pwd-title');
      const browserName = btn.dataset.browser === 'edge' ? 'Edge' : 'Chrome';
      if (/^passwords from (chrome|edge)$/i.test(titleEl.value.trim())) {
        titleEl.value = `Passwords from ${browserName}`;
      }
    });
  });

  function setImportStatus(msg, level = '') {
    const el = document.getElementById('import-pwd-status');
    el.textContent = msg;
    el.className   = 'import-status' + (level ? ' ' + level : '');
  }

  function showImportDebug(debugText) {
    const box = document.getElementById('import-debug-box');
    if (!debugText || !box) return;
    box.textContent = debugText;
    box.classList.remove('hidden');
  }

  function hideImportDebug() {
    const box = document.getElementById('import-debug-box');
    if (box) box.classList.add('hidden');
  }

  // Auto-import from browser SQLite
  document.getElementById('import-pwd-btn').addEventListener('click', async () => {
    const btn     = document.getElementById('import-pwd-btn');
    const browser = (document.querySelector('.browser-radio-btn.selected') || {}).dataset?.browser || 'chrome';
    const title   = (document.getElementById('import-pwd-title').value || '').trim()
                    || `Passwords from ${browser.charAt(0).toUpperCase() + browser.slice(1)}`;

    btn.disabled    = true;
    btn.textContent = 'Scanning…';
    setImportStatus('');
    hideImportDebug();

    try {
      const res = await api('/api/browser-import', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ browser, title }),
      });
      if (res.debug) showImportDebug(res.debug);
      if (res.ok) {
        setImportStatus(`✓ ${res.count} password${res.count === 1 ? '' : 's'} → "${res.note.replace(/\.md$/, '')}"`, 'ok');
      } else {
        setImportStatus(res.error || 'Import failed', 'err');
      }
    } catch (e) {
      setImportStatus('Request error: ' + e.message, 'err');
    } finally {
      btn.disabled    = false;
      btn.textContent = 'Import';
    }
  });

  // CSV import — file picker
  document.getElementById('import-csv-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const title = (document.getElementById('import-pwd-title').value || '').trim()
                  || 'Passwords from CSV';
    setImportStatus('Reading CSV…');
    hideImportDebug();
    try {
      const csv = await file.text();
      const res = await api('/api/browser-import/csv', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ csv, title }),
      });
      if (res.ok) {
        setImportStatus(`✓ ${res.count} password${res.count === 1 ? '' : 's'} → "${res.note.replace(/\.md$/, '')}"`, 'ok');
      } else {
        setImportStatus(res.error || 'CSV import failed', 'err');
      }
    } catch (e) {
      setImportStatus('CSV error: ' + e.message, 'err');
    } finally {
      e.target.value = '';   // reset so the same file can be re-picked
    }
  });
})();

// ── WORKFOLDER SETUP ────────────────────────────────────────────────────────

const workfolderSaveBtn = document.getElementById('workfolder-save-btn');
const workfolderPathInput = document.getElementById('workfolder-path');

function _wfEnableSave() { workfolderSaveBtn.disabled = false; }

document.getElementById('workfolder-browse-btn').addEventListener('click', async function () {
  if (window.pywebview && window.pywebview.api) {
    const path = await window.pywebview.api.pick_folder();
    if (path) {
      workfolderPathInput.value = path;
      _wfEnableSave();
    }
  } else {
    const st = document.getElementById('workfolder-status');
    st.textContent = 'Folder picker only works in the desktop app — type the path manually.';
    st.className = 'import-status err';
  }
});

// Re-enable Save whenever the user edits the path by hand
workfolderPathInput.addEventListener('input', _wfEnableSave);

workfolderSaveBtn.addEventListener('click', async function () {
  const path    = (workfolderPathInput.value || '').trim();
  const statusEl = document.getElementById('workfolder-status');

  function setStatus(msg, cls) {
    statusEl.textContent = msg;
    statusEl.className   = 'import-status' + (cls ? ' ' + cls : '');
  }

  if (!path) { setStatus('Enter a folder path first.', 'err'); return; }

  setStatus('Saving…');
  try {
    const res = await api('/api/settings/workfolder', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ path }),
    });
    if (res.error) { setStatus(res.error, 'err'); return; }

    // Show the resolved path (may differ if myNotes was appended)
    workfolderPathInput.value = res.path;
    workfolderSaveBtn.disabled = true;   // gray out until a new path is entered
    setStatus('✓ Saved — ' + res.path, 'ok');
    workfolderConfigured = true;
    settingsModal.classList.remove('setup-required');
    // Reload notes from the new folder
    await renderTags();
    await renderNotes();
  } catch (e) {
    setStatus('Error: ' + e.message, 'err');
  }
});

/** Called once on startup — checks if a work folder has been configured. */
async function checkWorkfolder() {
  try {
    const r = await api('/api/settings/workfolder');
    workfolderConfigured = r.configured;
    if (r.path) {
      workfolderPathInput.value = r.path;
      workfolderSaveBtn.disabled = true;  // already saved — nothing changed yet
    }
    if (!workfolderConfigured) openSettings('workfolder');
  } catch (_) {
    // If the API is unreachable on first tick, skip — user can open settings manually
  }
}

// ── NOTABLE IMPORT ──────────────────────────────────────────────────────────

document.getElementById('notable-browse-btn').addEventListener('click', async function () {
  if (window.pywebview && window.pywebview.api) {
    const path = await window.pywebview.api.pick_folder();
    if (path) {
      document.getElementById('notable-folder-path').value = path;
      document.getElementById('notable-import-btn').disabled = false;
    }
  } else {
    // Running in browser dev mode — native picker not available
    const statusEl = document.getElementById('notable-import-status');
    statusEl.textContent = 'Folder picker only works in the desktop app — type the path manually.';
    statusEl.className = 'import-status err';
  }
});

document.getElementById('notable-import-btn').addEventListener('click', async () => {
  const folder  = (document.getElementById('notable-folder-path').value || '').trim();
  const statusEl = document.getElementById('notable-import-status');
  const logEl    = document.getElementById('notable-import-log');

  function setStatus(msg, cls) {
    statusEl.textContent  = msg;
    statusEl.className    = 'import-status' + (cls ? ' ' + cls : '');
  }

  if (!folder) { setStatus('Enter a folder path first.', 'err'); return; }

  setStatus('Importing…');
  logEl.textContent = '';
  logEl.classList.add('hidden');

  try {
    const res = await api('/api/import/notable', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ folder }),
    });

    if (res.error) {
      setStatus(res.error, 'err');
      return;
    }

    const parts = [];
    if (res.imported) parts.push(`${res.imported} imported`);
    if (res.skipped)  parts.push(`${res.skipped} skipped`);
    setStatus('✓ ' + (parts.join(', ') || 'Nothing to import'), 'ok');

    if (res.errors && res.errors.length) {
      logEl.textContent = 'Errors:\n' + res.errors.join('\n');
      logEl.classList.remove('hidden');
    }

    if (res.imported) {
      renderTags().then(() => renderNotes());
      // Disable button until a new path is entered
      document.getElementById('notable-import-btn').disabled = true;
    }
  } catch (e) {
    setStatus('Error: ' + e.message, 'err');
  }
});

// Re-enable import button whenever the folder path is changed
document.getElementById('notable-folder-path').addEventListener('input', () => {
  document.getElementById('notable-import-btn').disabled = false;
});

// ── NEW NOTE ────────────────────────────────────────────────────────────────

document.getElementById('btn-new').addEventListener('click', async () => {
  if (activeTag === TRASH_TAG) return;

  if (activeButton === 'new') {
    clearTimeout(saveTimer);
    if (activeNote) await saveCurrentNote();
    enterViewMode();
    setActiveButton(null);
    return;
  }

  clearTimeout(saveTimer);
  if (activeNote && colContent.classList.contains('editing')) await saveCurrentNote();
  activeNote = '';
  setActiveButton('new');

  const { name } = await api('/api/next-untitled');
  await api('/api/note', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, content: `# ${name}\n` }),
  });

  if (activeTag !== '') {
    activeTag = '';
    document.querySelectorAll('.tag-item').forEach(el =>
      el.classList.toggle('active', el.dataset.tag === ''));
    updateToolbarState();
  }

  await renderNotes();
  await openNote(name, true);
});

// ── EDIT ────────────────────────────────────────────────────────────────────

document.getElementById('btn-edit').addEventListener('click', async () => {
  if (activeTag === TRASH_TAG) return;

  if (activeButton === 'edit') {
    clearTimeout(saveTimer);
    await saveCurrentNote();
    enterViewMode();
    setActiveButton(null);
    return;
  }

  if (!activeNote || (noteEncrypted && !notePasswords.has(activeNote))) return;
  setActiveButton('edit');
  enterEditMode();
});

// ── TAGS ────────────────────────────────────────────────────────────────────

document.getElementById('btn-tags').addEventListener('click', async () => {
  if (activeTag === TRASH_TAG) return;

  if (activeButton === 'tags') { await closeTagPopover(true); return; }

  if (!activeNote) return;
  clearTimeout(saveTimer);
  if (colContent.classList.contains('editing')) await saveCurrentNote();
  setActiveButton('tags');
  await openTagPopover();
});

// ── Tag popover ─────────────────────────────────────────────────────────────

const tagPopover       = document.getElementById('tag-popover');
const tagChipList      = document.getElementById('tag-chip-list');
const tagInputField    = document.getElementById('tag-input-field');
const tagSuggestionsEl = document.getElementById('tag-suggestions');

async function openTagPopover() {
  const tags  = await api(`/api/note/${encodeURIComponent(activeNote)}/tags`);
  editingTags = [...new Set(tags)];
  renderTagChips();
  renderTagSuggestions('');
  placeTagPopover();
  tagInputField.value = '';
  tagInputField.focus();
}

function renderTagChips() {
  tagChipList.innerHTML = '';
  editingTags.forEach((tag, i) => {
    const chip    = document.createElement('div');
    chip.className = 'tag-chip';
    const nameEl  = document.createElement('span');
    nameEl.className = 'tag-chip-name';
    nameEl.textContent = tag;
    const removeBtn = document.createElement('button');
    removeBtn.className = 'tag-chip-remove';
    removeBtn.textContent = '×';
    removeBtn.addEventListener('mousedown', e => {
      e.preventDefault(); e.stopPropagation();
      editingTags.splice(i, 1);
      renderTagChips(); placeTagPopover();
    });
    chip.appendChild(nameEl); chip.appendChild(removeBtn);
    tagChipList.appendChild(chip);
  });
}

function renderTagSuggestions(query) {
  tagSuggestionsEl.innerHTML = '';
  if (!query) return;
  const q = query.toLowerCase();
  allTagsCache
    .filter(t => t.toLowerCase().includes(q) && !editingTags.includes(t))
    .slice(0, 8)
    .forEach(tag => {
      const item = document.createElement('div');
      item.className = 'tag-suggestion-item';
      item.textContent = tag;
      item.addEventListener('mousedown', e => {
        e.preventDefault();
        if (!editingTags.includes(tag)) editingTags.push(tag);
        tagInputField.value = '';
        renderTagChips(); renderTagSuggestions(''); placeTagPopover();
        tagInputField.focus();
      });
      tagSuggestionsEl.appendChild(item);
    });
}

function placeTagPopover() {
  const btn   = document.getElementById('btn-tags');
  const rect  = btn.getBoundingClientRect();
  const btnCX = rect.left + rect.width / 2;
  tagPopover.classList.remove('tag-popover-hidden');
  const pw   = tagPopover.offsetWidth;
  let   left = Math.max(8, Math.min(btnCX - pw / 2, window.innerWidth - pw - 8));
  tagPopover.style.top  = (rect.bottom + 8) + 'px';
  tagPopover.style.left = left + 'px';
  tagPopover.style.setProperty('--arrow-x', (btnCX - left) + 'px');
}

async function closeTagPopover(saveInput = true) {
  const raw = tagInputField.value.trim();
  if (saveInput && raw && !editingTags.includes(raw) && !_isNewReservedTag(raw))
    editingTags.push(raw);
  tagInputField.value = '';
  tagSuggestionsEl.innerHTML = '';
  tagPopover.classList.add('tag-popover-hidden');
  if (activeNote) {
    await api(`/api/note/${encodeURIComponent(activeNote)}/tags`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags: editingTags }),
    });
    await renderTags();
  }
  setActiveButton(null);
  _flushPendingUpdate();
}

tagInputField.addEventListener('input', e => {
  renderTagSuggestions(e.target.value.trim());
  placeTagPopover();
});

tagInputField.addEventListener('keydown', async e => {
  if (e.key === 'Enter') {
    e.preventDefault(); e.stopPropagation();
    const val = tagInputField.value.trim();
    if (val && !editingTags.includes(val)) {
      if (_isNewReservedTag(val)) {
        // Flash the popover to signal the tag is reserved
        tagPopover.classList.add('tag-input-reserved');
        setTimeout(() => tagPopover.classList.remove('tag-input-reserved'), 600);
      } else {
        editingTags.push(val);
        tagInputField.value = '';
        renderTagChips(); renderTagSuggestions(''); placeTagPopover();
      }
    } else {
      tagInputField.value = '';
      renderTagChips(); renderTagSuggestions(''); placeTagPopover();
    }
    tagInputField.focus();
  } else if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    await closeTagPopover(false);
  }
});

document.addEventListener('mousedown', async e => {
  if (tagPopover.classList.contains('tag-popover-hidden') ||
      tagPopover.contains(e.target) ||
      document.getElementById('btn-tags').contains(e.target)) return;
  await closeTagPopover(true);
});

// ── TRASH ───────────────────────────────────────────────────────────────────

document.getElementById('btn-trash').addEventListener('click', async () => {
  const endpoint = activeTag === TRASH_TAG ? 'restore' : 'trash';

  if (selectedNotes.size >= 2) {
    // ── Multi-select: trash / restore every selected note ──
    const names = [...selectedNotes];
    for (const n of names) {
      try {
        await api(`/api/note/${encodeURIComponent(n)}/${endpoint}`, { method: 'POST' });
        notePasswords.delete(n);
      } catch (_) {}
    }
    clearMultiSelect();
    activeNote = '';
    setActiveButton(null);
    clearContentPane();
    await renderTags();
    await renderNotes();
    return;
  }

  // ── Single note ──
  if (!activeNote) return;
  if (endpoint === 'trash') {
    clearTimeout(saveTimer);
    if (colContent.classList.contains('editing')) await saveCurrentNote();
  }
  await api(`/api/note/${encodeURIComponent(activeNote)}/${endpoint}`, { method: 'POST' });
  notePasswords.delete(activeNote);
  activeNote = '';
  setActiveButton(null);
  clearContentPane();
  document.querySelectorAll('.note-item').forEach(el => el.classList.remove('active'));
  await renderTags();
  await renderNotes();
});

// ── Global Escape ───────────────────────────────────────────────────────────

document.addEventListener('keydown', async e => {
  if (e.key !== 'Escape') return;

  if (!encryptDialog.classList.contains('hidden')) { closeEncryptDialog(); return; }
  if (!settingsModal.classList.contains('hidden'))  { closeSettings();       return; }
  if (!tagPopover.classList.contains('tag-popover-hidden')) {
    await closeTagPopover(false); return;
  }
  if (selectedNotes.size >= 2) {
    clearMultiSelect();
    return;
  }
  if (activeButton === 'edit' || activeButton === 'new') {
    clearTimeout(saveTimer);
    if (activeNote) await saveCurrentNote();
    enterViewMode();
    setActiveButton(null);
  }
});

// ── Global Delete key → trash selected / active note ────────────────────────

document.addEventListener('keydown', e => {
  if (e.key !== 'Delete') return;

  // Never fire while the user is editing a note, composing a new note,
  // or has a tag input popover open.
  if (_isEditingActive()) return;

  // Also ignore if the active element is any input / textarea / contenteditable
  // (covers edge cases like the search box).
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' ||
      document.activeElement?.isContentEditable) return;

  // Trigger the trash button if it's enabled — it already handles both
  // multi-select and single-note modes, plus restore-from-trash logic.
  const trashBtn = document.getElementById('btn-trash');
  if (!trashBtn.disabled) trashBtn.click();
});

// ── Export ──────────────────────────────────────────────────────────────────

async function doExport(content) {
  try {
    const result = await api('/api/export-pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, name: activeNote }),
    });
    showExportToast(result.path);
  } catch (_) {}
}

document.getElementById('btn-export').addEventListener('click', () => {
  if (!activeNote) return;
  if (noteEncrypted) {
    // Always re-verify password before exporting an encrypted note
    openEncryptDialog('export');
  } else {
    doExport(noteBodyContent);
  }
});

// ── Share as HTML ────────────────────────────────────────────────────────────

document.getElementById('btn-share').addEventListener('click', async () => {
  if (!activeNote || noteEncrypted) return;
  const btn = document.getElementById('btn-share');
  btn.disabled = true;
  try {
    const result = await api('/api/share', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name: activeNote }),
    });
    showExportToast(result.path);
  } catch (err) {
    console.error('Share failed:', err);
  } finally {
    updateShareButton();
  }
});

function showExportToast(filePath) {
  toastFilePath = filePath;
  document.getElementById('export-toast-msg').textContent = filePath;
  document.getElementById('export-toast').classList.remove('hidden');
}

function hideExportToast() {
  document.getElementById('export-toast').classList.add('hidden');
  toastFilePath = null;
}

document.getElementById('export-toast-close').addEventListener('click', e => {
  e.stopPropagation();
  hideExportToast();
});

document.querySelector('.export-toast-body').addEventListener('click', async () => {
  if (!toastFilePath) return;
  try {
    await fetch('/api/reveal-file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: toastFilePath }),
    });
  } catch (_) {}
  hideExportToast();
});

document.addEventListener('mousedown', e => {
  const toast = document.getElementById('export-toast');
  if (!toast.classList.contains('hidden') && !toast.contains(e.target)) hideExportToast();
});

// ── Notification toast ─────────────────────────────────────────────────────

let _notifHideTimer = null;

function showNotif(msg, level = 'warn') {
  const toast = document.getElementById('notif-toast');
  document.getElementById('notif-toast-msg').textContent = msg;
  toast.className = `notif-toast ${level}`;
  clearTimeout(_notifHideTimer);
  _notifHideTimer = setTimeout(() => toast.classList.add('hidden'), 8000);
}

document.getElementById('notif-toast-close').addEventListener('click', () => {
  clearTimeout(_notifHideTimer);
  document.getElementById('notif-toast').classList.add('hidden');
});

// ── Server-Sent Events (live push from backend) ─────────────────────────────

// Pending TG update deferred while the user is editing.
let _pendingTgUpdate = null;

function _isEditingActive() {
  return colContent.classList.contains('editing') ||          // note in edit mode / new note
         !tagPopover.classList.contains('tag-popover-hidden'); // tag popover open
}

function _applyNotesUpdate(ev) {
  renderTags().then(() => renderNotes()).then(() => {
    if (!ev || !ev.appended || !ev.name) return;
    const noteName = ev.name.replace(/\.md$/i, '');
    const item = Array.from(document.querySelectorAll('.note-item'))
                      .find(el => el.dataset.name === noteName);
    if (item) {
      item.classList.remove('tg-blink');
      void item.offsetWidth;               // restart animation if already running
      item.classList.add('tg-blink');
      setTimeout(() => item.classList.remove('tg-blink'), 9000);
    }
  });
}

function _flushPendingUpdate() {
  if (_pendingTgUpdate) {
    const ev = _pendingTgUpdate;
    _pendingTgUpdate = null;
    _applyNotesUpdate(ev);
  }
}

function _handleNotesUpdated(ev) {
  if (_isEditingActive()) {
    _pendingTgUpdate = ev;   // overwrite — only latest matters
    return;
  }
  _applyNotesUpdate(ev);
}

(function connectSSE() {
  const src = new EventSource('/api/events');

  src.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }

    if (ev.type === 'notes-updated') {
      _handleNotesUpdated(ev);
    } else if (ev.type === 'notify') {
      showNotif(ev.msg, ev.level || 'warn');
    }
  };

  src.onerror = () => {
    src.close();
    // Reconnect after 5 s if the connection drops
    setTimeout(connectSSE, 5000);
  };
})();

// ── pywebview frameless title bar ─────────────────────────────────────────

// pywebview injects window.pywebview and fires 'pywebviewready' once the
// bridge is ready.  We use that to activate the custom title bar and wire
// the native window-control buttons.  In plain browser dev mode this block
// never runs and #titlebar stays hidden.
window.addEventListener('pywebviewready', function () {
  document.documentElement.classList.add('webview-app');

  document.getElementById('tb-close').addEventListener('click', function () {
    window.pywebview.api.close();
  });

  document.getElementById('tb-minimize').addEventListener('click', function () {
    window.pywebview.api.minimize();
  });

  document.getElementById('tb-maximize').addEventListener('click', function () {
    window.pywebview.api.toggle_maximize();
  });

  // Double-click drag area → toggle maximize (standard Windows behaviour)
  document.getElementById('titlebar-drag').addEventListener('dblclick', function () {
    window.pywebview.api.toggle_maximize();
  });

});

// ── Init ───────────────────────────────────────────────────────────────────

renderTags().then(() => renderNotes()).then(() => checkWorkfolder());
