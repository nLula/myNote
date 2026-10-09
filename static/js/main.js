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
  return md.replace(
    /\]\((\/attachments\/[^/\n]+\/)((?:[^()\n]+|\([^()\n]*\))*)\)/g,
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
                 `<video src="${href}" controls preload="none" class="note-media-video"></video>` +
                 `<div class="note-media-label">${fname}</div>` +
               `</div>`;
      }
      if (_MEDIA_AUDIO_EXTS.has(ext)) {
        return `<div class="note-media-wrap note-media-wrap--audio">` +
                 `<div class="note-media-label">${fname}</div>` +
                 `<audio src="${href}" controls preload="none" class="note-media-audio"></audio>` +
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
let _isCreatingNote  = false;
let searchQuery      = '';
let trashTagFilter   = '';   // secondary tag filter when browsing Trash
let lockedGroupExpanded = false;
let lockedCollapseTimer = null;
let alphaGroupExpanded  = new Set(); // letter keys of expanded alpha groups
let sortBy   = 'ctime';   // ctime | alpha | mtime
let sortDesc = true;      // true = descending
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
let titleBaseline    = '';    // heading the open note had when it was loaded
let openedBody       = '';    // body as loaded, for spotting an untouched editor
let _editingHbTimer  = null;  // heartbeat interval while editing is active
let _periodicSaveTimer   = null;  // interval that flushes changes every 5 s
let _periodicSavedBody   = null;  // body content at the last periodic write

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

// ── View ↔ edit scroll sync ────────────────────────────────────────────────
// Both panes scroll independently, so switching modes has to translate a
// position in one into the other.  The common currency is the markdown source
// line: the rendered view is mapped back to source lines through marked's
// token stream, and the textarea is measured with a hidden mirror div that
// reproduces its soft-wrapping.

let _editSrcNote = '';   // note whose content the textarea currently holds
let _viewSrcNote = '';   // note the rendered view currently shows
let _srcMirror   = null;

/** Hidden div whose text layout matches the textarea's, for line-offset math. */
function _syncMirror() {
  if (!_srcMirror) {
    _srcMirror = document.createElement('div');
    _srcMirror.setAttribute('aria-hidden', 'true');
    Object.assign(_srcMirror.style, {
      position: 'absolute', top: '0', left: '-99999px',
      visibility: 'hidden', pointerEvents: 'none', border: '0',
    });
    document.body.appendChild(_srcMirror);
  }
  const cs = getComputedStyle(noteEdit);
  for (const p of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontVariant',
                   'letterSpacing', 'lineHeight', 'wordSpacing', 'textIndent',
                   'textTransform', 'wordBreak', 'overflowWrap', 'tabSize',
                   'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft'])
    _srcMirror.style[p] = cs[p];
  _srcMirror.style.whiteSpace = 'pre-wrap';
  _srcMirror.style.boxSizing  = 'border-box';
  _srcMirror.style.width      = noteEdit.clientWidth + 'px';
  return _srcMirror;
}

/** Y offset in the textarea's scroll space of the start of source `line`. */
function _editOffsetForLine(line, lines) {
  if (line <= 0) return 0;
  const m = _syncMirror();
  m.textContent = '';
  m.appendChild(document.createTextNode(lines.slice(0, line).join('\n') + '\n'));
  const marker = document.createElement('span');
  marker.textContent = '​';
  m.appendChild(marker);
  return marker.offsetTop;
}

/** Source line currently at the top of the textarea's viewport. */
function _editTopLine() {
  const st = noteEdit.scrollTop;
  if (st <= 0) return 0;
  const lines = noteEdit.value.split('\n');
  // Largest L where offset(L) <= scrollTop
  let lo = 0, hi = lines.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (_editOffsetForLine(mid, lines) <= st) lo = mid; else hi = mid - 1;
  }
  return lo;
}

/**
 * Put the caret on the line directly below the note's heading, scrolled to the
 * top. This is how the placeholder opens: the heading is left empty and waiting
 * to be named, so typing should start in the body, not on the '#' line.
 */
function _placeCaretBelowHeading() {
  const lines = noteEdit.value.split('\n');
  const hIdx  = lines.findIndex(l => /^#/.test(l));
  const target = hIdx === -1 ? 0 : hIdx + 1;
  const pos = Math.min(
    lines.slice(0, target).join('\n').length + (target > 0 ? 1 : 0),
    noteEdit.value.length
  );
  try { noteEdit.setSelectionRange(pos, pos); } catch (_) {}
  noteEdit.scrollTop = 0;
}

/** Scroll the textarea so `line` sits at the top, and park the caret there. */
function _scrollEditToLine(line) {
  const lines = noteEdit.value.split('\n');
  const l     = Math.max(0, Math.min(line, lines.length));
  const idx   = lines.slice(0, l).join('\n').length + (l > 0 ? 1 : 0);
  try { noteEdit.setSelectionRange(idx, idx); } catch (_) {}
  noteEdit.scrollTop = _editOffsetForLine(l, lines);
}

/**
 * [{ el, line }] pairing each top-level rendered block with the source line it
 * came from.  marked's top-level tokens map 1:1 onto note-view's children once
 * whitespace-only tokens (which render nothing) are skipped.
 */
function _buildViewLineMap() {
  const map  = [];
  const kids = Array.from(noteView.children);
  if (!kids.length) return map;
  let tokens;
  try { tokens = marked.lexer(noteBodyContent); } catch (_) { return map; }
  let line = 0, ki = 0;
  for (const t of tokens) {
    if (t.type !== 'space') {
      if (ki >= kids.length) break;
      map.push({ el: kids[ki], line });
      ki += 1;
    }
    line += ((t.raw || '').match(/\n/g) || []).length;
  }
  return map;
}

/** Source line currently at the top of the rendered view's viewport. */
function _viewTopLine() {
  const map = _buildViewLineMap();
  if (!map.length) return null;
  const vTop = noteView.getBoundingClientRect().top;
  const st   = noteView.scrollTop;
  const tops = map.map(m => m.el.getBoundingClientRect().top - vTop + st);
  if (st <= 0) return map[0].line;

  let i = 0;
  while (i + 1 < map.length && tops[i + 1] <= st + 2) i += 1;
  // Interpolate inside the block so long paragraphs don't snap to their start
  if (i + 1 < map.length) {
    const span = tops[i + 1] - tops[i];
    if (span > 0) {
      const frac = Math.max(0, Math.min(1, (st - tops[i]) / span));
      return map[i].line + Math.round(frac * (map[i + 1].line - map[i].line));
    }
  }
  return map[i].line;
}

/** Scroll the rendered view so the block containing `line` sits at the top. */
function _scrollViewToLine(line) {
  const map = _buildViewLineMap();
  if (!map.length) return;
  let i = 0;
  while (i + 1 < map.length && map[i + 1].line <= line) i += 1;
  // At the very first block, show the pane's top padding rather than clipping it
  if (i === 0) { noteView.scrollTop = 0; return; }
  const vTop = noteView.getBoundingClientRect().top;
  noteView.scrollTop = Math.max(0, map[i].el.getBoundingClientRect().top - vTop + noteView.scrollTop);
}

function enterViewMode() {
  // Coming back from the editor on the same note — carry the scroll position over
  const _srcLine = (colContent.classList.contains('editing') && _editSrcNote === activeNote)
    ? _editTopLine() : null;

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

    // Replace broken attachment media with a "not here yet" placeholder
    noteView.querySelectorAll('img[src^="/attachments/"]').forEach(img => {
      img.addEventListener('error', () => {
        const ph = document.createElement('span');
        ph.className = 'attachment-missing';
        ph.textContent = 'File is not here yet';
        img.replaceWith(ph);
      }, { once: true });
    });
    noteView.querySelectorAll('video[src^="/attachments/"], audio[src^="/attachments/"]').forEach(el => {
      el.addEventListener('error', () => {
        const wrap = el.closest('.note-media-wrap');
        const ph = document.createElement('div');
        ph.className = 'attachment-missing';
        ph.textContent = 'File is not here yet';
        (wrap || el).replaceWith(ph);
      }, { once: true });
    });

    // Inject copy buttons into fenced code blocks.
    // The button lives in a wrapper DIV outside <pre> so it isn't inside
    // pre's horizontal scroll container — otherwise position:absolute right:8px
    // is computed from the scroll-content edge and the button drifts with code.
    noteView.querySelectorAll('pre').forEach(pre => {
      const wrapper = document.createElement('div');
      wrapper.className = 'pre-wrapper';
      pre.parentNode.insertBefore(wrapper, pre);
      wrapper.appendChild(pre);

      const btn = document.createElement('button');
      btn.className = 'code-copy-btn';
      btn.textContent = 'Copy';
      btn.addEventListener('click', () => {
        const text = (pre.querySelector('code') || pre).innerText;
        navigator.clipboard.writeText(text).then(() => {
          btn.classList.add('copied');
          btn.textContent = 'Copied!';
          setTimeout(() => { btn.classList.remove('copied'); btn.textContent = 'Copy'; }, 2000);
        }).catch(() => {});
      });
      wrapper.appendChild(btn);
    });
  }
  colContent.classList.remove('editing');
  document.body.classList.remove('editing-active');
  _stopEditingHeartbeat();
  _stopPeriodicSave();
  _flushPendingUpdate();
  if (_srcLine !== null) _scrollViewToLine(_srcLine);
  _viewSrcNote = activeNote;
  window._noteFindRerun?.();
}

function _notifyEditing(active) {
  fetch('/api/editing', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({active})
  }).catch(() => {});
}

function _startEditingHeartbeat() {
  _notifyEditing(true);
  if (_editingHbTimer) return;
  // Refresh the 30-second server window every 20 s while editing is active.
  _editingHbTimer = setInterval(() => _notifyEditing(true), 20000);
}

function _stopEditingHeartbeat() {
  if (_editingHbTimer) { clearInterval(_editingHbTimer); _editingHbTimer = null; }
  _notifyEditing(false);
}

function _startPeriodicSave() {
  _periodicSavedBody = noteBodyContent;  // baseline — don't save what's already on disk
  if (_periodicSaveTimer) return;
  _periodicSaveTimer = setInterval(async () => {
    if (!activeNote || !colContent.classList.contains('editing')) return;
    if (noteBodyContent === _periodicSavedBody) return;  // nothing changed
    _periodicSavedBody = noteBodyContent;
    try { await saveCurrentNote(false); } catch (_) {}
  }, 5000);
}

function _stopPeriodicSave() {
  if (_periodicSaveTimer) { clearInterval(_periodicSaveTimer); _periodicSaveTimer = null; }
  _periodicSavedBody = null;
}

function enterEditMode() {
  if (noteEncrypted && !notePasswords.has(activeNote)) return;
  // Only carry a scroll position over when the view is actually showing this
  // note — opening a different note straight into edit mode has nothing to carry.
  const _srcLine = (colContent.classList.contains('editing') || _viewSrcNote !== activeNote)
    ? null : _viewTopLine();

  window._noteFindClose?.();
  noteEdit.value = noteBodyContent;
  colContent.classList.add('editing');
  document.body.classList.add('editing-active');
  _editSrcNote = activeNote;
  _startEditingHeartbeat();
  _startPeriodicSave();
  noteEdit.focus();
  // After focus — focusing a textarea scrolls it to the caret, which would
  // otherwise undo the restored position.
  if (_srcLine !== null) _scrollEditToLine(_srcLine);
}

function clearContentPane() {
  cancelAutoLockTimer();
  window._noteFindClose?.();
  _editSrcNote  = '';
  _viewSrcNote  = '';
  titleBaseline = '';
  openedBody    = '';
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
  _stopEditingHeartbeat();
  _stopPeriodicSave();
  setMetaDates(null, null);
  document.getElementById('btn-trash').disabled = true;
  updateLockButton();
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
      updateTrashButton();
      updateLockButton();
      updateEditTagButtons();
    }
  }, 60000);
}

function cancelAutoLockTimer() {
  if (autoLockTimer) { clearTimeout(autoLockTimer); autoLockTimer = null; }
}

function updateExportButton() {
  document.getElementById('btn-export').disabled = !activeNote || noteEncrypted;
}

function updateLockButton() {
  const btn = document.getElementById('btn-lock');
  if (!activeNote) {
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  const locked = noteEncrypted && !notePasswords.has(activeNote);
  btn.removeAttribute('title');
  btn.dataset.tip = locked ? 'Unlock note' : 'Lock note';
}

function updateShareButton() {
  // Share is available for any open non-encrypted note
  const btn = document.getElementById('btn-share');
  btn.disabled = !activeNote || noteEncrypted;
}

function updateTrashButton() {
  // Trash (= "Move to Trash") requires the note to be unlocked first.
  // In Trash view the button means "Restore", which is always allowed.
  const inTrash = activeTag === TRASH_TAG;
  const locked  = noteEncrypted && !notePasswords.has(activeNote);
  // The placeholder note is never trashable — it is reused, not deleted.
  const fixture = !inTrash && isPlaceholderName(activeNote);
  document.getElementById('btn-trash').disabled =
    !activeNote || (!inTrash && locked) || fixture;
}

function updateEditTagButtons() {
  // Edit and Tags are disabled when: no note open, in Trash view, or note is locked
  const off = !activeNote
    || activeTag === TRASH_TAG
    || (noteEncrypted && !notePasswords.has(activeNote));
  document.getElementById('btn-edit').disabled = off;
  document.getElementById('btn-tags').disabled = off;
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
  _stopEditingHeartbeat();
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
  updateTrashButton();
  updateLockButton();
  updateExportButton();
  updateShareButton();
}

/**
 * Text of the note's first "# " heading, or '' when it has none.
 *
 * The separator is [ \t] and not \s on purpose: \s matches newlines too, so on
 * a note whose heading is still empty ("# " with nothing after it — what the
 * phone writes into its placeholder note) the old pattern ran straight through
 * the blank lines and captured the first line of the body instead.  That made
 * the note's own content its title, and saveAndRename then renamed the file
 * after it.
 */
function extractTitle(body) {
  const m = body.match(/^#[ \t]+(.+)/m);
  return m ? m[1].trim() : '';
}

function sanitizeFilename(name) {
  // Keep Unicode letters/digits/combining marks, space, dot, hyphen, underscore.
  // Everything else (punctuation, symbols, emoji) → underscore.
  return name
    .replace(/[^\p{L}\p{N}\p{M} .\-_]/gu, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '')
    .trim()
    .slice(0, 100) || 'Untitled';
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

async function saveCurrentNote(sessionEnd = false) {
  if (!activeNote) return;
  // Did anything actually change during this editing session? openedBody is the
  // text as loaded, and is re-baselined below on each session end, so this is
  // "changed since the last save" rather than "changed since the note opened".
  const contentChanged = noteBodyContent !== openedBody;
  let body = noteBodyContent;
  if (noteEncrypted && notePasswords.has(activeNote)) {
    body = await encryptText(noteBodyContent, notePasswords.get(activeNote));
    noteRawCiphertext = body;  // keep in sync so session-lock can restore it
  }
  await api(`/api/note/${encodeURIComponent(activeNote)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: noteFrontmatter + body, session_end: sessionEnd }),
  });
  if (sessionEnd) _invalidateHistoryCache(activeNote);
  noteMtime = Date.now() / 1000;
  metaSecondary.textContent = 'Last change: ' + formatDate(noteMtime);

  // Finishing an editing session pushes the note out straight away rather than
  // waiting for the interval. session_end is exactly that moment, and every way
  // of leaving the editor — the Edit toggle, Escape, opening another note,
  // trashing, starting a new note — routes through here.
  //
  // Only when something was actually written, though: merely opening a note and
  // closing it again is not a reason to go to the network. Re-baseline first so
  // leaving, re-entering and leaving again doesn't count as a second change.
  if (sessionEnd) {
    openedBody = noteBodyContent;
    // Deliberately not awaited: leaving the editor must not wait on the
    // network. runSync ignores the call if one is already in flight.
    if (contentChanged) window._syncNow?.({ silent: true });
  }
}

function extractNoteSuffix(noteName) {
  const m = noteName.match(/_([A-Za-z0-9]{8})$/);
  return m ? m[1] : null;
}

// The phone's scratch note. Mirrors _is_placeholder_note / _PLACEHOLDER_TITLE
// on the backend — it is a fixture that gets emptied and reused, so it is shown
// under a friendly name and can never be trashed.
const PLACEHOLDER_BASE  = 'no_name';
const PLACEHOLDER_TITLE = 'New';

function isPlaceholderName(name) {
  return String(name || '').replace(/_[A-Za-z0-9]{8}$/, '').toLowerCase() === PLACEHOLDER_BASE;
}

/**
 * Whether a finished sync may re-render and reload the open note.
 *
 * Never while the user has actually typed something that hasn't been written
 * back yet; an editor sitting open on unmodified text is fair game.
 */
function _canRefreshAfterSync() {
  if (!_isEditingActive()) return true;
  return noteBodyContent === openedBody;
}

/**
 * Pull the open note's text back off disk straight into the editor.
 *
 * Deliberately not openNote(): that saves the note being left before loading
 * the next one, and on the note already open that writes the stale buffer over
 * the fresher file the sync just pulled down. Only ever called on an untouched
 * editor, so there is nothing of the user's to overwrite.
 */
async function _reloadEditorBuffer() {
  if (!activeNote) return;
  // Locked notes are left alone — re-deriving plaintext here would mean
  // handling the password path all over again for no benefit.
  if (noteEncrypted) return;
  try {
    const data    = await api(`/api/note/${encodeURIComponent(activeNote)}`);
    const content = data.content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const fmMatch = content.match(/^(---\s*\n[\s\S]*?\n---\s*\n?)/);
    if (/^encrypted:\s*true/m.test(fmMatch ? fmMatch[1] : '')) return;
    noteFrontmatter = fmMatch ? fmMatch[1] : '';
    noteBodyContent = fmMatch ? content.slice(fmMatch[1].length).trimStart() : content;
    if (noteEdit.value !== noteBodyContent) noteEdit.value = noteBodyContent;
    openedBody    = noteBodyContent;
    titleBaseline = extractTitle(noteBodyContent);
    noteMtime     = data.mtime ?? noteMtime;
    setMetaDates(noteCtime, noteMtime);
  } catch (_) {}
}

/** What to show in the list for a note with no heading of its own. */
function fallbackNoteLabel(name) {
  return isPlaceholderName(name)
    ? PLACEHOLDER_TITLE
    : String(name || '').replace(/_[A-Za-z0-9]{8}$/, '');
}

async function saveAndRename(title) {
  if (!activeNote) return;
  await saveCurrentNote();

  // The filename only follows the heading, and only when the heading itself
  // changed since the note was opened.  Editing the body of a note that has no
  // heading (or an empty one) just saves in place — it must not spawn a second
  // note named after whatever was typed.
  const trimmed = (title || '').trim();
  if (!trimmed || trimmed === titleBaseline) return;
  titleBaseline = trimmed;

  const newBase = sanitizeFilename(trimmed);
  if (!newBase) return;
  const suffix  = extractNoteSuffix(activeNote);
  const newName = suffix ? `${newBase}_${suffix}` : newBase;
  if (newName === activeNote) return;

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
    if (item) {
      item.dataset.name = result.name;
      // Renaming away from the placeholder makes it an ordinary note, so drop
      // the marker styling and re-evaluate the buttons keyed off that state.
      item.classList.toggle('note-item--placeholder', isPlaceholderName(result.name));
    }
    updateTrashButton();
    if (result.graduated) {
      // Naming the scratch note turned it into a real one and the backend has
      // re-armed a fresh blank placeholder — show it, and update the counts.
      await renderTags(activeTag === TRASH_TAG);
      await renderNotes();
    }
  } catch (_) {}
}

// ── Live editor ────────────────────────────────────────────────────────────

noteEdit.addEventListener('input', () => {
  noteBodyContent = noteEdit.value;
  const activeItem = document.querySelector(`.note-item[data-name="${CSS.escape(activeNote)}"]`);
  if (activeItem) {
    const title   = extractTitle(noteBodyContent);
    const textEl  = activeItem.querySelector('.note-item-text');
    if (textEl) textEl.textContent = title || fallbackNoteLabel(activeNote);
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveAndRename(extractTitle(noteBodyContent)), 300);
});

// ── Attachments ────────────────────────────────────────────────────────────

function clipboardImageFilename(mimeType) {
  const ext = mimeType === 'image/jpeg' ? '.jpg' : '.png';
  const d = new Date(), p = n => String(n).padStart(2, '0');
  const ms = String(d.getMilliseconds()).padStart(3, '0');
  return `myNoteImage_${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}_${ms}${ext}`;
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
    await handleAttachment(imgItem.getAsFile(), clipboardImageFilename(imgItem.type));
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

/**
 * Link routing. The WebView treats an ordinary <a> as in-place navigation, which
 * would replace the whole app UI with the target page and leave no way back — so
 * every link is intercepted here: attachments open in their native application,
 * anything external goes to the system default browser.
 *
 * Bound on document rather than the note view so links rendered anywhere (setup
 * wizards, notifications) are covered too; the app shell itself has no anchors.
 */
async function _handleLinkActivation(e) {
  const a = e.target.closest?.('a');
  if (!a) return;
  const href = a.getAttribute('href') || '';
  if (!href || href.startsWith('#')) return;   // in-page anchor — leave alone

  if (href.startsWith('/attachments/')) {
    e.preventDefault();
    try {
      const r = await fetch('/api/attachment/open', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: decodeURIComponent(href.slice('/attachments/'.length)) }),
      });
      if (!r.ok) showNotif('File is not here yet', 'warn');
    } catch (_) {}
    return;
  }

  // Everything below navigates away from the app, so nothing is left to the
  // WebView's default handling — note content syncs in from other devices, and
  // a javascript: href left to default would run script in the app's own origin.
  e.preventDefault();

  let url = href;
  if (/^www\./i.test(url)) url = 'https://' + url;   // bare domain, as the WebView would
  let resolved;
  try { resolved = new URL(url, window.location.href); } catch (_) { return; }

  if (!['http:', 'https:', 'mailto:', 'tel:', 'ftp:'].includes(resolved.protocol)) {
    showNotif(`Blocked link with unsupported scheme: ${resolved.protocol}`, 'warn');
    return;
  }
  openExternalUrl(resolved.href);
}

document.addEventListener('click', _handleLinkActivation);
// Middle-click / "open in new tab" would otherwise spawn a second app window
document.addEventListener('auxclick', e => { if (e.button === 1) _handleLinkActivation(e); });

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

async function renderTags(forTrash = false) {
  const data = await api(forTrash ? '/api/tags?trash=1&counts=1' : '/api/tags?counts=1');
  // data = { tags: [{name, count}], all_count, trash_count }
  if (!forTrash) allTagsCache = data.tags.map(t => t.name);

  const list = document.getElementById('tag-list');
  list.innerHTML = '';

  [
    { label: 'All',   value: '',        system: true,  count: data.all_count   },
    ...data.tags.map(t => ({ label: t.name, value: t.name, system: false, count: t.count })),
    { label: 'Trash', value: TRASH_TAG, system: true,  count: data.trash_count },
  ].forEach(({ label, value, system, count }) => {
    const li = document.createElement('li');
    li.className = 'tag-item'
      + (system          ? ' tag-system' : '')
      + (activeTag === value ? ' active' : '');
    li.dataset.tag = value;

    const labelSpan = document.createElement('span');
    labelSpan.className = 'tag-item-label';
    labelSpan.textContent = label;
    li.appendChild(labelSpan);

    const badge = document.createElement('span');
    badge.className = 'tag-item-count';
    badge.textContent = count;
    li.appendChild(badge);

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
  // While in Trash view, clicking a user tag filters within Trash instead of
  // navigating away to the main notes list.
  if (activeTag === TRASH_TAG && tag !== TRASH_TAG && tag !== '') {
    trashTagFilter = tag;
    document.querySelectorAll('.tag-item').forEach(el =>
      el.classList.toggle('active', el.dataset.tag === tag));
    await renderNotes();
    return;
  }

  // Normal navigation — always reset the trash sub-filter, locked group, and alpha groups.
  trashTagFilter = '';
  lockedGroupExpanded = false;
  clearTimeout(lockedCollapseTimer);
  lockedCollapseTimer = null;
  alphaGroupExpanded.clear();
  clearTimeout(saveTimer);
  if (activeNote && noteEncrypted && notePasswords.has(activeNote)) {
    notePasswords.delete(activeNote);
    cancelAutoLockTimer();
  }
  if (activeNote && colContent.classList.contains('editing')) await saveCurrentNote(true);
  setActiveButton(null);
  activeTag = tag; activeNote = ''; searchQuery = '';
  document.getElementById('search-input').value = '';
  clearContentPane();
  await renderTags(tag === TRASH_TAG);
  updateToolbarState();
  await renderNotes();
}

// ── Locked group helpers ────────────────────────────────────────────────────

function _resetLockedCollapseTimer() {
  clearTimeout(lockedCollapseTimer);
  lockedCollapseTimer = setTimeout(() => {
    lockedGroupExpanded = false;
    renderNotes();
  }, 5 * 60 * 1000);
}

function _collapseLockedGroup() {
  clearTimeout(lockedCollapseTimer);
  lockedCollapseTimer = null;
  lockedGroupExpanded = false;
  renderNotes();
}

// Returns the first Unicode letter of a title, uppercased, skipping emojis/symbols.
function _firstLetter(title) {
  const m = (title || '').match(/\p{L}/u);
  return m ? m[0].toUpperCase() : '#';
}

// ── Notes (col 2) ──────────────────────────────────────────────────────────

function formatNoteDate(ts) {
  if (!ts) return '';
  const d       = new Date(ts * 1000);
  const now     = new Date();
  const diffMs  = now - d;
  const diffDay = Math.floor(diffMs / 86400000);
  if (diffDay === 0) return 'Today ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (diffDay === 1) return 'Yesterday';
  if (diffDay < 7)  return d.toLocaleDateString([], { weekday: 'long' });
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

async function renderNotes() {
  const sortParams = `sort=${sortBy}&order=${sortDesc ? 'desc' : 'asc'}`;
  let url;
  if (searchQuery) {
    // Search results are not re-sorted server-side (full-text scores govern order)
    const unlockedParam = notePasswords.size
      ? '&unlocked=' + encodeURIComponent([...notePasswords.keys()].join(','))
      : '';
    url = `/api/search?q=${encodeURIComponent(searchQuery)}` + (activeTag ? `&tag=${encodeURIComponent(activeTag)}` : '') + unlockedParam;
  } else if (activeTag === TRASH_TAG && trashTagFilter) {
    url = `/api/notes?tag=${encodeURIComponent(TRASH_TAG)}&trash_filter=${encodeURIComponent(trashTagFilter)}&${sortParams}`;
  } else if (activeTag) {
    url = `/api/notes?tag=${encodeURIComponent(activeTag)}&${sortParams}`;
  } else {
    url = `/api/notes?${sortParams}`;
  }

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
  const unlockedNotes = items.filter(n => !n.encrypted);
  const lockedNotes   = items.filter(n =>  n.encrypted);

  // Auto-expand the locked group when the active note is inside it
  if (lockedNotes.length && activeNote && lockedNotes.some(n => n.name === activeNote)) {
    lockedGroupExpanded = true;
  }

  // Build a note list item and attach the standard click handler.
  // lockIconClick — optional extra handler bound to the lock icon (stop-propagated).
  function makeNoteItem(name, title, encrypted, mtime, placeholder, lockIconClick) {
    const li = document.createElement('li');
    li.className = 'note-item' + (activeNote === name ? ' active' : '')
                 + (placeholder ? ' note-item--placeholder' : '');
    li.dataset.name = name;

    const row = document.createElement('div');
    row.className = 'note-item-row';

    const lockImg = document.createElement('img');
    lockImg.className = 'note-lock-icon' + (encrypted ? '' : ' hidden');
    lockImg.src = '/static/images/lock.png';
    lockImg.alt = '';
    if (lockIconClick) {
      lockImg.addEventListener('click', e => { e.stopPropagation(); lockIconClick(); });
    }

    const textSpan = document.createElement('span');
    textSpan.className = 'note-item-text';
    textSpan.textContent = title;
    row.appendChild(lockImg);
    row.appendChild(textSpan);
    li.appendChild(row);

    if (mtime) {
      const metaSpan = document.createElement('span');
      metaSpan.className = 'note-item-meta';
      metaSpan.textContent = formatNoteDate(mtime);
      li.appendChild(metaSpan);
    }

    li.addEventListener('click', e => {
      // Always read data-name at click time — saveAndRename patches it after a
      // rename, so the closure value of `name` may be stale.
      const currentName = li.dataset.name;

      if (e.shiftKey && multiSelectAnchor) {
        // ── Shift-click: select every note between anchor and here ──
        e.preventDefault();
        const order = Array.from(
          document.querySelectorAll('#note-list .note-item:not(.empty)')
        ).map(el => el.dataset.name).filter(Boolean);

        const ai = order.indexOf(multiSelectAnchor);
        const bi = order.indexOf(currentName);
        if (ai !== -1 && bi !== -1) {
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
        if (selectedNotes.size === 0 && activeNote) selectedNotes.add(activeNote);
        if (selectedNotes.has(currentName)) selectedNotes.delete(currentName);
        else                                selectedNotes.add(currentName);

        multiSelectAnchor = currentName;

        if (selectedNotes.size >= 2) {
          activeNote = '';
          enterMultiSelectMode();
        } else if (selectedNotes.size === 1) {
          const only = [...selectedNotes][0];
          clearMultiSelect();
          setActiveButton(null);
          openNote(only);
        } else {
          clearMultiSelect();
        }

      } else {
        // ── Normal click ──
        multiSelectAnchor = currentName;
        clearMultiSelect();
        setActiveButton(null);
        if (encrypted) _resetLockedCollapseTimer();
        // The placeholder is a scratchpad — skip the rendered view and drop
        // straight into the editor, the way the phone opens it.
        openNote(currentName, isPlaceholderName(currentName));
      }
    });

    return li;
  }

  // Render unlocked notes (with collapsible alpha group headers in alphabetic sort)
  if (sortBy === 'alpha' && !searchQuery) {
    const letterCount = {};
    unlockedNotes.forEach(({ title }) => {
      const l = _firstLetter(title);
      letterCount[l] = (letterCount[l] || 0) + 1;
    });

    // Auto-expand the group containing the active note
    if (activeNote) {
      const activeItem = unlockedNotes.find(n => n.name === activeNote);
      if (activeItem) {
        const al = _firstLetter(activeItem.title);
        if (letterCount[al] >= 6) alphaGroupExpanded.add(al);
      }
    }

    // Pass 1: bucket notes into qualifying groups or ungrouped.
    // groupOrder preserves the order groups first appear in the sorted list.
    const groupOrder   = [];
    const groupNotes   = {}; // letter → [notes]
    const ungrouped    = [];

    unlockedNotes.forEach(note => {
      const letter = _firstLetter(note.title);
      if (letterCount[letter] >= 6) {
        if (!groupNotes[letter]) {
          groupNotes[letter] = [];
          groupOrder.push(letter);
        }
        groupNotes[letter].push(note);
      } else {
        ungrouped.push(note);
      }
    });

    // Pass 2: render each group header (+ notes if expanded), then ungrouped notes.
    let firstHeader = true;
    groupOrder.forEach(letter => {
      const expanded = alphaGroupExpanded.has(letter);

      const hdr = document.createElement('li');
      hdr.className = 'note-group-header alpha-group-header' + (firstHeader ? ' first' : '');
      firstHeader = false;

      const hText = document.createElement('span');
      hText.className = 'alpha-group-label';
      hText.textContent = letter;

      const hCount = document.createElement('span');
      hCount.className = 'alpha-group-count';
      hCount.textContent = groupNotes[letter].length;

      const hChevron = document.createElement('span');
      hChevron.className = 'alpha-group-chevron';
      hChevron.textContent = expanded ? '▾' : '▸';

      hdr.appendChild(hText);
      hdr.appendChild(hCount);
      hdr.appendChild(hChevron);

      hdr.addEventListener('click', () => {
        if (alphaGroupExpanded.has(letter)) alphaGroupExpanded.delete(letter);
        else                                alphaGroupExpanded.add(letter);
        renderNotes();
      });

      list.appendChild(hdr);
      if (expanded) {
        groupNotes[letter].forEach(({ name, title, encrypted, mtime, placeholder }) =>
          list.appendChild(makeNoteItem(name, title, encrypted, mtime, placeholder)));
      }
    });

    // Ungrouped notes appear after all groups
    ungrouped.forEach(({ name, title, encrypted, mtime, placeholder }) =>
      list.appendChild(makeNoteItem(name, title, encrypted, mtime, placeholder)));
  } else {
    unlockedNotes.forEach(({ name, title, encrypted, mtime, placeholder }) => {
      list.appendChild(makeNoteItem(name, title, encrypted, mtime, placeholder));
    });
  }

  // Render locked group
  if (lockedNotes.length) {
    const header = document.createElement('li');
    header.className = 'note-group-header locked-group-header';

    const hLock = document.createElement('img');
    hLock.src   = '/static/images/lock.png';
    hLock.alt   = '';
    hLock.className = 'note-lock-icon';

    const hText = document.createElement('span');
    hText.className = 'locked-group-label';
    hText.textContent = 'Locked';

    const hCount = document.createElement('span');
    hCount.className = 'locked-group-count';
    hCount.textContent = lockedNotes.length;

    const hChevron = document.createElement('span');
    hChevron.className = 'locked-group-chevron';
    hChevron.textContent = lockedGroupExpanded ? '▾' : '▸';

    header.appendChild(hLock);
    header.appendChild(hText);
    header.appendChild(hCount);
    header.appendChild(hChevron);

    header.addEventListener('click', () => {
      if (lockedGroupExpanded) {
        _collapseLockedGroup();
      } else {
        lockedGroupExpanded = true;
        _resetLockedCollapseTimer();
        renderNotes();
      }
    });

    list.appendChild(header);

    if (lockedGroupExpanded) {
      lockedNotes.forEach(({ name, title, encrypted, mtime, placeholder }) => {
        const el = makeNoteItem(name, title, encrypted, mtime, placeholder);
        el.dataset.lockedGroupItem = '1';
        list.appendChild(el);
      });
    }
  }
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
  if (activeNote && colContent.classList.contains('editing')) await saveCurrentNote(true);

  activeNote = name;
  document.querySelectorAll('.note-item').forEach(el =>
    el.classList.toggle('active', el.dataset.name === name));

  const data      = await api(`/api/note/${encodeURIComponent(name)}`);
  const content   = data.content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const fmMatch   = content.match(/^(---\s*\n[\s\S]*?\n---\s*\n?)/);
  noteFrontmatter = fmMatch ? fmMatch[1] : '';
  const rawBody   = fmMatch ? content.slice(fmMatch[1].length).trimStart() : content;

  noteCtime     = data.ctime ?? null;
  noteMtime     = data.mtime ?? null;
  noteEncrypted = /^encrypted:\s*true/m.test(noteFrontmatter);

  // Always save ciphertext so we can restore it after a session-lock
  if (noteEncrypted) noteRawCiphertext = rawBody;
  else noteRawCiphertext = '';

  updateTrashButton();
  updateLockButton();
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
  // Baseline for the rename check — a later edit only renames the file if it
  // moves the heading away from what the note was opened with.
  titleBaseline = extractTitle(noteBodyContent);
  // Content as loaded, so a sync finishing later can tell an editor that is
  // merely open from one the user has actually typed into.
  openedBody = noteBodyContent;

  if (startEditing && activeTag !== TRASH_TAG && !(noteEncrypted && !notePasswords.has(name))) {
    enterEditMode();
    // The placeholder opens ready to type: caret on the line under the heading,
    // which is left empty until the note is given a name.  The button state is
    // only touched here — the new-note flow owns 'new' and uses it as its own
    // re-entrancy guard, so it must not be overwritten.
    if (isPlaceholderName(name)) {
      setActiveButton('edit');
      _placeCaretBelowHeading();
    }
  } else {
    enterViewMode();
  }
  if (noteEncrypted && notePasswords.has(name)) startAutoLockTimer();
  updateExportButton();
  updateEditTagButtons();
}

// ── Toolbar state ───────────────────────────────────────────────────────────

function updateToolbarState() {
  const inTrash = activeTag === TRASH_TAG;
  document.getElementById('btn-trash').title =
    inTrash ? 'Restore from Trash' : 'Move to Trash';
  document.querySelector('.content-toolbar').classList.toggle('trash-mode', inTrash);
  document.getElementById('btn-new').disabled = inTrash;
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
    updateTrashButton();
    updateLockButton();
    updateEditTagButtons();

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
      updateTrashButton();
      updateLockButton();
      updateEditTagButtons();
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
        updateTrashButton();
        updateLockButton();
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
      updateTrashButton();
      updateLockButton();
      updateEditTagButtons();
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

/**
 * Generic dirty tracker for a Save button.
 *
 * Pass an array of HTMLInputElement / HTMLSelectElement.
 * Call .setBaseline() right after you load values from the server.
 * Call .markClean()   right after a successful save.
 *
 * The button stays disabled until the user changes something from the baseline.
 */
function makeDirtyTracker(saveBtn, fields) {
  let baseline   = null;
  let forceDirty = false;   // overrides diff logic — button stays active until markClean()

  function snapshot() {
    return fields.map(f => (f.type === 'checkbox' ? String(f.checked) : f.value));
  }

  function refresh() {
    if (forceDirty)          { saveBtn.disabled = false; return; }
    if (baseline === null)   { saveBtn.disabled = true;  return; }
    saveBtn.disabled = snapshot().every((v, i) => v === baseline[i]);
  }

  fields.forEach(f => {
    f.addEventListener('change', refresh);
    f.addEventListener('input',  refresh);
  });

  saveBtn.disabled = true;

  return {
    setBaseline() { forceDirty = false; baseline = snapshot(); saveBtn.disabled = true; },
    markClean()   { forceDirty = false; baseline = snapshot(); saveBtn.disabled = true; },
    // Force the button active regardless of baseline (e.g. first-time setup with unsaved data).
    // Any subsequent field change still triggers refresh(); button stays active until markClean().
    markDirty()   { forceDirty = true;  saveBtn.disabled = false; },
  };
}

/**
 * Open a URL in the system default browser. Uses the pywebview bridge when running inside
 * the desktop app, and falls back to window.open() when viewed in a plain browser tab so
 * download links always work in either context.
 */
function openExternalUrl(url) {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.open_url) {
    window.pywebview.api.open_url(url);
  } else {
    window.open(url, '_blank');
  }
}

/**
 * Builds a drag-slider interval control inside the given .interval-bar div.
 * Reads stops/labels from data-stops and data-labels attributes.
 * Exposes bar.value getter/setter and dispatches 'change'/'input' on snap.
 * Value of "0" means "no autosync".
 */
function makeIntervalBar(id) {
  const bar = document.getElementById(id);
  if (!bar) return null;

  const stops  = (bar.dataset.stops  || '0,30,60,300,900,1800,3600').split(',');
  const labels = (bar.dataset.labels || stops.join(',')).split(',');
  const initialVal = bar.dataset.value || stops[0];
  let currentIdx = Math.max(stops.indexOf(String(initialVal)), 0);

  // Build inner DOM
  const title = bar.dataset.title || 'Auto-sync interval:';
  bar.innerHTML = `
    <div class="interval-bar__label">${title} <strong class="interval-bar__val-text"></strong></div>
    <div class="interval-bar__track-wrap">
      <div class="interval-bar__track"></div>
      <div class="interval-bar__fill"></div>
      <div class="interval-bar__thumb"></div>
    </div>
    <div class="interval-bar__ticks"></div>`;

  const valText  = bar.querySelector('.interval-bar__val-text');
  const fill     = bar.querySelector('.interval-bar__fill');
  const thumb    = bar.querySelector('.interval-bar__thumb');
  const tickWrap = bar.querySelector('.interval-bar__ticks');

  // Build tick marks + labels
  stops.forEach((_, i) => {
    const tick = document.createElement('div');
    tick.className = 'interval-bar__tick';
    tick.innerHTML = `<div class="interval-bar__tick-mark"></div>
                      <div class="interval-bar__tick-label">${labels[i] || stops[i]}</div>`;
    tick.querySelector('.interval-bar__tick-label').addEventListener('click', () => snap(i, true));
    tickWrap.appendChild(tick);
  });

  const tickLabels = Array.from(tickWrap.querySelectorAll('.interval-bar__tick-label'));

  function pct(idx) { return stops.length > 1 ? (idx / (stops.length - 1)) * 100 : 0; }

  function paint() {
    const p = pct(currentIdx);
    thumb.style.left = p + '%';
    fill.style.width = p + '%';
    valText.textContent = labels[currentIdx] || stops[currentIdx];
    tickLabels.forEach((lbl, i) => lbl.classList.toggle('active', i === currentIdx));
  }

  function snap(idx, emit) {
    const prev = currentIdx;
    currentIdx = Math.max(0, Math.min(stops.length - 1, idx));
    paint();
    if (emit && currentIdx !== prev) {
      bar.dispatchEvent(new Event('change', { bubbles: true }));
      bar.dispatchEvent(new Event('input',  { bubbles: true }));
    }
  }

  // Pointer drag
  function idxFromPointer(clientX) {
    const rect = bar.querySelector('.interval-bar__track').getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return Math.round(ratio * (stops.length - 1));
  }

  thumb.addEventListener('pointerdown', e => {
    e.preventDefault();
    thumb.setPointerCapture(e.pointerId);
    const move = ev => snap(idxFromPointer(ev.clientX), false);
    const up   = ev => {
      snap(idxFromPointer(ev.clientX), true);
      thumb.removeEventListener('pointermove', move);
      thumb.removeEventListener('pointerup',   up);
    };
    thumb.addEventListener('pointermove', move);
    thumb.addEventListener('pointerup',   up);
  });

  // Click anywhere on track also snaps
  bar.querySelector('.interval-bar__track-wrap').addEventListener('click', e => {
    if (e.target === thumb) return;
    snap(idxFromPointer(e.clientX), true);
  });

  Object.defineProperty(bar, 'value', {
    configurable: true,
    get() { return stops[currentIdx]; },
    set(v) {
      const i = stops.indexOf(String(v));
      if (i >= 0) { currentIdx = i; paint(); }
    },
  });

  paint();
  return bar;
}

/** Switch the visible settings panel + highlight the matching nav item. */
function activateSettingsPanel(panelName) {
  document.querySelectorAll('.settings-nav-item').forEach(i =>
    i.classList.toggle('active', i.dataset.panel === panelName));
  document.querySelectorAll('.settings-panel').forEach(p =>
    p.classList.toggle('active', p.id === 'settings-panel-' + panelName));
  // Lock/unlock right-panel scroll (storage panel must not scroll)
  document.querySelector('.settings-right-panel').style.overflowY =
    (panelName === 'storage' || panelName === 'locked') ? 'hidden' : '';

  // Show the refresh button only on the storage panel
  document.getElementById('storage-refresh-btn').style.display =
    panelName === 'storage' ? '' : 'none';

  if (panelName === 'telegram') loadTelegramSettings();
  if (panelName === 'icloud')   loadIcloudSettings();
  if (panelName === 'android')  loadAndroidSettings();
  if (panelName === 'github')   loadGithubSettings();
  if (panelName === 'storage')  loadStorageSettings();
  if (panelName === 'trash')    loadTrashSettings();
}

const _emailDirty = makeDirtyTracker(
  document.getElementById('settings-save-btn'),
  [document.getElementById('settings-email-addr'),
   document.getElementById('settings-email-pass')]
);

function openSettings(panel) {
  // Pre-fill email and set baseline so Save is disabled until something changes
  api('/api/settings').then(s => {
    document.getElementById('settings-email-addr').value = s.email || '';
    document.getElementById('settings-email-pass').value = s.emailPasswordSet ? '••••••••' : '';
    _emailDirty.setBaseline();
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
    _emailDirty.markClean();
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

makeIntervalBar('settings-tg-interval');

const _tgDirty = makeDirtyTracker(
  document.getElementById('settings-tg-save-btn'),
  [document.getElementById('settings-tg-chat'),
   document.getElementById('settings-tg-interval')]
);

async function loadTelegramSettings() {
  try {
    const s = await api('/api/telegram');
    if (s.authorized) {
      // Pre-fill the chat input (lives in step 3) and interval selector
      document.getElementById('settings-tg-chat').value      = s.chatId || '';
      const tgBar = document.getElementById('settings-tg-interval');
      if (tgBar) tgBar.value = s.enabled ? String(s.pollInterval || 30) : '0';
      // Show connected account name
      const label = s.username ? `@${s.username}` : (s.name || 'Connected');
      document.getElementById('tg-connected-name').textContent = label;
      tgSetStatus('settings-tg-status-conn', '');
      if (s.chatId) {
        // Fully configured — jump straight to step 4
        document.getElementById('tg-connected-channel-id').textContent = s.chatId;
        tgSetStep('connected');
        _tgDirty.setBaseline();     // ← baseline set; Save stays grey until something changes
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
  // First-time channel entry: no saved baseline yet — force Save active so the
  // user can persist the channel ID.  markClean() after a successful save resets this.
  _tgDirty.markDirty();
});

// Step 4 — change channel (go back to step 3)
document.getElementById('settings-tg-change-channel-btn').addEventListener('click', () => {
  tgSetStatus('settings-tg-status-channel', '');
  tgSetStep('channel');
});

// Step 4 — save polling config
document.getElementById('settings-tg-save-btn').addEventListener('click', async () => {
  const chatId         = document.getElementById('settings-tg-chat').value.trim();
  const tgIntervalVal  = parseInt(document.getElementById('settings-tg-interval')?.value || '0', 10);
  const enabled        = tgIntervalVal !== 0;
  const pollInterval    = tgIntervalVal || 30;
  try {
    await api('/api/telegram', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chatId, enabled, pollInterval }),
    });
    // Update channel display in case it changed
    document.getElementById('tg-connected-channel-id').textContent = chatId || '—';
    _tgDirty.markClean();
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

// ── iCloud Drive sync wizard ─────────────────────────────────────────────────
(function () {
  const IC_STEPS = ['locate', 'folder', 'connected'];

  function icSetStep(step) {
    IC_STEPS.forEach(s => {
      document.getElementById(`icloud-step-${s}`).classList.toggle('hidden', s !== step);
    });
    const idx = IC_STEPS.indexOf(step);
    IC_STEPS.forEach((s, i) => {
      const dot  = document.querySelector(`.tg-wizard-dot[data-ic-step="${s}"]`);
      const line = document.querySelector(`.tg-wizard-line[data-after="${s}"]`);
      if (dot) {
        dot.classList.toggle('active', i === idx);
        dot.classList.toggle('done',   i < idx);
      }
      if (line) line.classList.toggle('done', i < idx);
    });
  }

  function icStatus(id, msg, type = '') {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg;
    el.className   = 'tg-status' + (type ? ' ' + type : '');
  }

  // Held between steps
  let _icDrivePath = '';

  makeIntervalBar('icloud-interval-sel');

  const _icDirty = makeDirtyTracker(
    document.getElementById('icloud-save-btn'),
    [document.getElementById('icloud-interval-sel')]
  );

  async function loadIcloudSettings() {
    try {
      const s = await api('/api/icloud');
      if (s.connected) {
        _icDrivePath = s.drivePath;
        document.getElementById('icloud-conn-drive').textContent    = s.drivePath;
        document.getElementById('icloud-conn-folder').textContent   = s.folder;
        document.getElementById('icloud-conn-lastsync').textContent = s.lastSyncStr;
        document.getElementById('icloud-interval-sel').value =
          s.enabled ? String(s.syncInterval) : '0';
        icSetStep('connected');
        _icDirty.setBaseline();
      } else {
        // Pre-fill path if we have one saved (but not yet connected)
        if (s.drivePath) document.getElementById('icloud-drive-path').value = s.drivePath;
        icSetStep('locate');
      }
    } catch (_) { icSetStep('locate'); }
  }

  // Expose so activateSettingsPanel can call it
  window.loadIcloudSettings = loadIcloudSettings;

  // ── Step 1: Auto-detect ──────────────────────────────────────────────────
  document.getElementById('icloud-detect-btn').addEventListener('click', async () => {
    icStatus('icloud-status-locate', 'Searching…');
    try {
      const r = await api('/api/icloud/detect');
      if (r.found) {
        document.getElementById('icloud-drive-path').value = r.path;
        icStatus('icloud-status-locate', `Found: ${r.path}`, 'ok');
      } else {
        icStatus('icloud-status-locate',
          'Not found. Install iCloud for Windows and sign in, then try again.', 'err');
      }
    } catch (e) {
      icStatus('icloud-status-locate', 'Detection failed: ' + e.message, 'err');
    }
  });

  // ── Step 1: Browse ───────────────────────────────────────────────────────
  document.getElementById('icloud-browse-btn').addEventListener('click', async () => {
    try {
      const picked = await window.pywebview?.api?.pick_folder?.();
      if (picked) document.getElementById('icloud-drive-path').value = picked;
    } catch (_) {
      icStatus('icloud-status-locate', 'Browse requires the desktop app.', 'err');
    }
  });

  // ── Step 1: Open Apple download page ────────────────────────────────────
  document.getElementById('icloud-open-download').addEventListener('click', () => {
    openExternalUrl('https://updates.cdn-apple.com/2020/windows/001-39935-20200911-1A70AA56-F448-11EA-8CC0-99D41950005E/iCloudSetup.exe');
  });

  // ── Step 1: Next ─────────────────────────────────────────────────────────
  document.getElementById('icloud-locate-next-btn').addEventListener('click', async () => {
    const path = document.getElementById('icloud-drive-path').value.trim();
    if (!path) { icStatus('icloud-status-locate', 'Please enter or detect your iCloud Drive path.', 'err'); return; }
    _icDrivePath = path;
    const folderName = document.getElementById('icloud-folder-name').value.trim() || 'myNotes';

    // Recursively search the drive for an existing myNotes folder (incl. nested ones).
    // If found, connect to it directly; only fall through to "create folder" if nothing exists.
    icStatus('icloud-status-locate', 'Searching for an existing myNotes folder…');
    try {
      const check = await api(`/api/icloud/check-folder?drivePath=${encodeURIComponent(path)}&folder=${encodeURIComponent(folderName)}`);
      if (check.exists) {
        icStatus('icloud-status-locate', `Found existing folder, connecting…`);
        const r = await api('/api/icloud/setup', {
          method: 'POST',
          body: JSON.stringify({ drivePath: check.drivePath, folder: check.folder, syncInterval: 300 }),
        });
        if (r.ok) {
          await loadIcloudSettings();
          api('/api/icloud/sync', { method: 'POST' }).catch(() => {});
          icStatus('icloud-status-locate', '');
          return;
        }
        icStatus('icloud-status-locate', r.error || 'Setup failed.', 'err');
        return;
      }
    } catch (e) {
      // If the check fails for any reason, fall through to the manual create-folder step.
    }

    // Update Step 2 preview labels
    document.getElementById('icloud-tree-root-label').textContent =
      `📁 ${path.split(/[\\/]/).pop() || path} /`;
    document.getElementById('icloud-folder-preview').textContent = folderName;
    icStatus('icloud-status-locate', '');
    icSetStep('folder');
  });

  // Update tree preview when folder name changes
  document.getElementById('icloud-folder-name').addEventListener('input', function () {
    document.getElementById('icloud-folder-preview').textContent = this.value.trim() || 'myNotes';
  });

  // ── Step 2: Back ─────────────────────────────────────────────────────────
  document.getElementById('icloud-folder-back-btn').addEventListener('click', () => {
    icStatus('icloud-status-folder', '');
    icSetStep('locate');
  });

  // ── Step 2: Create & Connect ─────────────────────────────────────────────
  document.getElementById('icloud-folder-create-btn').addEventListener('click', async () => {
    const folder = document.getElementById('icloud-folder-name').value.trim() || 'myNotes';
    const syncInterval = 300;
    icStatus('icloud-status-folder', 'Creating folders…');
    try {
      const r = await api('/api/icloud/setup', {
        method: 'POST',
        body: JSON.stringify({ drivePath: _icDrivePath, folder, syncInterval }),
      });
      if (r.ok) {
        // Refresh and show connected step
        await loadIcloudSettings();
        // Trigger first sync in background
        api('/api/icloud/sync', { method: 'POST' }).catch(() => {});
      } else {
        icStatus('icloud-status-folder', r.error || 'Setup failed.', 'err');
      }
    } catch (e) {
      icStatus('icloud-status-folder', 'Error: ' + e.message, 'err');
    }
  });

  // ── Step 3: Sync Now ─────────────────────────────────────────────────────
  document.getElementById('icloud-sync-now-btn').addEventListener('click', async () => {
    icStatus('icloud-status-conn', 'Syncing…');
    try {
      const r = await api('/api/icloud/sync', { method: 'POST' });
      if (r.ok) {
        const msg = `Done — ↑${r.toRemote} to iCloud, ↓${r.toLocal} to PC, ${r.skipped} unchanged`;
        icStatus('icloud-status-conn', msg, 'ok');
        document.getElementById('icloud-conn-lastsync').textContent =
          new Date().toLocaleString();
        setTimeout(() => icStatus('icloud-status-conn', ''), 4000);
      } else {
        icStatus('icloud-status-conn', r.error || 'Sync failed.', 'err');
      }
    } catch (e) {
      icStatus('icloud-status-conn', 'Error: ' + e.message, 'err');
    }
  });

  // ── Step 3: Save ─────────────────────────────────────────────────────────
  document.getElementById('icloud-save-btn').addEventListener('click', async () => {
    const icIntervalVal = parseInt(document.getElementById('icloud-interval-sel').value, 10);
    const enabled  = icIntervalVal !== 0;
    const interval = icIntervalVal || 300;
    try {
      const r = await api('/api/icloud', {
        method: 'POST',
        body: JSON.stringify({ enabled, syncInterval: interval }),
      });
      if (r.ok) {
        _icDirty.markClean();
        icStatus('icloud-status-conn', 'Saved.', 'ok');
        setTimeout(() => icStatus('icloud-status-conn', ''), 2000);
      } else {
        icStatus('icloud-status-conn', 'Save failed.', 'err');
      }
    } catch (e) {
      icStatus('icloud-status-conn', 'Error: ' + e.message, 'err');
    }
  });

  // ── Step 3: Disconnect ───────────────────────────────────────────────────
  document.getElementById('icloud-disconnect-btn').addEventListener('click', async () => {
    if (!confirm('Disconnect iCloud Drive sync? Your existing notes are not deleted.')) return;
    try {
      await fetch('/api/icloud', { method: 'DELETE' });
      _icDrivePath = '';
      document.getElementById('icloud-drive-path').value = '';
      icStatus('icloud-status-locate', '');
      icSetStep('locate');
    } catch (_) {}
  });

  // ── SSE: reflect live sync result in the UI ──────────────────────────────
  document.addEventListener('sse-icloud_sync', e => {
    const r = e.detail?.result;
    if (!r) return;
    document.getElementById('icloud-conn-lastsync').textContent =
      new Date().toLocaleString();
    const msg = `Auto-sync — ↑${r.toRemote} to iCloud, ↓${r.toLocal} to PC`;
    icStatus('icloud-status-conn', msg, 'ok');
    setTimeout(() => icStatus('icloud-status-conn', ''), 4000);
    // Refresh sidebar to pick up notes added, modified, or removed by sync
    if (_canRefreshAfterSync()) {
      const wasEditing = colContent.classList.contains('editing');
      renderTags(activeTag === TRASH_TAG).then(() => renderNotes()).then(async () => {
        // If sync downloaded anything, reload the open note in case it was updated
        if ((r.toLocal || 0) > 0 && activeNote) {
          if (wasEditing) await _reloadEditorBuffer();
          else            await openNote(activeNote);
        }
      });
    }
  });
})();

// ── Android / Google Drive sync wizard ───────────────────────────────────────
(function () {
  const GD_STEPS = ['locate', 'folder', 'connected'];

  function gdSetStep(step) {
    GD_STEPS.forEach(s => {
      document.getElementById(`android-step-${s}`).classList.toggle('hidden', s !== step);
    });
    const idx = GD_STEPS.indexOf(step);
    GD_STEPS.forEach((s, i) => {
      const dot  = document.querySelector(`.tg-wizard-dot[data-gd-step="${s}"]`);
      const line = document.querySelector(`.tg-wizard-line[data-gd-after="${s}"]`);
      if (dot) {
        dot.classList.toggle('active', i === idx);
        dot.classList.toggle('done',   i < idx);
      }
      if (line) line.classList.toggle('done', i < idx);
    });
  }

  function gdStatus(id, msg, type = '') {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg;
    el.className   = 'tg-status' + (type ? ' ' + type : '');
  }

  let _gdDrivePath = '';

  makeIntervalBar('android-interval-sel');

  const _gdDirty = makeDirtyTracker(
    document.getElementById('android-save-btn'),
    [document.getElementById('android-interval-sel')]
  );

  async function loadAndroidSettings() {
    try {
      const s = await api('/api/gdrive');
      if (s.connected) {
        _gdDrivePath = s.drivePath;
        document.getElementById('android-conn-drive').textContent    = s.drivePath;
        document.getElementById('android-conn-folder').textContent   = s.folder;
        document.getElementById('android-conn-lastsync').textContent = s.lastSyncStr;
        document.getElementById('android-interval-sel').value =
          s.enabled ? String(s.syncInterval) : '0';
        gdSetStep('connected');
        _gdDirty.setBaseline();
      } else {
        if (s.drivePath) document.getElementById('android-drive-path').value = s.drivePath;
        gdSetStep('locate');
      }
    } catch (_) { gdSetStep('locate'); }
  }

  window.loadAndroidSettings = loadAndroidSettings;

  // ── Step 1: Auto-detect ──────────────────────────────────────────────────
  document.getElementById('android-detect-btn').addEventListener('click', async () => {
    gdStatus('android-status-locate', 'Searching…');
    try {
      const r = await api('/api/gdrive/detect');
      if (r.found) {
        document.getElementById('android-drive-path').value = r.path;
        gdStatus('android-status-locate', `Found: ${r.path}`, 'ok');
      } else {
        gdStatus('android-status-locate',
          'Not found. Install Google Drive for Desktop, sign in, then try again.', 'err');
      }
    } catch (e) {
      gdStatus('android-status-locate', 'Detection failed: ' + e.message, 'err');
    }
  });

  // ── Step 1: Browse ───────────────────────────────────────────────────────
  document.getElementById('android-browse-btn').addEventListener('click', async () => {
    try {
      const picked = await window.pywebview?.api?.pick_folder?.();
      if (picked) document.getElementById('android-drive-path').value = picked;
    } catch (_) {
      gdStatus('android-status-locate', 'Browse requires the desktop app.', 'err');
    }
  });

  // ── Step 1: Open Google Drive download page ──────────────────────────────
  document.getElementById('android-open-download').addEventListener('click', () => {
    openExternalUrl('https://dl.google.com/drive-file-stream/GoogleDriveSetup.exe');
  });

  // ── Step 1: Next ─────────────────────────────────────────────────────────
  document.getElementById('android-locate-next-btn').addEventListener('click', async () => {
    const path = document.getElementById('android-drive-path').value.trim();
    if (!path) { gdStatus('android-status-locate', 'Please enter or detect your Google Drive path.', 'err'); return; }
    _gdDrivePath = path;
    const folderName = document.getElementById('android-folder-name').value.trim() || 'myNotes';

    // Recursively search the drive for an existing myNotes folder (incl. nested ones).
    // If found, connect to it directly; only fall through to "create folder" if nothing exists.
    gdStatus('android-status-locate', 'Searching for an existing myNotes folder…');
    try {
      const check = await api(`/api/gdrive/check-folder?drivePath=${encodeURIComponent(path)}&folder=${encodeURIComponent(folderName)}`);
      if (check.exists) {
        gdStatus('android-status-locate', `Found existing folder, connecting…`);
        const r = await api('/api/gdrive/setup', {
          method: 'POST',
          body: JSON.stringify({ drivePath: check.drivePath, folder: check.folder, syncInterval: 300 }),
        });
        if (r.ok) {
          await loadAndroidSettings();
          api('/api/gdrive/sync', { method: 'POST' }).catch(() => {});
          gdStatus('android-status-locate', '');
          return;
        }
        gdStatus('android-status-locate', r.error || 'Setup failed.', 'err');
        return;
      }
    } catch (e) {
      // If the check fails for any reason, fall through to the manual create-folder step.
    }

    document.getElementById('android-tree-root-label').textContent =
      `📁 ${path.split(/[\\/]/).pop() || path} /`;
    document.getElementById('android-folder-preview').textContent = folderName;
    gdStatus('android-status-locate', '');
    gdSetStep('folder');
  });

  // Update tree preview when folder name changes
  document.getElementById('android-folder-name').addEventListener('input', function () {
    document.getElementById('android-folder-preview').textContent = this.value.trim() || 'myNotes';
  });

  // ── Step 2: Back ─────────────────────────────────────────────────────────
  document.getElementById('android-folder-back-btn').addEventListener('click', () => {
    gdStatus('android-status-folder', '');
    gdSetStep('locate');
  });

  // ── Step 2: Create & Connect ─────────────────────────────────────────────
  document.getElementById('android-folder-create-btn').addEventListener('click', async () => {
    const folder = document.getElementById('android-folder-name').value.trim() || 'myNotes';
    const syncInterval = 300;
    gdStatus('android-status-folder', 'Creating folders…');
    try {
      const r = await api('/api/gdrive/setup', {
        method: 'POST',
        body: JSON.stringify({ drivePath: _gdDrivePath, folder, syncInterval }),
      });
      if (r.ok) {
        await loadAndroidSettings();
        api('/api/gdrive/sync', { method: 'POST' }).catch(() => {});
      } else {
        gdStatus('android-status-folder', r.error || 'Setup failed.', 'err');
      }
    } catch (e) {
      gdStatus('android-status-folder', 'Error: ' + e.message, 'err');
    }
  });

  // ── Step 3: Sync Now ─────────────────────────────────────────────────────
  document.getElementById('android-sync-now-btn').addEventListener('click', async () => {
    gdStatus('android-status-conn', 'Syncing…');
    try {
      const r = await api('/api/gdrive/sync', { method: 'POST' });
      if (r.ok) {
        const msg = `Done — ↑${r.toRemote} to Drive, ↓${r.toLocal} to PC, ${r.skipped} unchanged`;
        gdStatus('android-status-conn', msg, 'ok');
        document.getElementById('android-conn-lastsync').textContent =
          new Date().toLocaleString();
        setTimeout(() => gdStatus('android-status-conn', ''), 4000);
      } else {
        gdStatus('android-status-conn', r.error || 'Sync failed.', 'err');
      }
    } catch (e) {
      gdStatus('android-status-conn', 'Error: ' + e.message, 'err');
    }
  });

  // ── Step 3: Save ─────────────────────────────────────────────────────────
  document.getElementById('android-save-btn').addEventListener('click', async () => {
    const gdIntervalVal = parseInt(document.getElementById('android-interval-sel').value, 10);
    const enabled  = gdIntervalVal !== 0;
    const interval = gdIntervalVal || 300;
    try {
      const r = await api('/api/gdrive', {
        method: 'POST',
        body: JSON.stringify({ enabled, syncInterval: interval }),
      });
      if (r.ok) {
        _gdDirty.markClean();
        gdStatus('android-status-conn', 'Saved.', 'ok');
        setTimeout(() => gdStatus('android-status-conn', ''), 2000);
      } else {
        gdStatus('android-status-conn', 'Save failed.', 'err');
      }
    } catch (e) {
      gdStatus('android-status-conn', 'Error: ' + e.message, 'err');
    }
  });

  // ── Step 3: Disconnect ───────────────────────────────────────────────────
  document.getElementById('android-disconnect-btn').addEventListener('click', async () => {
    if (!confirm('Disconnect Google Drive sync? Your existing notes are not deleted.')) return;
    try {
      await fetch('/api/gdrive', { method: 'DELETE' });
      _gdDrivePath = '';
      document.getElementById('android-drive-path').value = '';
      gdStatus('android-status-locate', '');
      gdSetStep('locate');
    } catch (_) {}
  });

  // ── SSE: reflect live sync result in the UI ──────────────────────────────
  document.addEventListener('sse-gdrive_sync', e => {
    const r = e.detail?.result;
    if (!r) return;
    document.getElementById('android-conn-lastsync').textContent =
      new Date().toLocaleString();
    const msg = `Auto-sync — ↑${r.toRemote} to Drive, ↓${r.toLocal} to PC`;
    gdStatus('android-status-conn', msg, 'ok');
    setTimeout(() => gdStatus('android-status-conn', ''), 4000);
    if (_canRefreshAfterSync()) {
      const wasEditing = colContent.classList.contains('editing');
      renderTags(activeTag === TRASH_TAG).then(() => renderNotes()).then(async () => {
        if ((r.toLocal || 0) > 0 && activeNote) {
          if (wasEditing) await _reloadEditorBuffer();
          else            await openNote(activeNote);
        }
      });
    }
  });
})();

// ── GitHub sync settings ────────────────────────────────────────────────────
(function () {
  function ghSetStep(step) {
    document.getElementById('github-step-setup').classList.toggle('hidden', step !== 'setup');
    document.getElementById('github-step-connected').classList.toggle('hidden', step !== 'connected');
  }

  function ghStatus(id, msg, level = '') {
    const el = document.getElementById(id);
    el.textContent = msg;
    el.className   = 'tg-status' + (level ? ' ' + level : '');
  }

  async function loadGithubSettings() {
    const s = await api('/api/github');
    if (s.connected) {
      document.getElementById('github-conn-repo').textContent      = s.repoUrl;
      document.getElementById('github-conn-lastsync').textContent  = s.lastSyncStr;
      document.getElementById('github-interval-sel').value =
        s.enabled ? String(s.syncInterval) : '0';
      _ghDirty.markClean();
      ghSetStep('connected');
    } else {
      ghSetStep('setup');
    }
  }
  window.loadGithubSettings = loadGithubSettings;

  makeIntervalBar('github-interval-sel');

  const _ghDirty = makeDirtyTracker(
    document.getElementById('github-save-btn'),
    [document.getElementById('github-interval-sel')]
  );

  // Connect
  document.getElementById('github-connect-btn').addEventListener('click', async () => {
    const repoUrl      = document.getElementById('github-repo-url').value.trim();
    const pat          = document.getElementById('github-pat').value.trim();
    const syncInterval = 300;
    if (!repoUrl || !pat) {
      ghStatus('github-status-setup', 'Enter the repository URL and PAT.', 'error');
      return;
    }
    ghStatus('github-status-setup', 'Connecting…');
    document.getElementById('github-connect-btn').disabled = true;
    try {
      const r = await api('/api/github/setup', {
        method: 'POST',
        body: JSON.stringify({ repoUrl, pat, syncInterval }),
      });
      if (r.ok) {
        document.getElementById('github-pat').value = '';
        await loadGithubSettings();
        api('/api/github/sync', { method: 'POST' }).catch(() => {});
      } else {
        ghStatus('github-status-setup', r.error || 'Connection failed.', 'error');
      }
    } catch (e) {
      ghStatus('github-status-setup', 'Connection failed.', 'error');
    } finally {
      document.getElementById('github-connect-btn').disabled = false;
    }
  });

  // Save
  document.getElementById('github-save-btn').addEventListener('click', async () => {
    const ghIntervalVal = parseInt(document.getElementById('github-interval-sel').value, 10);
    const enabled      = ghIntervalVal !== 0;
    const syncInterval = ghIntervalVal || 300;
    await api('/api/github', {
      method: 'POST',
      body: JSON.stringify({ enabled, syncInterval }),
    });
    _ghDirty.markClean();
    ghStatus('github-status-conn', 'Saved.', 'ok');
    setTimeout(() => ghStatus('github-status-conn', ''), 2500);
  });

  // Sync Now
  document.getElementById('github-sync-now-btn').addEventListener('click', async () => {
    ghStatus('github-status-conn', 'Syncing…');
    const btn = document.getElementById('github-sync-now-btn');
    btn.disabled = true;
    try {
      const r = await api('/api/github/sync', { method: 'POST' });
      if (!r.ok) {
        ghStatus('github-status-conn', `Sync error: ${r.error || 'unknown'}`, 'error');
        return;
      }
      document.getElementById('github-conn-lastsync').textContent = new Date().toLocaleString();
      const msg = `Synced — ↑${r.toRemote ?? 0} to GitHub, ↓${r.toLocal ?? 0} to PC`;
      ghStatus('github-status-conn', msg, 'ok');
      setTimeout(() => ghStatus('github-status-conn', ''), 4000);
      if (_canRefreshAfterSync() && (r.toLocal ?? 0) > 0) {
        const wasEditing = colContent.classList.contains('editing');
        await renderTags(activeTag === TRASH_TAG);
        await renderNotes();
        if (activeNote) {
          if (wasEditing) await _reloadEditorBuffer();
          else            await openNote(activeNote);
        }
      }
    } catch (e) {
      ghStatus('github-status-conn', 'Sync failed.', 'error');
    } finally {
      btn.disabled = false;
    }
  });

  // Disconnect
  document.getElementById('github-disconnect-btn').addEventListener('click', async () => {
    if (!confirm('Disconnect GitHub sync? Your notes will not be affected.')) return;
    await api('/api/github', { method: 'DELETE' });
    ghStatus('github-status-setup', '');
    document.getElementById('github-repo-url').value = '';
    document.getElementById('github-pat').value      = '';
    ghSetStep('setup');
  });

  // SSE: background sync result
  document.addEventListener('sse-github_sync', e => {
    const r = e.detail?.result;
    if (!r) return;
    document.getElementById('github-conn-lastsync').textContent =
      new Date().toLocaleString();
    const msg = `Auto-sync — ↑${r.toRemote} to GitHub, ↓${r.toLocal} to PC`;
    ghStatus('github-status-conn', msg, 'ok');
    setTimeout(() => ghStatus('github-status-conn', ''), 4000);
    if (_canRefreshAfterSync()) {
      const wasEditing = colContent.classList.contains('editing');
      renderTags(activeTag === TRASH_TAG).then(() => renderNotes()).then(async () => {
        if ((r.toLocal || 0) > 0 && activeNote) {
          if (wasEditing) await _reloadEditorBuffer();
          else            await openNote(activeNote);
        }
      });
    }
  });
})();

// ── myNote sync icon (Tags column header) ───────────────────────────────────

(function () {
  const btn  = document.getElementById('myNote-sync-btn');
  const icon = document.getElementById('myNote-sync-icon');
  if (!btn || !icon) return;

  const DEFAULT_TITLE = 'Sync now';
  const FADE_MS = 20000;
  let fadeTimer = null;
  let queuedSync = null;   // a run requested while one was already in flight

  // Background (cron-triggered) syncs that are currently in flight, keyed by
  // source name. The icon spins whenever this is non-empty OR a manual click
  // is in progress, and only stops once every source has reported "end".
  const autoActive = new Set();

  function updateSpin() {
    const spinning = autoActive.size > 0 || btn.classList.contains('syncing');
    icon.classList.toggle('sync-spin', spinning);
    if (spinning) btn.title = 'Syncing…';
  }

  function flash(ok, message) {
    clearTimeout(fadeTimer);
    btn.classList.remove('sync-flash-ok', 'sync-flash-err');
    btn.style.transition = 'none';
    btn.classList.add(ok ? 'sync-flash-ok' : 'sync-flash-err');
    btn.title = message;
    // Force the "no transition" state to apply before re-enabling it,
    // so the border starts solid and then eases back to grey.
    void btn.offsetWidth;
    requestAnimationFrame(() => {
      btn.style.transition = `border-color ${FADE_MS}ms ease`;
      btn.classList.remove('sync-flash-ok', 'sync-flash-err');
    });
    fadeTimer = setTimeout(() => {
      btn.style.transition = '';
      btn.title = DEFAULT_TITLE;
    }, FADE_MS);
  }

  /**
   * Sync, then bring the UI back in line with what's on disk.
   *
   * alwaysRefresh re-renders even when the sync pulled nothing — that's what F5
   * is for, where the point is an explicit "show me the current state", not just
   * reacting to incoming changes.
   *
   * silent is for syncs the user didn't ask for by hand (leaving the editor):
   * the icon still spins so the activity is visible, but a run that moved
   * nothing stays quiet instead of flashing "0 0" every time a note is closed.
   * Failures always flash.
   */
  async function runSync({ alwaysRefresh = false, silent = false } = {}) {
    // A request that arrives mid-sync is remembered rather than dropped: it is
    // usually a second note being closed while the first one's push is still in
    // flight, and its changes would otherwise wait for the next interval.
    if (btn.classList.contains('syncing')) {
      queuedSync = { alwaysRefresh: alwaysRefresh || (queuedSync?.alwaysRefresh ?? false),
                     silent: silent && (queuedSync?.silent ?? true) };
      return;
    }
    clearTimeout(fadeTimer);
    btn.classList.add('syncing');
    btn.classList.remove('sync-flash-ok', 'sync-flash-err');
    btn.style.transition = '';
    updateSpin();
    // The note open when the sync started; if the user has moved on by the time
    // it finishes, reopening it would yank them back.
    const startedOn = activeNote;
    try {
      const r = await api('/api/github/sync', { method: 'POST' });
      btn.classList.remove('syncing');
      updateSpin();
      if (r && r.ok) {
        const moved = (r.toRemote ?? 0) + (r.toLocal ?? 0);
        if (!silent || moved > 0) flash(true, `↑${r.toRemote ?? 0} ↓${r.toLocal ?? 0}`);
        // An open editor normally blocks the refresh so nothing is yanked out
        // from under the typist. An editor that is merely open and untouched —
        // the scratch note on startup, which opens straight into edit mode — has
        // nothing to lose, so it does get the freshly pulled text.
        if (_canRefreshAfterSync() && (alwaysRefresh || (r.toLocal ?? 0) > 0)) {
          const wasEditing = colContent.classList.contains('editing');
          await renderTags(activeTag === TRASH_TAG);
          await renderNotes();
          if (activeNote && activeNote === startedOn) {
            if (wasEditing) await _reloadEditorBuffer();
            else            await openNote(activeNote);
          }
        }
      } else {
        flash(false, (r && r.error) ? r.error : 'Sync failed');
      }
    } catch (e) {
      btn.classList.remove('syncing');
      updateSpin();
      flash(false, e.message || 'Sync failed');
      // The sync failed, but the on-disk state may still have moved on
      if (alwaysRefresh && !_isEditingActive()) {
        try {
          await renderTags(activeTag === TRASH_TAG);
          await renderNotes();
        } catch (_) {}
      }
    } finally {
      btn.classList.remove('syncing');
      updateSpin();
      if (queuedSync) {
        const next = queuedSync;
        queuedSync = null;
        runSync(next);
      }
    }
  }

  btn.addEventListener('click', () => runSync());
  window._syncNow = runSync;

  // Background cron-triggered syncs (iCloud, Google Drive, GitHub, Telegram)
  // also spin the icon, so the user knows a sync is happening even when they
  // didn't click it themselves.
  document.addEventListener('sse-auto_sync', e => {
    const ev = e.detail;
    if (!ev || !ev.source) return;
    if (ev.phase === 'start') {
      autoActive.add(ev.source);
      updateSpin();
    } else if (ev.phase === 'end') {
      autoActive.delete(ev.source);
      updateSpin();
      // Don't stomp on a flash from a manual click still fading out.
      if (!btn.classList.contains('syncing') && autoActive.size === 0) {
        if (ev.ok) {
          flash(true, `↑${ev.toRemote ?? 0} ↓${ev.toLocal ?? 0}`);
        } else {
          flash(false, ev.error || 'Sync failed');
        }
      }
    }
  });
})();

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

async function _finalizeNewNote(caller) {
  clearTimeout(saveTimer);
  try {
    if (activeNote) await saveCurrentNote(true);
  } catch (err) {
    console.error('[finalize] save failed, exiting edit mode anyway:', err);
  }
  enterViewMode();
  setActiveButton(null);
}

document.getElementById('btn-new').addEventListener('click', async () => {
  if (activeTag === TRASH_TAG) return;

  // Already composing a new note — finalize it, don't spawn another.
  if (activeButton === 'new' || _isCreatingNote) {
    await _finalizeNewNote('btn-new-click');
    return;
  }

  _isCreatingNote = true;
  // Set activeButton synchronously before any await so re-entrant clicks
  // hit the guard above instead of starting a second creation.
  setActiveButton('new');
  clearTimeout(saveTimer);
  if (activeNote && colContent.classList.contains('editing')) await saveCurrentNote(true);
  activeNote = '';

  try {
    const { name: baseName } = await api('/api/next-untitled');
    const { name: createdName } = await api('/api/note', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: baseName, content: `# ${baseName}\n` }),
    });

    if (activeTag !== '') {
      activeTag = '';
      document.querySelectorAll('.tag-item').forEach(el =>
        el.classList.toggle('active', el.dataset.tag === ''));
      updateToolbarState();
    }

    await renderNotes();
    await openNote(createdName, true);
  } finally {
    _isCreatingNote = false;
  }
});

// ── EDIT ────────────────────────────────────────────────────────────────────

document.getElementById('btn-edit').addEventListener('click', async () => {
  if (activeTag === TRASH_TAG) return;

  if (activeButton === 'edit') {
    clearTimeout(saveTimer);
    try { await saveCurrentNote(true); } catch (err) { console.error('[edit-btn] save failed:', err); }
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
  if (colContent.classList.contains('editing')) await saveCurrentNote(true);
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
    await renderTags(activeTag === TRASH_TAG);
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

// Clicking anywhere outside col-content while in new-note or edit mode saves and
// closes the active editing session.
// btn-new is in the sidebar (not col-content) and handles its own save→create
// transition, so exclude it to avoid a double-save.
document.addEventListener('mousedown', async e => {
  const isNew  = (activeButton === 'new') && !_isCreatingNote;
  const isEdit = (activeButton === 'edit');
  if (!isNew && !isEdit) return;
  if (colContent.contains(e.target)) return;
  if (e.target.closest('#btn-new')) return;  // btn-new handles its own transition
  await _finalizeNewNote('outside-mousedown');
});

// ── TRASH ───────────────────────────────────────────────────────────────────

// Returns a Promise that resolves true (Yes) or false (No/dismiss).
function confirmTrash() {
  return new Promise(resolve => {
    const overlay = document.getElementById('send-to-trash-overlay');
    overlay.classList.remove('hidden');

    let settled = false;
    function finish(result) {
      if (settled) return;
      settled = true;
      overlay.classList.add('hidden');
      resolve(result);
    }

    document.getElementById('send-to-trash-yes').addEventListener('click', e => {
      e.stopPropagation();
      finish(true);
    }, { once: true });

    document.getElementById('send-to-trash-no').addEventListener('click', e => {
      e.stopPropagation();
      finish(false);
    }, { once: true });

    // Defer the backdrop-dismiss listener to the next event-loop task so the
    // click that opened this dialog (still propagating right now) can't
    // immediately close it.
    setTimeout(() => {
      overlay.addEventListener('click', e => {
        if (e.target === overlay) finish(false);
      }, { once: true });
    }, 0);
  });
}

document.getElementById('btn-trash').addEventListener('click', async () => {
  const inTrash  = activeTag === TRASH_TAG;
  const endpoint = inTrash ? 'restore' : 'trash';

  if (!inTrash) {
    const confirmed = await confirmTrash();
    if (!confirmed) return;
  }

  if (selectedNotes.size >= 2) {
    // ── Multi-select: trash / restore every selected note ──
    // The placeholder is skipped rather than failing the whole batch.
    const names = [...selectedNotes].filter(n => inTrash || !isPlaceholderName(n));
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
    await renderTags(activeTag === TRASH_TAG);
    await renderNotes();
    return;
  }

  // ── Single note ──
  if (!activeNote) return;
  if (endpoint === 'trash' && isPlaceholderName(activeNote)) return;
  if (endpoint === 'trash' && noteEncrypted && !notePasswords.has(activeNote)) return;
  if (endpoint === 'trash') {
    clearTimeout(saveTimer);
    if (colContent.classList.contains('editing')) await saveCurrentNote(true);
  }
  await api(`/api/note/${encodeURIComponent(activeNote)}/${endpoint}`, { method: 'POST' });
  notePasswords.delete(activeNote);
  activeNote = '';
  setActiveButton(null);
  clearContentPane();
  document.querySelectorAll('.note-item').forEach(el => el.classList.remove('active'));
  await renderTags(activeTag === TRASH_TAG);
  await renderNotes();
});

// ── Note history tooltip ────────────────────────────────────────────────────

const _historyCache = new Map(); // noteName → history[] (lazy-loaded)
let   _historyFor   = null;      // name of the note currently shown in the tooltip

const _MONTHS = ['January','February','March','April','May','June',
                 'July','August','September','October','November','December'];

function _tooltipEl() { return document.getElementById('note-history-tooltip'); }

function _fmtHistoryDate(isoStr) {
  const d = new Date(isoStr);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()} ${_MONTHS[d.getMonth()]} ${d.getDate()} at ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _buildTooltipHtml(history) {
  if (!history.length) return '';
  const ACT_LABEL = { created: 'Created', added: 'Added', removed: 'Removed', changed: 'Edited' };

  const rows = history.map(entry => {
    const label   = ACT_LABEL[entry.act] || 'Edited';
    const hideVia = entry.act === 'created' && entry.via === 'myNote';
    const via     = hideVia ? '' : ` <span class="ht-via">via ${entry.via}</span>`;
    return `<div class="ht-row"><span class="ht-label">${label}:</span><span class="ht-date">${_fmtHistoryDate(entry.at)}</span>${via}</div>`;
  });

  // Append a "Last change" summary row if there was at least one edit after creation
  const nonCreated = history.filter(e => e.act !== 'created');
  if (nonCreated.length) {
    const last = nonCreated[nonCreated.length - 1];
    const via  = ` <span class="ht-via">via ${last.via}</span>`;
    rows.push(`<div class="ht-row ht-last"><span class="ht-label">Last change:</span><span class="ht-date">${_fmtHistoryDate(last.at)}</span>${via}</div>`);
  }

  return rows.join('');
}

function _positionTooltip(tip, clientX, clientY) {
  const tw = tip.offsetWidth  || 260;
  const th = tip.offsetHeight || 80;
  let x = clientX + 14;
  let y = clientY - 8;
  if (x + tw > window.innerWidth  - 8) x = clientX - tw - 14;
  if (y + th > window.innerHeight - 8) y = window.innerHeight - th - 8;
  tip.style.left = x + 'px';
  tip.style.top  = y + 'px';
}

function _hideHistoryTooltip() {
  const tip = _tooltipEl();
  if (tip) tip.classList.add('hidden');
  _historyFor = null;
}

// Invalidate cache entry whenever a note is saved so next hover is fresh
function _invalidateHistoryCache(noteName) {
  _historyCache.delete(noteName);
}

// Show history for a given note name, positioned at cursor coords.
async function _showHistoryFor(noteName, clientX, clientY) {
  const tip = _tooltipEl();
  if (!tip || !noteName) return;

  if (noteName === _historyFor) {
    _positionTooltip(tip, clientX, clientY);
    return;
  }

  _historyFor = noteName;
  tip.classList.add('hidden');

  if (!_historyCache.has(noteName)) {
    try {
      const h = await api(`/api/note/${encodeURIComponent(noteName)}/history`);
      _historyCache.set(noteName, Array.isArray(h) ? h : []);
    } catch (err) {
      _historyCache.set(noteName, []);
    }
  }

  if (_historyFor !== noteName) return;

  const history = _historyCache.get(noteName);
  if (!history.length) return;

  tip.innerHTML = _buildTooltipHtml(history);
  tip.classList.remove('hidden');
  _positionTooltip(tip, clientX, clientY);
}

// ── Tooltip on note-title bar (the "Created / Last change" strip) ───────────
const _noteTitleEl = document.getElementById('note-title');

_noteTitleEl.addEventListener('mouseover', e => {
  if (!activeNote) return;
  _showHistoryFor(activeNote, e.clientX, e.clientY);
});

_noteTitleEl.addEventListener('mousemove', e => {
  const tip = _tooltipEl();
  if (!tip || tip.classList.contains('hidden')) return;
  _positionTooltip(tip, e.clientX, e.clientY);
});

_noteTitleEl.addEventListener('mouseout', e => {
  if (e.relatedTarget && e.relatedTarget.closest('#note-title')) return;
  _hideHistoryTooltip();
});

_noteTitleEl.addEventListener('wheel', e => {
  const tip = _tooltipEl();
  if (!tip || tip.classList.contains('hidden')) return;
  e.preventDefault();
  tip.scrollTop += e.deltaY;
}, { passive: false });

// ── Collapse locked group on outside click ──────────────────────────────────
// When expanded but no note is currently session-unlocked, any click that
// isn't on the group header or one of its expanded items collapses the group.

document.addEventListener('click', e => {
  if (!lockedGroupExpanded) return;
  if (!document.getElementById('send-to-trash-overlay').classList.contains('hidden')) return;
  if (e.target.closest('.locked-group-header')) return;
  if (e.target.closest('[data-locked-group-item]')) return;
  if (e.target.closest('#sort-bar')) return;
  if (e.target.closest('.col-search-bar')) return;
  _collapseLockedGroup();
});

// ── Global Escape ───────────────────────────────────────────────────────────

document.addEventListener('keydown', async e => {
  if (e.key !== 'Escape') return;

  if (!document.getElementById('note-find-bar').classList.contains('hidden')) {
    window._noteFindClose?.(); return;
  }
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
    try { if (activeNote) await saveCurrentNote(true); } catch (err) { console.error('[escape] save failed:', err); }
    enterViewMode();
    setActiveButton(null);
    return;
  }
  if (activeNote) {
    const wasLocked = noteEncrypted;
    clearMultiSelect();
    activeNote = '';
    setActiveButton(null);
    clearContentPane();
    document.querySelectorAll('.note-item').forEach(el => el.classList.remove('active'));
    if (wasLocked) _collapseLockedGroup();
  }
});

// ── F5 → sync, then re-render from disk ─────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key !== 'F5' && e.code !== 'F5') return;
  // Never let the WebView reload the page itself — that would drop unsaved
  // editor state and restart the app shell rather than refreshing the notes.
  e.preventDefault();
  if (typeof window._syncNow === 'function') window._syncNow({ alwaysRefresh: true });
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
  const noteName = ev?.name ? ev.name.replace(/\.md$/i, '') : null;
  if (noteName) _invalidateHistoryCache(noteName);
  renderTags(activeTag === TRASH_TAG).then(() => renderNotes()).then(async () => {
    // If the updated note is currently open in view mode, reload its content.
    if (noteName && noteName === activeNote && !colContent.classList.contains('editing')) {
      await openNote(activeNote);
    }
    // Blink the note list item to signal the Telegram update.
    if (ev?.appended && noteName) {
      const allItems = Array.from(document.querySelectorAll('.note-item'));
      const item = allItems.find(el => el.dataset.name === noteName);
      if (item) {
        item.classList.remove('tg-blink');
        void item.offsetWidth;             // restart animation if already running
        item.classList.add('tg-blink');
        document.addEventListener('click', () => {
          item.classList.remove('tg-blink');
        }, { once: true });
      } else {
        console.warn(`[blink] item NOT found for noteName=${noteName}`);
      }
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

// ── Manage Space ─────────────────────────────────────────────────────────────
(function () {
  // Colours and icons matching the backend categories
  const CAT_ICONS = {
    notes:     '📝',
    images:    '🖼',
    documents: '📄',
    videos:    '🎬',
    audio:     '🎵',
    archives:  '🗜',
  };

  function fmtBytes(bytes) {
    if (bytes === 0) return '0 B';
    const units = ['B','KB','MB','GB','TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    const v = bytes / Math.pow(1024, i);
    return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
  }

  async function loadStorage() {
    const loading = document.getElementById('storage-loading');
    const content = document.getElementById('storage-content');
    const refreshBtn = document.getElementById('storage-refresh-btn');
    loading.classList.remove('hidden');
    content.classList.add('hidden');
    refreshBtn.classList.add('spinning');

    let data;
    try {
      data = await api('/api/storage');
    } catch (e) {
      loading.innerHTML = `<span style="color:var(--danger)">Error: ${e.message}</span>`;
      refreshBtn.classList.remove('spinning');
      return;
    }

    loading.classList.add('hidden');

    const total = data.total;
    // Backend already returns sorted desc; also sort client-side for safety
    const cats  = [...data.categories]
      .sort((a, b) => b.size - a.size)
      .filter(c => c.size > 0);

    // ── Stacked bar (largest → smallest, left → right) ─────────────────────
    const bar = document.getElementById('storage-bar');
    bar.innerHTML = '';
    cats.forEach(c => {
      const pct = total > 0 ? (c.size / total * 100) : 0;
      const seg = document.createElement('div');
      seg.className = 'storage-bar-seg';
      seg.style.cssText = `width:${Math.max(pct, 0.4)}%;background:${c.color}`;
      seg.title = `${c.label}: ${fmtBytes(c.size)}`;
      bar.appendChild(seg);
    });

    // ── Legend ─────────────────────────────────────────────────────────────
    const legend = document.getElementById('storage-legend');
    legend.innerHTML = '';
    cats.forEach(c => {
      const dot = document.createElement('span');
      dot.className = 'storage-legend-item';
      dot.innerHTML =
        `<span class="storage-legend-dot" style="background:${c.color}"></span>${c.label}`;
      legend.appendChild(dot);
    });

    // ── Total ──────────────────────────────────────────────────────────────
    document.getElementById('storage-total-val').textContent = fmtBytes(total);

    // ── Category rows ──────────────────────────────────────────────────────
    const rows = document.getElementById('storage-rows');
    rows.innerHTML = '';

    // Rows: ALL categories, sorted largest → smallest (0-byte last)
    const sortedCats = [...data.categories].sort((a, b) => b.size - a.size);
    sortedCats.forEach(c => {
      const pct = total > 0 ? (c.size / total * 100) : 0;
      const row = document.createElement('div');
      row.className = 'storage-row';

      row.innerHTML = `
        <div class="storage-row-left">
          <span class="storage-row-icon">${CAT_ICONS[c.key] || '📁'}</span>
          <span class="storage-row-label">${c.label}</span>
          <span class="storage-row-count">${c.count} file${c.count !== 1 ? 's' : ''}</span>
        </div>
        <div class="storage-row-right">
          <span class="storage-row-size">${fmtBytes(c.size)}</span>
          <div class="storage-row-bar">
            <div class="storage-row-fill"
                 style="width:${Math.max(pct,0)}%;background:${c.color}"></div>
          </div>
        </div>`;

      // Tooltip — top 3 entries only
      if (c.top && c.top.length > 0) {
        const tip = document.createElement('div');
        tip.className = 'storage-tooltip hidden';

        let tipHtml;
        if (c.key === 'notes') {
          // Notes: show note title + size breakdown
          tipHtml = `<div class="storage-tip-header">Largest notes
              <span class="storage-tip-sub">(note + linked attachments)</span></div>`
            + c.top.map(t =>
                `<div class="storage-tip-row">
                  <span class="storage-tip-name" title="${t.name}">${t.name}</span>
                  <span class="storage-tip-meta">${t.detail}</span>
                 </div>`).join('');
        } else {
          // Attachments: show filename + which note uses it
          tipHtml = `<div class="storage-tip-header">Largest files</div>`
            + c.top.map(t => {
                const noteInfo = t.noteTitle
                  ? `<span class="storage-tip-note" title="${t.noteTitle}">📝 ${t.noteTitle}</span>`
                  : '';
                return `<div class="storage-tip-row storage-tip-row--attach">
                  <div class="storage-tip-attach-names">
                    <span class="storage-tip-name" title="${t.name}">${t.name}</span>
                    ${noteInfo}
                  </div>
                  <span class="storage-tip-meta">${t.detail}</span>
                 </div>`;
              }).join('');
        }

        tip.innerHTML = tipHtml;
        row.appendChild(tip);

        row.addEventListener('mouseenter', () => tip.classList.remove('hidden'));
        row.addEventListener('mouseleave', () => tip.classList.add('hidden'));
      }

      rows.appendChild(row);
    });

    refreshBtn.classList.remove('spinning');
    content.classList.remove('hidden');
  }

  window.loadStorageSettings = loadStorage;

  document.getElementById('storage-refresh-btn')
    .addEventListener('click', loadStorage);
})();

/* ── Trash settings panel ─────────────────────────────────────────────────── */
(function () {
  const sel     = document.getElementById('trash-expire-select');
  const saveBtn = document.getElementById('trash-save-btn');
  const status  = document.getElementById('trash-settings-status');
  const emptyBtn  = document.getElementById('trash-empty-btn');
  const overlay   = document.getElementById('trash-confirm-overlay');
  const cancelBtn = document.getElementById('trash-confirm-cancel');
  const okBtn     = document.getElementById('trash-confirm-ok');

  const _trashDirty = makeDirtyTracker(saveBtn, [sel]);

  // Load current setting from server; set baseline so Save stays grey
  async function loadTrashSettings() {
    try {
      const data = await api('/api/trash/settings');
      sel.value = String(data.expireMonths || 3);
      _trashDirty.setBaseline();
    } catch { /* silent */ }
  }

  // Save expiry period
  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    try {
      await api('/api/trash/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expireMonths: parseInt(sel.value, 10) })
      });
      _trashDirty.markClean();      // back to grey — nothing "new" to save
      status.textContent = 'Saved';
      setTimeout(() => { status.textContent = ''; }, 2000);
    } catch {
      status.textContent = 'Error saving';
      saveBtn.disabled = false;     // re-enable so user can retry
    }
  });

  // Empty Trash → show confirm dialog
  emptyBtn.addEventListener('click', () => {
    overlay.classList.remove('hidden');
  });

  // Cancel — close dialog
  cancelBtn.addEventListener('click', () => {
    overlay.classList.add('hidden');
  });
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.classList.add('hidden');
  });

  // OK — permanently delete all trash
  okBtn.addEventListener('click', async () => {
    overlay.classList.add('hidden');
    okBtn.disabled = true;
    emptyBtn.disabled = true;
    status.textContent = 'Emptying…';
    try {
      const data = await api('/api/trash/empty', { method: 'POST' });
      status.textContent = data.deleted
        ? `Deleted ${data.deleted} note${data.deleted !== 1 ? 's' : ''}`
        : 'Trash already empty';
      setTimeout(() => { status.textContent = ''; }, 3000);
      // Refresh tag + note lists if any notes were removed
      if (data.deleted) {
        await renderTags(activeTag === TRASH_TAG);
        await renderNotes();
      }
    } catch {
      status.textContent = 'Error';
    } finally {
      okBtn.disabled = false;
      emptyBtn.disabled = false;
    }
  });

  window.loadTrashSettings = loadTrashSettings;
})();

(function connectSSE() {
  const src = new EventSource('/api/events');

  src.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }

    if (ev.type === 'notes-updated') {
      _handleNotesUpdated(ev);
    } else if (ev.type === 'notify') {
      showNotif(ev.msg, ev.level || 'warn');
    } else if (ev.type === 'icloud_sync') {
      document.dispatchEvent(new CustomEvent('sse-icloud_sync', { detail: ev }));
    } else if (ev.type === 'gdrive_sync') {
      document.dispatchEvent(new CustomEvent('sse-gdrive_sync', { detail: ev }));
    } else if (ev.type === 'github_sync') {
      document.dispatchEvent(new CustomEvent('sse-github_sync', { detail: ev }));
    } else if (ev.type === 'auto_sync') {
      document.dispatchEvent(new CustomEvent('sse-auto_sync', { detail: ev }));
    }
  };

  src.onerror = () => {
    src.close();
    // Reconnect after 5 s if the connection drops
    setTimeout(connectSSE, 5000);
  };
})();

// ── Flush pending edit on unexpected close ─────────────────────────────────
// Backstop for closes that skip the custom title-bar button (Alt+F4, taskbar
// close, browser tab close in dev mode). beforeunload can't await a normal
// fetch, so fire a keepalive request that's allowed to outlive the page.
window.addEventListener('beforeunload', () => {
  if (!activeNote || !colContent.classList.contains('editing')) return;
  clearTimeout(saveTimer);
  let body = noteBodyContent;
  if (noteEncrypted && notePasswords.has(activeNote)) body = noteRawCiphertext;
  fetch(`/api/note/${encodeURIComponent(activeNote)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: noteFrontmatter + body, session_end: true }),
    keepalive: true,
  }).catch(() => {});
});

// ── pywebview frameless title bar ─────────────────────────────────────────

// pywebview injects window.pywebview and fires 'pywebviewready' once the
// bridge is ready.  We use that to activate the custom title bar and wire
// the native window-control buttons.  In plain browser dev mode this block
// never runs and #titlebar stays hidden.
window.addEventListener('pywebviewready', function () {
  document.documentElement.classList.add('webview-app');

  document.getElementById('tb-close').addEventListener('click', async function () {
    // Flush the last edit (with a real history entry) before the window — and
    // the whole process — goes away. Without this, an edit made right before
    // closing never gets a "changed" entry: the debounced auto-save writes the
    // .md file but only a session_end save appends to .history.json.
    // Reuses the same finalize path as "click outside the editor" so new-note
    // and edit-mode sessions are both handled consistently — and awaiting it
    // here matters: clicking the close button also fires that outside-click
    // handler (mousedown fires before click), but it doesn't block this click
    // handler, so without this await api.close() could destroy the window —
    // and kill the whole process — before that save finishes.
    try {
      if (activeButton === 'new' || activeButton === 'edit') {
        await _finalizeNewNote('tb-close');
      }
    } catch (e) {
      console.error('[close] final save failed:', e);
    }
    window.pywebview.api.close();
  });

  document.getElementById('tb-minimize').addEventListener('click', function () {
    window.pywebview.api.minimize();
  });

  document.getElementById('tb-maximize').addEventListener('click', function () {
    window.pywebview.api.toggle_maximize();
  });

  // ── Title-bar drag ────────────────────────────────────────────────────────
  // WM_NCLBUTTONDOWN cannot be used: WebView2 captures the mouse in its own
  // process, so the outer window's move loop never receives WM_MOUSEMOVE.
  // Instead, track screen coords in JS and call SetWindowPos each RAF tick.
  let _drag = null;
  let _dragRaf = null;

  document.getElementById('titlebar-drag').addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    e.preventDefault();
    const sx = e.screenX, sy = e.screenY;
    window.pywebview.api.get_window_rect().then(function (rect) {
      if (!rect) return;
      _drag = { ox: sx, oy: sy, wx: rect.x, wy: rect.y, cx: sx, cy: sy };
    });
  });

  document.addEventListener('mousemove', function (e) {
    if (!_drag) return;
    _drag.cx = e.screenX;
    _drag.cy = e.screenY;
    if (_dragRaf) return;
    _dragRaf = requestAnimationFrame(function () {
      _dragRaf = null;
      if (!_drag) return;
      window.pywebview.api.move_window(
        _drag.wx + (_drag.cx - _drag.ox),
        _drag.wy + (_drag.cy - _drag.oy)
      );
    });
  });

  document.addEventListener('mouseup', function (e) {
    if (e.button === 0) {
      _drag = null;
      if (_dragRaf) { cancelAnimationFrame(_dragRaf); _dragRaf = null; }
    }
  });

  // Double-click drag area → toggle maximize (standard Windows behaviour)
  document.getElementById('titlebar-drag').addEventListener('dblclick', function () {
    window.pywebview.api.toggle_maximize();
  });

});

// ── Sort bar ───────────────────────────────────────────────────────────────

function renderSortBar() {
  document.querySelectorAll('.sort-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.sort === sortBy);
  });
  const dirEl = document.getElementById('sort-direction');
  dirEl.textContent = sortDesc ? '▾' : '▴';
  const tips = {
    ctime: { asc: 'OLD\nNEW', desc: 'NEW\nOLD' },
    mtime: { asc: 'OLD\nNEW', desc: 'NEW\nOLD' },
    alpha: { asc: 'A\nZ',     desc: 'Z\nA'     },
  };
  const t = tips[sortBy] || tips.ctime;
  dirEl.dataset.tooltip = sortDesc ? t.desc : t.asc;
}

const _sortDirTooltip = document.getElementById('sort-dir-tooltip');
const _sortDirEl      = document.getElementById('sort-direction');

_sortDirEl.addEventListener('mouseenter', () => {
  const text = _sortDirEl.dataset.tooltip;
  if (!text) return;
  _sortDirTooltip.textContent = text;
  _sortDirTooltip.style.display = 'block';
  const r = _sortDirEl.getBoundingClientRect();
  _sortDirTooltip.style.top  = Math.round(r.top + r.height / 2 - _sortDirTooltip.offsetHeight / 2) + 'px';
  _sortDirTooltip.style.left = Math.round(r.right + 6) + 'px';
});

_sortDirEl.addEventListener('mouseleave', () => {
  _sortDirTooltip.style.display = 'none';
});

// ── Generic button tooltip ─────────────────────────────────────────────────
// Replaces native browser title= tooltips on <button> elements so they
// respect the app's current theme (light/dark) instead of the OS default.
// Uses event delegation so dynamically-set .title (e.g. btn-trash) is caught
// on each hover without needing a MutationObserver.
(function () {
  const tip = document.getElementById('ui-tooltip');
  let _cur = null;

  document.addEventListener('mouseover', e => {
    const btn = e.target.closest('button');
    if (!btn) return;

    // Capture title attribute each time — handles dynamic changes like btn-trash
    if (btn.hasAttribute('title')) {
      btn.dataset.tip = btn.getAttribute('title');
      btn.removeAttribute('title');   // suppress native tooltip
    }
    const text = btn.dataset.tip;
    if (!text) return;

    _cur = btn;
    tip.textContent = text;
    tip.style.display = 'block';

    const r   = btn.getBoundingClientRect();
    const cx  = Math.round(r.left + r.width / 2);
    const tw  = tip.offsetWidth;
    const th  = tip.offsetHeight;
    const gap = 5;

    // Prefer below; flip above if not enough space
    let top = r.bottom + gap;
    if (top + th > window.innerHeight) top = r.top - th - gap;

    // Centre horizontally, clamp to viewport
    let left = cx - tw / 2;
    left = Math.max(6, Math.min(left, window.innerWidth - tw - 6));

    tip.style.top  = Math.round(top)  + 'px';
    tip.style.left = Math.round(left) + 'px';
  });

  document.addEventListener('mouseout', e => {
    if (!_cur) return;
    if (!_cur.contains(e.relatedTarget)) {
      tip.style.display = 'none';
      _cur = null;
    }
  });
})();

async function _saveSortPrefs() {
  try {
    await api('/api/prefs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sort_by: sortBy, sort_desc: sortDesc }),
    });
  } catch (_) {}
}

document.querySelectorAll('.sort-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const clicked = btn.dataset.sort;
    if (clicked === sortBy) {
      // Same option — toggle direction
      sortDesc = !sortDesc;
    } else {
      sortBy = clicked;
      alphaGroupExpanded.clear();
    }
    renderSortBar();
    await _saveSortPrefs();
    await renderNotes();
  });
});

// ── Init ───────────────────────────────────────────────────────────────────

(async () => {
  // Load saved sort preference before first render
  try {
    const prefs = await api('/api/settings');
    if (prefs.sort_by)              sortBy   = prefs.sort_by;
    if (prefs.sort_desc !== undefined) sortDesc = prefs.sort_desc;
  } catch (_) {}
  renderSortBar();
  await renderTags();
  await renderNotes();
  await checkWorkfolder();

  // Sync on open. Until now the first thing shown was whatever was on disk when
  // the app last closed, so a note written on the phone only appeared on the
  // *next* launch. Not awaited: the list above is already usable, and this
  // re-renders it once the notes have come down.
  window._syncNow?.({ alwaysRefresh: true, silent: true });
})();

// ── In-note find bar (Ctrl+F, both view and edit mode) ─────────────────────

(function () {
  const bar   = document.getElementById('note-find-bar');
  const input = document.getElementById('note-find-input');
  const count = document.getElementById('note-find-count');
  let matches = [];          // view mode: <mark> elements; edit mode: {start,end}
  let idx     = -1;
  let editing = false;       // which pane the current match list belongs to

  const _inEditMode = () => colContent.classList.contains('editing');

  function open() {
    // Switching panes with the bar already open invalidates the match list
    if (!bar.classList.contains('hidden') && editing !== _inEditMode()) _clear();
    editing = _inEditMode();
    bar.classList.remove('hidden');
    input.focus();
    input.select();
    if (input.value.trim()) _run(input.value.trim());
  }

  function close() {
    bar.classList.add('hidden');
    _clear();
    matches = [];
    idx     = -1;
    count.textContent = '';
    if (editing && _inEditMode()) noteEdit.focus();
  }

  // ── Edit-mode highlight backdrop ────────────────────────────────────────
  // A textarea can't hold <mark> elements, so matches are painted on a div
  // placed directly behind it with the same font, padding, wrapping and scroll
  // offset. Its text is transparent, so all the user sees is the highlight
  // boxes under the real text the textarea draws on top.
  let hl = null;

  const _esc = t => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  function _hlSyncScroll() {
    if (!hl) return;
    hl.scrollTop  = noteEdit.scrollTop;
    hl.scrollLeft = noteEdit.scrollLeft;
  }

  function _hlLayout(el) {
    const cs = getComputedStyle(noteEdit);
    for (const prop of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle',
                        'letterSpacing', 'lineHeight', 'wordSpacing', 'textIndent',
                        'textTransform', 'wordBreak', 'overflowWrap', 'tabSize',
                        'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft'])
      el.style[prop] = cs[prop];
    el.style.boxSizing = 'border-box';
    // clientWidth/Height rather than offset*: they exclude the scrollbar, so the
    // backdrop wraps at exactly the column the textarea wraps at.
    el.style.left   = noteEdit.offsetLeft + 'px';
    el.style.top    = noteEdit.offsetTop + 'px';
    el.style.width  = noteEdit.clientWidth + 'px';
    el.style.height = noteEdit.clientHeight + 'px';
  }

  function _hlPaint(active) {
    if (!hl) {
      hl = document.createElement('div');
      hl.className = 'note-edit-highlights';
      hl.setAttribute('aria-hidden', 'true');
      noteEdit.parentNode.insertBefore(hl, noteEdit);
      noteEdit.addEventListener('scroll', _hlSyncScroll);
    }
    _hlLayout(hl);
    const text = noteEdit.value;
    let html = '', cursor = 0;
    matches.forEach((m, i) => {
      html += _esc(text.slice(cursor, m.start));
      html += '<mark class="search-hl' + (i === active ? ' search-hl--active' : '') + '">'
            + _esc(text.slice(m.start, m.end)) + '</mark>';
      cursor = m.end;
    });
    // The trailing newline keeps the last line's box from collapsing.
    hl.innerHTML = html + _esc(text.slice(cursor)) + String.fromCharCode(10);
    noteEdit.classList.add('has-highlights');
    _hlSyncScroll();
  }

  function _hlRemove() {
    if (hl) {
      noteEdit.removeEventListener('scroll', _hlSyncScroll);
      hl.remove();
      hl = null;
    }
    noteEdit.classList.remove('has-highlights');
  }

  function _clear() {
    _hlRemove();
    const parents = new Set();
    noteView.querySelectorAll('mark.search-hl').forEach(m => {
      const p = m.parentNode;
      m.replaceWith(document.createTextNode(m.textContent));
      if (p) parents.add(p);
    });
    parents.forEach(p => p.normalize());
  }

  function _run(q) {
    _clear();
    matches = [];
    idx     = -1;
    editing = _inEditMode();
    if (!q) { count.textContent = ''; return; }

    if (editing) { _runEdit(q); return; }

    const lq = q.toLowerCase();
    const walker = document.createTreeWalker(noteView, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        let p = node.parentElement;
        while (p && p !== noteView) {
          if (p.tagName === 'MARK' || p.tagName === 'SCRIPT' || p.tagName === 'STYLE')
            return NodeFilter.FILTER_REJECT;
          p = p.parentElement;
        }
        return node.textContent.toLowerCase().includes(lq)
          ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      }
    });

    // Collect all match positions per text node
    const byNode = new Map();
    let node;
    while ((node = walker.nextNode())) {
      const text  = node.textContent;
      const lower = text.toLowerCase();
      const hits  = [];
      let start = 0, i;
      while ((i = lower.indexOf(lq, start)) !== -1) {
        hits.push({ start: i, end: i + q.length });
        start = i + 1;
      }
      if (hits.length) byNode.set(node, hits);
    }

    // Replace each text node with a fragment containing <mark> spans
    byNode.forEach((hits, node) => {
      const text   = node.textContent;
      const parent = node.parentNode;
      const frag   = document.createDocumentFragment();
      let cursor   = 0;

      hits.forEach(({ start, end }) => {
        if (cursor < start) frag.appendChild(document.createTextNode(text.slice(cursor, start)));
        const m = document.createElement('mark');
        m.className   = 'search-hl';
        m.textContent = text.slice(start, end);
        frag.appendChild(m);
        cursor = end;
      });

      if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
      parent.replaceChild(frag, node);
    });

    // Collect in DOM order (querySelectorAll guarantees this)
    matches = Array.from(noteView.querySelectorAll('mark.search-hl'));

    if (matches.length) {
      idx = 0;
      _activate(0);
    } else {
      count.textContent = 'No matches';
    }
  }

  /** Edit mode: collect match offsets in the raw textarea text. */
  function _runEdit(q) {
    const text = noteEdit.value;
    const lq   = q.toLowerCase();
    const lt   = text.toLowerCase();
    let start = 0, i;
    while ((i = lt.indexOf(lq, start)) !== -1) {
      matches.push({ start: i, end: i + q.length });
      start = i + 1;
    }
    if (matches.length) {
      idx = 0;
      _activate(0);
    } else {
      count.textContent = 'No matches';
    }
  }

  function _activate(i) {
    if (editing) {
      const m = matches[i];
      if (!m) return;
      // Select the hit so it stays visible while focus remains in the find box,
      // then centre it vertically using the same line metrics as the mode switch.
      noteEdit.setSelectionRange(m.start, m.end);
      _hlPaint(i);
      const lines = noteEdit.value.slice(0, m.start).split('\n');
      const off   = _editOffsetForLine(lines.length - 1, noteEdit.value.split('\n'));
      noteEdit.scrollTop = Math.max(0, off - noteEdit.clientHeight / 2);
      _hlSyncScroll();
      count.textContent = `${i + 1} of ${matches.length}`;
      return;
    }
    matches.forEach((m, j) => m.classList.toggle('search-hl--active', j === i));
    matches[i]?.scrollIntoView({ block: 'center', inline: 'nearest' });
    count.textContent = `${i + 1} of ${matches.length}`;
  }

  function step(dir) {
    if (!matches.length) return;
    idx = (idx + dir + matches.length) % matches.length;
    _activate(idx);
  }

  input.addEventListener('input',   () => _run(input.value.trim()));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  });

  // Typing moves every offset, so recompute and repaint — without touching the
  // selection or scroll, which would yank the caret away mid-keystroke.
  function _hlRefresh() {
    if (bar.classList.contains('hidden') || !_inEditMode()) return;
    const q = input.value.trim();
    if (!q) return;
    matches = [];
    const lt = noteEdit.value.toLowerCase(), lq = q.toLowerCase();
    let start = 0, at;
    while ((at = lt.indexOf(lq, start)) !== -1) {
      matches.push({ start: at, end: at + q.length });
      start = at + 1;
    }
    if (!matches.length) {
      _hlRemove();
      idx = -1;
      count.textContent = 'No matches';
      return;
    }
    if (idx < 0 || idx >= matches.length) idx = 0;
    _hlPaint(idx);
    count.textContent = `${idx + 1} of ${matches.length}`;
  }
  noteEdit.addEventListener('input', _hlRefresh);
  // The backdrop is absolutely positioned, so it has to follow the textarea.
  window.addEventListener('resize', () => { if (hl) _hlPaint(idx); });

  document.getElementById('note-find-prev').addEventListener('click', () => step(-1));
  document.getElementById('note-find-next').addEventListener('click', () => step(1));
  document.getElementById('note-find-close').addEventListener('click', close);

  // Ctrl+F / Cmd+F opens the bar in both view and edit mode.
  // Matched on e.code (physical key) rather than e.key, so it keeps working
  // under non-Latin keyboard layouts where the F key reports e.g. Cyrillic 'а'.
  document.addEventListener('keydown', e => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    if (e.code !== 'KeyF' && e.key !== 'f' && e.key !== 'F') return;
    if (!activeNote) return;
    e.preventDefault();
    open();
  });

  // Re-run after enterViewMode rebuilds innerHTML (new note or refresh)
  window._noteFindRerun = () => {
    if (!bar.classList.contains('hidden') && input.value.trim())
      setTimeout(() => _run(input.value.trim()), 0);
  };

  // Close hook called from enterEditMode() and clearContentPane()
  window._noteFindClose = close;
})();

// ── Activity-based auto-lock (reset 60 s idle timer on user interaction) ───

(function () {
  let _throttle = null;

  function _resetOnActivity() {
    if (!noteEncrypted || !notePasswords.has(activeNote)) return;
    if (_throttle) return;
    _throttle = setTimeout(() => { _throttle = null; }, 1000);
    startAutoLockTimer();  // cancels existing timer and starts a fresh 60 s countdown
  }

  // Mouse movement, clicks, and scrolling inside the view area all count as activity
  noteView.addEventListener('mousemove', _resetOnActivity);
  noteView.addEventListener('click',     _resetOnActivity);
  noteView.addEventListener('scroll',    _resetOnActivity);
  noteView.addEventListener('keydown',   _resetOnActivity);
})();
