from flask import Flask, render_template, jsonify, request, abort, send_from_directory
from datetime import datetime as _dtt
import os
import re
import glob as glob_module
import base64
import subprocess
import threading
import time
import logging
import queue as _queue_mod
import json as _json
import socket as _socket
import shutil
import ctypes
import hashlib as _hashlib
import urllib.parse as _urlparse
import requests
import paths
import json
import sys as _sys
import uuid as _uuid_mod

# Console output must never be able to fail. On Windows stdout defaults to the
# legacy ANSI codepage, so printing a note name (or any non-ASCII text) from a
# request handler raises UnicodeEncodeError — which Flask turns into a 500 and
# which, on the save path, blocks switching away from the note entirely.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass   # None under a windowed PyInstaller build, or already suitable

app = Flask(
    __name__,
    template_folder=paths.TEMPLATES_DIR,
    static_folder=paths.STATIC_DIR,
)

TRASH_TAG = 'trash'

# ---------------------------------------------------------------------------
# Attachment subfolder helpers
# ---------------------------------------------------------------------------

_ATTACH_IMAGE_EXTS   = {'.png','.jpg','.jpeg','.gif','.bmp','.svg','.webp',
                        '.ico','.tiff','.tif','.heic','.heif'}
_ATTACH_ARCHIVE_EXTS = {'.zip','.rar','.7z','.tar','.gz','.bz2','.xz','.tgz'}
_ATTACH_VIDEO_EXTS   = {'.mp4','.avi','.mov','.mkv','.wmv','.flv','.webm',
                        '.m4v','.mpg','.mpeg','.3gp'}
_ATTACH_AUDIO_EXTS   = {'.mp3','.m4a','.ogg','.oga','.wav','.flac',
                        '.aac','.opus','.weba','.wma'}

def _attachment_subfolder(filename: str) -> str:
    """Return 'images', 'archives', 'videos', 'audio', or 'documents' for a filename."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in _ATTACH_IMAGE_EXTS:   return 'images'
    if ext in _ATTACH_ARCHIVE_EXTS: return 'archives'
    if ext in _ATTACH_VIDEO_EXTS:   return 'videos'
    if ext in _ATTACH_AUDIO_EXTS:   return 'audio'
    return 'documents'

def _ensure_attach_subdir(subfolder: str) -> str:
    """Ensure attachments/<subfolder>/ exists and return its path."""
    d = os.path.join(paths.get_attachments_dir(), subfolder)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def _read_frontmatter(filepath: str) -> dict:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return {}
    m = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not m:
        return {'tags': [], 'encrypted': False, 'id': '', 'trashed_at': None}
    fm = m.group(1)
    tags: list = []
    tm = re.search(r'^tags:\s*\[([^\]]*)\]', fm, re.MULTILINE)
    if tm:
        tags = [t.strip().strip('"\'') for t in tm.group(1).split(',') if t.strip()]
    else:
        tm2 = re.search(r'^tags:\s*\n((?:[ \t]+-[ \t]+.+\n?)+)', fm, re.MULTILINE)
        if tm2:
            tags = [re.sub(r'^[ \t]+-[ \t]+', '', l).strip()
                    for l in tm2.group(1).splitlines() if l.strip()]
    encrypted  = bool(re.search(r'^encrypted:\s*true', fm, re.MULTILINE))
    id_m       = re.search(r'^id:\s*(\S+)', fm, re.MULTILINE)
    note_id    = id_m.group(1) if id_m else ''
    ta_m       = re.search(r'^trashed_at:\s*(\S+)', fm, re.MULTILINE)
    trashed_at = float(ta_m.group(1)) if ta_m else None
    return {'tags': tags, 'encrypted': encrypted, 'id': note_id, 'trashed_at': trashed_at}


def _write_tags(filepath: str, tags: list) -> None:
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    tag_line = 'tags: [' + ', '.join(tags) + ']'
    m = re.match(r'^---\s*\n([\s\S]*?)\n---\s*\n?', content)
    if m:
        fm   = m.group(1)
        rest = content[m.end():]
        # Preserve encrypted field before stripping
        encrypted = bool(re.search(r'^encrypted:\s*true', fm, re.MULTILINE))
        fm = re.sub(r'^tags:[ \t]*\[.*\]\n?',         '', fm, flags=re.MULTILINE)
        fm = re.sub(r'^tags:[ \t]*\n(?:[ \t]+-[ \t]+.*\n)*', '', fm, flags=re.MULTILINE)
        fm = re.sub(r'^encrypted:[ \t]+.*\n?',         '', fm, flags=re.MULTILINE)
        stripped = fm.rstrip('\n')
        parts = []
        if encrypted:
            parts.append('encrypted: true')
        if tags:
            parts.append(tag_line)
        fm = (stripped + '\n' if stripped else '') + ('\n'.join(parts) if parts else '')
        new_content = '---\n' + fm + '\n---\n' + rest
    else:
        new_content = ('---\n' + tag_line + '\n---\n' if tags else '') + content
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)


def _inject_note_id(filepath: str) -> str:
    """Ensure the note has an id: UUID in its frontmatter.
    Creates frontmatter if absent. Returns the UUID string."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return ''
    fm_match = re.match(r'^---\s*\n([\s\S]*?)\n---\s*\n?', content)
    if fm_match:
        fm_block = fm_match.group(1)
        m = re.search(r'^id:\s*(\S+)', fm_block, re.MULTILINE)
        if m:
            return m.group(1)                       # already has id
        note_id     = str(_uuid_mod.uuid4())
        rest        = content[fm_match.end():]
        new_content = '---\nid: ' + note_id + '\n' + fm_block + '\n---\n' + rest
    else:
        note_id     = str(_uuid_mod.uuid4())
        new_content = '---\nid: ' + note_id + '\n---\n' + content
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except OSError:
        pass
    return note_id


def _set_trashed_at(filepath: str, ts: 'float | None') -> None:
    """Write or remove the trashed_at: field in a note's frontmatter."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return
    fm_match = re.match(r'^---\s*\n([\s\S]*?)\n---\s*\n?', content)
    if not fm_match:
        if ts is None:
            return
        new_content = f'---\ntrashed_at: {ts}\n---\n' + content
    else:
        fm_block = fm_match.group(1)
        rest     = content[fm_match.end():]
        fm_block = re.sub(r'^trashed_at:[ \t]+.*\n?', '', fm_block, flags=re.MULTILINE)
        if ts is not None:
            fm_block = f'trashed_at: {ts}\n' + fm_block
        new_content = '---\n' + fm_block.lstrip('\n') + '\n---\n' + rest
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)


# ---------------------------------------------------------------------------
# Note history (sidecar *.history.json)
# ---------------------------------------------------------------------------

def _history_path(fp: str) -> str:
    return os.path.splitext(fp)[0] + '.history.json'

def _read_history(fp: str) -> list:
    try:
        with open(_history_path(fp), 'r', encoding='utf-8') as f:
            return _json.load(f)
    except (OSError, ValueError):
        return []

def _history_ts(entry: dict) -> float:
    """Epoch seconds for a history entry's 'at', or 0.0 if unreadable.

    The two apps write different shapes and this is the only place they meet:
    the PC writes naive LOCAL time ('2026-09-27T10:46:37') while the phone
    writes UTC with a Z and milliseconds ('2026-09-27T07:32:43.451Z').  Compared
    as strings, or parsed as if both were the same clock, entries from the two
    devices land in the wrong order by exactly the UTC offset — so each form is
    resolved to a real instant here.
    """
    from datetime import datetime as _dt, timezone as _tzmod
    raw = str(entry.get('at', '')).strip()
    if not raw:
        return 0.0
    utc = raw.endswith('Z')
    if utc:
        raw = raw[:-1]
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            dt = _dt.strptime(raw, fmt)
        except ValueError:
            continue
        # A 'Z' timestamp is UTC; a bare one is this machine's local time.
        dt = dt.replace(tzinfo=_tzmod.utc) if utc else dt.astimezone()
        return dt.timestamp()
    return 0.0


def _note_entry_time(fp: str) -> float:
    """When this note's content was last entered, as epoch seconds.

    The history sidecar is the authority because both apps write it at edit
    time; file mtime is the fallback, since a synced copy carries the mtime of
    whenever it was written locally rather than when it was typed."""
    times = [_history_ts(e) for e in _read_history(fp)]
    times = [t for t in times if t > 0]
    if times:
        return max(times)
    try:
        return os.path.getmtime(fp)
    except OSError:
        return 0.0


_HISTORY_SESSION_WINDOW = 15 * 60  # seconds — edits within this gap are merged

# Maps realpath(fp) → note content at the time the last history entry was written.
# Used by api_save_note to detect real changes regardless of auto-save frequency.
_last_history_content: 'dict[str, str]' = {}

def _append_history(fp: str, act: str, via: str) -> None:
    now     = _dtt.now()
    history = _read_history(fp)

    if act != 'created' and history:
        last = history[-1]
        if last.get('act') == 'created' and last.get('via') == via and not last.get('settled'):
            try:
                elapsed = (now - _dtt.strptime(last['at'], '%Y-%m-%dT%H:%M:%S')).total_seconds()
                within  = elapsed <= _HISTORY_SESSION_WINDOW
            except (ValueError, KeyError):
                within = False
            if within:
                # First edit session right after creation — slide the creation
                # timestamp forward; don't record a separate "Edited" entry.
                # Mark settled so this rule fires only once per note.
                last['at']      = now.strftime('%Y-%m-%dT%H:%M:%S')
                last['settled'] = True
                with open(_history_path(fp), 'w', encoding='utf-8') as f:
                    _json.dump(history, f, ensure_ascii=False)
                return
            # else: created long ago — fall through and append a real edit entry

    history.append({'at': now.strftime('%Y-%m-%dT%H:%M:%S'), 'via': via, 'act': act})
    try:
        with open(_history_path(fp), 'w', encoding='utf-8') as f:
            _json.dump(history, f, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

_SUFFIX_RE = re.compile(r'_[A-Za-z0-9]{8}$')


def _strip_note_suffix(stem: str) -> str:
    """Remove trailing _XXXXXXXX suffix from a note filename stem."""
    return _SUFFIX_RE.sub('', stem)


# The phone app keeps a scratch note called no_name.md as a place to start
# typing before a note has a name.  On the PC it is shown as "New" and marked
# out in the list; while it is still blank there is nothing to show at all.
_PLACEHOLDER_BASE  = 'no_name'
_PLACEHOLDER_TITLE = 'New'


def _is_placeholder_note(stem: str) -> bool:
    """True for the phone app's unnamed scratch note (with or without suffix)."""
    return _strip_note_suffix(stem).lower() == _PLACEHOLDER_BASE


_PLACEHOLDER_TEMPLATE = '# \n\n'


def _ensure_placeholder_note(notes_dir: 'str | None' = None) -> None:
    """Re-arm the scratch note: create a blank no_name.md when none exists.

    Called after the placeholder graduates into a real note, so the standing
    "start typing here" entry is immediately available again — the same thing
    the phone does when its scratch note is promoted."""
    notes_dir = notes_dir or paths.get_notes_dir()
    fp = os.path.join(notes_dir, _PLACEHOLDER_BASE + '.md')
    if os.path.exists(fp):
        return
    try:
        # newline='' keeps the exact LF bytes the phone writes — letting Windows
        # translate to CRLF would make the same blank template differ between
        # devices, and the two would then overwrite each other on every sync.
        with open(fp, 'w', encoding='utf-8', newline='') as f:
            f.write(_PLACEHOLDER_TEMPLATE)
    except OSError:
        pass


def _strip_placeholder_deletions(deletions: list) -> list:
    """Drop any deletion-log entry for the phone's placeholder note.

    The placeholder is a permanent fixture on the phone, not a real note: it is
    emptied and reused rather than created and destroyed.  Recording it as
    deleted makes every device refuse to accept it again, so the phone appears
    to save into it while the note never actually syncs.  Filtering on every
    read/write also heals logs that already carry such an entry, and — because
    the merged log is what gets pushed — removes it from the shared copy."""
    out = []
    for e in deletions:
        if e.get('type') == 'note' and _is_placeholder_note(
                os.path.splitext(str(e.get('path', '')))[0]):
            continue
        out.append(e)
    return out


def _extract_title(filepath: str) -> str:
    """Return text of the first # heading in the note body, or the filename."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return _strip_note_suffix(os.path.splitext(os.path.basename(filepath))[0])
    body = re.sub(r'^---\s*\n[\s\S]*?\n---\s*\n?', '', content).lstrip()
    # [ \t] rather than \s: \s matches newlines, so a note whose heading is
    # still empty ('# ' with nothing after it) would otherwise take its first
    # body line as the title.
    m = re.search(r'^#[ \t]+(.+)', body, re.MULTILINE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return _strip_note_suffix(os.path.splitext(os.path.basename(filepath))[0])


def _note_files():
    return sorted(glob_module.glob(os.path.join(paths.get_notes_dir(), '*.md')))


def _unique_note_path(safe_name: str, notes_dir: str, exclude_fp: str = None):
    """Return (unique_safe_name, filepath) that doesn't conflict with existing files
    or reserved command names.  exclude_fp is the current path of a note being renamed."""
    candidate = safe_name
    i = 1
    while True:
        if candidate.lower() in _RESERVED_NAMES:
            candidate = f'{safe_name}_{i}'
            i += 1
            continue
        fp      = os.path.join(notes_dir, candidate + '.md')
        real_fp = os.path.realpath(fp)
        if not os.path.exists(fp):
            return candidate, fp
        if exclude_fp and real_fp == os.path.realpath(exclude_fp):
            return candidate, fp  # renaming a note to its own name — no conflict
        candidate = f'{safe_name}_{i}'
        i += 1


def _find_note_by_title(title: str):
    """Return the most recently modified non-trashed note whose heading matches title, or None."""
    title_lower = title.strip().lower()
    matches = []
    for fp in _note_files():
        if TRASH_TAG in _read_frontmatter(fp).get('tags', []):
            continue
        if _extract_title(fp).strip().lower() == title_lower:
            matches.append(fp)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _sanitize_filename(name: str) -> str:
    """Keep Unicode letters/digits/combining marks, space, dot, hyphen, underscore.
    Everything else (punctuation, symbols, emoji, control chars) → underscore."""
    import re as _re
    # \w matches Unicode word chars (letters, digits, underscore) in Python 3
    result = _re.sub(r'[^\w .\-]', '_', name)
    result = _re.sub(r'_+', '_', result).strip('_').strip()[:100]
    return result


import random as _random, string as _string

_NOTE_SUFFIX_LEN = 8
_NOTE_SUFFIX_CHARS = _string.ascii_letters + _string.digits


def _random_note_suffix() -> str:
    return ''.join(_random.choices(_NOTE_SUFFIX_CHARS, k=_NOTE_SUFFIX_LEN))


def _unique_suffixed_name(base: str, notes_dir: str) -> str:
    """Return 'base_SUFFIX' where SUFFIX is 8 random alphanumeric chars that
    are not already used on disk or recorded in the deletion log."""
    del_paths = {
        e['path']
        for e in _load_deletion_log(_deletion_log_path(notes_dir))['deletions']
    }
    for _ in range(50):
        suffix = _random_note_suffix()
        full   = f'{base}_{suffix}'
        fp     = os.path.join(notes_dir, full + '.md')
        if not os.path.exists(fp) and (full + '.md') not in del_paths:
            return full
    # Practically unreachable fallback
    return f'{base}_{int(time.time() * 1000) % 100_000_000}'


def _safe_path(name: str) -> str:
    notes_dir = paths.get_notes_dir()
    fp   = os.path.realpath(os.path.join(notes_dir, name + '.md'))
    base = os.path.realpath(notes_dir)
    if not fp.startswith(base + os.sep):
        abort(403)
    return fp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    # cache_bust changes whenever style.css or main.js is saved, forcing the
    # browser to fetch fresh copies instead of serving 304-cached versions.
    _css = os.path.join(paths.STATIC_DIR, 'css', 'style.css')
    _js  = os.path.join(paths.STATIC_DIR, 'js',  'main.js')
    bust = int(max(os.path.getmtime(_css), os.path.getmtime(_js)))
    return render_template('index.html', cache_bust=bust)


@app.route('/api/next-untitled')
def api_next_untitled():
    # Suffix added at creation time makes every note unique — just return base name.
    return jsonify({'name': 'Untitled'})


@app.route('/api/tags')
def api_tags():
    trash_view  = request.args.get('trash')  == '1'
    want_counts = request.args.get('counts') == '1'
    tag_counts: dict = {}
    all_count   = 0
    trash_count = 0
    for fp in _note_files():
        fm         = _read_frontmatter(fp)
        is_trashed = TRASH_TAG in fm.get('tags', [])
        if is_trashed:
            trash_count += 1
        else:
            all_count += 1
        if trash_view != is_trashed:
            continue
        for t in fm.get('tags', []):
            if t != TRASH_TAG:
                tag_counts[t] = tag_counts.get(t, 0) + 1
    tags_sorted = sorted(tag_counts.keys())
    if want_counts:
        return jsonify({
            'tags':        [{'name': t, 'count': tag_counts[t]} for t in tags_sorted],
            'all_count':   all_count,
            'trash_count': trash_count,
        })
    return jsonify(tags_sorted)


@app.route('/api/notes')
def api_notes():
    tag          = request.args.get('tag', '').strip()
    trash_filter = request.args.get('trash_filter', '').strip()
    entries = []
    for fp in _note_files():
        fm         = _read_frontmatter(fp)
        fm_tags    = fm.get('tags', [])
        is_trashed = TRASH_TAG in fm_tags
        if tag == TRASH_TAG:
            if not is_trashed:
                continue
            # Optional secondary tag filter within Trash view
            if trash_filter and trash_filter not in fm_tags:
                continue
        else:
            if is_trashed:
                continue
            if tag and tag not in fm_tags:
                continue
        name = os.path.splitext(os.path.basename(fp))[0]
        # The placeholder is always listed, empty or not — it is the standing
        # "start typing here" note, the same fixture the phone opens into.
        is_placeholder = _is_placeholder_note(name)
        # Prefer history "created" timestamp over st_ctime, which resets on copy
        fs_ctime = os.path.getctime(fp)
        true_ctime = fs_ctime
        for entry in _read_history(fp):
            if entry.get('act') == 'created':
                try:
                    true_ctime = _dtt.strptime(entry['at'], '%Y-%m-%dT%H:%M:%S').timestamp()
                except (ValueError, KeyError):
                    pass
                break
        entries.append({
            'name':        name,
            'title':       _PLACEHOLDER_TITLE if is_placeholder else _extract_title(fp),
            'encrypted':   fm.get('encrypted', False),
            'mtime':       os.path.getmtime(fp),
            'placeholder': is_placeholder,
            '_ctime':      true_ctime,
        })
    sort_by  = request.args.get('sort',  'ctime')   # ctime | alpha | mtime
    sort_desc = request.args.get('order', 'desc') == 'desc'
    if sort_by == 'alpha':
        entries.sort(key=lambda e: e['title'].lower(), reverse=sort_desc)
    elif sort_by == 'mtime':
        entries.sort(key=lambda e: e['mtime'], reverse=sort_desc)
    else:  # ctime (default)
        entries.sort(key=lambda e: e['_ctime'], reverse=sort_desc)
    return jsonify([{k: v for k, v in e.items() if not k.startswith('_')}
                    for e in entries])


@app.route('/api/search')
def api_search():
    q       = request.args.get('q', '').strip().lower()
    tag     = request.args.get('tag', '').strip()
    raw_ul  = request.args.get('unlocked', '').strip()
    unlocked = set(raw_ul.split(',')) if raw_ul else set()
    results = []
    for fp in _note_files():
        fm         = _read_frontmatter(fp)
        fm_tags    = fm.get('tags', [])
        is_trashed = TRASH_TAG in fm_tags
        if tag == TRASH_TAG:
            if not is_trashed:
                continue
        else:
            if is_trashed:
                continue
        name      = os.path.splitext(os.path.basename(fp))[0]
        encrypted = fm.get('encrypted', False)

        is_placeholder = _is_placeholder_note(name)
        title = _PLACEHOLDER_TITLE if is_placeholder else _extract_title(fp)

        # Always match on filename
        if not q or q in name.lower():
            results.append({'name': name, 'title': title, 'encrypted': encrypted,
                            'mtime': os.path.getmtime(fp), 'placeholder': is_placeholder})
            continue

        # Locked encrypted notes: skip body search entirely
        if encrypted and name not in unlocked:
            continue

        try:
            with open(fp, 'r', encoding='utf-8') as f:
                raw = f.read()
            # Strip YAML frontmatter so searches don't match id/tags/etc.
            if raw.startswith('---'):
                end = raw.find('\n---', 3)
                body = raw[end + 4:] if end != -1 else raw
            else:
                body = raw
            if q in body.lower():
                results.append({'name': name, 'title': title, 'encrypted': encrypted,
                                'mtime': os.path.getmtime(fp), 'placeholder': is_placeholder})
        except OSError:
            pass
    return jsonify(results)


@app.route('/api/note/<path:name>')
def api_note(name):
    fp = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    stat = os.stat(fp)
    with open(fp, 'r', encoding='utf-8') as f:
        content = f.read()

    # st_ctime is the filesystem creation time — it resets whenever the file is
    # copied (e.g. pulled from iCloud on a new machine).  Use the timestamp from
    # the first "created" history entry instead, which travels with the sidecar.
    ctime = stat.st_ctime
    for entry in _read_history(fp):
        if entry.get('act') == 'created':
            try:
                ctime = _dtt.strptime(entry['at'], '%Y-%m-%dT%H:%M:%S').timestamp()
            except (ValueError, KeyError):
                pass
            break

    return jsonify({
        'name':    name,
        'content': content,
        'ctime':   ctime,
        'mtime':   stat.st_mtime,
    })


@app.route('/api/note', methods=['POST'])
def api_create_note():
    data      = request.get_json(force=True)
    base_name = (data.get('name') or '').strip()
    content   = data.get('content', '')
    if not base_name or re.search(r'[/\\<>:"|?*]', base_name) or base_name.startswith('.'):
        abort(400)
    if base_name.lower() in _RESERVED_NAMES:
        abort(400)
    notes_dir = paths.get_notes_dir()
    # Always generate a unique random suffix so the filename is globally unique
    # and can never be confused with a deletion-log entry from a previous note
    # with the same title.
    full_name = _unique_suffixed_name(_sanitize_filename(base_name), notes_dir)
    fp = os.path.join(notes_dir, full_name + '.md')
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(content)
    _inject_note_id(fp)
    _append_history(fp, 'created', 'myNote')
    _last_history_content[os.path.realpath(fp)] = content
    return jsonify({'name': full_name}), 201


@app.route('/api/note/<path:name>', methods=['PATCH'])
def api_save_note(name):
    fp      = _safe_path(name)
    real_fp = os.path.realpath(fp)
    if not os.path.isfile(fp):
        abort(404)
    data        = request.get_json(force=True)
    content     = data.get('content', '')
    session_end = bool(data.get('session_end', False))
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(content)
    _inject_note_id(fp)
    # Content typed into the scratch note starts its forced-sync countdown. The
    # editor autosaves every few seconds, so this fires without the user having
    # to save or close anything. Whitespace-only edits don't count as content.
    if _is_placeholder_note(os.path.splitext(os.path.basename(fp))[0]):
        body = re.sub(r'^---\s*\n[\s\S]*?\n---\s*\n?', '', content)
        if any(line.strip().lstrip('#').strip() for line in body.splitlines()):
            _placeholder_mark_dirty()
    if session_end:
        # Compare against content at last history write, not last auto-save, so
        # frequent auto-saves don't mask real changes at session-end time.
        last_hist = _last_history_content.get(real_fp)
        changed   = (last_hist is None) or (content != last_hist)
        if changed:
            # ASCII only: a non-ASCII character here raises UnicodeEncodeError on a
            # console using a legacy codepage (cp1251 et al), which fails the whole
            # save request with a 500 and blocks switching away from the note.
            print(f'[hist] PATCH {name!r} act=changed session_end=True -> history_written=True')
            _append_history(fp, 'changed', 'myNote')
            _last_history_content[real_fp] = content
        else:
            print(f'[hist] PATCH {name!r} session_end=True changed=False -> history_written=False')
    return jsonify({'ok': True})


@app.route('/api/note/<path:name>/history')
def api_note_history(name):
    fp = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    return jsonify(_read_history(fp))


@app.route('/api/note/<path:name>/tags', methods=['GET'])
def api_get_note_tags(name):
    fp = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    tags = [t for t in _read_frontmatter(fp).get('tags', []) if t != TRASH_TAG]
    return jsonify(tags)


@app.route('/api/note/<path:name>/tags', methods=['PUT'])
def api_update_tags(name):
    fp = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    data = request.get_json(force=True)
    new_tags = [str(t).strip() for t in data.get('tags', []) if str(t).strip()]
    # Preserve trash status independently of user edits
    if TRASH_TAG in _read_frontmatter(fp).get('tags', []) and TRASH_TAG not in new_tags:
        new_tags.append(TRASH_TAG)
    _write_tags(fp, new_tags)
    return jsonify({'ok': True})


@app.route('/api/note/<path:name>/trash', methods=['POST'])
def api_trash_note(name):
    fp   = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    # The placeholder note is a fixture the phone reuses, not a document. Trashing
    # it hides the note the phone opens into, and emptying the Trash afterwards
    # records it as deleted, which stops every device accepting it again.
    if _is_placeholder_note(os.path.splitext(os.path.basename(fp))[0]):
        return jsonify({'ok': False, 'error': 'placeholder note cannot be trashed'}), 409
    tags = _read_frontmatter(fp).get('tags', [])
    if TRASH_TAG not in tags:
        tags.append(TRASH_TAG)
    _write_tags(fp, tags)
    _set_trashed_at(fp, time.time())
    return jsonify({'ok': True})


@app.route('/api/note/<path:name>/restore', methods=['POST'])
def api_restore_note(name):
    fp   = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    tags = [t for t in _read_frontmatter(fp).get('tags', []) if t != TRASH_TAG]
    _write_tags(fp, tags)
    _set_trashed_at(fp, None)
    return jsonify({'ok': True})


def _generate_pdf(content: str, name: str, output_path: str) -> None:
    try:
        from fpdf import FPDF
    except ImportError:
        raise RuntimeError('fpdf2 is not installed. Run: pip install fpdf2')

    class NotePDF(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_font('Helvetica', 'I', 7)
            self.set_text_color(180, 180, 180)
            self.cell(0, 8, f'Page {self.page_no()}', align='C')

    pdf = NotePDF()
    pdf.set_margins(25, 25, 25)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()

    # Prefer Windows Arial for Unicode; fall back to built-in Helvetica
    win_arial   = r'C:\Windows\Fonts\arial.ttf'
    win_arialbd = r'C:\Windows\Fonts\arialbd.ttf'
    if os.path.exists(win_arial):
        pdf.add_font('F', '',  win_arial)
        pdf.add_font('F', 'B', win_arialbd if os.path.exists(win_arialbd) else win_arial)
        fam = 'F'
    else:
        fam = 'Helvetica'

    # Safe multi_cell: resets x, skips empty, uses explicit width
    def mc(h, text, **kwargs):
        if not text:
            return
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin, h, text, **kwargs)

    _IMG_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif'}

    # Emoji icons matching the CSS data-ext rules — rendered with Segoe UI Symbol
    _FILE_ICONS = {
        'pdf':  '📄',
        'doc':  '📝', 'docx': '📝',
        'xls':  '📊', 'xlsx': '📊',
        'ppt':  '📊', 'pptx': '📊',
        'zip':  '🗜',  'rar':  '🗜',  '7z':  '🗜',
        'mp4':  '🎬', 'mov':  '🎬', 'avi': '🎬',
        'mp3':  '🎵', 'wav':  '🎵',
        'txt':  '📃',
    }
    # Text-label fallback used when no symbol font is available
    _FILE_LABELS = {
        'pdf':  'PDF',  'doc':  'DOC',  'docx': 'DOCX',
        'xls':  'XLS',  'xlsx': 'XLSX', 'ppt':  'PPT',  'pptx': 'PPTX',
        'zip':  'ZIP',  'rar':  'RAR',  '7z':   '7Z',
        'mp4':  'MP4',  'mov':  'MOV',  'avi':  'AVI',
        'mp3':  'MP3',  'wav':  'WAV',  'txt':  'TXT',
    }

    # Load Segoe UI Symbol (monochrome emoji, always present on Win 10/11)
    _sym_fam = None
    _segsym  = r'C:\Windows\Fonts\seguisym.ttf'
    if os.path.exists(_segsym):
        try:
            pdf.add_font('Sym', '', _segsym)
            _sym_fam = 'Sym'
        except Exception:
            pass

    def _file_icon(filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower().lstrip('.')
        return _FILE_ICONS.get(ext, '📎')

    def _file_label(filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower().lstrip('.')
        lbl = _FILE_LABELS.get(ext, ext.upper() or 'FILE')
        return f'[{lbl}]'

    def _natural_image_width_mm(path: str) -> float | None:
        """Return the image's natural width in mm (at 96 dpi), or None on failure."""
        ext = os.path.splitext(path)[1].lower()
        px_w = None
        try:
            if ext == '.png':
                with open(path, 'rb') as f:
                    f.read(16)                          # 8-byte sig + 4-byte length + 4-byte 'IHDR'
                    px_w = int.from_bytes(f.read(4), 'big')
            elif ext in ('.jpg', '.jpeg'):
                with open(path, 'rb') as f:
                    data = f.read()
                i = 2
                while i + 4 < len(data):
                    if data[i] != 0xFF:
                        break
                    marker = data[i + 1]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                  0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        px_w = int.from_bytes(data[i + 7:i + 9], 'big')
                        break
                    seg_len = int.from_bytes(data[i + 2:i + 4], 'big')
                    i += 2 + seg_len
        except Exception:
            pass
        if px_w:
            return px_w * 25.4 / 96  # 96 dpi → mm
        return None

    def try_embed_image(url: str) -> bool:
        """Embed a /attachments/<file> image directly into the PDF. Returns True on success."""
        m = re.match(r'^/attachments/(.+)$', url)
        if not m:
            return False
        filename = m.group(1)
        if os.path.splitext(filename)[1].lower() not in _IMG_EXTS:
            return False
        img_path = os.path.join(paths.get_attachments_dir(), filename)
        if not os.path.exists(img_path):
            return False
        try:
            usable_w = pdf.w - pdf.l_margin - pdf.r_margin
            nat_w    = _natural_image_width_mm(img_path)
            w        = min(nat_w, usable_w) if nat_w else usable_w * 0.5
            pdf.set_x(pdf.l_margin)
            pdf.image(img_path, x=pdf.l_margin, w=w)
            pdf.ln(2)
            return True
        except Exception:
            return False

    def _write_file_link(link_text: str, url: str, h: int) -> None:
        """Write an attachment link: icon (symbol font or text label) + filename."""
        fname = url.split('/')[-1]
        pdf.set_x(pdf.l_margin)
        if _sym_fam:
            icon = _file_icon(fname)
            pdf.set_font(_sym_fam, '', 11)
            pdf.write(h, icon + ' ')
        else:
            label = _file_label(fname)
            pdf.set_font(fam, 'B', 9)
            pdf.write(h, label + ' ')
        pdf.set_font(fam, '', 11)
        pdf.write(h, link_text)
        pdf.ln(h)

    def render_inline(text: str, h: int = 6) -> None:
        """Render a text span that may contain ![img]() and [link]() tokens."""
        # Split on image tokens first so we can embed them
        parts = re.split(r'(!\[.*?\]\([^)]*\))', text)
        for part in parts:
            img_m = re.match(r'!\[(.*?)\]\(([^)]*)\)', part)
            if img_m:
                url = img_m.group(2)
                if not try_embed_image(url):
                    alt = img_m.group(1)
                    if alt:
                        pdf.set_font(fam, 'I', 9)
                        pdf.set_text_color(150, 150, 150)
                        mc(5, f'[Image: {alt}]')
                        pdf.set_text_color(0, 0, 0)
            else:
                # Split again on link tokens to handle each one individually
                link_parts = re.split(r'(\[.+?\]\([^)]*\))', part)
                for lpart in link_parts:
                    link_m = re.match(r'\[(.+?)\]\(([^)]*)\)', lpart)
                    if link_m:
                        ltext, lurl = link_m.group(1), link_m.group(2)
                        if lurl.startswith('/attachments/'):
                            _write_file_link(ltext, lurl, h)
                        else:
                            pdf.set_font(fam, '', 11)
                            mc(h, ltext)
                    else:
                        chunk = lpart.strip()
                        if chunk:
                            pdf.set_font(fam, '', 11)
                            mc(h, chunk)

    def strip_inline(text: str) -> str:
        """Strip markdown formatting for headings / list items (no image embedding)."""
        text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text)
        text = re.sub(r'\*\*(.+?)\*\*',     r'\1', text)
        text = re.sub(r'\*(.+?)\*',         r'\1', text)
        text = re.sub(r'`(.+?)`',           r'\1', text)
        text = re.sub(r'!\[.*?\]\([^)]*\)', '',    text)
        text = re.sub(r'\[(.+?)\]\([^)]*\)',r'\1', text)
        return text.strip()

    def _render_table(rows: list) -> None:
        """Render a list of markdown table row strings as a bordered PDF grid."""
        def parse_cells(r: str) -> list:
            r = r.strip()
            if r.startswith('|'): r = r[1:]
            if r.endswith('|'):   r = r[:-1]
            return [c.strip() for c in r.split('|')]

        parsed = []
        for row in rows:
            cells = parse_cells(row)
            # skip separator rows (only dashes / colons / spaces)
            if cells and all(re.match(r'^[-: ]+$', c) for c in cells):
                continue
            parsed.append(cells)

        if not parsed:
            return

        col_n = max(len(r) for r in parsed)
        if col_n == 0:
            return

        usable_w = pdf.w - pdf.l_margin - pdf.r_margin
        col_w    = usable_w / col_n
        row_h    = 6

        for ri, cells in enumerate(parsed):
            while len(cells) < col_n:
                cells.append('')
            is_hdr = (ri == 0)
            pdf.set_x(pdf.l_margin)
            if is_hdr:
                pdf.set_font(fam, 'B', 9)
                pdf.set_fill_color(220, 220, 220)
            else:
                pdf.set_font(fam, '', 9)
                pdf.set_fill_color(248, 248, 248) if ri % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            for cell in cells[:col_n]:
                pdf.cell(col_w, row_h, strip_inline(cell), border=1, fill=True)
            pdf.ln()

        pdf.set_fill_color(255, 255, 255)
        pdf.ln(3)

    # Strip YAML frontmatter before rendering
    content = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, flags=re.DOTALL)

    in_code      = False
    table_buffer: list = []

    def flush_table() -> None:
        if table_buffer:
            _render_table(table_buffer[:])
            table_buffer.clear()

    for raw in content.split('\n'):
        line = raw.rstrip()

        if line.startswith('```'):
            flush_table()
            in_code = not in_code
            continue

        if in_code:
            pdf.set_font('Courier', '', 9)
            pdf.set_fill_color(240, 240, 240)
            mc(5, line or ' ', fill=True)
            pdf.set_fill_color(255, 255, 255)
            continue

        if line.startswith('|'):
            table_buffer.append(line)
            continue

        flush_table()

        if not line:
            pdf.ln(3)
            continue

        if line.startswith('# '):
            pdf.set_font(fam, 'B', 18); mc(10, strip_inline(line[2:])); pdf.ln(2)
        elif line.startswith('## '):
            pdf.set_font(fam, 'B', 14); mc(8, strip_inline(line[3:])); pdf.ln(1)
        elif line.startswith('### '):
            pdf.set_font(fam, 'B', 12); mc(7, strip_inline(line[4:]))
        elif re.match(r'^[-*+] ', line):
            pdf.set_font(fam, '', 11); mc(6, '•  ' + strip_inline(line[2:]))
        elif re.match(r'^\d+\. ', line):
            pdf.set_font(fam, '', 11); mc(6, strip_inline(line))
        elif re.match(r'^>+ ?', line):
            pdf.set_font(fam, 'I', 11)
            pdf.set_text_color(120, 120, 120)
            mc(6, strip_inline(re.sub(r'^>+ ?', '', line)))
            pdf.set_text_color(0, 0, 0)
        elif re.match(r'^[-*]{3,}$', line):
            pdf.set_x(pdf.l_margin)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(4)
        else:
            # Plain text — strip bold/italic/code, then let render_inline handle images & links
            text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', line)
            text = re.sub(r'\*\*(.+?)\*\*',     r'\1', text)
            text = re.sub(r'\*(.+?)\*',         r'\1', text)
            text = re.sub(r'`(.+?)`',           r'\1', text)
            render_inline(text)

    flush_table()
    pdf.output(output_path)


@app.route('/api/export-pdf', methods=['POST'])
def export_pdf():
    data    = request.get_json(force=True)
    content = data.get('content', '')
    name    = (data.get('name') or 'Note').strip()

    downloads = os.path.join(os.path.expanduser('~'), 'Downloads')
    os.makedirs(downloads, exist_ok=True)

    safe = re.sub(r'[/\\<>:"|?*]', '', name) or 'Note'
    out  = os.path.join(downloads, safe + '.pdf')
    i    = 1
    while os.path.exists(out):
        out = os.path.join(downloads, f'{safe}_{i}.pdf')
        i  += 1

    try:
        _generate_pdf(content, name, out)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 503

    return jsonify({'path': out, 'filename': os.path.basename(out)})


# ---------------------------------------------------------------------------
# Share-as-HTML
# ---------------------------------------------------------------------------

_SHARE_CSS = """\
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #f8f7f4; --text: #1c1b18; --text-muted: #71706c;
  --border: #e3e0d9; --accent: #d4843e;
  --accent-dim: rgba(212,132,62,.12); --code-bg: #f0ede7; --r: 7px;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 15px; line-height: 1.65; color: var(--text);
  background: var(--bg); padding: 0;
  display: flex; flex-direction: column; min-height: 100vh;
}
.wrapper { max-width: 760px; width: 100%; margin: 0 auto; padding: 40px 24px 40px; flex: 1; }
.note-header { padding-bottom: 20px; border-bottom: 1px solid var(--border); margin-bottom: 28px; }
.note-title { font-size: 1.7em; font-weight: 700; line-height: 1.25; margin-bottom: 10px; }
.note-meta { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }
.note-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.note-tag { display: inline-block; background: var(--accent-dim); border: 1px solid rgba(212,132,62,.3); color: var(--accent); border-radius: 4px; padding: 1px 9px; font-size: 11px; font-weight: 500; }
.note-body { }
.note-body h1 { font-size: 1.65em; font-weight: 700; margin: 1.4em 0 .45em; line-height: 1.25; }
.note-body h2 { font-size: 1.35em; font-weight: 700; margin: 1.3em 0 .4em; line-height: 1.3; }
.note-body h3 { font-size: 1.15em; font-weight: 600; margin: 1.2em 0 .35em; }
.note-body h4, .note-body h5, .note-body h6 { font-size: 1em; font-weight: 600; margin: 1.1em 0 .3em; }
.note-body p { margin: .65em 0; }
.note-body a { color: var(--accent); text-decoration: none; }
.note-body a:hover { text-decoration: underline; }
.note-body img { max-width: 100%; height: auto; border-radius: var(--r); display: block; margin: .8em 0; }
.note-body ul, .note-body ol { padding-left: 1.4em; margin: .6em 0; }
.note-body li { margin-bottom: .25em; }
.note-body li > p { margin: .25em 0; }
.note-body pre { background: var(--code-bg); border-radius: var(--r); padding: 14px 16px; overflow-x: auto; font-size: 13px; margin: .8em 0; line-height: 1.55; }
.note-body code { font-family: "SF Mono","Cascadia Code","Fira Code",Consolas,monospace; font-size: .875em; }
.note-body p code, .note-body li code { background: var(--code-bg); border-radius: 4px; padding: 1px 6px; }
.note-body blockquote { border-left: 3px solid var(--accent); background: var(--accent-dim); border-radius: 0 var(--r) var(--r) 0; padding: 8px 16px; margin: .8em 0; color: var(--text-muted); }
.note-body blockquote p { margin: .3em 0; }
.note-body table { width: 100%; border-collapse: collapse; margin: .9em 0; font-size: 13.5px; }
.note-body th, .note-body td { border: 1px solid var(--border); padding: 7px 12px; text-align: left; }
.note-body th { background: var(--code-bg); font-weight: 600; }
.note-body tr:nth-child(even) { background: rgba(0,0,0,.022); }
.note-body hr { border: none; border-top: 1px solid var(--border); margin: 1.4em 0; }
.note-media-wrap { margin: 14px 0; display: flex; flex-direction: column; gap: 5px; max-width: 100%; }
.note-media-video { max-width: 100%; max-height: 420px; border-radius: var(--r); background: #000; box-shadow: 0 2px 10px rgba(0,0,0,.18); display: block; }
.note-media-wrap--audio { max-width: 480px; width: 100%; }
.note-media-audio { width: 100%; height: 36px; display: block; accent-color: var(--accent); }
.note-media-label { font-size: 11px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.note-footer { width: 100%; padding: 16px 24px; border-top: 1px solid var(--border); font-size: 11px; color: var(--text-muted); text-align: center; }
.note-footer strong { color: var(--accent); }
.skipped-notice { background: #fff8ee; border: 1px solid #f0d090; border-radius: var(--r); padding: 10px 14px; margin-bottom: 20px; font-size: 12px; color: #7a5a10; }
.skipped-notice ul { margin: 4px 0 0 16px; }
"""

# JS is split into three parts so md_json and attach_map_json can be safely
# concatenated without risking template-injection from user content.
# The approach keeps data URIs OUT of the markdown string, so marked.js never
# has to scan a multi-megabyte base64 blob as text (which causes stack overflow).
_SHARE_JS_BEFORE = '(function() {\n  marked.use({ breaks: true, gfm: true });\n  var md = '

_SHARE_JS_MIDDLE = ';\n  var _attachMap = '

# Raw string so \b in the JS regex stays as two chars, not a backspace.
_SHARE_JS_AFTER = r"""
;
  // 1. Parse markdown normally — no data URIs inside, so marked never chokes.
  var html = marked.parse(md);

  // 2. Swap every /attachments/... src/href in the rendered HTML string with
  //    its embedded data URI before the string ever reaches the DOM
  //    (avoids the browser firing blocked file:// requests).
  html = html.replace(
    /\b(src|href)="(\/attachments\/[^"]+)"/g,
    function(match, attr, url) {
      var uri = _attachMap[url] || _attachMap[decodeURIComponent(url)];
      return uri ? (attr + '="' + uri + '"') : match;
    }
  );
  document.getElementById('note-body').innerHTML = html;

  // 3. Turn data-URI <a> links into inline video/audio players.
  ['video', 'audio'].forEach(function(tag) {
    document.querySelectorAll(
      '#note-body a[href^="data:' + tag + '/"]'
    ).forEach(function(a) {
      var el = document.createElement(tag);
      el.src = a.href;
      el.controls = true;
      el.preload = 'metadata';
      el.className = 'note-media-' + tag;
      var wrap = document.createElement('div');
      wrap.className = 'note-media-wrap' +
        (tag === 'audio' ? ' note-media-wrap--audio' : '');
      var lbl = document.createElement('div');
      lbl.className = 'note-media-label';
      lbl.textContent = a.textContent;
      if (tag === 'audio') { wrap.appendChild(lbl); wrap.appendChild(el); }
      else                 { wrap.appendChild(el);  wrap.appendChild(lbl); }
      a.replaceWith(wrap);
    });
  });
})();
"""


def _build_share_html(title_esc, created, modified, tags_html,
                      skipped_html, body_html):
    """Assemble a self-contained shareable HTML string.
    body_html is pre-rendered HTML — no JavaScript needed to display content."""
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>' + title_esc + '</title>\n'
        '<style>\n' + _SHARE_CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="wrapper">\n'
        '  <header class="note-header">\n'
        '    <div class="note-title">' + title_esc + '</div>\n'
        '    <div class="note-meta">\n'
        '      <span>Created: ' + created + '</span>\n'
        '      <span>Last modified: ' + modified + '</span>\n'
        '    </div>\n'
        '    ' + tags_html + '\n'
        '  </header>\n'
        + ('  ' + skipped_html + '\n' if skipped_html else '') +
        '  <div class="note-body" id="note-body">\n'
        + body_html +
        '\n  </div>\n'
        '</div>\n'
        '<footer class="note-footer">Shared with <strong>myNote</strong></footer>\n'
        '</body>\n'
        '</html>\n'
    )


@app.route('/api/share', methods=['POST'])
def api_share():
    """Generate a self-contained shareable HTML file and save to Downloads."""
    import mimetypes as _mimetypes
    from html import escape as _he
    from datetime import datetime as _dt

    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        abort(400)

    fp = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)

    with open(fp, 'r', encoding='utf-8') as f:
        raw = f.read()

    fm = _read_frontmatter(fp)
    if fm.get('encrypted'):
        return jsonify({'error': 'Cannot share encrypted notes'}), 403

    title  = _extract_title(fp)
    tags   = [t for t in fm.get('tags', []) if t != TRASH_TAG]
    stat   = os.stat(fp)

    def _fmt(ts):
        return _dt.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')

    # Strip frontmatter to get body
    body_m = re.match(r'^---\s*\n[\s\S]*?\n---\s*\n?', raw)
    body = raw[body_m.end():] if body_m else raw

    # Remove the leading # H1 heading — it is already shown in the share header
    body = re.sub(r'^[ \t]*#[^#\n][^\n]*\n?', '', body.lstrip('\n'), count=1).lstrip('\n')

    # Two goals in one pass:
    #   1. URL-encode filenames so marked.js can parse links with spaces/& chars.
    #   2. Build data_map {encoded_url: data_uri} kept OUTSIDE the markdown so
    #      marked.js never scans a multi-MB base64 blob (prevents stack overflow).
    attach_dir = paths.get_attachments_dir()
    MAX_EMBED  = 50 * 1024 * 1024   # 50 MB per file

    ATTACH_PAT = re.compile(
        r'\]\((\/attachments\/[^/\n]+\/)((?:[^()\n]+|\([^()\n]*\))*)\)'
    )
    data_map = {}   # encoded_url -> data_uri  (injected as JS object, not in markdown)
    skipped  = []

    def _process(m):
        prefix   = m.group(1)   # e.g. "/attachments/audio/"
        raw_name = m.group(2)   # filename, possibly URL-encoded or raw
        try:
            decoded = _urlparse.unquote(raw_name)
        except Exception:
            decoded = raw_name

        # Percent-encode so marked.js treats it as a valid URL (no spaces etc.)
        encoded_name = _urlparse.quote(decoded, safe='')
        encoded_url  = prefix + encoded_name   # used as key in JS _attachMap

        # Load file and store data URI in data_map (NOT embedded in markdown)
        subdir    = prefix.lstrip('/')[len('attachments/'):].rstrip('/')
        file_path = os.path.join(attach_dir, subdir, decoded)
        if os.path.isfile(file_path):
            sz = os.path.getsize(file_path)
            if sz <= MAX_EMBED:
                mime_type = _mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
                with open(file_path, 'rb') as f:
                    b64_data = base64.b64encode(f.read()).decode('ascii')
                data_map[encoded_url] = 'data:' + mime_type + ';base64,' + b64_data
            else:
                skipped.append(decoded)

        return '](' + encoded_url + ')'

    body_processed = ATTACH_PAT.sub(_process, body)

    # Metadata HTML
    title_esc = _he(title)
    tags_html = ''
    if tags:
        items = ''.join('<span class="note-tag">' + _he(t) + '</span>' for t in tags)
        tags_html = '<div class="note-tags">' + items + '</div>'

    skipped_html = ''
    if skipped:
        items = ''.join('<li>' + _he(f) + '</li>' for f in skipped)
        skipped_html = (
            '<div class="skipped-notice">'
            '<strong>⚠ Some files were too large to embed (&gt;50 MB):</strong>'
            '<ul>' + items + '</ul></div>'
        )

    # Render markdown to HTML server-side so content displays on any viewer
    # (mobile share-sheet viewers and Quick Look don't execute JavaScript).
    import markdown as _md_pkg
    body_html = _md_pkg.markdown(
        body_processed,
        extensions=['fenced_code', 'tables', 'nl2br'],
    )

    # Substitute /attachments/... paths with embedded data URIs
    def _sub_attach(m):
        attr = m.group(1)
        url  = m.group(2)
        uri  = data_map.get(url) or data_map.get(_urlparse.unquote(url))
        return (attr + '="' + uri + '"') if uri else m.group(0)

    body_html = re.sub(r'(src|href)="(/attachments/[^"]*)"', _sub_attach, body_html)

    # Upgrade data-URI <a> links to inline <video>/<audio> players
    def _upgrade_media(m):
        href, text = m.group(1), m.group(2)
        if href.startswith('data:video/'):
            return (
                '<div class="note-media-wrap">'
                '<video class="note-media-video" src="' + href + '" controls preload="metadata"></video>'
                '<div class="note-media-label">' + text + '</div>'
                '</div>'
            )
        if href.startswith('data:audio/'):
            return (
                '<div class="note-media-wrap note-media-wrap--audio">'
                '<div class="note-media-label">' + text + '</div>'
                '<audio class="note-media-audio" src="' + href + '" controls preload="metadata"></audio>'
                '</div>'
            )
        return m.group(0)

    body_html = re.sub(r'<a href="(data:[^"]+)">([^<]*)</a>', _upgrade_media, body_html)

    html_out = _build_share_html(
        title_esc  = title_esc,
        created    = _fmt(stat.st_ctime),
        modified   = _fmt(stat.st_mtime),
        tags_html  = tags_html,
        skipped_html = skipped_html,
        body_html  = body_html,
    )

    # Save to ~/Downloads
    downloads = os.path.join(os.path.expanduser('~'), 'Downloads')
    os.makedirs(downloads, exist_ok=True)
    safe_name = re.sub(r'[/\\<>:"|?*]', '', name) or 'Note'
    out = os.path.join(downloads, safe_name + '_share.html')
    i = 1
    while os.path.exists(out):
        out = os.path.join(downloads, safe_name + '_share_' + str(i) + '.html')
        i += 1

    with open(out, 'w', encoding='utf-8') as f:
        f.write(html_out)

    return jsonify({'path': out, 'filename': os.path.basename(out)})


@app.route('/api/reveal-file', methods=['POST'])
def reveal_file():
    data = request.get_json(force=True)
    path = os.path.normpath(data.get('path', ''))
    if not os.path.isfile(path):
        abort(404)
    # Shell-quote the path so spaces in folder/file names don't split the argument
    subprocess.Popen(f'explorer.exe /select,"{path}"', shell=True)
    return jsonify({'ok': True})


@app.route('/api/locked-notes')
def api_locked_notes():
    result = []
    for fp in _note_files():
        fm = _read_frontmatter(fp)
        if fm.get('encrypted'):
            name = os.path.splitext(os.path.basename(fp))[0]
            result.append({'name': name, 'title': _extract_title(fp)})
    return jsonify(result)


@app.route('/api/settings/workfolder', methods=['GET'])
def get_workfolder():
    path = paths._load_config().get('notes_dir')
    return jsonify({'path': path or '', 'configured': path is not None})


def _resolve_notes_folder(selected: str) -> str:
    """
    Normalise the user-supplied path to always end in a 'myNotes' folder:
      • If the selected folder is already named 'myNotes' → use it as-is.
      • Otherwise → use (or create) selected/myNotes.
    """
    if os.path.basename(os.path.normpath(selected)) == 'myNotes':
        return selected
    return os.path.join(selected, 'myNotes')


def _apply_folder_icon(folder: str) -> None:
    """
    Give the notes folder a custom Windows icon via desktop.ini.
    Copies myNote.ico into the folder as a hidden file and writes a
    hidden+system desktop.ini so Explorer picks it up.
    Silently skips on any error (non-critical cosmetic feature).
    """
    try:
        src_ico = os.path.join(paths.STATIC_DIR, 'images', 'myNote.ico')
        if not os.path.isfile(src_ico):
            return

        dst_ico  = os.path.join(folder, 'folder.ico')
        ini_path = os.path.join(folder, 'desktop.ini')

        # (Re-)copy the icon so it travels with the folder
        shutil.copy2(src_ico, dst_ico)

        # Write desktop.ini (CRLF required by Windows shell parser)
        ini_text = (
            '[.ShellClassInfo]\r\n'
            'IconResource=folder.ico,0\r\n'
            '[ViewState]\r\n'
            'Mode=\r\n'
            'Vid=\r\n'
            'FolderType=Generic\r\n'
        )
        # Clear any existing read-only/system flags before writing
        if os.path.exists(ini_path):
            ctypes.windll.kernel32.SetFileAttributesW(ini_path, 0x80)  # NORMAL
        with open(ini_path, 'w', encoding='utf-8', newline='') as fh:
            fh.write(ini_text)

        # Attributes: desktop.ini → hidden + system; folder.ico → hidden
        #             folder itself → read-only (tells Explorer to honour desktop.ini)
        FILE_HIDDEN   = 0x02
        FILE_SYSTEM   = 0x04
        FILE_READONLY = 0x01
        ctypes.windll.kernel32.SetFileAttributesW(ini_path, FILE_HIDDEN | FILE_SYSTEM)
        ctypes.windll.kernel32.SetFileAttributesW(dst_ico,  FILE_HIDDEN)
        ctypes.windll.kernel32.SetFileAttributesW(folder,   FILE_READONLY)
    except Exception:
        pass   # cosmetic — never crash the save operation


@app.route('/api/settings/workfolder', methods=['POST'])
def set_workfolder():
    data     = request.get_json(force=True)
    selected = (data.get('path', '') or '').strip()
    if not selected:
        return jsonify({'error': 'Path is required.'}), 400
    try:
        folder = _resolve_notes_folder(selected)
        # Create the notes folder and all attachment subfolders
        os.makedirs(folder, exist_ok=True)
        for sub in ('images', 'documents', 'archives', 'videos', 'audio'):
            os.makedirs(os.path.join(folder, 'attachments', sub), exist_ok=True)
        _apply_folder_icon(folder)
        paths.set_notes_dir(folder)
        return jsonify({'ok': True, 'path': folder})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/settings', methods=['GET'])
def get_settings():
    cfg = paths._load_config()
    s   = cfg.get('settings', {})
    return jsonify({
        'email':            s.get('email', ''),
        'emailPasswordSet': bool(s.get('email_password', '')),
        'sort_by':          s.get('sort_by',   'ctime'),
        'sort_desc':        s.get('sort_desc',  True),
    })


@app.route('/api/settings', methods=['POST'])
def save_settings():
    data  = request.get_json(force=True)
    cfg   = paths._load_config()
    if 'settings' not in cfg:
        cfg['settings'] = {}
    if 'email' in data:
        cfg['settings']['email'] = str(data['email']).strip()
    pwd = data.get('emailPassword', '')
    if pwd:
        cfg['settings']['email_password'] = base64.b64encode(pwd.encode()).decode()
    paths._save_config(cfg)
    return jsonify({'ok': True})


@app.route('/api/prefs', methods=['POST'])
def save_prefs():
    """Save arbitrary UI preferences into config[settings]."""
    data = request.get_json(force=True) or {}
    cfg  = paths._load_config()
    s    = cfg.setdefault('settings', {})
    for key, val in data.items():
        s[key] = val
    paths._save_config(cfg)
    return jsonify({'ok': True})


@app.route('/api/note/<path:name>/rename', methods=['POST'])
def api_rename_note(name):
    fp = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    data     = request.get_json(force=True)
    new_name = _sanitize_filename(data.get('name', ''))
    if not new_name:
        abort(400)
    notes_dir = paths.get_notes_dir()
    base      = os.path.realpath(notes_dir)
    # Naming the scratch note graduates it into a real note: it takes the same
    # Title_XXXXXXXX shape as one made with the New button, and no_name.md is
    # freed afterwards so there is always somewhere to start typing.
    graduating = _is_placeholder_note(os.path.splitext(os.path.basename(fp))[0])
    if graduating:
        new_name = _unique_suffixed_name(new_name, notes_dir)
        new_fp   = os.path.join(notes_dir, new_name + '.md')
    else:
        # Auto-increment name if the target filename already exists (and isn't the same file)
        new_name, new_fp = _unique_note_path(new_name, notes_dir, exclude_fp=fp)
    if not new_fp.startswith(base + os.sep):
        abort(403)
    if os.path.realpath(new_fp) != os.path.realpath(fp):
        old_real = os.path.realpath(fp)
        os.rename(fp, new_fp)
        old_hp = _history_path(fp)
        if os.path.exists(old_hp):
            os.rename(old_hp, _history_path(new_fp))
        # Keep _last_history_content key in sync with the new path.
        if old_real in _last_history_content:
            _last_history_content[os.path.realpath(new_fp)] = _last_history_content.pop(old_real)
        if graduating:
            _ensure_placeholder_note(notes_dir)
    return jsonify({'name': new_name, 'graduated': graduating})


@app.route('/attachments/<path:filename>')
def serve_attachment(filename):
    return send_from_directory(paths.get_attachments_dir(), filename)


@app.route('/api/attachment/upload', methods=['POST'])
def upload_attachment():
    if 'file' not in request.files:
        abort(400)
    f        = request.files['file']
    raw_name = os.path.basename(f.filename or '') or 'attachment'
    name     = _sanitize_filename(raw_name) or 'attachment'
    base_n, ext = os.path.splitext(name)
    sub      = _attachment_subfolder(name)
    sub_dir  = _ensure_attach_subdir(sub)
    filename = name
    fp       = os.path.join(sub_dir, filename)
    i = 1
    while os.path.exists(fp):
        filename = f'{base_n}_{i}{ext}'
        fp       = os.path.join(sub_dir, filename)
        i += 1
    f.save(fp)
    rel = f'{sub}/{filename}'
    return jsonify({'filename': rel, 'url': f'/attachments/{rel}'})


@app.route('/api/attachment/open', methods=['POST'])
def open_attachment():
    data       = request.get_json(force=True)
    raw        = (data.get('filename', '') or '').replace('\\', '/')
    # Accept "subfolder/name" or bare "name"; sanitize each path component
    parts      = [_sanitize_filename(p) for p in raw.split('/') if p and p != '..']
    parts      = [p for p in parts if p]
    if not parts:
        abort(400)
    attach_dir = paths.get_attachments_dir()
    fp   = os.path.realpath(os.path.join(attach_dir, *parts))
    base = os.path.realpath(attach_dir)
    if not fp.startswith(base + os.sep):
        abort(403)
    if not os.path.isfile(fp):
        abort(404)
    os.startfile(fp)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Telegram user API (MTProto via Telethon) → notes ingestion
# ---------------------------------------------------------------------------
# Requires:  pip install telethon
# One-time setup in Settings → Telegram:
#   1. Enter API ID + API Hash from https://my.telegram.org/apps
#   2. Enter your phone number → click Send Code
#   3. Enter the code Telegram sends you (+ 2FA password if enabled)
#   4. Enter the group/channel username or numeric ID → Save
# ---------------------------------------------------------------------------

def _tg_ts_title() -> str:
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M')

def _tg_ts_file() -> str:
    from datetime import datetime
    return datetime.now().strftime('%Y%m%d_%H%M%S')

_TG_DIVIDER = '\n\n---\n\n'

def _tg_create_note(title: str, body: str) -> None:
    """Append to the most recent non-trashed note whose heading matches title,
    or create a new note with a unique name if none is found."""
    notes_dir = paths.get_notes_dir()

    # Search all non-trashed notes by heading (handles renames and name collisions)
    existing_fp = _find_note_by_title(title)

    if existing_fp:
        note_name = os.path.basename(existing_fp)
        if body.strip():
            with open(existing_fp, 'r', encoding='utf-8') as f:
                existing = f.read()
            with open(existing_fp, 'w', encoding='utf-8') as f:
                f.write(existing.rstrip() + _TG_DIVIDER + body.strip())
            _append_history(existing_fp, 'added', 'Telegram')
            _tg_log.info('  appended to existing note: %r', note_name)
            _sse_broadcast(_json.dumps({'type': 'notes-updated', 'name': note_name, 'appended': True}))
        else:
            _tg_log.debug('  skipped append — no body content')
    else:
        # No matching active note — create a new one with a unique filename
        safe             = _sanitize_filename(title) or f'tg_{_tg_ts_file()}'
        _, new_fp        = _unique_note_path(safe, notes_dir)
        note_name        = os.path.basename(new_fp)
        fm      = '---\nid: ' + str(_uuid_mod.uuid4()) + '\ntags: [Telegram]\n---\n'
        content = fm + f'# {title}\n' + (('\n' + body.strip()) if body.strip() else '')
        with open(new_fp, 'w', encoding='utf-8') as f:
            f.write(content)
        _append_history(new_fp, 'created', 'Telegram')
        _tg_log.info('  new note created: %r', note_name)
        _sse_broadcast(_json.dumps({'type': 'notes-updated', 'name': note_name, 'appended': False}))

# ── Telegram logger (prints to Flask console with [TG] prefix) ───────────────
_tg_log = logging.getLogger('myNote.telegram')

# ── SSE (Server-Sent Events) bus ─────────────────────────────────────────────
_sse_clients     = []          # list[queue.Queue]  — one per open browser tab
_sse_clients_lck = threading.Lock()

def _sse_broadcast(data: str) -> None:
    """Push a JSON string to every listening browser tab."""
    with _sse_clients_lck:
        for q in list(_sse_clients):
            try:
                q.put_nowait(data)
            except _queue_mod.Full:
                pass  # slow client; drop rather than block

# Startup notifications arrive before any browser tab has opened /api/events.
# Store the latest one and replay it to the next subscriber within 90 s.
_startup_notify = {'data': None, 'at': 0.0}

def _sse_notify(msg: str, level: str = 'warn') -> None:
    data = _json.dumps({'type': 'notify', 'level': level, 'msg': msg})
    _startup_notify['data'] = data
    _startup_notify['at']   = time.time()
    _sse_broadcast(data)   # also push to any already-open tabs

def _sse_auto_sync(source: str, phase: str, ok: bool = None, error: str = None,
                   to_remote: int = 0, to_local: int = 0) -> None:
    """Notify open tabs that a cron-triggered (background) sync started/ended,
    so the UI can spin the sync icon for syncs it didn't initiate itself."""
    payload = {'type': 'auto_sync', 'source': source, 'phase': phase}
    if ok is not None:
        payload['ok'] = ok
    if error is not None:
        payload['error'] = error
    if phase == 'end':
        payload['toRemote'] = to_remote
        payload['toLocal']  = to_local
    _sse_broadcast(_json.dumps(payload))
_tg_log.setLevel(logging.DEBUG)
if not _tg_log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        '%(asctime)s [TG] %(levelname)-5s  %(message)s', '%H:%M:%S'))
    _tg_log.addHandler(_h)
_tg_log.propagate = False


def _tg_make_client(session_str: str, api_id: int, api_hash: str):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise RuntimeError('telethon is not installed. Run: pip install telethon')
    return TelegramClient(StringSession(session_str or ''), api_id, api_hash)

def _tg_run(coro):
    """Run an async coroutine synchronously in a fresh event loop."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ── Telegram command helpers ───────────────────────────────────────────────────

_TG_CMD_GET      = re.compile(r'^get\s+(.+)$',  re.IGNORECASE)
_TG_CMD_LIST     = re.compile(r'^list$',        re.IGNORECASE)
_RESERVED_NAMES  = frozenset({'list', 'get'})   # note names that clash with Telegram commands
_TG_MAX_SEND  = 4000    # stay safely under Telegram's 4096-char hard limit
# Invisible marker (U+200C ZERO WIDTH NON-JOINER) prepended to every reply we send.
# Lets us skip our own bot replies on re-poll without blocking the user's own commands.
_TG_REPLY_MARK = '‌'  # U+200C ZERO WIDTH NON-JOINER — invisible in Telegram UI

# Claim-reply format: sent as a reply to the original message after downloading it.
# Other myNote instances seeing this in their poll batch will skip that message.
_TG_CLAIM_PREFIX = _TG_REPLY_MARK + '[myNote-claim:'
_TG_CLAIM_SUFFIX = ']'

def _tg_extract_claim_email(text: str) -> 'str | None':
    """Return the owner e-mail if text is a claim marker, else None."""
    if text.startswith(_TG_CLAIM_PREFIX) and text.endswith(_TG_CLAIM_SUFFIX):
        return text[len(_TG_CLAIM_PREFIX):-len(_TG_CLAIM_SUFFIX)]
    return None


def _tg_find_note(search: str) -> 'tuple[str, str] | None':
    """Find the single best-matching non-trashed note by name/title.

    Matching priority:
      1. Exact match (name or title equals query) — returned immediately.
      2. Best partial match by coverage score (len(query) / len(field)).
         When multiple notes partially match, the highest-scoring one wins.
    Returns (filepath, display_title) or None.
    """
    q = search.lower().strip()
    if not q:
        return None
    best: 'tuple[str, str] | None' = None
    best_score = -1.0
    for fp in _note_files():
        if TRASH_TAG in _read_frontmatter(fp).get('tags', []):
            continue
        name  = os.path.splitext(os.path.basename(fp))[0]
        title = (_extract_title(fp) or '').strip()
        nl    = name.lower()
        tl    = title.lower()
        if nl == q or tl == q:
            return (fp, title or name)   # exact — best possible, stop immediately
        if q in nl or q in tl:
            score = max(
                len(q) / len(nl) if nl else 0.0,
                len(q) / len(tl) if tl else 0.0,
            )
            if score > best_score:
                best_score = score
                best = (fp, title or name)
    return best


def _tg_note_body(fp: str) -> str:
    """Read a note file and return its body with frontmatter stripped."""
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError:
        return '[Error reading note]'
    body = re.sub(r'^---\s*\n[\s\S]*?\n---\s*\n?', '', raw).strip()
    return body or '[Empty note]'


def _md_to_telegram(body: str) -> 'tuple[str, list]':
    """Convert markdown body to plain text and collect local attachment paths.

    Returns (plain_text, [local_file_paths_in_order_of_appearance]).
    Images are removed from the text and queued for sending as Telegram files.
    Non-image attachment links are replaced with a 📎 filename label.
    """
    attach_dir  = paths.get_attachments_dir()
    media_files: list = []

    def _resolve(url: str) -> 'str | None':
        """Return local path if url is a known /attachments/… file, else None."""
        if not url.startswith('/attachments/'):
            return None
        subpath = _urlparse.unquote(url[len('/attachments/'):])
        fp = os.path.join(attach_dir, subpath)
        return fp if os.path.exists(fp) else None

    def _queue(fp: str) -> None:
        if fp and fp not in media_files:
            media_files.append(fp)

    def _img_sub(m: 're.Match') -> str:
        fp = _resolve(m.group(2))
        if fp:
            _queue(fp)
        return ''   # always erase image markdown; file is sent separately if found

    def _link_sub(m: 're.Match') -> str:
        ltext, url = m.group(1), m.group(2)
        fp = _resolve(url)
        if fp:
            _queue(fp)
            fname = _urlparse.unquote(url.split('/')[-1])
            return f'📎 {fname}'
        return ltext            # external URL → just show the label

    lines     = body.split('\n')
    out: list = []
    in_code   = False
    code_buf: list = []
    code_lang = ''

    for raw in lines:
        line = raw.rstrip()

        # ── fenced code block ───────────────────────────────────────────────
        if re.match(r'^\s*```', line):
            if not in_code:
                in_code   = True
                code_lang = line.lstrip('`').strip()
                code_buf  = []
            else:
                in_code = False
                if code_buf:
                    if code_lang:
                        out.append(f'[{code_lang}]')
                    out.extend(code_buf)
                    out.append('')
                code_buf  = []
                code_lang = ''
            continue

        if in_code:
            code_buf.append(line)
            continue

        # ── table row ───────────────────────────────────────────────────────
        if line.lstrip().startswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            # separator row (dashes / colons) → underline the previous row
            if all(re.match(r'^[-: ]*$', c) for c in cells):
                if out:
                    out.append('─' * max(len(out[-1]), 4))
                continue
            out.append('   '.join(c for c in cells if c))
            continue

        # ── images → queue file, erase from text ────────────────────────────
        line = re.sub(r'!\[([^\]]*)\]\(([^)]*)\)', _img_sub, line)
        line = re.sub(r'!\[[^\]]*\]',              '',        line)  # safety: bare ![...] remnants

        # ── non-image links ──────────────────────────────────────────────────
        line = re.sub(r'\[([^\]]+)\]\(([^)]*)\)', _link_sub, line)

        # ── headings ────────────────────────────────────────────────────────
        hm = re.match(r'^(#{1,6})\s+(.*)', line)
        if hm:
            text  = hm.group(2).strip()
            out.append(text)
            out.append('─' * min(60, max(len(text), 4)))
            continue

        # ── horizontal rule ──────────────────────────────────────────────────
        if re.match(r'^[-*_]{3,}\s*$', line):
            out.append('─' * 40)
            continue

        # ── blockquote ───────────────────────────────────────────────────────
        line = re.sub(r'^(>\s?)+', '| ', line)

        # ── inline formatting (bold / italic / strikethrough / code) ─────────
        line = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', line)
        line = re.sub(r'\*\*(.+?)\*\*',     r'\1', line)
        line = re.sub(r'__(.+?)__',          r'\1', line)
        line = re.sub(r'\*(.+?)\*',          r'\1', line)
        line = re.sub(r'_(.+?)_',            r'\1', line)
        line = re.sub(r'~~(.+?)~~',          r'\1', line)
        line = re.sub(r'`(.+?)`',            r'\1', line)

        out.append(line)

    # flush any unclosed code block
    if in_code and code_buf:
        if code_lang:
            out.append(f'[{code_lang}]')
        out.extend(code_buf)

    plain = '\n'.join(out).strip()

    # ── final sweep: remove every surviving image/media reference ─────────────
    # Pass 1: standard markdown image syntax that slipped through line processing
    plain = re.sub(r'!\[([^\]]*)\]\([^)]*\)', '', plain)
    # Pass 2: bare ![alt] without a URL
    plain = re.sub(r'!\[[^\]]*\]', '', plain)
    # Pass 3: bare !filename.ext — catches tg_20260610_131817.jpg style names
    plain = re.sub(
        r'![\w%._-]+\.(?:jpe?g|png|gif|webp|bmp|tiff?|mp4|m4v|mov|avi|wmv|flv|'
        r'mp3|wav|ogg|flac|aac|m4a|3gp|webm)',
        '', plain, flags=re.IGNORECASE)

    # collapse blank lines that result from removals
    plain = re.sub(r'\n{3,}', '\n\n', plain)
    plain = plain.strip()
    return plain, media_files


async def _tg_process_message(client, msg) -> None:
    """Convert one Telethon Message into a note + downloaded attachments."""
    try:
        from telethon.tl.types import DocumentAttributeFilename
    except ImportError:
        return False

    # Service messages (photo changed, user joined/left, pin, channel create, etc.)
    # have msg.action set to a non-None TL object. They carry no user content.
    if getattr(msg, 'action', None) is not None:
        _tg_log.debug('  skipped — service message (action=%s)', type(msg.action).__name__)
        return False

    # msg.text applies the client's parse_mode and returns e.g. **bold** for bold entities.
    # msg.raw_text is always the plain string the user typed, with no markdown markup added.
    text  = (msg.raw_text or '').strip()

    # Skip our own bot replies and claim markers — they carry the invisible prefix.
    if text.startswith(_TG_REPLY_MARK):
        _tg_log.debug('  skipped — bot/claim marker detected')
        return False

    lines = text.split('\n') if text else []
    title = lines[0][:80].strip() if lines else _tg_ts_title()
    body  = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ''

    _tg_log.debug('  msg id=%-8s  photo=%-5s  doc=%-5s  text=%.50r',
                  msg.id, bool(msg.photo), bool(msg.document), text)

    # ── Command: list ─────────────────────────────────────────────────────────
    if text and _TG_CMD_LIST.match(text) and not msg.photo and not msg.document:
        _tg_log.info('Command "list" received')
        titles = []
        for fp in _note_files():
            fm_l = _read_frontmatter(fp)
            if TRASH_TAG in fm_l.get('tags', []):
                continue
            if fm_l.get('encrypted'):
                continue
            titles.append(_extract_title(fp) or os.path.splitext(os.path.basename(fp))[0])
        titles.sort(key=str.lower)
        if titles:
            body = '\n'.join(f'• {t}' for t in titles)
            header = _TG_REPLY_MARK + f'📋 Notes ({len(titles)}):\n\n'
            if len(header) + len(body) > _TG_MAX_SEND:
                body = body[:_TG_MAX_SEND - len(header) - 15] + '\n\n…[truncated]'
            sent = await client.send_message(msg.peer_id, header + body)
        else:
            sent = await client.send_message(msg.peer_id, _TG_REPLY_MARK + '📋 No notes found.')
        # Advance watermark so our reply isn't re-ingested as a note on the next poll.
        if sent:
            _c = paths._load_config()
            _t = _c.setdefault('telegram', {})
            _t['last_message_id'] = max(int(_t.get('last_message_id', 0)), sent.id)
            paths._save_config(_c)
            _tg_log.debug('  watermark advanced to %d', sent.id)
        return False  # command handled — no note created, no claim needed

    # ── Command: get <notename> ────────────────────────────────────────────────
    cmd_m = _TG_CMD_GET.match(text) if text else None
    if cmd_m and not msg.photo and not msg.document:
        search = cmd_m.group(1).strip()
        _tg_log.info('Command "get" received — searching for %r', search)
        match = _tg_find_note(search)

        sent_ids: 'list[int]' = []

        if match is None:
            sent = await client.send_message(msg.peer_id,
                                             _TG_REPLY_MARK + f'❌ Note not found: {search}')
            if sent: sent_ids.append(sent.id)

        else:
            fp_r, note_name = match
            fm_r = _read_frontmatter(fp_r)
            if fm_r.get('encrypted'):
                sent = await client.send_message(
                    msg.peer_id,
                    _TG_REPLY_MARK + f'\U0001f512 "{note_name}" is encrypted and cannot be sent.',
                )
                if sent: sent_ids.append(sent.id)
            else:
                raw_body = _tg_note_body(fp_r)
                # Strip leading H1 — it duplicates the title we show in the header
                raw_body = re.sub(r'^[ \t]*#[^#\n][^\n]*\n?', '',
                                  raw_body.lstrip('\n'), count=1).lstrip('\n')
                plain, media_files = _md_to_telegram(raw_body)
                if not plain and not media_files:
                    plain = '(empty note)'
                underline = '─' * min(40, max(len(note_name), 4))
                header = _TG_REPLY_MARK + f'\U0001f4d2 {note_name}\n{underline}\n\n'
                if len(header) + len(plain) > _TG_MAX_SEND:
                    plain = plain[:_TG_MAX_SEND - len(header) - 15] + '\n\n…[truncated]'
                try:
                    sent = await client.send_message(msg.peer_id, header + plain)
                    if sent: sent_ids.append(sent.id)
                    _tg_log.info('  text sent for "get %s"', search)
                except Exception as _e:
                    _tg_log.error('  failed to send text: %s', _e)
                for mf in media_files:
                    try:
                        sent_f = await client.send_file(msg.peer_id, mf)
                        if sent_f: sent_ids.append(sent_f.id)
                        _tg_log.debug('  sent file: %s', os.path.basename(mf))
                    except Exception as _ef:
                        _tg_log.error('  failed to send file %s: %s',
                                      os.path.basename(mf), _ef)

        # Advance the poll watermark past our own replies.
        if sent_ids:
            _c = paths._load_config()
            _t = _c.setdefault('telegram', {})
            _t['last_message_id'] = max(int(_t.get('last_message_id', 0)), max(sent_ids))
            paths._save_config(_c)
            _tg_log.debug('  watermark advanced to %d', max(sent_ids))
        return False  # do not create a note from the command message

    # Skip entirely empty messages (no text, no media — nothing to save)
    if not text and not msg.photo and not msg.document:
        _tg_log.debug('  skipped — no text and no media')
        return False

    media_md = ''

    if msg.photo:
        try:
            fname   = f'tg_{_tg_ts_file()}.jpg'
            sub_dir = _ensure_attach_subdir('images')
            fp      = os.path.join(sub_dir, fname)
            await client.download_media(msg.photo, file=fp)
            if os.path.exists(fp):
                media_md = f'\n\n![{fname}](/attachments/images/{fname})'
                _tg_log.debug('  photo saved → images/%s', fname)
            else:
                _tg_log.warning('  photo download returned no file')
        except Exception as e:
            _tg_log.error('  photo download failed: %s', e, exc_info=True)

    elif msg.document:
        try:
            orig    = next((a.file_name for a in msg.document.attributes
                            if isinstance(a, DocumentAttributeFilename)), None)
            base    = _sanitize_filename(os.path.splitext(orig)[0]) if orig else f'tg_doc_{_tg_ts_file()}'
            ext     = (os.path.splitext(orig)[1] if orig else '') or ''
            fname   = base + ext
            sub     = _attachment_subfolder(fname)
            sub_dir = _ensure_attach_subdir(sub)
            fp = os.path.join(sub_dir, fname)
            i  = 1
            while os.path.exists(fp):
                fname = f'{base}_{i}{ext}'
                fp    = os.path.join(sub_dir, fname)
                i += 1
            await client.download_media(msg.document, file=fp)
            if os.path.exists(fp):
                media_md = f'\n\n[{fname}](/attachments/{sub}/{fname})'
                _tg_log.debug('  document saved → %s/%s', sub, fname)
            else:
                _tg_log.warning('  document download returned no file')
        except Exception as e:
            _tg_log.error('  document download failed: %s', e, exc_info=True)

    _tg_create_note(title, body + media_md)   # logs "appended" or "new note created" itself
    return True


# Event that allows the settings-save route to wake the poll thread immediately
# instead of waiting for the current sleep to expire.
_tg_wake = threading.Event()


def _telegram_user_poll_loop() -> None:
    """Daemon thread: polls the configured Telegram group periodically."""
    import asyncio
    _tg_log.info('Poll thread started')

    # ── startup connectivity check (runs once, ~1 s after thread launch) ─────
    time.sleep(1)
    _tg_log.info('Startup connectivity check …')
    _internet_ok = False
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(4)
        s.connect(('8.8.8.8', 53))
        s.close()
        _internet_ok = True
        _tg_log.info('Startup check: internet OK')
    except OSError:
        _tg_log.warning('Startup check: no internet connectivity')
        _sse_notify('No internet connection detected — Telegram polling paused')

    if _internet_ok:
        _cfg0 = paths._load_config().get('telegram', {})
        _s0, _ai0, _ah0 = _cfg0.get('session',''), _cfg0.get('api_id',''), _cfg0.get('api_hash','')
        if _s0 and _ai0 and _ah0:
            async def _startup_auth_check():
                c = _tg_make_client(_s0, int(_ai0), _ah0)
                await c.connect()
                ok = await c.is_user_authorized()
                await c.disconnect()
                return ok
            try:
                _lp0 = asyncio.new_event_loop()
                try:
                    _auth_ok = _lp0.run_until_complete(_startup_auth_check())
                finally:
                    _lp0.close()
                if _auth_ok:
                    _tg_log.info('Startup check: Telegram session valid ✓')
                else:
                    _tg_log.warning('Startup check: Telegram session expired')
                    _sse_notify('Telegram session expired — re-authenticate in Settings → Telegram')
            except Exception as _e0:
                _tg_log.error('Startup check: cannot reach Telegram: %s', _e0)
                _sse_notify('Cannot reach Telegram servers — check your connection')
    # ─────────────────────────────────────────────────────────────────────────

    while True:
        cfg      = paths._load_config().get('telegram', {})
        enabled  = cfg.get('enabled', False)
        session  = cfg.get('session', '')
        api_id   = cfg.get('api_id', '')
        api_hash = cfg.get('api_hash', '')
        chat_id  = cfg.get('chat_id', '')

        if not enabled:
            _tg_log.debug('Polling disabled — thread idle, waiting for settings change')
            _tg_wake.wait(timeout=300)
            _tg_wake.clear()
            continue
        if not session:
            _tg_log.debug('No session — waiting for auth')
            _tg_wake.wait(timeout=300)
            _tg_wake.clear()
            continue
        if not chat_id:
            _tg_log.debug('No chat_id configured — waiting for settings change')
            _tg_wake.wait(timeout=300)
            _tg_wake.clear()
            continue
        if not (api_id and api_hash):
            _tg_log.debug('Missing api_id/api_hash — waiting for settings change')
            _tg_wake.wait(timeout=300)
            _tg_wake.clear()
            continue

        # Quick internet gate — avoids Telethon's 5-retry storm when offline
        try:
            _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _sock.settimeout(3)
            _sock.connect(('8.8.8.8', 53))
            _sock.close()
        except OSError:
            _tg_log.warning('No internet — skipping poll, sleeping 60 s')
            time.sleep(60)
            continue

        async def _poll():
            from telethon.utils import get_peer_id
            _tg_log.debug('Connecting to Telegram …')
            client = _tg_make_client(session, int(api_id), api_hash)
            await client.connect()
            try:
                if not await client.is_user_authorized():
                    _tg_log.warning('Client is not authorized — skipping poll (re-auth needed)')
                    return

                # Warm the entity cache so chat_id can be resolved.
                # A fresh StringSession has no cached dialogs, so passing a
                # bare ID string to get_messages would raise "Cannot find entity".
                _tg_log.debug('Loading dialogs to warm entity cache …')
                dialogs = await client.get_dialogs()
                target_entity = None
                for d in dialogs:
                    if str(get_peer_id(d.entity)) == str(chat_id):
                        target_entity = d.entity
                        _tg_log.debug('Resolved entity: %r (type=%s)',
                                      d.name, type(d.entity).__name__)
                        break
                if target_entity is None:
                    _tg_log.error(
                        'Chat %s not found in your dialogs — '
                        'verify the ID in Settings → Telegram', chat_id)
                    return

                full_cfg  = paths._load_config()
                owner_email = full_cfg.get('settings', {}).get('email', '').strip().lower()
                last_id   = int(full_cfg.get('telegram', {}).get('last_message_id', 0))
                _tg_log.debug('Polling chat=%s  min_id=%s  owner=%s', chat_id, last_id, owner_email or '(none)')
                messages  = await client.get_messages(target_entity, limit=50, min_id=last_id)
                msg_list  = list(reversed(list(messages)))
                _tg_log.info('Fetched %d new message(s) from chat %s', len(msg_list), chat_id)

                # ── Pass 1: build claim index from this batch ─────────────────
                # Claim messages are invisible replies sent by any myNote instance
                # after downloading a note.  Format: _TG_CLAIM_PREFIX + email + _TG_CLAIM_SUFFIX
                # claimed[original_msg_id] = set of owner emails that already took it
                claimed: dict = {}
                for _m in msg_list:
                    _txt   = (_m.raw_text or '').strip()
                    _email = _tg_extract_claim_email(_txt)
                    if _email:
                        _rid = getattr(getattr(_m, 'reply_to', None), 'reply_to_msg_id', None)
                        if _rid:
                            claimed.setdefault(_rid, set()).add(_email.lower())

                # ── Pass 2: process regular messages ─────────────────────────
                for msg in msg_list:
                    _tg_log.debug('Processing msg id=%s', msg.id)

                    # Skip if this owner already claimed the note in this batch
                    if owner_email and msg.id in claimed and owner_email in claimed[msg.id]:
                        _tg_log.info('  msg id=%s already claimed by %s — skipping',
                                     msg.id, owner_email)
                    else:
                        try:
                            note_created = await _tg_process_message(client, msg)
                        except Exception as e:
                            _tg_log.error('Error processing msg id=%s: %s', msg.id, e, exc_info=True)
                            note_created = False

                        # After a successful note download, reply with a claim marker so
                        # other myNote instances with the same owner email skip this message.
                        if note_created and owner_email:
                            claim_text = _TG_CLAIM_PREFIX + owner_email + _TG_CLAIM_SUFFIX
                            try:
                                sent_claim = await client.send_message(
                                    target_entity, claim_text, reply_to=msg.id)
                                # Advance watermark past the claim reply so we never
                                # re-ingest it as a note in a future poll cycle.
                                _cc = paths._load_config()
                                _ct = _cc.setdefault('telegram', {})
                                _ct['last_message_id'] = max(
                                    int(_ct.get('last_message_id', 0)), sent_claim.id)
                                paths._save_config(_cc)
                                _tg_log.info('  claim sent for msg id=%s (owner: %s)',
                                             msg.id, owner_email)
                            except Exception as ec:
                                _tg_log.error('  could not send claim for msg id=%s: %s',
                                              msg.id, ec)

                    c = paths._load_config()
                    t = c.setdefault('telegram', {})
                    # Use max() so an advanced watermark is never rolled back
                    t['last_message_id'] = max(int(t.get('last_message_id', 0)), msg.id)
                    paths._save_config(c)
            finally:
                await client.disconnect()
                _tg_log.debug('Disconnected')

        _sse_auto_sync('telegram', 'start')
        loop = asyncio.new_event_loop()
        _tg_poll_ok, _tg_poll_err = True, None
        try:
            loop.run_until_complete(_poll())
        except Exception as e:
            _tg_log.error('Poll loop error: %s', e, exc_info=True)
            _tg_poll_ok, _tg_poll_err = False, str(e)
        finally:
            loop.close()
        _sse_auto_sync('telegram', 'end', ok=_tg_poll_ok, error=_tg_poll_err)

        poll_interval = int(paths._load_config().get('telegram', {}).get('poll_interval', 30))
        _tg_log.debug('Next poll in %d s (or on settings change)', poll_interval)
        _tg_wake.wait(timeout=poll_interval)
        _tg_wake.clear()


# ---------------------------------------------------------------------------
# Telegram routes
# ---------------------------------------------------------------------------

@app.route('/api/telegram', methods=['GET'])
def get_telegram():
    cfg = paths._load_config().get('telegram', {})
    return jsonify({
        'authorized':    bool(cfg.get('session', '')),
        'codeSent':      bool(cfg.get('phone_code_hash', '')),
        'phone':         cfg.get('phone', ''),
        'apiId':         cfg.get('api_id', ''),
        'apiHash':       cfg.get('api_hash', ''),
        'chatId':        cfg.get('chat_id', ''),
        'enabled':       cfg.get('enabled', False),
        'pollInterval':  int(cfg.get('poll_interval', 30)),
    })


@app.route('/api/telegram', methods=['POST'])
def save_telegram():
    data = request.get_json(force=True)
    cfg  = paths._load_config()
    t    = cfg.setdefault('telegram', {})
    if 'chatId' in data:
        new_id = str(data['chatId']).strip()
        if new_id != t.get('chat_id', ''):
            # Switching to a different chat — reset the watermark so we
            # don't skip all existing messages with the old chat's last ID.
            t['last_message_id'] = 0
            _tg_log.info('chat_id changed → last_message_id reset to 0')
        t['chat_id'] = new_id
    if 'enabled' in data: t['enabled'] = bool(data['enabled'])
    if 'pollInterval' in data:
        interval = int(data['pollInterval'])
        t['poll_interval'] = max(10, min(interval, 3600))
    paths._save_config(cfg)
    _tg_log.info('Config saved — chat_id=%r  enabled=%s  last_msg_id=%s  poll_interval=%s',
                 t.get('chat_id'), t.get('enabled'), t.get('last_message_id', 0),
                 t.get('poll_interval', 30))
    _tg_wake.set()   # wake the poll thread so it picks up new settings immediately
    return jsonify({'ok': True})


@app.route('/api/telegram/reset', methods=['POST'])
def tg_reset_position():
    """Reset the sync watermark so the next poll re-reads all messages."""
    cfg = paths._load_config()
    cfg.setdefault('telegram', {})['last_message_id'] = 0
    paths._save_config(cfg)
    _tg_log.info('Sync position reset to 0 — next poll will re-read all messages')
    return jsonify({'ok': True})


@app.route('/api/telegram/auth/code', methods=['POST'])
def tg_auth_send_code():
    """Step 1: save credentials + send verification code to the user's phone."""
    data     = request.get_json(force=True)
    api_id   = str(data.get('apiId',   '')).strip()
    api_hash = str(data.get('apiHash', '')).strip()
    phone    = str(data.get('phone',   '')).strip()
    if not all([api_id, api_hash, phone]):
        return jsonify({'ok': False, 'error': 'API ID, API Hash and phone are all required.'})

    _tg_log.info('send_code → phone=%s  api_id=%s', phone, api_id)

    async def _do():
        client = _tg_make_client('', int(api_id), api_hash)
        await client.connect()
        result = await client.send_code_request(phone)
        # Save the partial session NOW — it encodes the exact DC the code was sent from.
        # Reconnecting with an empty session may land on a different DC and invalidate the hash.
        auth_session = client.session.save()
        await client.disconnect()
        return result.phone_code_hash, auth_session

    try:
        phone_code_hash, auth_session = _tg_run(_do())
        cfg = paths._load_config()
        cfg.setdefault('telegram', {}).update({
            'api_id':       api_id,
            'api_hash':     api_hash,
            'phone':        phone,
            'phone_code_hash': phone_code_hash,
            'auth_session': auth_session,   # partial session keeps DC context
        })
        paths._save_config(cfg)
        _tg_log.info('send_code OK — code hash stored, DC session saved')
        return jsonify({'ok': True})
    except Exception as e:
        _tg_log.error('send_code failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/telegram/auth/verify', methods=['POST'])
def tg_auth_verify():
    """Step 2: sign in with the received code (+ optional 2FA password)."""
    data     = request.get_json(force=True)
    code     = str(data.get('code',     '')).strip()
    password = str(data.get('password', '')).strip()

    cfg             = paths._load_config()
    t               = cfg.get('telegram', {})
    api_id          = t.get('api_id', '')
    api_hash        = t.get('api_hash', '')
    phone           = t.get('phone', '')
    phone_code_hash = t.get('phone_code_hash', '')
    auth_session    = t.get('auth_session', '')

    if not all([api_id, api_hash, phone, phone_code_hash]):
        return jsonify({'ok': False, 'error': 'Send the code first.'})

    _tg_log.info('verify → phone=%s  has_2fa=%s  has_auth_session=%s',
                 phone, bool(password), bool(auth_session))

    async def _do():
        from telethon.errors import SessionPasswordNeededError
        # Reuse the partial session so we reconnect to the same DC that issued the code
        client = _tg_make_client(auth_session, int(api_id), api_hash)
        await client.connect()
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                await client.disconnect()
                raise Exception('2FA_REQUIRED')
            _tg_log.debug('  2FA password provided — signing in …')
            await client.sign_in(password=password)
        session_str = client.session.save()
        me = await client.get_me()
        await client.disconnect()
        return session_str, (me.first_name or ''), (me.username or '')

    try:
        session_str, first_name, username = _tg_run(_do())
        cfg = paths._load_config()
        t   = cfg.setdefault('telegram', {})
        t['session'] = session_str
        t.pop('phone_code_hash', None)
        t.pop('auth_session',    None)
        paths._save_config(cfg)
        _tg_log.info('verify OK — logged in as %s (@%s)', first_name, username)
        _tg_wake.set()   # session is now valid — let the poll thread start
        return jsonify({'ok': True, 'name': first_name, 'username': username})
    except Exception as e:
        err = str(e)
        if '2FA_REQUIRED' in err:
            _tg_log.info('verify: 2FA required')
            return jsonify({'ok': False, 'need2fa': True})
        _tg_log.error('verify failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': err})


@app.route('/api/telegram/dialogs', methods=['GET'])
def tg_get_dialogs():
    """Return all groups/channels the authed user belongs to."""
    cfg = paths._load_config()
    t   = cfg.get('telegram', {})
    session_str = t.get('session', '')
    api_id   = t.get('api_id')
    api_hash = t.get('api_hash')
    if not (session_str and api_id and api_hash):
        _tg_log.warning('get_dialogs: not authorised (no session/api_id/api_hash)')
        return jsonify({'error': 'Not authorised'}), 403

    _tg_log.debug('get_dialogs: fetching …')

    async def _do():
        from telethon.tl.types import Channel, Chat
        from telethon.utils import get_peer_id
        client = _tg_make_client(session_str, int(api_id), api_hash)
        await client.connect()
        try:
            dialogs = await client.get_dialogs()
            result  = []
            for d in dialogs:
                if isinstance(d.entity, (Channel, Chat)):
                    result.append({'name': d.name, 'id': get_peer_id(d.entity)})
            return result
        finally:
            await client.disconnect()

    try:
        dialogs = _tg_run(_do())
        _tg_log.info('get_dialogs: returned %d group(s)/channel(s)', len(dialogs))
        return jsonify({'dialogs': dialogs})
    except Exception as e:
        _tg_log.error('get_dialogs failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/events')
def sse_stream():
    """SSE endpoint — browser tabs subscribe here for live push events."""
    from flask import Response, stream_with_context
    client_q = _queue_mod.Queue(maxsize=20)
    with _sse_clients_lck:
        _sse_clients.append(client_q)
    # Replay a startup notification that arrived before this tab connected,
    # as long as it is less than 90 s old.
    sn = _startup_notify
    if sn['data'] and time.time() - sn['at'] < 90:
        try:
            client_q.put_nowait(sn['data'])
        except _queue_mod.Full:
            pass
    def generate():
        try:
            while True:
                try:
                    data = client_q.get(timeout=20)
                    yield f'data: {data}\n\n'
                except _queue_mod.Empty:
                    yield ': heartbeat\n\n'   # keep connection alive
        finally:
            with _sse_clients_lck:
                try: _sse_clients.remove(client_q)
                except ValueError: pass
    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/telegram/session', methods=['DELETE'])
def tg_delete_session():
    """Log out: clear the saved session string."""
    cfg = paths._load_config()
    t   = cfg.get('telegram', {})
    t.pop('session', None)
    t.pop('phone_code_hash', None)
    t['enabled'] = False
    paths._save_config(cfg)
    _tg_log.info('Session deleted — user logged out, polling disabled')
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Note-ID index + ID-aware bidirectional sync
# ---------------------------------------------------------------------------

def _build_note_id_index(notes_dir: str) -> dict:
    """Scan .md files; return {'by_id': {uuid: info}, 'no_id': [info]}.
    info = {'path': abs_path, 'mtime': float, 'filename': basename}"""
    by_id = {}
    no_id = []
    if not os.path.isdir(notes_dir):
        return {'by_id': by_id, 'no_id': no_id}
    for entry in os.scandir(notes_dir):
        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith('.md'):
            continue
        fm      = _read_frontmatter(entry.path)
        note_id = fm.get('id', '')
        mtime   = entry.stat().st_mtime
        info    = {'path': entry.path, 'mtime': mtime, 'filename': entry.name}
        if note_id:
            if note_id not in by_id or mtime > by_id[note_id]['mtime']:
                by_id[note_id] = info
        else:
            no_id.append(info)
    return {'by_id': by_id, 'no_id': no_id}


def _sync_notes_by_id(local_dir: str, remote_dir: str) -> dict:
    """Bidirectional note sync matched by UUID, not filename.
    When the same UUID has different filenames (rename happened) the newer
    mtime wins: the stale file on the losing side is deleted and replaced.
    Falls back to filename matching for notes without an id field."""
    os.makedirs(remote_dir, exist_ok=True)
    to_remote = 0
    to_local  = 0
    skipped   = 0

    local_idx  = _build_note_id_index(local_dir)
    remote_idx = _build_note_id_index(remote_dir)
    l_by_id, l_no_id = local_idx['by_id'], local_idx['no_id']
    r_by_id, r_no_id = remote_idx['by_id'], remote_idx['no_id']

    # Don't pull back notes we deliberately deleted
    _deleted_names = {e['path']
                      for e in _load_deletion_log(_deletion_log_path())['deletions']
                      if e['type'] == 'note'}

    # ── Pass 1: ID-aware ──────────────────────────────────────────────────────
    for note_id in set(l_by_id) | set(r_by_id):
        l = l_by_id.get(note_id)
        r = r_by_id.get(note_id)

        if l and not r:
            _atomic_copy(l['path'], os.path.join(remote_dir, l['filename']))
            to_remote += 1
        elif r and not l:
            if r['filename'] not in _deleted_names:
                shutil.copy2(r['path'], os.path.join(local_dir, r['filename']))
                to_local += 1
            else:
                # Was deleted locally — ensure the remote copy is also gone
                try:
                    os.remove(r['path'])
                except OSError:
                    pass
                hist_rp = _history_path(r['path'])
                if os.path.isfile(hist_rp):
                    try:
                        os.remove(hist_rp)
                    except OSError:
                        pass
        else:
            lm, rm = l['mtime'], r['mtime']
            if lm > rm + 1:                          # local is newer → push
                # Same guard as the pull direction: if iCloud async-updated the
                # remote mtime (making local look newer) but content is identical,
                # skip — otherwise we'd clobber a change another PC just pushed
                # that iCloud hasn't delivered to this machine yet.
                if l['filename'] == r['filename'] and not _files_differ(l['path'], r['path']):
                    skipped += 1
                else:
                    if l['filename'] != r['filename']:
                        try: os.remove(r['path'])
                        except OSError: pass
                    _atomic_copy(l['path'], os.path.join(remote_dir, l['filename']))
                    to_remote += 1
            elif rm > lm + 1:                        # remote mtime is newer
                # Guard: iCloud/GDrive updates file mtimes asynchronously when
                # uploading. Don't pull if content is identical — that's just
                # mtime drift, not a real remote edit.
                if _files_differ(l['path'], r['path']):
                    if r['filename'] != l['filename']:
                        try: os.remove(l['path'])
                        except OSError: pass
                    shutil.copy2(r['path'], os.path.join(local_dir, r['filename']))
                    to_local += 1
                else:
                    skipped += 1
            else:                                    # same age (within 1 s)
                if l['filename'] != r['filename']:   # filename drift — local wins
                    try: os.remove(r['path'])
                    except OSError: pass
                    _atomic_copy(l['path'], os.path.join(remote_dir, l['filename']))
                    to_remote += 1
                else:
                    skipped += 1

    # ── Pass 2: filename-based fallback for notes without an id ───────────────
    l_no = {i['filename']: i for i in l_no_id}
    r_no = {i['filename']: i for i in r_no_id}
    for fname in set(l_no) | set(r_no):
        l = l_no.get(fname)
        r = r_no.get(fname)
        if l and not r:
            _atomic_copy(l['path'], os.path.join(remote_dir, fname))
            to_remote += 1
        elif r and not l:
            if fname not in _deleted_names:
                shutil.copy2(r['path'], os.path.join(local_dir, fname))
                to_local += 1
            else:
                # Was deleted locally — ensure the remote copy is also gone
                try:
                    os.remove(r['path'])
                except OSError:
                    pass
                hist_rp = _history_path(r['path'])
                if os.path.isfile(hist_rp):
                    try:
                        os.remove(hist_rp)
                    except OSError:
                        pass
        else:
            lm, rm = l['mtime'], r['mtime']
            if lm > rm + 1:
                if not _files_differ(l['path'], r['path']):
                    skipped += 1
                else:
                    _atomic_copy(l['path'], r['path']); to_remote += 1
            elif rm > lm + 1:
                if _files_differ(l['path'], r['path']):
                    shutil.copy2(r['path'], l['path']); to_local += 1
                else:
                    skipped += 1
            else:
                skipped += 1

    return {'toRemote': to_remote, 'toLocal': to_local, 'skipped': skipped}


# ---------------------------------------------------------------------------
# Android SAF stray-file cleanup
# ---------------------------------------------------------------------------

# Extensions that a legitimate attachment would never have as its FINAL
# extension — if a file ends in .txt but the part before .txt has one of
# these, it's a SAF-generated stray copy (e.g. photo.jpg.txt, note.md.txt).
_SAF_STRAY_INNER_EXTS = {
    '.md', '.json',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.svg',
    '.mp4', '.mov', '.avi', '.mkv', '.webm',
    '.mp3', '.m4a', '.ogg', '.wav', '.flac', '.aac',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.rar', '.7z', '.tar', '.gz',
}


def _cleanup_android_txt_strays(remote_notes: str, remote_attach: str, log) -> int:
    """Delete *.txt files that Android SAF created by appending .txt to real
    filenames (text/plain MIME bug).  Safe to run before every sync.

    Notes folder: every .txt is a stray (no legitimate .txt lives there).
    Attachments tree: only .txt whose inner extension is a known non-text type
    (e.g. photo.jpg.txt → stray; readme.txt → kept).
    Returns number of files removed."""
    removed = 0

    # Notes folder (non-recursive — flat layout)
    if os.path.isdir(remote_notes):
        for entry in os.scandir(remote_notes):
            if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith('.txt'):
                try:
                    os.remove(entry.path)
                    log.info('SAF cleanup: removed stray notes file %s', entry.name)
                    removed += 1
                except OSError as exc:
                    log.warning('SAF cleanup: could not remove %s: %s', entry.name, exc)

    # Attachments tree (recursive)
    if os.path.isdir(remote_attach):
        for dirpath, _dirs, filenames in os.walk(remote_attach):
            for fname in filenames:
                if not fname.lower().endswith('.txt'):
                    continue
                inner_ext = os.path.splitext(fname[:-4])[1].lower()  # ext before .txt
                if inner_ext in _SAF_STRAY_INNER_EXTS:
                    fp = os.path.join(dirpath, fname)
                    try:
                        os.remove(fp)
                        log.info('SAF cleanup: removed stray attachment %s', fname)
                        removed += 1
                    except OSError as exc:
                        log.warning('SAF cleanup: could not remove %s: %s', fname, exc)

    if removed:
        log.info('SAF cleanup: removed %d stray file(s) total', removed)
    return removed


# ---------------------------------------------------------------------------
# Orphan attachment cleanup
# ---------------------------------------------------------------------------

# Matches the URL part of every attachment link in a note:
#   /attachments/<subfolder>/<filename>
# Filename may be URL-encoded (spaces → %20, Cyrillic → %XX…).
_ATTACH_LINK_RE = re.compile(r'\(/attachments/([^/)\n]+)/([^)\n]+)\)')


def _orphan_attachment_cleanup(notes_dir: str, attach_dir: str, log) -> int:
    """Delete attachment files that are not referenced by any note.

    An orphan is a file in the attachments tree whose path never appears in
    any .md file (including trashed notes — those still exist on disk).
    Orphaned files are removed locally and logged to .deletions.json so the
    deletion propagates to every other device on the next sync.

    Returns the number of files removed."""
    if not os.path.isdir(attach_dir):
        return 0

    # ── Step 1: collect every attachment path referenced by any note ─────────
    referenced: 'set[str]' = set()
    try:
        note_entries = list(os.scandir(notes_dir))
    except OSError:
        return 0

    for entry in note_entries:
        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith('.md'):
            continue
        try:
            with open(entry.path, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read()
        except OSError:
            continue
        for m in _ATTACH_LINK_RE.finditer(content):
            subfolder = m.group(1)
            filename  = _urlparse.unquote(m.group(2))  # decode %20, %D0%91…
            referenced.add(f'{subfolder}/{filename}')

    # ── Step 2: walk the attachment tree and identify orphans ─────────────────
    orphans: 'list[tuple[str, str]]' = []   # (rel_path, abs_path)
    for dirpath, _dirs, filenames in os.walk(attach_dir):
        for fname in filenames:
            fp  = os.path.join(dirpath, fname)
            rel = os.path.relpath(fp, attach_dir).replace('\\', '/')
            if rel not in referenced:
                orphans.append((rel, fp))

    if not orphans:
        return 0

    # ── Step 3: delete orphans and log each one ───────────────────────────────
    now          = time.time()
    new_entries: list = []
    removed      = 0
    for rel, fp in orphans:
        try:
            os.remove(fp)
            new_entries.append({'path': rel, 'type': 'attachment', 'deleted_at': now})
            log.info('Orphan cleanup: removed %s', rel)
            removed += 1
        except OSError as exc:
            log.warning('Orphan cleanup: could not remove %s: %s', rel, exc)

    if new_entries:
        _log_deletions(new_entries)

    if removed:
        log.info('Orphan cleanup: %d orphaned attachment(s) removed', removed)
    return removed


# ---------------------------------------------------------------------------
# Attachment safety + deletion log
# ---------------------------------------------------------------------------

_DELETION_LOG = '.deletions.json'


def _deletion_log_path(notes_dir: 'str | None' = None) -> str:
    return os.path.join(notes_dir or paths.get_notes_dir(), _DELETION_LOG)


def _load_deletion_log(log_path: str) -> dict:
    """Return {'settings': {...}, 'deletions': [...]}.
    Transparently migrates old list-only format."""
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):                      # legacy format
            return {'settings': {}, 'deletions': _strip_placeholder_deletions(data)}
        raw = data.get('deletions', [])
        return {
            'settings':  data.get('settings', {}),
            # Filtered on read too, so an entry arriving in another device's log
            # is never acted on even before it gets written back out
            'deletions': _strip_placeholder_deletions(raw) if isinstance(raw, list) else [],
        }
    except (OSError, json.JSONDecodeError):
        return {'settings': {}, 'deletions': []}


def _atomic_copy(src: str, dst: str) -> None:
    """Copy src to dst via a temp file + os.replace.

    iCloud/GDrive for Windows holds exclusive locks on files while uploading,
    which blocks a direct open()-for-write. Writing to a new temp file then
    renaming into place works because the rename bypasses the open-file lock.
    """
    tmp = dst + '.mntmp'
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _files_differ(p1: str, p2: str) -> bool:
    """Return True when the two files have different content.

    Checks size first (cheap), then reads both files only when sizes match.
    Used to distinguish genuine remote updates from iCloud/GDrive async mtime
    updates that make a remote copy appear newer without changing its content.
    """
    try:
        if os.path.getsize(p1) != os.path.getsize(p2):
            return True
        with open(p1, 'rb') as f1, open(p2, 'rb') as f2:
            return f1.read() != f2.read()
    except OSError:
        return True  # can't compare → assume they differ, let caller decide


def _save_deletion_log(log_path: str, data: dict) -> None:
    # Single choke point for every deletion-log write (local and remote), so the
    # placeholder guard here covers trash-emptying, orphan cleanup and merges alike.
    data = dict(data)
    data['deletions'] = _strip_placeholder_deletions(data.get('deletions', []))
    # Write via temp + atomic replace so iCloud/GDrive file locks don't block us.
    tmp = log_path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, log_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _log_deletions(new_entries: list) -> None:
    """Append file-deletion entries to the local log, deduplicating by path."""
    log_path = _deletion_log_path()
    log_data = _load_deletion_log(log_path)
    by_path  = {e['path']: e for e in log_data['deletions']}
    for e in new_entries:
        by_path[e['path']] = e
    log_data['deletions'] = list(by_path.values())
    _save_deletion_log(log_path, log_data)


def _unlog_deletion(fname: str) -> None:
    """Remove a note filename from the deletion log (called when a note is
    (re-)created so sync doesn't mistake it for a deleted ghost)."""
    log_path = _deletion_log_path()
    log_data = _load_deletion_log(log_path)
    before = len(log_data['deletions'])
    log_data['deletions'] = [e for e in log_data['deletions'] if e.get('path') != fname]
    if len(log_data['deletions']) != before:
        _save_deletion_log(log_path, log_data)


def _get_note_attachments(note_path: str) -> list:
    """Return list of 'subfolder/filename' for every attachment referenced in this note."""
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            body = f.read()
    except OSError:
        return []
    attach_root = paths.get_attachments_dir()
    results = []
    for sub in ('images', 'documents', 'videos', 'audio', 'archives'):
        sub_dir = os.path.join(attach_root, sub)
        if not os.path.isdir(sub_dir):
            continue
        for fname in os.listdir(sub_dir):
            if fname in body:
                results.append(f'{sub}/{fname}')
    return results


def _is_attachment_safe_to_delete(attach_rel: str, exclude_note: str) -> bool:
    """True when no non-trash note other than exclude_note references this attachment."""
    fname = os.path.basename(attach_rel)
    for fp in _note_files():
        if fp == exclude_note:
            continue
        if TRASH_TAG in _read_frontmatter(fp).get('tags', []):
            continue
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                if fname in f.read():
                    return False
        except OSError:
            pass
    return True


def _delete_note_permanently(filepath: str) -> None:
    """Delete a note and any attachments not referenced by other non-trash notes.
    Logs every deletion to .deletions.json for cross-device sync."""
    entries = []
    now     = time.time()

    for attach_rel in _get_note_attachments(filepath):
        if _is_attachment_safe_to_delete(attach_rel, filepath):
            abs_path = os.path.join(paths.get_attachments_dir(), attach_rel)
            if os.path.isfile(abs_path):
                try:
                    os.remove(abs_path)
                except OSError:
                    pass
            entries.append({'path': attach_rel, 'type': 'attachment', 'deleted_at': now})

    fname = os.path.basename(filepath)
    if os.path.isfile(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass
    hist_fp = _history_path(filepath)
    if os.path.isfile(hist_fp):
        try:
            os.remove(hist_fp)
        except OSError:
            pass
    entries.append({'path': fname, 'type': 'note', 'deleted_at': now})
    _log_deletions(entries)


def _apply_deletion_log(notes_dir: str, attach_dir: str,
                        log_path: 'str | None' = None) -> 'set[str]':
    """Delete any files listed in the deletion log from the given directory pair.

    Returns the set of entry paths that were *skipped* because the file's mtime
    is newer than deleted_at — meaning it was recreated on another device after
    the deletion (resurrection).  The caller should remove these from the log.
    """
    log_data = _load_deletion_log(log_path or _deletion_log_path(notes_dir))
    resurrected: set = set()
    for e in log_data['deletions']:
        fp = os.path.join(notes_dir if e['type'] == 'note' else attach_dir, e['path'])
        deleted_at = e.get('deleted_at', 0)
        if os.path.isfile(fp):
            if os.path.getmtime(fp) > deleted_at:
                # File was modified/created after the deletion — resurrection wins
                resurrected.add(e['path'])
                continue
            if e['type'] == 'attachment' and not _is_attachment_safe_to_delete(e['path'], ''):
                pass  # referenced by a local note — skip deletion
            else:
                try:
                    os.remove(fp)
                except OSError:
                    pass
        if e['type'] == 'note':
            hist_fp = _history_path(fp)
            if os.path.isfile(hist_fp):
                try:
                    os.remove(hist_fp)
                except OSError:
                    pass
    return resurrected


def _sync_deletion_log(local_notes: str, remote_notes: str,
                        local_attach: str, remote_attach: str) -> None:
    """Merge .deletions.json from both sides:
    - settings: latest lastchanged wins and is applied to local config
    - deletions: union by path, newest deleted_at wins
    - housekeeping: entries older than 6 months are purged
    Merged result is written to both sides, then deletions are applied."""
    local_log  = _deletion_log_path(local_notes)
    remote_log = _deletion_log_path(remote_notes)

    local_data  = _load_deletion_log(local_log)
    remote_data = _load_deletion_log(remote_log)

    # ── Settings merge: latest lastchanged wins ───────────────────────────────
    l_cfg = local_data.get('settings', {})
    r_cfg = remote_data.get('settings', {})
    if r_cfg.get('lastchanged', 0) > l_cfg.get('lastchanged', 0):
        merged_settings = r_cfg
        # Apply remote trash-expiry setting to local config
        if 'trash_expire_months' in r_cfg:
            cfg = paths._load_config()
            cfg['trash_expire_months'] = r_cfg['trash_expire_months']
            paths._save_config(cfg)
    else:
        merged_settings = l_cfg

    # ── Deletion entries merge: union, newest deleted_at wins ─────────────────
    by_path = {}
    for e in local_data['deletions'] + remote_data['deletions']:
        p = e['path']
        if p not in by_path or e.get('deleted_at', 0) > by_path[p].get('deleted_at', 0):
            by_path[p] = e

    # Housekeeping: purge records older than 6 months
    cutoff  = time.time() - 180 * 86400
    by_path = {p: e for p, e in by_path.items() if e.get('deleted_at', 0) > cutoff}

    merged = {'settings': merged_settings, 'deletions': list(by_path.values())}

    _save_deletion_log(local_log, merged)
    try:
        _save_deletion_log(remote_log, merged)
    except OSError:
        pass

    resurrected_l = _apply_deletion_log(local_notes,  local_attach,  local_log)
    resurrected_r: set = set()
    try:
        resurrected_r = _apply_deletion_log(remote_notes, remote_attach, remote_log)
    except OSError:
        pass

    # If a file exists on either side with mtime > deleted_at, it was recreated
    # on another device after the local deletion.  Remove those entries from the
    # log so that the next _sync_notes_by_id pass pulls them in normally.
    all_resurrected = resurrected_l | resurrected_r
    if all_resurrected:
        cleaned = {
            'settings': merged_settings,
            'deletions': [e for e in merged['deletions']
                          if e['path'] not in all_resurrected],
        }
        _save_deletion_log(local_log, cleaned)
        try:
            _save_deletion_log(remote_log, cleaned)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Trash auto-cleanup (3-month expiry)
# ---------------------------------------------------------------------------

_TRASH_MAX_AGE = 90 * 86400   # 3 months in seconds


def _trash_cleanup_pass() -> int:
    """Permanently delete notes that have been in trash longer than the configured period."""
    months  = paths._load_config().get('trash_expire_months', 3)
    cutoff  = time.time() - months * 30 * 86400
    deleted = 0
    for fp in _note_files():
        fm = _read_frontmatter(fp)
        if TRASH_TAG not in fm.get('tags', []):
            continue
        ta = fm.get('trashed_at')
        if ta is None or float(ta) > cutoff:
            continue
        try:
            _delete_note_permanently(fp)
            deleted += 1
        except Exception:
            pass
    if deleted:
        _sse_broadcast(json.dumps({'type': 'notes-updated'}))
    return deleted


def _trash_cleanup_loop() -> None:
    """Background thread: check for expired trash notes every hour."""
    while True:
        try:
            _trash_cleanup_pass()
        except Exception:
            pass
        time.sleep(3600)


threading.Thread(target=_trash_cleanup_loop, daemon=True, name='trash-cleanup').start()


# ---------------------------------------------------------------------------
# iCloud Drive sync
# ---------------------------------------------------------------------------

_icloud_log = logging.getLogger('[iCloud]')
_icloud_log.setLevel(logging.DEBUG)
if not _icloud_log.handlers:
    _ich = logging.StreamHandler()
    _ich.setFormatter(logging.Formatter('%(name)s %(message)s'))
    _icloud_log.addHandler(_ich)

_icloud_wake      = threading.Event()
_icloud_sync_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Remote-folder delivery guard
# ---------------------------------------------------------------------------
# When a cloud folder doesn't exist yet at first sync it means the cloud
# client (iCloud / GDrive) is still downloading it from another device.
# If we create an empty folder immediately the cloud client renames ours to
# myNotes(1) and delivers the real folder as myNotes — everything breaks.
#
# Fix: track when we first noticed the folder was absent.  Refuse to create
# it for _CLOUD_FOLDER_WAIT seconds.  If the cloud delivers it in that time
# we use it; if the folder is still absent afterwards it really is a fresh
# install and os.makedirs in the sync is the right thing to do.
_CLOUD_FOLDER_WAIT: float = 180.0          # seconds to wait before creating
_folder_wait_since: dict  = {}             # 'icloud' | 'gdrive' → float ts

# Timestamp until which sync is paused because the user is actively editing.
# Set via POST /api/editing. Auto-expires after 30 s of inactivity so a crash
# or missed "editing done" call never blocks sync permanently.
_editing_until: float = 0.0
_EDITING_GRACE = 30.0   # seconds to hold the pause after each heartbeat

# ── Placeholder note: sync without waiting for the editor to close ──────────
# Anything typed into the scratch note is worth getting off this machine even
# while it is still open, because the note is never deliberately "finished" —
# the user types a thought and walks away. Editing normally pauses sync, so for
# this note that pause is lifted and a deadline is armed instead.
#
# The deadline is armed ONCE, when content first appears, and is deliberately
# not pushed back by further typing: an idle-reset timer would never fire for
# someone who keeps adding a line every couple of minutes.  Whichever arrives
# first — this deadline or the configured sync interval — carries the content.
_PLACEHOLDER_SYNC_DELAY = 180.0   # 3 minutes
_placeholder_due: float = 0.0     # when to force a sync; 0 = nothing pending


def _placeholder_mark_dirty() -> None:
    """Arm the forced-sync deadline the first time real content shows up."""
    global _placeholder_due
    if _placeholder_due == 0.0:
        _placeholder_due = time.time() + _PLACEHOLDER_SYNC_DELAY
        # Wake the sync loops. Each computed its sleep when it last went idle,
        # so a loop parked on a 30-minute interval would otherwise sleep right
        # through a deadline armed a moment later. They re-check whether
        # anything is actually due before syncing, so this only makes them
        # recompute the wait — it does not force a sync now.
        for _ev_name in ('_github_wake', '_icloud_wake', '_gdrive_wake'):
            _ev = globals().get(_ev_name)
            if _ev is not None:
                _ev.set()


def _placeholder_clear() -> None:
    global _placeholder_due
    _placeholder_due = 0.0


def _placeholder_rearm() -> None:
    """Push the deadline out after a failed attempt, so a provider that stays
    unreachable is retried on the same cadence instead of spun on every pass."""
    global _placeholder_due
    if _placeholder_due != 0.0:
        _placeholder_due = time.time() + _PLACEHOLDER_SYNC_DELAY


def _placeholder_settle(forced: bool, ok: bool) -> None:
    """Resolve the pending deadline after a sync pass that was carrying it."""
    if not forced:
        return
    _placeholder_clear() if ok else _placeholder_rearm()


def _placeholder_sync_pending() -> bool:
    """True while the scratch note holds content that has not synced yet."""
    return _placeholder_due != 0.0


def _placeholder_due_now() -> bool:
    """True once the forced-sync deadline has actually fallen due."""
    return _placeholder_due != 0.0 and time.time() >= _placeholder_due


def _sync_paused_for_editing() -> bool:
    """Whether the editing pause applies right now.

    It does not when the scratch note has content waiting — that content has to
    go out on schedule regardless of the editor still being open."""
    if _placeholder_sync_pending():
        return False
    return time.time() < _editing_until


def _sync_wait_timeout(interval: float, last_sync_mono: float = 0.0) -> float:
    """How long a sync loop should sleep: whatever is left of the interval,
    shortened so the loop wakes exactly when the placeholder deadline falls due.

    last_sync_mono stops a re-computed sleep from restarting the whole interval
    every time the loop is woken early."""
    remaining = float(interval)
    if last_sync_mono:
        remaining = interval - (time.monotonic() - last_sync_mono)
    if _placeholder_sync_pending():
        remaining = min(remaining, _placeholder_due - time.time())
    return max(1.0, remaining)


def _detect_icloud_drive() -> 'str | None':
    """Try to find the iCloud Drive root folder on this Windows machine."""
    # ── 1. Registry (most reliable when iCloud for Windows is installed) ──────
    try:
        import winreg
        _reg_candidates = [
            (winreg.HKEY_CURRENT_USER,  r'Software\Apple Inc.\iCloud\MobileDocuments'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Apple Inc.\iCloud'),
            (winreg.HKEY_CURRENT_USER,  r'Software\Apple Computer, Inc.\iCloud'),
        ]
        for hive, keypath in _reg_candidates:
            try:
                with winreg.OpenKey(hive, keypath) as k:
                    for valname in ('Path', 'iCloudDriveRoot', 'RootPath', ''):
                        try:
                            val, _ = winreg.QueryValueEx(k, valname)
                            p = str(val).strip()
                            if p and os.path.isdir(p):
                                _icloud_log.debug('Found via registry [%s] %r', keypath, p)
                                return p
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass

    # ── 2. Well-known filesystem paths ────────────────────────────────────────
    home = os.path.expanduser('~')
    localapp = os.environ.get('LOCALAPPDATA', '')
    candidates = [
        os.path.join(home,     'iCloudDrive'),
        os.path.join(home,     'Apple', 'CloudDrive'),
        os.path.join(home,     'Apple', 'iCloud Drive'),
        os.path.join(localapp, 'Apple', 'CloudDrive'),
    ]
    for p in candidates:
        if p and os.path.isdir(p):
            _icloud_log.debug('Found via filesystem scan: %r', p)
            return p

    _icloud_log.debug('iCloud Drive not found on this machine')
    return None


def _has_notes_structure(path: str) -> bool:
    """Return True if *path* already looks like a myNote sync folder.

    Canonical layout: ``.md`` files and ``.deletions.json`` sit directly in
    *path*; attachments live in ``path/attachments/``.  We also accept the
    legacy ``notes/`` sub-directory layout so that setup can recognise an
    existing remote without creating a conflicting second folder (migration
    to the flat layout happens automatically on the first sync).
    """
    if not os.path.isdir(path):
        return False
    # New flat layout: has attachments/ subdir
    if os.path.isdir(os.path.join(path, 'attachments')):
        return True
    # Legacy layout: notes stored in a notes/ subfolder (will be migrated on sync)
    if os.path.isdir(os.path.join(path, 'notes')):
        return True
    # Fallback: any .md file directly inside
    try:
        return any(
            f.lower().endswith('.md')
            for f in os.listdir(path)
            if os.path.isfile(os.path.join(path, f))
        )
    except OSError:
        return False


def _migrate_remote_notes_subdir(remote_root: str, log) -> None:
    """Flatten the legacy ``notes/`` subfolder into ``remote_root/``.

    Old layout: ``remote_root/notes/*.md`` and
                ``remote_root/notes/.deletions.json``
    New layout: ``remote_root/*.md`` and
                ``remote_root/.deletions.json``

    Runs automatically before every sync so no user action is needed.
    Files that already exist in the destination are kept if they are newer;
    the old copy is discarded otherwise.  The empty ``notes/`` dir is
    removed after migration.
    """
    notes_sub = os.path.join(remote_root, 'notes')
    if not os.path.isdir(notes_sub):
        return
    moved = 0
    for fname in os.listdir(notes_sub):
        src = os.path.join(notes_sub, fname)
        if not os.path.isfile(src):
            continue                        # skip subdirs
        dst = os.path.join(remote_root, fname)
        try:
            if os.path.exists(dst):
                if os.path.getmtime(src) > os.path.getmtime(dst):
                    os.replace(src, dst)    # remote copy is newer — overwrite
                else:
                    os.remove(src)          # local copy is newer — discard remote
            else:
                shutil.move(src, dst)
            moved += 1
        except OSError as exc:
            log.warning('migrate: could not move %s → %s: %s', src, dst, exc)
    if moved:
        log.info('Migrated %d file(s) from legacy notes/ subdir at %s', moved, remote_root)
    try:
        os.rmdir(notes_sub)                 # only succeeds when dir is now empty
        log.info('Removed empty legacy notes/ subdir at %s', remote_root)
    except OSError:
        pass                                # not empty yet — leave it


def _icloud_sync_dirs(local_dir: str, remote_dir: str,
                      *, recursive: bool = False,
                      exts: 'set | None' = None,
                      name_filter: 'object' = None,
                      skip_remote_names: 'set | None' = None) -> dict:
    """Sync two directories with 'newest wins' logic. Returns stats dict.

    name_filter: optional callable(filename) → bool, takes priority over exts.
    skip_remote_names: filenames that must NOT be pulled from remote; if the
        remote copy exists it is deleted (stale orphan cleanup).
    """
    os.makedirs(remote_dir, exist_ok=True)
    to_remote = 0
    to_local  = 0
    skipped   = 0

    def _collect(d: str) -> dict:          # rel_path → abs_path
        out = {}
        if not os.path.isdir(d):
            return out
        for entry in os.scandir(d):
            if entry.is_file(follow_symlinks=False):
                rel = entry.name
                if name_filter is not None:
                    if name_filter(rel):
                        out[rel] = entry.path
                elif exts is None or os.path.splitext(rel)[1].lower() in exts:
                    out[rel] = entry.path
            elif recursive and entry.is_dir(follow_symlinks=False):
                for sub_rel, sub_abs in _collect(entry.path).items():
                    out[os.path.join(entry.name, sub_rel)] = sub_abs
        return out

    local_files  = _collect(local_dir)
    remote_files = _collect(remote_dir)

    for name in set(local_files) | set(remote_files):
        lp = local_files.get(name)
        rp = remote_files.get(name)

        if lp and not rp:
            # Only on PC → push to remote folder
            dst = os.path.join(remote_dir, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _atomic_copy(lp, dst)
            to_remote += 1
        elif rp and not lp:
            if skip_remote_names and name in skip_remote_names:
                # Remote-only orphan that belongs to a deleted note → purge it
                try:
                    os.remove(rp)
                except OSError:
                    pass
            else:
                # Only in remote folder → pull to PC
                dst = os.path.join(local_dir, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(rp, dst)
                to_local += 1
        else:
            lm = os.path.getmtime(lp)
            rm = os.path.getmtime(rp)
            if lm > rm + 1:            # +1 s tolerance for FAT/APFS rounding
                _atomic_copy(lp, rp)
                to_remote += 1
            elif rm > lm + 1:
                # Guard against iCloud/GDrive async mtime updates: only pull
                # when content actually differs, not just when remote mtime is newer.
                if _files_differ(lp, rp):
                    shutil.copy2(rp, lp)
                    to_local += 1
                else:
                    skipped += 1
            else:
                skipped += 1

    return {'toRemote': to_remote, 'toLocal': to_local, 'skipped': skipped}


def _icloud_do_sync() -> dict:
    """Full bidirectional sync: notes (.md) + all attachment sub-dirs."""
    cfg   = paths._load_config().get('icloud', {})
    drive = cfg.get('drive_path', '')
    folder = cfg.get('folder', 'myNotes')
    if not drive or not os.path.isdir(drive):
        return {'ok': False, 'error': 'iCloud Drive path not found or not accessible.'}

    remote_root   = os.path.join(drive, folder)
    remote_attach = os.path.join(remote_root, 'attachments')
    local_notes   = paths.get_notes_dir()
    local_attach  = paths.get_attachments_dir()

    # Guard: don't create the remote folder until iCloud has had time to
    # deliver an existing one from the cloud (avoids myNotes → myNotes(1)).
    if not os.path.isdir(remote_root):
        now = time.time()
        _folder_wait_since.setdefault('icloud', now)
        elapsed = now - _folder_wait_since['icloud']
        if elapsed < _CLOUD_FOLDER_WAIT:
            _icloud_log.info(
                'Remote folder absent — waiting for iCloud to deliver it '
                '(%.0f / %.0f s)', elapsed, _CLOUD_FOLDER_WAIT)
            return {'ok': True, 'toRemote': 0, 'toLocal': 0, 'skipped': 0}
        _icloud_log.info(
            'Remote folder still absent after %.0f s — treating as fresh install',
            elapsed)
    else:
        _folder_wait_since.pop('icloud', None)   # folder arrived, reset

    # Flatten legacy notes/ subfolder into remote_root before syncing
    _migrate_remote_notes_subdir(remote_root, _icloud_log)

    # Notes live directly in remote_root (same flat layout as local)
    remote_notes  = remote_root

    # One-time sweep: delete stray *.txt files Android SAF deposited
    _cleanup_android_txt_strays(remote_notes, remote_attach, _icloud_log)

    try:
        with _icloud_sync_lock:
            _sync_deletion_log(local_notes, remote_notes, local_attach, remote_attach)
            r1   = _sync_notes_by_id(local_notes, remote_notes)
            # Build skip-sets from the merged deletion log so orphaned remote
            # files are purged rather than pulled back to local.
            _del_entries = _load_deletion_log(_deletion_log_path(local_notes))['deletions']
            _del_hist    = {
                os.path.splitext(e['path'])[0] + '.history.json'
                for e in _del_entries if e['type'] == 'note'
            }
            # Normalise separators to match os.path.join output used in _collect
            _del_attach  = {
                e['path'].replace('/', os.sep)
                for e in _del_entries if e['type'] == 'attachment'
            }
            r1h  = _icloud_sync_dirs(local_notes, remote_notes,
                                     name_filter=lambda n: n.endswith('.history.json'),
                                     skip_remote_names=_del_hist)
            r2   = _icloud_sync_dirs(local_attach, remote_attach,
                                     recursive=True,
                                     skip_remote_names=_del_attach)
        total = {k: r1[k] + r1h[k] + r2[k] for k in r1}
        _icloud_log.info('Sync done → %s', total)

        now = time.time()
        c   = paths._load_config()
        c.setdefault('icloud', {})['last_sync'] = now
        paths._save_config(c)

        _orphan_attachment_cleanup(local_notes, local_attach, _icloud_log)
        _sse_broadcast(json.dumps({'type': 'icloud_sync', 'result': total}))
        return {'ok': True, 'lastSync': now, **total}
    except Exception as e:
        _icloud_log.error('Sync failed: %s', e, exc_info=True)
        return {'ok': False, 'error': str(e)}


def _icloud_loop() -> None:
    """Background auto-sync thread."""
    _icloud_log.info('iCloud sync thread started')
    last_sync_mono = 0.0
    while True:
        cfg      = paths._load_config().get('icloud', {})
        enabled  = cfg.get('enabled', False)
        interval = int(cfg.get('sync_interval', 300))
        if enabled and cfg.get('connected', False):
            if _sync_paused_for_editing():
                _icloud_log.debug('Sync skipped — note is being edited')
                _icloud_wake.clear()
                _icloud_wake.wait(timeout=min(_EDITING_GRACE, interval))
                continue
            interval_due = (last_sync_mono == 0.0
                            or time.monotonic() - last_sync_mono >= interval)
            if not interval_due and not _placeholder_due_now():
                _icloud_wake.clear()
                _icloud_wake.wait(timeout=_sync_wait_timeout(interval, last_sync_mono))
                continue
            _forced = _placeholder_sync_pending()
            _icloud_log.debug('Auto-sync starting …')
            _sse_auto_sync('icloud', 'start')
            result = _icloud_do_sync()
            _sse_auto_sync('icloud', 'end', ok=result.get('ok', False), error=result.get('error'),
                           to_remote=result.get('toRemote', 0), to_local=result.get('toLocal', 0))
            _placeholder_settle(_forced, bool(result.get('ok')))
            last_sync_mono = time.monotonic()
        _icloud_wake.clear()
        _icloud_wake.wait(timeout=_sync_wait_timeout(interval, last_sync_mono))


_icloud_thread = threading.Thread(target=_icloud_loop,
                                  daemon=True, name='icloud-sync')
_icloud_thread.start()


def _migrate_existing_note_ids() -> None:
    """Backfill UUIDs into old notes; set trashed_at for already-trashed notes."""
    notes_dir = paths.get_notes_dir()
    if not os.path.isdir(notes_dir):
        return
    now = time.time()
    for fname in os.listdir(notes_dir):
        if not fname.endswith('.md'):
            continue
        fp = os.path.join(notes_dir, fname)
        try:
            _inject_note_id(fp)
            fm = _read_frontmatter(fp)
            if TRASH_TAG in fm.get('tags', []) and fm.get('trashed_at') is None:
                _set_trashed_at(fp, now)
        except Exception:
            pass


threading.Thread(target=_migrate_existing_note_ids,
                 daemon=True, name='note-id-migration').start()


# ── iCloud routes ─────────────────────────────────────────────────────────────

@app.route('/api/editing', methods=['POST'])
def api_editing():
    global _editing_until
    data = request.get_json(force=True, silent=True) or {}
    if data.get('active'):
        _editing_until = time.time() + _EDITING_GRACE
    else:
        _editing_until = 0.0
    return '', 204


@app.route('/api/icloud', methods=['GET'])
def get_icloud():
    cfg = paths._load_config().get('icloud', {})
    last = cfg.get('last_sync', 0)
    return jsonify({
        'connected':    cfg.get('connected',    False),
        'enabled':      cfg.get('enabled',      False),
        'drivePath':    cfg.get('drive_path',   ''),
        'folder':       cfg.get('folder',       'myNotes'),
        'syncInterval': int(cfg.get('sync_interval', 300)),
        'lastSync':     last,
        'lastSyncStr':  (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last))
                         if last else 'Never'),
    })


@app.route('/api/icloud/detect', methods=['GET'])
def icloud_detect():
    path = _detect_icloud_drive()
    return jsonify({'found': path is not None, 'path': path or ''})


def _looks_like_mynotes(path: str) -> bool:
    """A directory is treated as an existing myNotes sync folder if it contains a
    notes/ or attachments/ subfolder, or any .md file directly inside it."""
    try:
        if os.path.isdir(os.path.join(path, 'notes')) or os.path.isdir(os.path.join(path, 'attachments')):
            return True
        for name in os.listdir(path):
            if name.endswith('.md'):
                return True
    except OSError:
        pass
    return False


def _find_existing_mynotes(root: str, folder_name: str, max_depth: int = 4) -> 'str | None':
    """Recursively search a drive for an existing sync folder named *folder_name*
    (within folders and subfolders, up to *max_depth* levels). Returns the full path
    of the best match, or None. A direct hit at root/folder_name is preferred; otherwise
    a match validated by _looks_like_mynotes wins over a same-named-but-empty folder."""
    if not root or not os.path.isdir(root):
        return None

    # Fast path: the conventional location directly under the drive root.
    direct = os.path.join(root, folder_name)
    if os.path.isdir(direct):
        return direct

    fallback_match = None
    target = folder_name.lower()
    root_depth = root.rstrip('\\/').count(os.sep)
    for cur, dirs, _files in os.walk(root):
        # Bound the search depth and skip hidden/system folders to stay fast on large drives.
        depth = cur.rstrip('\\/').count(os.sep) - root_depth
        if depth >= max_depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('$')]
        for d in dirs:
            if d.lower() == target:
                hit = os.path.join(cur, d)
                if _looks_like_mynotes(hit):
                    return hit
                if fallback_match is None:
                    fallback_match = hit
    return fallback_match


@app.route('/api/icloud/check-folder', methods=['GET'])
def icloud_check_folder():
    """Recursively search the drive for an existing myNotes sync folder so the setup
    wizard can skip the create-folder step and use the found one. Falls back to
    suggesting creation only when nothing is found anywhere on the drive."""
    drive  = request.args.get('drivePath', '').strip()
    folder = request.args.get('folder', 'myNotes').strip() or 'myNotes'
    found  = _find_existing_mynotes(drive, folder)
    if found:
        return jsonify({'exists': True, 'path': found,
                        'drivePath': os.path.dirname(found),
                        'folder': os.path.basename(found)})
    return jsonify({'exists': False, 'path': os.path.join(drive, folder)})


@app.route('/api/icloud/setup', methods=['POST'])
def icloud_setup():
    """Validate path, create folder structure, mark connected."""
    data   = request.get_json(force=True)
    drive  = str(data.get('drivePath', '')).strip()
    folder = str(data.get('folder', 'myNotes')).strip() or 'myNotes'
    if not drive:
        return jsonify({'ok': False, 'error': 'Please enter the iCloud Drive path.'})
    if not os.path.isdir(drive):
        return jsonify({'ok': False,
                        'error': f'Folder not found: {drive}\n'
                                  'Make sure iCloud for Windows is installed and signed in.'})
    try:
        remote_root  = os.path.join(drive, folder)
        attach_subs  = [
            os.path.join('attachments', 'images'),
            os.path.join('attachments', 'documents'),
            os.path.join('attachments', 'archives'),
            os.path.join('attachments', 'videos'),
        ]
        if os.path.isdir(remote_root):
            # Folder already exists (may not be fully downloaded yet — iCloud
            # shows folders as directories even before their contents arrive).
            # Only fill in any missing attachment subdirs; never recreate the
            # root to avoid triggering a conflict-rename (myNotes → myNotes(1)).
            for sub in attach_subs:
                os.makedirs(os.path.join(remote_root, sub), exist_ok=True)
            _icloud_log.info('Using existing folder at %s', remote_root)
        else:
            # Folder does not exist locally yet — iCloud may still be
            # downloading it from another device.  Do NOT create it here;
            # the first sync pass will create it safely via os.makedirs once
            # the cloud client has had time to resolve its state.
            _icloud_log.info('Remote folder not present yet — will be created on first sync: %s', remote_root)

        cfg = paths._load_config()
        c   = cfg.setdefault('icloud', {})
        c['drive_path']    = drive
        c['folder']        = folder
        c['connected']     = True
        c['enabled']       = True
        c['sync_interval'] = int(data.get('syncInterval', 300))
        paths._save_config(cfg)
        _folder_wait_since.pop('icloud', None)   # fresh window on every setup
        _icloud_wake.set()
        return jsonify({'ok': True})
    except Exception as e:
        _icloud_log.error('setup failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/icloud', methods=['POST'])
def save_icloud():
    """Update enabled / sync-interval without re-running setup."""
    data = request.get_json(force=True)
    cfg  = paths._load_config()
    c    = cfg.setdefault('icloud', {})
    if 'enabled'      in data: c['enabled']       = bool(data['enabled'])
    if 'syncInterval' in data: c['sync_interval'] = max(60, int(data['syncInterval']))
    paths._save_config(cfg)
    _icloud_wake.set()
    return jsonify({'ok': True})


@app.route('/api/icloud/sync', methods=['POST'])
def icloud_sync_now():
    result = _icloud_do_sync()
    return jsonify(result)


@app.route('/api/icloud', methods=['DELETE'])
def icloud_disconnect():
    cfg = paths._load_config()
    c   = cfg.get('icloud', {})
    c['connected'] = False
    c['enabled']   = False
    paths._save_config(cfg)
    _icloud_log.info('iCloud disconnected by user')
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Google Drive (Android) sync
# ---------------------------------------------------------------------------

_gdrive_log  = logging.getLogger('[GDrive]')
_gdrive_log.setLevel(logging.DEBUG)
if not _gdrive_log.handlers:
    _gh = logging.StreamHandler()
    _gh.setFormatter(logging.Formatter('%(name)s %(message)s'))
    _gdrive_log.addHandler(_gh)

_gdrive_wake      = threading.Event()
_gdrive_sync_lock = threading.Lock()


def _detect_google_drive() -> 'str | None':
    """Locate the Google Drive local sync folder on this Windows machine."""
    # ── 1. Registry (Google Drive for Desktop) ────────────────────────────
    try:
        import winreg
        _reg_keys = [
            (winreg.HKEY_CURRENT_USER,  r'Software\Google\DriveFS'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Google\DriveFS'),
            (winreg.HKEY_CURRENT_USER,  r'Software\Google\Drive'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Google\Drive'),
        ]
        for hive, keypath in _reg_keys:
            try:
                with winreg.OpenKey(hive, keypath) as k:
                    for valname in ('DefaultMountPoint', 'Path', 'MountPoint'):
                        try:
                            val, _ = winreg.QueryValueEx(k, valname)
                            p = str(val).strip()
                            # Google Drive for Desktop mounts as a virtual drive,
                            # "My Drive" is a sub-folder of the mount point
                            for candidate in [os.path.join(p, 'My Drive'), p]:
                                if candidate and os.path.isdir(candidate):
                                    _gdrive_log.debug(
                                        'Found via registry [%s] %r', keypath, candidate)
                                    return candidate
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass

    # ── 2. Scan all drive letters for a Google Drive virtual mount ─────────
    import string
    for letter in string.ascii_uppercase:
        p = f'{letter}:\\My Drive'
        if os.path.isdir(p):
            _gdrive_log.debug('Found virtual drive mount: %r', p)
            return p

    # ── 3. Common filesystem paths (Backup and Sync / legacy) ─────────────
    home = os.path.expanduser('~')
    fs_candidates = [
        os.path.join(home, 'Google Drive'),
        os.path.join(home, 'Google Drive (Stream)'),
        os.path.join(home, 'My Drive'),
        os.path.join(home, 'OneDrive'),    # fallback hint only — not Google Drive
    ]
    for p in fs_candidates[:3]:            # skip OneDrive
        if p and os.path.isdir(p):
            _gdrive_log.debug('Found via filesystem: %r', p)
            return p

    _gdrive_log.debug('Google Drive folder not found on this machine')
    return None


def _gdrive_do_sync() -> dict:
    """Full bidirectional sync (notes + attachments) with Google Drive folder."""
    cfg    = paths._load_config().get('gdrive', {})
    drive  = cfg.get('drive_path', '')
    folder = cfg.get('folder', 'myNotes')
    if not drive or not os.path.isdir(drive):
        return {'ok': False, 'error': 'Google Drive path not found or not accessible.'}

    remote_root   = os.path.join(drive, folder)
    remote_attach = os.path.join(remote_root, 'attachments')
    local_notes   = paths.get_notes_dir()
    local_attach  = paths.get_attachments_dir()

    # Guard: same delivery-wait logic as iCloud (avoids myNotes → myNotes(1)).
    if not os.path.isdir(remote_root):
        now = time.time()
        _folder_wait_since.setdefault('gdrive', now)
        elapsed = now - _folder_wait_since['gdrive']
        if elapsed < _CLOUD_FOLDER_WAIT:
            _gdrive_log.info(
                'Remote folder absent — waiting for GDrive to deliver it '
                '(%.0f / %.0f s)', elapsed, _CLOUD_FOLDER_WAIT)
            return {'ok': True, 'toRemote': 0, 'toLocal': 0, 'skipped': 0}
        _gdrive_log.info(
            'Remote folder still absent after %.0f s — treating as fresh install',
            elapsed)
    else:
        _folder_wait_since.pop('gdrive', None)

    # Flatten legacy notes/ subfolder into remote_root before syncing
    _migrate_remote_notes_subdir(remote_root, _gdrive_log)

    # Notes live directly in remote_root (same flat layout as local)
    remote_notes  = remote_root

    # One-time sweep: delete stray *.txt files Android SAF deposited
    _cleanup_android_txt_strays(remote_notes, remote_attach, _gdrive_log)

    try:
        with _gdrive_sync_lock:
            _sync_deletion_log(local_notes, remote_notes, local_attach, remote_attach)
            r1   = _sync_notes_by_id(local_notes, remote_notes)
            _del_entries = _load_deletion_log(_deletion_log_path(local_notes))['deletions']
            _del_hist    = {
                os.path.splitext(e['path'])[0] + '.history.json'
                for e in _del_entries if e['type'] == 'note'
            }
            _del_attach  = {
                e['path'].replace('/', os.sep)
                for e in _del_entries if e['type'] == 'attachment'
            }
            r1h  = _icloud_sync_dirs(local_notes, remote_notes,
                                     name_filter=lambda n: n.endswith('.history.json'),
                                     skip_remote_names=_del_hist)
            r2   = _icloud_sync_dirs(local_attach, remote_attach,
                                     recursive=True,
                                     skip_remote_names=_del_attach)
        total = {k: r1[k] + r1h[k] + r2[k] for k in r1}
        _gdrive_log.info('Sync done → %s', total)

        now = time.time()
        c   = paths._load_config()
        c.setdefault('gdrive', {})['last_sync'] = now
        paths._save_config(c)

        _orphan_attachment_cleanup(local_notes, local_attach, _gdrive_log)
        _sse_broadcast(json.dumps({'type': 'gdrive_sync', 'result': total}))
        return {'ok': True, 'lastSync': now, **total}
    except Exception as e:
        _gdrive_log.error('Sync failed: %s', e, exc_info=True)
        return {'ok': False, 'error': str(e)}


def _gdrive_loop() -> None:
    """Background auto-sync thread."""
    _gdrive_log.info('Google Drive sync thread started')
    _last_sync_mono = 0.0
    while True:
        cfg      = paths._load_config().get('gdrive', {})
        enabled  = cfg.get('enabled', False)
        interval = int(cfg.get('sync_interval', 300))
        if enabled and cfg.get('connected', False):
            now_mono = time.monotonic()
            elapsed  = now_mono - _last_sync_mono
            # A placeholder deadline that has come due is a real wake-up, not a
            # spurious one, so it is allowed through the too-soon guard below.
            if (_last_sync_mono and elapsed < max(interval - 5, interval * 0.9)
                    and not _placeholder_due_now()):
                # Woken too soon after the last sync — skip this spurious wake-up.
                _gdrive_wake.clear()
                _gdrive_wake.wait(timeout=max(interval - elapsed, 5))
                continue
            if _sync_paused_for_editing():
                _gdrive_log.debug('Sync skipped — note is being edited')
                _gdrive_wake.clear()
                _gdrive_wake.wait(timeout=min(_EDITING_GRACE, interval))
                continue
            _forced = _placeholder_sync_pending()
            _gdrive_log.debug('Auto-sync starting …')
            _sse_auto_sync('gdrive', 'start')
            result = _gdrive_do_sync()
            _sse_auto_sync('gdrive', 'end', ok=result.get('ok', False), error=result.get('error'),
                           to_remote=result.get('toRemote', 0), to_local=result.get('toLocal', 0))
            _placeholder_settle(_forced, bool(result.get('ok')))
            _last_sync_mono = time.monotonic()
        _gdrive_wake.clear()
        _gdrive_wake.wait(timeout=_sync_wait_timeout(interval, _last_sync_mono))


_gdrive_thread = threading.Thread(target=_gdrive_loop,
                                  daemon=True, name='gdrive-sync')
_gdrive_thread.start()


# ── Google Drive routes ───────────────────────────────────────────────────────

@app.route('/api/gdrive', methods=['GET'])
def get_gdrive():
    cfg  = paths._load_config().get('gdrive', {})
    last = cfg.get('last_sync', 0)
    return jsonify({
        'connected':    cfg.get('connected',    False),
        'enabled':      cfg.get('enabled',      False),
        'drivePath':    cfg.get('drive_path',   ''),
        'folder':       cfg.get('folder',       'myNotes'),
        'syncInterval': int(cfg.get('sync_interval', 300)),
        'lastSync':     last,
        'lastSyncStr':  (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last))
                         if last else 'Never'),
    })


@app.route('/api/gdrive/detect', methods=['GET'])
def gdrive_detect():
    path = _detect_google_drive()
    return jsonify({'found': path is not None, 'path': path or ''})


@app.route('/api/gdrive/check-folder', methods=['GET'])
def gdrive_check_folder():
    """Recursively search the drive for an existing myNotes sync folder so the setup
    wizard can skip the create-folder step and use the found one. Falls back to
    suggesting creation only when nothing is found anywhere on the drive."""
    drive  = request.args.get('drivePath', '').strip()
    folder = request.args.get('folder', 'myNotes').strip() or 'myNotes'
    found  = _find_existing_mynotes(drive, folder)
    if found:
        return jsonify({'exists': True, 'path': found,
                        'drivePath': os.path.dirname(found),
                        'folder': os.path.basename(found)})
    return jsonify({'exists': False, 'path': os.path.join(drive, folder)})


@app.route('/api/gdrive/setup', methods=['POST'])
def gdrive_setup():
    """Validate path, create folder structure, mark connected."""
    data   = request.get_json(force=True)
    drive  = str(data.get('drivePath', '')).strip()
    folder = str(data.get('folder', 'myNotes')).strip() or 'myNotes'
    if not drive:
        return jsonify({'ok': False, 'error': 'Please enter the Google Drive path.'})
    if not os.path.isdir(drive):
        return jsonify({'ok': False,
                        'error': f'Folder not found: {drive}\n'
                                  'Make sure Google Drive for Desktop is installed and signed in.'})
    try:
        remote_root  = os.path.join(drive, folder)
        attach_subs  = [
            os.path.join('attachments', 'images'),
            os.path.join('attachments', 'documents'),
            os.path.join('attachments', 'archives'),
            os.path.join('attachments', 'videos'),
        ]
        if os.path.isdir(remote_root):
            # Folder already exists — only fill in any missing attachment subdirs.
            for sub in attach_subs:
                os.makedirs(os.path.join(remote_root, sub), exist_ok=True)
            _gdrive_log.info('Using existing folder at %s', remote_root)
        else:
            # Folder not present locally yet — GDrive may still be downloading
            # it.  Let the first sync create it to avoid a name-collision rename.
            _gdrive_log.info('Remote folder not present yet — will be created on first sync: %s', remote_root)

        cfg = paths._load_config()
        c   = cfg.setdefault('gdrive', {})
        c['drive_path']    = drive
        c['folder']        = folder
        c['connected']     = True
        c['enabled']       = True
        c['sync_interval'] = int(data.get('syncInterval', 300))
        paths._save_config(cfg)
        _folder_wait_since.pop('gdrive', None)   # fresh window on every setup
        _gdrive_wake.set()
        return jsonify({'ok': True})
    except Exception as e:
        _gdrive_log.error('setup failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/gdrive', methods=['POST'])
def save_gdrive():
    """Update enabled / sync-interval without re-running setup."""
    data = request.get_json(force=True)
    cfg  = paths._load_config()
    c    = cfg.setdefault('gdrive', {})
    if 'enabled'      in data: c['enabled']       = bool(data['enabled'])
    if 'syncInterval' in data: c['sync_interval'] = max(60, int(data['syncInterval']))
    paths._save_config(cfg)
    _gdrive_wake.set()
    return jsonify({'ok': True})


@app.route('/api/gdrive/sync', methods=['POST'])
def gdrive_sync_now():
    result = _gdrive_do_sync()
    return jsonify(result)


@app.route('/api/gdrive', methods=['DELETE'])
def gdrive_disconnect():
    cfg = paths._load_config()
    c   = cfg.get('gdrive', {})
    c['connected'] = False
    c['enabled']   = False
    paths._save_config(cfg)
    _gdrive_log.info('Google Drive disconnected by user')
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# GitHub sync
# ---------------------------------------------------------------------------

_github_wake      = threading.Event()
_github_sync_lock = threading.Lock()
_github_log       = logging.getLogger('[GitHub]')
_github_log.setLevel(logging.DEBUG)
if not _github_log.handlers:
    try:
        os.makedirs(paths.APPDATA_DIR, exist_ok=True)
        _gh_log_file = os.path.join(paths.APPDATA_DIR, 'github_sync.log')
        _gh_fh = logging.FileHandler(_gh_log_file, encoding='utf-8')
        _gh_fh.setFormatter(logging.Formatter('%(asctime)s %(name)s %(message)s'))
        _github_log.addHandler(_gh_fh)
        print(f'[GitHub] log file: {_gh_log_file}', flush=True)
    except Exception as _e:
        print(f'[GitHub] WARNING: could not create log file: {_e}', flush=True)
    _gh_sh = logging.StreamHandler()
    _gh_sh.setFormatter(logging.Formatter('%(name)s %(message)s'))
    _github_log.addHandler(_gh_sh)

_GITHUB_STATE_FILE = os.path.join(paths.APPDATA_DIR, 'github_state.json')
_GITHUB_PAT_FILE   = os.path.join(paths.APPDATA_DIR, 'github_pat.bin')


def _gh_cfg() -> dict:
    return paths._load_config().get('github', {})


def _save_gh_pat(pat: str) -> None:
    """Encrypt the PAT with DPAPI and store it in a separate binary file."""
    os.makedirs(paths.APPDATA_DIR, exist_ok=True)
    with open(_GITHUB_PAT_FILE, 'wb') as f:
        f.write(_dpapi_encrypt(pat.encode('utf-8')))


def _gh_pat() -> str:
    """Load and decrypt the PAT from the DPAPI-protected file."""
    try:
        with open(_GITHUB_PAT_FILE, 'rb') as f:
            return _dpapi_decrypt(f.read()).decode('utf-8')
    except Exception:
        return ''


def _gh_api_base(repo_url: str) -> str:
    """https://github.com/owner/repo.git → https://api.github.com/repos/owner/repo"""
    url = repo_url.strip().rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    parts = url.split('github.com/', 1)
    if len(parts) != 2 or not parts[1]:
        raise ValueError(f'Cannot parse GitHub URL: {repo_url}')
    return f'https://api.github.com/repos/{parts[1].rstrip("/")}'


def _gh_request(method: str, url: str, pat: str, json_body=None, timeout: int = 30) -> dict:
    headers = {
        'Authorization': f'Bearer {pat}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'myNote-sync/1.0',
    }
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    if not resp.ok:
        msg = ''
        try:
            msg = resp.json().get('message', '')
        except Exception:
            pass
        raise RuntimeError(f'GitHub {method}: HTTP {resp.status_code} {msg}')
    return resp.json() if resp.content else {}


def _git_blob_sha(data: bytes) -> str:
    h = _hashlib.sha1()
    h.update(f'blob {len(data)}\0'.encode())
    h.update(data)
    return h.hexdigest()


_LARGE_FILE_THRESHOLD = 900 * 1024        # 900 KB — GitHub's Contents API caps uploads around 1MB
_GIT_HARD_MAX         = 40 * 1024 * 1024  # GitHub's Git Data API rejected a 47MB blob in testing
_GITHUB_TOO_LARGE_FILE = os.path.join(paths.APPDATA_DIR, 'github_too_large.json')

_default_branch_cache: dict = {}


def _load_too_large_set() -> set:
    try:
        with open(_GITHUB_TOO_LARGE_FILE, 'r', encoding='utf-8') as f:
            return set(_json.load(f))
    except (OSError, _json.JSONDecodeError):
        return set()


def _save_too_large_set(rels: set) -> None:
    tmp = _GITHUB_TOO_LARGE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        _json.dump(sorted(rels), f)
    os.replace(tmp, _GITHUB_TOO_LARGE_FILE)


def _mark_too_large(rel: str) -> None:
    rels = _load_too_large_set()
    rels.add(rel)
    _save_too_large_set(rels)


def _gh_default_branch(api_base: str, pat: str) -> str:
    if api_base in _default_branch_cache:
        return _default_branch_cache[api_base]
    repo = _gh_request('GET', api_base, pat)
    branch = repo.get('default_branch', 'main')
    _default_branch_cache[api_base] = branch
    return branch


def _gh_data_push_file(api_base: str, pat: str, rel: str, raw: bytes) -> str:
    """Pushes a large file (>=900KB) via the Git Data API instead of the Contents API, which
    GitHub caps around 1MB. Creates a blob, grafts it into a new tree off the current commit,
    commits it, and moves the branch ref forward — i.e. a manual single-file 'git push'."""
    branch = _gh_default_branch(api_base, pat)
    # Scale the timeout to payload size — a 30s default isn't enough for large video/audio
    # uploads (base64 inflates size ~33%), especially over slower upstream connections.
    upload_timeout = max(60, len(raw) // (256 * 1024))  # ~4s per 1MB, floor of 60s
    blob = _gh_request('POST', f'{api_base}/git/blobs', pat, {
        'content': base64.b64encode(raw).decode(), 'encoding': 'base64',
    }, timeout=upload_timeout)
    ref = _gh_request('GET', f'{api_base}/git/refs/heads/{branch}', pat)
    parent_commit_sha = ref['object']['sha']
    parent_commit = _gh_request('GET', f'{api_base}/git/commits/{parent_commit_sha}', pat)
    new_tree = _gh_request('POST', f'{api_base}/git/trees', pat, {
        'base_tree': parent_commit['tree']['sha'],
        'tree': [{'path': rel, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}],
    })
    new_commit = _gh_request('POST', f'{api_base}/git/commits', pat, {
        'message': f'sync: add/update large file {rel}',
        'tree': new_tree['sha'],
        'parents': [parent_commit_sha],
    })
    _gh_request('PATCH', f'{api_base}/git/refs/heads/{branch}', pat, {'sha': new_commit['sha']})
    return blob['sha']


def _gh_load_state() -> dict:
    try:
        with open(_GITHUB_STATE_FILE, 'r', encoding='utf-8') as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError):
        return {}


def _gh_save_state(state: dict) -> None:
    tmp = _GITHUB_STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        _json.dump(state, f)
    os.replace(tmp, _GITHUB_STATE_FILE)


def _gh_should_sync(rel: str) -> bool:
    """Return True if this relative path belongs in the GitHub repo."""
    parts = rel.split('/')
    if any(p.startswith('.') for p in parts):
        return False
    fname = parts[-1]
    if fname.endswith('.tmp'):
        return False
    if len(parts) == 1:
        return fname.endswith('.md') or fname.endswith('.history.json')
    if parts[0] == 'attachments' and len(parts) == 3:
        return True
    return False


def _gh_file_commit_time(api_base: str, pat: str, rel: str) -> float:
    """Return the UTC timestamp of the most recent GitHub commit that touched rel."""
    try:
        import urllib.parse as _up
        from datetime import datetime, timezone as _tz
        resp = _gh_request('GET',
            f'{api_base}/commits?path={_up.quote(rel, safe="")}&per_page=1', pat)
        if resp:
            dt_str = resp[0]['commit']['committer']['date']
            return datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=_tz.utc).timestamp()
    except Exception:
        pass
    return 0.0


def _gh_fetch_blob(api_base: str, pat: str, sha: str) -> bytes:
    """Fetch raw blob content by git object SHA. Generous timeout since this is also used
    for large attachment downloads (video/audio), not just small merge-base text fetches."""
    resp = _gh_request('GET', f'{api_base}/git/blobs/{sha}', pat, timeout=120)
    return base64.b64decode(resp.get('content', '').replace('\n', ''))


def _gh_remote_entry_time(api_base: str, pat: str, rel: str,
                          github_files: dict, gh_sha: str) -> float:
    """When the remote copy of `rel` was entered, as epoch seconds.

    Prefers the note's history sidecar in the repo, which both apps write at
    edit time, and falls back to the file's commit time — that is when the other
    device pushed rather than when the text was typed, but it still orders the
    two sides correctly in the overwhelming majority of cases."""
    hist_rel = re.sub(r'\.md$', '.history.json', rel)
    if hist_rel in github_files:
        try:
            blob = _gh_fetch_blob(api_base, pat, github_files[hist_rel])
            entries = _json.loads(blob.decode('utf-8', errors='replace'))
            times = [t for t in (_history_ts(e) for e in entries) if t > 0]
            if times:
                return max(times)
        except Exception:
            pass
    try:
        return _gh_file_commit_time(api_base, pat, rel)
    except Exception:
        return 0.0


def _removed_base_indices(base_lines: list, side_lines: list) -> set:
    """Indices of base lines this side no longer has.

    'replace' counts as removal of the old lines — the replacement itself comes
    back through _added_blocks and is placed by timestamp like any other
    addition."""
    from difflib import SequenceMatcher
    gone = set()
    for tag, i1, i2, _j1, _j2 in SequenceMatcher(
            None, base_lines, side_lines, autojunk=False).get_opcodes():
        if tag in ('delete', 'replace'):
            gone.update(range(i1, i2))
    return gone


def _added_blocks(base_lines: list, side_lines: list) -> list:
    """Contiguous runs of lines present in `side` but not in `base`."""
    from difflib import SequenceMatcher
    blocks = []
    for tag, _i1, _i2, j1, j2 in SequenceMatcher(
            None, base_lines, side_lines, autojunk=False).get_opcodes():
        if tag in ('insert', 'replace') and j2 > j1:
            blocks.append(side_lines[j1:j2])
    return blocks


def _merge_by_timeline(base: str, local: str, remote: str,
                       local_at: float, remote_at: float) -> str:
    """Merge two concurrent versions by when their content was entered.

    Shared text is kept once; whatever each side added on top is then stacked
    oldest-first, one blank line between blocks.  So a phone note typed at 10:00
    and a PC note typed at 11:00 come back in that order, and neither is lost.

    Unlike a 3-way text merge this never rewrites a line in place, which is why
    a paragraph reworded on one device arrives as a second copy rather than
    replacing the first — the trade for never dropping anything.
    """
    if local == remote:
        return local

    b = base.splitlines()
    l = local.splitlines()
    r = remote.splitlines()

    # Lines the base had that a side has since dropped. A deletion on either
    # side applies: without this the merge could only ever grow a note, so a
    # line removed on one device was restored from the base and pushed straight
    # back to the device that deleted it.
    removed   = _removed_base_indices(b, l) | _removed_base_indices(b, r)
    surviving = [ln for i, ln in enumerate(b) if i not in removed]

    sides = [
        (local_at,  _added_blocks(b, l)),
        (remote_at, _added_blocks(b, r)),
    ]
    # Oldest first. Ties keep local ahead of remote for a stable result.
    sides.sort(key=lambda s: s[0])

    out = list(surviving)
    seen = {ln.strip() for ln in out if ln.strip()}
    for _at, blocks in sides:
        for block in blocks:
            text = '\n'.join(block).strip('\n')
            if not text.strip():
                continue
            # Skip a block already present — the same words typed on both
            # devices, or a stale base making shared text look "added".
            kept = [ln for ln in text.split('\n') if ln.strip() not in seen]
            # Trim blank edges so the separator below is the only gap — a block
            # can start or end blank once shared lines are filtered out of it.
            while kept and not kept[0].strip():
                kept.pop(0)
            while kept and not kept[-1].strip():
                kept.pop()
            if not any(ln.strip() for ln in kept):
                continue
            if out and out[-1].strip():
                out.append('')          # the one blank line between blocks
            out.extend(kept)
            seen.update(ln.strip() for ln in kept if ln.strip())

    return '\n'.join(out).rstrip('\n') + '\n'


def _extend_terminated(out: list, block: list) -> None:
    """Append a block, ending it on a line boundary.

    A conflict marker written after an unterminated line would otherwise be
    glued to the end of that text — "...last words=======" — which no longer
    reads as a marker at all.
    """
    if not block:
        return
    out.extend(block[:-1])
    last = block[-1]
    out.append(last if last.endswith('\n') else last + '\n')


def _merge_texts(base: str, local: str, remote: str) -> 'tuple[str, bool]':
    """
    3-way line merge. Returns (merged_text, had_conflicts).
    Non-overlapping changes from both sides are applied automatically.
    Insertions at the same position are merged (remote order first, unique local appended).
    Overlapping replacements/deletions get <<<< ==== >>>> conflict markers.
    """
    from difflib import SequenceMatcher

    # Normalise line endings before diffing: the PC historically saved CRLF
    # while the mobile app saves LF, making otherwise-identical lines compare
    # as different and defeating both the diff and the insertion dedupe.
    # Merged output is always LF.
    #
    # Also guarantee a trailing newline.  An editor saves exactly what was
    # typed, so a note rarely ends with one — and then appending text *modifies*
    # the last line instead of inserting after it.  Both sides end up replacing
    # the same line, so two independent appends collide as a conflict rather
    # than merging.  Terminating every side first makes an append a true
    # insertion.
    def _norm(s: str) -> str:
        lf = s.replace('\r\n', '\n').replace('\r', '\n')
        return lf + '\n' if lf and not lf.endswith('\n') else lf

    b = _norm(base).splitlines(keepends=True)
    l = _norm(local).splitlines(keepends=True)
    r = _norm(remote).splitlines(keepends=True)

    # Degenerate-base guard: when the base shares (almost) nothing with either
    # side — e.g. stale prev_sha after a rename or repo wipe — both sides look
    # like one giant insertion and the merge below would concatenate the two
    # full documents into a doubled note.  Refuse to merge in that case and
    # signal a conflict so the caller falls back to last-write-wins.
    if b and l and r:
        _b_set = {ln.rstrip() for ln in b if ln.strip()}
        _l_set = {ln.rstrip() for ln in l if ln.strip()}
        _r_set = {ln.rstrip() for ln in r if ln.strip()}
        if _b_set and _l_set and _r_set:
            _l_overlap = len(_b_set & _l_set) / len(_b_set | _l_set)
            _r_overlap = len(_b_set & _r_set) / len(_b_set | _r_set)
            if _l_overlap < 0.2 and _r_overlap < 0.2:
                raise ValueError('merge base unrelated to both sides')
    elif not b and (l and r):
        raise ValueError('merge base is empty')

    l_ch = [(i1, i2, j1, j2) for tag, i1, i2, j1, j2
            in SequenceMatcher(None, b, l, autojunk=False).get_opcodes()
            if tag != 'equal']
    r_ch = [(i1, i2, j1, j2) for tag, i1, i2, j1, j2
            in SequenceMatcher(None, b, r, autojunk=False).get_opcodes()
            if tag != 'equal']

    out = []
    had_conflicts = False
    pos = 0
    li = ri = 0

    while True:
        # Skip fully-consumed changes
        while li < len(l_ch) and l_ch[li][1] <= pos:
            li += 1
        while ri < len(r_ch) and r_ch[ri][1] <= pos:
            ri += 1

        next_l = l_ch[li][0] if li < len(l_ch) else None
        next_r = r_ch[ri][0] if ri < len(r_ch) else None

        if next_l is None and next_r is None:
            out.extend(b[pos:])
            break

        next_ev = min(x for x in [next_l, next_r] if x is not None)

        # Emit unchanged base lines up to the next event
        if next_ev > pos:
            out.extend(b[pos:next_ev])
            pos = next_ev

        l_here = l_ch[li] if li < len(l_ch) and l_ch[li][0] == pos else None
        r_here = r_ch[ri] if ri < len(r_ch) and r_ch[ri][0] == pos else None

        if l_here and r_here:
            l_i1, l_i2, l_j1, l_j2 = l_here
            r_i1, r_i2, r_j1, r_j2 = r_here
            lc = l[l_j1:l_j2]
            rc = r[r_j1:r_j2]

            if lc == rc and l_i2 == r_i2:
                # Both made the same change
                out.extend(rc)
                pos = l_i2
                li += 1
                ri += 1
            elif l_i1 == l_i2 and r_i1 == r_i2:
                # Both are pure insertions at the same spot — merge (remote first).
                # Dedupe ignoring trailing whitespace so a CRLF/trailing-space
                # variant of the same line doesn't get inserted twice.
                merged_ins   = list(rc)
                _stripped    = {ln.rstrip() for ln in rc}
                for line in lc:
                    if line.rstrip() not in _stripped:
                        merged_ins.append(line)
                        _stripped.add(line.rstrip())
                out.extend(merged_ins)
                # pos unchanged (insertions consume no base lines)
                li += 1
                ri += 1
            else:
                # Overlapping non-identical change → conflict markers
                conflict_end = max(l_i2, r_i2)
                had_conflicts = True
                out.append('<<<<<<< local\n')
                _extend_terminated(out, lc)
                out.append('=======\n')
                _extend_terminated(out, rc)
                out.append('>>>>>>> github\n')
                pos = conflict_end
                # Advance past anything inside the conflict region
                while li < len(l_ch) and l_ch[li][0] < conflict_end:
                    li += 1
                while ri < len(r_ch) and r_ch[ri][0] < conflict_end:
                    ri += 1
        elif l_here:
            l_i1, l_i2, l_j1, l_j2 = l_here
            out.extend(l[l_j1:l_j2])
            pos = l_i2
            li += 1
        else:
            r_i1, r_i2, r_j1, r_j2 = r_here
            out.extend(r[r_j1:r_j2])
            pos = r_i2
            ri += 1

    return ''.join(out), had_conflicts


def _gh_sync_deletion_log(api_base: str, pat: str, notes_dir: str, attach_dir: str) -> None:
    """Fetch .deletions.json from GitHub, union-merge with local, push merged,
    then apply the merged deletions to the local notes/attachments tree.

    .deletions.json is a dotfile excluded from the normal _gh_should_sync walk,
    so it is handled explicitly here before the main file loop."""
    local_log  = _deletion_log_path(notes_dir)
    local_data = _load_deletion_log(local_log)
    remote_sha: 'str | None' = None

    # Try to fetch the remote copy
    remote_data: dict = {'settings': {}, 'deletions': []}
    try:
        resp = _gh_request('GET', f'{api_base}/contents/.deletions.json', pat)
        raw  = base64.b64decode(resp['content'].replace('\n', ''))
        parsed = _json.loads(raw.decode('utf-8'))
        if isinstance(parsed, list):
            remote_data = {'settings': {}, 'deletions': parsed}
        else:
            remote_data = {
                'settings':  parsed.get('settings', {}),
                'deletions': parsed.get('deletions', [])
                             if isinstance(parsed.get('deletions'), list) else [],
            }
        remote_sha = resp.get('sha')
    except RuntimeError:
        pass   # file absent on GitHub — will be created below
    except Exception as exc:
        _github_log.warning('Could not fetch .deletions.json from GitHub: %s', exc)
        return

    # Settings merge: latest lastchanged wins
    l_cfg = local_data.get('settings', {})
    r_cfg = remote_data.get('settings', {})
    merged_settings = r_cfg if r_cfg.get('lastchanged', 0) > l_cfg.get('lastchanged', 0) else l_cfg

    # Deletions merge: union, newest deleted_at per path; purge entries > 6 months
    cutoff  = time.time() - 180 * 86400
    by_path: dict = {}
    for e in local_data['deletions'] + remote_data['deletions']:
        p = e['path']
        if e.get('deleted_at', 0) > cutoff:
            if p not in by_path or e.get('deleted_at', 0) > by_path[p].get('deleted_at', 0):
                by_path[p] = e

    # The push below builds its own body rather than going through
    # _save_deletion_log, so the placeholder guard has to be applied here too —
    # this is what clears an existing entry out of the shared GitHub copy.
    merged = {'settings': merged_settings,
              'deletions': _strip_placeholder_deletions(list(by_path.values()))}

    # Write merged log locally
    _save_deletion_log(local_log, merged)

    # Push merged log to GitHub
    merged_bytes = _json.dumps(merged, ensure_ascii=False, indent=2).encode('utf-8')
    body: dict = {
        'message': 'sync: update .deletions.json',
        'content': base64.b64encode(merged_bytes).decode(),
    }
    if remote_sha:
        body['sha'] = remote_sha
    try:
        _gh_request('PUT', f'{api_base}/contents/.deletions.json', pat, body)
        _github_log.debug('Pushed merged .deletions.json to GitHub')
    except RuntimeError as exc:
        _github_log.warning('Could not push .deletions.json: %s', exc)

    # Apply merged deletions to local tree
    _apply_deletion_log(notes_dir, attach_dir, local_log)


def _gh_do_sync() -> dict:
    print('[GitHub] SYNC DEBUG: _gh_do_sync() entered', flush=True)
    _github_log.info('SYNC DEBUG: _gh_do_sync() entered')
    cfg = _gh_cfg()
    _github_log.info('SYNC DEBUG: cfg=%s', {k: v for k, v in cfg.items() if k != 'pat_b64'})
    if not cfg.get('connected'):
        _github_log.info('SYNC DEBUG: early return — not connected')
        return {'ok': True, 'toRemote': 0, 'toLocal': 0, 'skipped': 0}

    pat      = _gh_pat()
    repo_url = cfg.get('repo_url', '')
    _github_log.info('SYNC DEBUG: pat present=%s repo_url=%s', bool(pat), repo_url)
    if not pat or not repo_url:
        _github_log.info('SYNC DEBUG: early return — missing pat or repo_url')
        return {'ok': False, 'error': 'Not configured'}

    try:
        api_base = _gh_api_base(repo_url)
        _github_log.info('SYNC DEBUG: api_base=%s', api_base)
    except ValueError as e:
        _github_log.info('SYNC DEBUG: early return — api_base error: %s', e)
        return {'ok': False, 'error': str(e)}

    notes_dir   = paths.get_notes_dir()
    attach_dir  = paths.get_attachments_dir()

    # ── Deletion-log sync (must happen before the main file loop) ────────────
    # .deletions.json is a dotfile so it is excluded from the normal sync walk.
    # We handle it explicitly here: fetch remote, union-merge with local,
    # push merged back, then apply the merged deletions to the local tree.
    _gh_sync_deletion_log(api_base, pat, notes_dir, attach_dir)

    # Fetch GitHub tree in a single API call
    _github_log.info('SYNC DEBUG: fetching tree from %s', api_base)
    try:
        tree = _gh_request('GET', f'{api_base}/git/trees/HEAD?recursive=1', pat)
        github_files = {
            item['path']: item['sha']
            for item in tree.get('tree', [])
            if item['type'] == 'blob' and _gh_should_sync(item['path'])
        }
        github_file_sizes = {
            item['path']: item.get('size', 0)
            for item in tree.get('tree', [])
            if item['type'] == 'blob' and _gh_should_sync(item['path'])
        }
        _github_log.info('SYNC DEBUG: tree fetched OK, %d syncable blobs', len(github_files))
    except RuntimeError as e:
        _github_log.info('SYNC DEBUG: tree fetch exception: %s', e)
        if '404' in str(e) or '409' in str(e):
            github_files = {}   # empty / uninitialised repo
            github_file_sizes = {}
        else:
            _github_log.error('Tree fetch failed: %s', e)
            return {'ok': False, 'error': str(e)}

    # Walk local notes directory
    local_files: dict = {}
    for root, dirs, files in os.walk(notes_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if fname.startswith('.') or fname.endswith('.tmp'):
                continue
            fp  = os.path.join(root, fname)
            rel = os.path.relpath(fp, notes_dir).replace('\\', '/')
            if _gh_should_sync(rel):
                local_files[rel] = fp

    # Build a map of deletion-log rels → deleted_at for the resurrection guard.
    # Files newer than their deletion timestamp were recreated and must be kept.
    _del_log_data = _load_deletion_log(_deletion_log_path(notes_dir))
    _deleted_info: dict = {}   # rel → deleted_at timestamp
    for _e in _del_log_data['deletions']:
        if _e['type'] == 'note':
            _deleted_info[_e['path']] = _e.get('deleted_at', 0)
        elif _e['type'] == 'attachment':
            _deleted_info['attachments/' + _e['path'].replace('\\', '/')] = _e.get('deleted_at', 0)
    for _rel, _del_at in list(_deleted_info.items()):
        if _rel not in local_files:
            continue
        try:
            _lmtime = os.path.getmtime(local_files[_rel])
        except OSError:
            _lmtime = 0
        if _lmtime > _del_at:
            # File was recreated after the deletion — resurrection wins, keep it
            _github_log.debug('Resurrection: keeping %s (mtime %.0f > deleted_at %.0f)',
                              _rel, _lmtime, _del_at)
        else:
            _is_attach_del = _rel.startswith('attachments/')
            if _is_attach_del and not _is_attachment_safe_to_delete(_rel[len('attachments/'):], ''):
                _github_log.debug('Skipping deletion of referenced attachment: %s', _rel)
            else:
                try:
                    os.remove(local_files[_rel])
                    _github_log.debug('Removed locally present deleted file: %s', _rel)
                except OSError:
                    pass
                del local_files[_rel]

    _deleted_rels: set = set(_deleted_info.keys())

    # State: {rel: {"sha": last_synced_gh_sha, "at": sync_timestamp}}
    # sha → detect if GitHub changed since last sync
    # at  → detect if local file changed since last sync (via mtime)
    raw_state: dict = _gh_load_state()
    new_state: dict = {}
    sync_now  = time.time()
    to_remote = to_local = skipped = 0
    too_large_set = _load_too_large_set()

    _github_log.info('SYNC DEBUG: notes_dir=%s', notes_dir)
    _github_log.info('SYNC DEBUG: github=%d local=%d state=%d',
                     len(github_files), len(local_files), len(raw_state))

    def _state_entry(rel: str) -> dict:
        v = raw_state.get(rel)
        if v is None:
            return {'sha': None, 'at': 0}
        if isinstance(v, str):          # old format migration
            return {'sha': v, 'at': 0}
        return v

    def _push_file(rel: str, raw: bytes, local_sha: str, gh_sha: 'str | None') -> None:
        nonlocal to_remote, skipped
        if len(raw) >= _LARGE_FILE_THRESHOLD:
            if len(raw) >= _GIT_HARD_MAX:
                _github_log.info('SYNC DEBUG: SKIP %s — %d KB exceeds Git\'s hard size limit', rel, len(raw) // 1024)
                skipped += 1
                return
            if rel in too_large_set:
                _github_log.info('SYNC DEBUG: SKIP %s — previously confirmed too large for Git', rel)
                skipped += 1
                return
            _github_log.info('SYNC DEBUG: PUSH %s (%d KB, via Git Data API)', rel, len(raw) // 1024)
            try:
                _gh_data_push_file(api_base, pat, rel, raw)
                new_state[rel] = {'sha': local_sha, 'at': sync_now}
                to_remote += 1
            except RuntimeError as push_err:
                _github_log.warning('Large file push failed for %s: %s', rel, push_err)
                if 'too large' in str(push_err).lower():
                    _mark_too_large(rel)
                    too_large_set.add(rel)
                skipped += 1
            return
        body: dict = {
            'message': f'sync: {"update" if gh_sha else "add"} {rel}',
            'content': base64.b64encode(raw).decode(),
        }
        if gh_sha:
            body['sha'] = gh_sha
        _github_log.info('SYNC DEBUG: PUSH %s', rel)
        try:
            _gh_request('PUT', f'{api_base}/contents/{_urlparse.quote(rel, safe="/")}', pat, body)
            new_state[rel] = {'sha': local_sha, 'at': sync_now}
            to_remote += 1
        except RuntimeError as push_err:
            if '409' in str(push_err):
                _github_log.warning('Push blocked for %s: %s', rel, push_err)
                _sse_broadcast(json.dumps({'type': 'notify',
                    'msg': f'GitHub sync: skipped "{rel}" — push protection blocked it.',
                    'level': 'warn'}))
                skipped += 1
            else:
                raise

    def _pull_file(rel: str, gh_sha: str) -> None:
        nonlocal to_local
        _github_log.info('SYNC DEBUG: PULL %s', rel)
        if github_file_sizes.get(rel, 0) >= _LARGE_FILE_THRESHOLD:
            _github_log.info('SYNC DEBUG: PULL %s (large, via Git Data API)', rel)
            content = _gh_fetch_blob(api_base, pat, gh_sha)
        else:
            resp    = _gh_request('GET', f'{api_base}/contents/{_urlparse.quote(rel, safe="/")}', pat)
            content = base64.b64decode(resp['content'].replace('\n', ''))
        dest    = os.path.join(notes_dir, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp     = dest + '.tmp'
        with open(tmp, 'wb') as wf:
            wf.write(content)
        os.replace(tmp, dest)
        new_state[rel] = {'sha': gh_sha, 'at': sync_now}
        to_local += 1

    for rel in set(github_files) | set(local_files):
        in_local  = rel in local_files
        in_github = rel in github_files

        raw = local_sha = local_mtime = None
        if in_local:
            try:
                fp = local_files[rel]
                with open(fp, 'rb') as f:
                    raw = f.read()
                local_sha   = _git_blob_sha(raw)
                local_mtime = os.path.getmtime(fp)
            except OSError as e:
                _github_log.info('SYNC DEBUG: %s OSError: %s', rel, e)
                continue

        entry    = _state_entry(rel)
        prev_sha = entry['sha']
        sync_at  = entry['at']

        if in_local and in_github:
            gh_sha = github_files[rel]

            # Deletion log wins over both sides: delete from GitHub + local
            if rel in _deleted_rels:
                _is_attach = rel.startswith('attachments/')
                if _is_attach and not _is_attachment_safe_to_delete(rel[len('attachments/'):], ''):
                    _github_log.info('SYNC DEBUG: KEEP %s — referenced by local note, skipping deletion', rel)
                    # fall through to normal sync logic
                else:
                    _github_log.info('SYNC DEBUG: DELETE %s (in deletion log, cleaning GitHub)', rel)
                    try:
                        _gh_request('DELETE', f'{api_base}/contents/{_urlparse.quote(rel, safe="/")}', pat, {
                            'message': f'sync: delete {rel}',
                            'sha':     gh_sha,
                        })
                    except RuntimeError:
                        pass
                    try:
                        os.remove(local_files[rel])
                    except OSError:
                        pass
                    continue

            if local_sha == gh_sha:
                # Content identical — no transfer needed
                new_state[rel] = {'sha': gh_sha, 'at': sync_at}
                skipped += 1
                continue

            # Determine what changed since last sync
            local_changed  = local_mtime > sync_at + 2
            remote_changed = gh_sha != prev_sha

            if local_changed and remote_changed:
                # Both PCs changed this file — attempt 3-way merge for .md files
                if rel.endswith('.md') and prev_sha:
                    try:
                        base_bytes   = _gh_fetch_blob(api_base, pat, prev_sha)
                        r_resp       = _gh_request('GET', f'{api_base}/contents/{_urlparse.quote(rel, safe="/")}', pat)
                        remote_bytes = base64.b64decode(r_resp['content'].replace('\n', ''))
                        base_text    = base_bytes.decode('utf-8', errors='replace')
                        local_text   = raw.decode('utf-8', errors='replace')
                        remote_text  = remote_bytes.decode('utf-8', errors='replace')

                        # When each side's content was entered decides the order.
                        # Locally the history sidecar knows; for the remote copy
                        # its sidecar is fetched from the repo when present,
                        # falling back to the commit time of the note itself.
                        local_at  = _note_entry_time(local_files[rel])
                        remote_at = _gh_remote_entry_time(api_base, pat, rel,
                                                          github_files, gh_sha)

                        merged_text  = _merge_by_timeline(base_text, local_text, remote_text,
                                                          local_at, remote_at)
                        merged_bytes = merged_text.encode('utf-8')
                        merged_sha   = _git_blob_sha(merged_bytes)
                        _github_log.info(
                            'SYNC DEBUG: TIMELINE MERGE %s local_at=%.0f remote_at=%.0f order=%s',
                            rel, local_at, remote_at,
                            'remote-first' if remote_at < local_at else 'local-first')

                        if merged_sha == gh_sha:
                            # All local changes already present in remote — just pull
                            _pull_file(rel, gh_sha)
                        elif merged_sha == local_sha:
                            # Merged same as local — just push local
                            _push_file(rel, raw, local_sha, gh_sha)
                        else:
                            # True merge: push merged content and update local file
                            _push_file(rel, merged_bytes, merged_sha, gh_sha)
                            fp  = local_files[rel]
                            tmp = fp + '.tmp'
                            with open(tmp, 'wb') as wf:
                                wf.write(merged_bytes)
                            os.replace(tmp, fp)

                        continue

                    except Exception as _me:
                        _github_log.warning('Merge failed for %s: %s — falling back to last-write-wins', rel, _me)

                # Non-.md file or merge failed — last write wins
                gh_time = _gh_file_commit_time(api_base, pat, rel)
                if local_mtime >= gh_time:
                    _push_file(rel, raw, local_sha, gh_sha)
                else:
                    _pull_file(rel, gh_sha)
            elif local_changed:
                _push_file(rel, raw, local_sha, gh_sha)
            elif remote_changed:
                _pull_file(rel, gh_sha)
            else:
                # Stale state (sha mismatch but no detected change) — push local
                _push_file(rel, raw, local_sha, gh_sha)

        elif in_local:
            # File exists only locally — push to GitHub as a new file
            _push_file(rel, raw, local_sha, None)

        else:
            # File exists only on GitHub
            gh_sha = github_files[rel]
            if rel in _deleted_rels or prev_sha is not None:
                # In deletion log, OR was synced before and now missing locally.
                # Guard: if a local note still references this attachment, pull it
                # back rather than propagating the deletion to GitHub — the deletion
                # log entry was stale (note was added/updated after the attachment
                # was orphaned on this device).
                _is_attach = rel.startswith('attachments/')
                if _is_attach and not _is_attachment_safe_to_delete(rel[len('attachments/'):], ''):
                    _github_log.info(
                        'SYNC DEBUG: KEEP %s — referenced by local note, pulling instead of deleting', rel)
                    _pull_file(rel, gh_sha)
                else:
                    _github_log.info('SYNC DEBUG: DELETE %s (%s)', rel,
                                     'in deletion log' if rel in _deleted_rels else 'removed locally')
                    try:
                        _gh_request('DELETE', f'{api_base}/contents/{_urlparse.quote(rel, safe="/")}', pat, {
                            'message': f'sync: delete {rel}',
                            'sha':     gh_sha,
                        })
                    except RuntimeError:
                        pass
            else:
                # Never synced on this PC and not deleted → pull from GitHub
                _pull_file(rel, gh_sha)

    _gh_save_state(new_state)

    now  = time.time()
    cfg2 = paths._load_config()
    cfg2.setdefault('github', {})['last_sync'] = now
    paths._save_config(cfg2)

    total = {'toRemote': to_remote, 'toLocal': to_local, 'skipped': skipped}
    _github_log.info('GitHub sync done → %s', total)
    _orphan_attachment_cleanup(notes_dir, attach_dir, _github_log)
    _sse_broadcast(json.dumps({'type': 'github_sync', 'result': total, 'lastSync': now}))
    return {'ok': True, **total}


def _github_loop() -> None:
    _github_log.info('GitHub sync thread started')
    last_sync_mono = 0.0
    while True:
        cfg      = _gh_cfg()
        enabled  = cfg.get('enabled', False)
        interval = int(cfg.get('sync_interval', 300))
        if enabled and cfg.get('connected', False):
            if _sync_paused_for_editing():
                _github_log.debug('Sync skipped — note is being edited')
                _github_wake.clear()
                _github_wake.wait(timeout=min(_EDITING_GRACE, interval))
                continue
            interval_due = (last_sync_mono == 0.0
                            or time.monotonic() - last_sync_mono >= interval)
            if not interval_due and not _placeholder_due_now():
                # Woken early, most likely to take note of a freshly armed
                # deadline. Sleep again on the recomputed timeout.
                _github_wake.clear()
                _github_wake.wait(timeout=_sync_wait_timeout(interval, last_sync_mono))
                continue
            _forced = _placeholder_sync_pending()
            _github_log.debug('Auto-sync starting …')
            print('[GitHub] SYNC DEBUG: about to call _gh_do_sync()', flush=True)
            _sse_auto_sync('github', 'start')
            with _github_sync_lock:
                try:
                    result = _gh_do_sync()
                    _sse_auto_sync('github', 'end', ok=result.get('ok', False), error=result.get('error'),
                                   to_remote=result.get('toRemote', 0), to_local=result.get('toLocal', 0))
                    _placeholder_settle(_forced, bool(result.get('ok')))
                except Exception as e:
                    _github_log.error('Sync error: %s', e, exc_info=True)
                    _sse_auto_sync('github', 'end', ok=False, error=str(e))
                    _placeholder_settle(_forced, False)
            last_sync_mono = time.monotonic()
        _github_wake.clear()
        _github_wake.wait(timeout=_sync_wait_timeout(interval, last_sync_mono))


# Start the GitHub sync thread only once: in a frozen exe there is no reloader,
# and under the Werkzeug reloader only the child process (WERKZEUG_RUN_MAIN=true)
# must run it — otherwise both processes sync concurrently and race each other
# (double pushes, 409s, merges against stale state).
if paths._is_frozen() or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    _github_thread = threading.Thread(target=_github_loop,
                                      daemon=True, name='github-sync')
    _github_thread.start()


@app.route('/api/github', methods=['GET'])
def api_github_get():
    cfg  = _gh_cfg()
    last = cfg.get('last_sync', 0)
    return jsonify({
        'connected':    cfg.get('connected', False),
        'enabled':      cfg.get('enabled', True),
        'repoUrl':      cfg.get('repo_url', ''),
        'syncInterval': cfg.get('sync_interval', 300),
        'lastSync':     last,
        'lastSyncStr':  _dtt.fromtimestamp(last).strftime('%Y-%m-%d %H:%M') if last else '—',
    })


@app.route('/api/github', methods=['POST'])
def api_github_save():
    data = request.get_json(force=True, silent=True) or {}
    cfg  = paths._load_config()
    g    = cfg.setdefault('github', {})
    if 'enabled' in data:
        g['enabled'] = bool(data['enabled'])
    if 'syncInterval' in data:
        g['sync_interval'] = int(data['syncInterval'])
    paths._save_config(cfg)
    _github_wake.set()
    return jsonify({'ok': True})


@app.route('/api/github/setup', methods=['POST'])
def api_github_setup():
    data     = request.get_json(force=True, silent=True) or {}
    repo_url = data.get('repoUrl', '').strip()
    pat      = data.get('pat', '').strip()
    interval = int(data.get('syncInterval', 300))
    if not repo_url or not pat:
        return jsonify({'ok': False, 'error': 'Repo URL and PAT are required'}), 400
    try:
        _gh_request('GET', _gh_api_base(repo_url), pat)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    _save_gh_pat(pat)
    cfg = paths._load_config()
    cfg['github'] = {
        'repo_url':      repo_url,
        'connected':     True,
        'enabled':       interval != 0,
        'sync_interval': interval or 300,
        'last_sync':     0,
    }
    paths._save_config(cfg)
    _github_wake.set()
    return jsonify({'ok': True})


@app.route('/api/github/sync', methods=['POST'])
def api_github_sync_now():
    with _github_sync_lock:
        try:
            result = _gh_do_sync()
        except Exception as e:
            _github_log.error('Manual sync error: %s', e, exc_info=True)
            result = {'ok': False, 'error': str(e)}
    return jsonify(result)


@app.route('/api/github', methods=['DELETE'])
def api_github_disconnect():
    cfg = paths._load_config()
    g   = cfg.get('github', {})
    g['connected'] = False
    g['enabled']   = False
    paths._save_config(cfg)
    _github_log.info('GitHub disconnected by user')
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Storage analytics
# ---------------------------------------------------------------------------

_STORAGE_CATEGORIES = [
    # key matches the attachments sub-folder name
    {'key': 'notes',     'label': 'Notes',     'color': '#ff9f0a', 'exts': {'.md'}},
    {'key': 'images',    'label': 'Images',    'color': '#30d158',
     'exts': _ATTACH_IMAGE_EXTS},
    {'key': 'documents', 'label': 'Documents', 'color': '#0a84ff',
     'exts': {'.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx',
              '.txt','.csv','.rtf','.odt','.ods','.odp'}},
    {'key': 'videos',    'label': 'Videos',    'color': '#ff375f',
     'exts': _ATTACH_VIDEO_EXTS},
    {'key': 'audio',     'label': 'Audio',     'color': '#bf5af2',
     'exts': _ATTACH_AUDIO_EXTS},
    {'key': 'archives',  'label': 'Archives',  'color': '#ffd60a',
     'exts': _ATTACH_ARCHIVE_EXTS},
]

# Attachment filenames are matched via plain substring search (no regex).
# This handles every possible embedding style — markdown, HTML, bare paths —
# and works correctly with Unicode filenames, parentheses, &, commas, etc.


@app.route('/api/trash/settings', methods=['GET'])
def get_trash_settings():
    cfg = paths._load_config()
    return jsonify({'expireMonths': cfg.get('trash_expire_months', 3)})


@app.route('/api/trash/settings', methods=['POST'])
def save_trash_settings():
    data   = request.get_json(force=True)
    months = int(data.get('expireMonths', 3))
    months = max(1, min(12, months))

    # Persist to config
    cfg = paths._load_config()
    cfg['trash_expire_months'] = months
    paths._save_config(cfg)

    # Update in-memory threshold
    global _TRASH_MAX_AGE
    _TRASH_MAX_AGE = months * 30 * 86400

    # Write timestamped setting into deletion log for cross-device sync
    log_path = _deletion_log_path()
    log_data = _load_deletion_log(log_path)
    log_data['settings']['trash_expire_months'] = months
    log_data['settings']['lastchanged'] = time.time()
    _save_deletion_log(log_path, log_data)

    return jsonify({'ok': True, 'expireMonths': months})


@app.route('/api/trash/empty', methods=['POST'])
def empty_trash():
    deleted = 0
    for fp in list(_note_files()):
        fm = _read_frontmatter(fp)
        if TRASH_TAG in fm.get('tags', []):
            try:
                _delete_note_permanently(fp)
                deleted += 1
            except Exception:
                pass
    if deleted:
        _sse_broadcast(json.dumps({'type': 'notes-updated'}))
    return jsonify({'ok': True, 'deleted': deleted})


@app.route('/api/storage', methods=['GET'])
def get_storage():
    notes_dir  = paths.get_notes_dir()
    attach_dir = paths.get_attachments_dir()

    # ── Build flat map: filename → size for every attachment file ────────────
    attach_sizes: dict[str, int] = {}   # basename → bytes
    for cat in _STORAGE_CATEGORIES[1:]:
        cat_dir = os.path.join(attach_dir, cat['key'])
        if not os.path.isdir(cat_dir):
            continue
        for fname in os.listdir(cat_dir):
            fp = os.path.join(cat_dir, fname)
            if os.path.isfile(fp):
                attach_sizes[fname] = os.path.getsize(fp)

    # ── Single-pass note scan: sizes + reverse attachment→note map ───────────
    notes_entries: list[dict] = []
    notes_total   = 0
    # filename → note title of FIRST note that references the file
    note_ref_map: dict[str, str] = {}

    if os.path.isdir(notes_dir):
        for fname in os.listdir(notes_dir):
            if not fname.endswith('.md'):
                continue
            fp        = os.path.join(notes_dir, fname)
            note_size = os.path.getsize(fp)
            notes_total += note_size
            title     = _extract_title(fp)
            attach_sum = 0
            try:
                body = open(fp, 'r', encoding='utf-8', errors='ignore').read()
                # Plain substring search: works for any syntax and any filename
                # characters (Unicode, parentheses, &, commas, etc.)
                for aname, asize in attach_sizes.items():
                    if aname in body:
                        attach_sum += asize
                        if aname not in note_ref_map:
                            note_ref_map[aname] = title
            except OSError:
                pass
            notes_entries.append({
                'name':       title,
                'noteSize':   note_size,
                'attachSize': attach_sum,
                'total':      note_size + attach_sum,
            })

    notes_entries.sort(key=lambda x: x['total'], reverse=True)

    # ── Build result categories ───────────────────────────────────────────────
    result_cats: list[dict] = []

    # Notes category
    result_cats.append({
        'key':   'notes',
        'label': 'Notes',
        'color': '#ff9f0a',
        'size':  notes_total,
        'count': len(notes_entries),
        'top': [
            {
                'name':      e['name'],
                'size':      e['total'],
                'noteTitle': '',      # not used for notes row
                'detail':    (f"note {_fmt_bytes(e['noteSize'])}"
                              + (f" + attachments {_fmt_bytes(e['attachSize'])}"
                                 if e['attachSize'] else '')),
            }
            for e in notes_entries[:3]
        ],
    })

    for cat in _STORAGE_CATEGORIES[1:]:
        cat_dir = os.path.join(attach_dir, cat['key'])
        files: list[dict] = []
        cat_total = 0
        if os.path.isdir(cat_dir):
            for f in os.listdir(cat_dir):
                fp = os.path.join(cat_dir, f)
                if os.path.isfile(fp):
                    sz = os.path.getsize(fp)
                    cat_total += sz
                    files.append({
                        'name':      f,
                        'size':      sz,
                        'noteTitle': note_ref_map.get(f, ''),
                        'detail':    _fmt_bytes(sz),
                    })
        files.sort(key=lambda x: x['size'], reverse=True)
        result_cats.append({
            'key':   cat['key'],
            'label': cat['label'],
            'color': cat['color'],
            'size':  cat_total,
            'count': len(files),
            'top':   files[:3],
        })

    # Sort categories largest → smallest
    result_cats.sort(key=lambda x: x['size'], reverse=True)

    grand_total = sum(c['size'] for c in result_cats)
    return jsonify({'total': grand_total, 'categories': result_cats})


def _fmt_bytes(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


# ---------------------------------------------------------------------------
# Browser password import (Chrome / Edge, Windows only)
# ---------------------------------------------------------------------------

_bi_log = logging.getLogger('[BI]')
_bi_log.setLevel(logging.DEBUG)
if not _bi_log.handlers:
    _bih = logging.StreamHandler()
    _bih.setFormatter(logging.Formatter('%(name)s %(message)s'))
    _bi_log.addHandler(_bih)


def _dpapi_encrypt(plaintext: bytes) -> bytes:
    """Encrypt bytes with Windows DPAPI (CryptProtectData) via ctypes."""
    import ctypes, ctypes.wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [('cbData', ctypes.wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf      = ctypes.create_string_buffer(plaintext, len(plaintext))
    blob_in  = _BLOB(len(plaintext), buf)
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out))
    if not ok:
        raise RuntimeError(
            f'DPAPI CryptProtectData failed (WinError {ctypes.GetLastError()})')
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _dpapi_decrypt(data: bytes) -> bytes:
    """Decrypt bytes with Windows DPAPI (CryptUnprotectData) via ctypes."""
    import ctypes, ctypes.wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [('cbData', ctypes.wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf      = ctypes.create_string_buffer(data, len(data))
    blob_in  = _BLOB(len(data), buf)
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out))
    if not ok:
        raise RuntimeError(
            f'DPAPI CryptUnprotectData failed (WinError {ctypes.GetLastError()})')
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _browser_aes_key(local_state_path: str) -> bytes:
    """Read and DPAPI-decrypt the AES-256 master key from Chrome/Edge Local State."""
    with open(local_state_path, 'r', encoding='utf-8') as fh:
        ls = _json.load(fh)
    enc_key = base64.b64decode(ls['os_crypt']['encrypted_key'])
    # First 5 bytes are the ASCII prefix b'DPAPI'
    return _dpapi_decrypt(enc_key[5:])


def _browser_decrypt_pwd(enc_value: bytes, aes_key: bytes) -> str:
    """Decrypt one password value from a browser Login Data SQLite column."""
    prefix = enc_value[:3]
    if prefix in (b'v10', b'v11'):
        # AES-256-GCM: 3-byte version tag + 12-byte nonce + ciphertext+tag
        nonce      = enc_value[3:15]
        ciphertext = enc_value[15:]
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            return AESGCM(aes_key).decrypt(nonce, ciphertext, None).decode('utf-8')
        except ImportError:
            return '[install cryptography package]'
        except Exception as exc:
            return f'[AES-GCM failed: {exc}]'
    elif prefix == b'v20':
        # Chrome 127+ App-Bound Encryption — requires running inside the browser process.
        # Cannot be decrypted externally. User must use Chrome CSV export.
        return '[v20 App-Bound — use Chrome CSV export]'
    elif enc_value:
        # Legacy per-entry DPAPI (pre-Chrome 80)
        try:
            return _dpapi_decrypt(enc_value).decode('utf-8', errors='replace')
        except Exception as exc:
            return f'[DPAPI failed: {exc}]'
    return ''


def _read_one_login_db(db_path: str, aes_key: bytes) -> tuple:
    """Open one SQLite Login Data file, return (creds_list, row_count, v20_count)."""
    import sqlite3, shutil, tempfile

    tmp = tempfile.mktemp(suffix='.sqlite')
    shutil.copy2(db_path, tmp)
    creds = []
    row_count = 0
    v20_count = 0
    try:
        conn = sqlite3.connect(tmp)
        cur  = conn.cursor()
        # Verify logins table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='logins'")
        if not cur.fetchone():
            _bi_log.warning('  no "logins" table in %s', db_path)
            conn.close()
            return [], 0, 0
        cur.execute('SELECT origin_url, username_value, password_value '
                    'FROM logins ORDER BY origin_url')
        rows = cur.fetchall()
        row_count = len(rows)
        _bi_log.info('  %d rows in %s', row_count, os.path.basename(db_path))
        for url, user, enc_pwd in rows:
            enc_bytes = bytes(enc_pwd) if enc_pwd else b''
            prefix    = enc_bytes[:3]
            _bi_log.debug('  url=%-40r  user=%-30r  prefix=%r  len=%d',
                          url, user, prefix, len(enc_bytes))
            if prefix == b'v20':
                v20_count += 1
            try:
                pwd = _browser_decrypt_pwd(enc_bytes, aes_key) if enc_bytes else ''
            except Exception as exc:
                _bi_log.warning('  decrypt error for %r: %s', url, exc)
                pwd = f'[error: {exc}]'
            creds.append({'url': url or '', 'username': user or '', 'password': pwd})
        conn.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return creds, row_count, v20_count


def _import_browser_passwords(browser: str) -> tuple:
    """Scan all candidate password DBs and return (creds_list, debug_lines)."""
    app_data = os.environ.get('LOCALAPPDATA', '')
    _bi_log.info('browser=%r  LOCALAPPDATA=%r', browser, app_data)

    if browser == 'chrome':
        base = os.path.join(app_data, 'Google', 'Chrome', 'User Data')
    elif browser == 'edge':
        base = os.path.join(app_data, 'Microsoft', 'Edge', 'User Data')
    else:
        raise ValueError(f'Unknown browser: {browser!r}')

    _bi_log.info('User Data dir: %s  exists=%s', base, os.path.isdir(base))

    state_path = os.path.join(base, 'Local State')
    _bi_log.info('Local State:   %s  exists=%s', state_path, os.path.exists(state_path))
    if not os.path.exists(state_path):
        raise FileNotFoundError(
            f'{browser.title()} "Local State" file not found.\nExpected: {state_path}')

    aes_key = _browser_aes_key(state_path)
    _bi_log.info('AES master key decrypted OK (%d bytes)', len(aes_key))

    # Build candidate list: Default profile first, then numbered profiles
    db_filenames = ('Login Data', 'Login Data For Account')
    profiles = ['Default']
    try:
        profiles += sorted(e for e in os.listdir(base) if e.startswith('Profile '))
    except OSError:
        pass

    all_creds   = []
    debug_lines = []
    total_v20   = 0

    for profile in profiles:
        for fname in db_filenames:
            candidate = os.path.join(base, profile, fname)
            exists    = os.path.exists(candidate)
            short     = f'{profile}/{fname}'
            _bi_log.info('candidate: %-55s  exists=%s', short, exists)
            if not exists:
                debug_lines.append(f'✗ {short}')
                continue
            try:
                creds, row_count, v20_count = _read_one_login_db(candidate, aes_key)
                total_v20 += v20_count
                note = f'{row_count} rows'
                if v20_count:
                    note += f', {v20_count} v20(App-Bound)'
                debug_lines.append(f'✓ {short}  [{note}]')
                all_creds.extend(creds)
            except Exception as exc:
                _bi_log.warning('error reading %s: %s', short, exc)
                debug_lines.append(f'! {short}  [ERROR: {exc}]')

    _bi_log.info('total credentials: %d  (v20 across all files: %d)', len(all_creds), total_v20)

    if total_v20 and not any(
            c['password'] and not c['password'].startswith('[') for c in all_creds):
        _bi_log.warning('ALL passwords are v20 App-Bound — Chrome 127+ blocks external reading. '
                        'Use chrome://password-manager/settings → Export.')

    return all_creds, debug_lines


@app.route('/api/browser-import', methods=['POST'])
def api_browser_import():
    data    = request.get_json(force=True)
    browser = (data.get('browser') or 'chrome').lower().strip()
    title   = (data.get('title') or f'Passwords from {browser.title()}').strip()

    try:
        creds, debug_lines = _import_browser_passwords(browser)
    except FileNotFoundError as exc:
        return jsonify({'ok': False, 'error': str(exc)})
    except Exception as exc:
        _bi_log.exception('unexpected error during import')
        return jsonify({'ok': False, 'error': f'Import error: {exc}'})

    debug_text = '\n'.join(debug_lines)

    # Strip blanks
    creds = [c for c in creds if c.get('url') or c.get('username')]

    if not creds:
        return jsonify({'ok': False,
                        'error': 'No saved passwords found.',
                        'debug': debug_text})

    # Detect all-v20 (App-Bound) situation
    all_v20 = creds and all(
        c['password'].startswith('[v20') for c in creds if c.get('password'))
    if all_v20:
        return jsonify({
            'ok': False,
            'error': ('Chrome 127+ App-Bound Encryption — passwords cannot be read externally.\n'
                      'Use chrome://password-manager/settings → Export passwords → CSV,\n'
                      'then use the CSV Import button below.'),
            'debug': debug_text,
        })

    # Build Markdown note
    count = len(creds)
    lines = [
        f'# {title}', '',
        f'*{count} entr{"y" if count == 1 else "ies"} imported from '
        f'{browser.title()} on {time.strftime("%Y-%m-%d %H:%M")}*', '',
        '| URL | Username | Password |',
        '|-----|----------|----------|',
    ]
    for c in creds:
        url  = c['url'].replace('|', '&#124;')
        user = c['username'].replace('|', '&#124;')
        pwd  = c['password'].replace('|', '&#124;')
        lines.append(f'| {url} | {user} | {pwd} |')

    body = '\n'.join(lines)
    fm   = '---\nid: ' + str(_uuid_mod.uuid4()) + '\ntags: [passwords, imported]\n---\n'

    safe = _sanitize_filename(title) or f'passwords_{int(time.time())}'
    nd   = paths.get_notes_dir()
    fp   = os.path.join(nd, safe + '.md')
    i    = 1
    while os.path.exists(fp):
        fp = os.path.join(nd, f'{safe}_{i}.md')
        i += 1
    note_name = os.path.basename(fp)

    with open(fp, 'w', encoding='utf-8') as fh:
        fh.write(fm + body)

    _sse_broadcast(_json.dumps({'type': 'notes-updated', 'name': note_name, 'appended': False}))
    return jsonify({'ok': True, 'count': count, 'note': note_name, 'debug': debug_text})


@app.route('/api/browser-import/csv', methods=['POST'])
def api_browser_import_csv():
    """Import passwords from a Chrome/Edge CSV export and save as a note."""
    import csv, io

    data    = request.get_json(force=True)
    csv_raw = data.get('csv', '')
    title   = (data.get('title') or 'Passwords from CSV').strip()

    if not csv_raw:
        return jsonify({'ok': False, 'error': 'No CSV data provided.'})

    try:
        reader = csv.DictReader(io.StringIO(csv_raw))
        # Chrome CSV headers: name, url, username, password
        # Edge CSV headers:   name, url, username, password  (same)
        creds = []
        for row in reader:
            url  = row.get('url') or row.get('URL') or ''
            user = row.get('username') or row.get('Username') or ''
            pwd  = row.get('password') or row.get('Password') or ''
            if url or user:
                creds.append({'url': url, 'username': user, 'password': pwd})
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'CSV parse error: {exc}'})

    if not creds:
        return jsonify({'ok': False, 'error': 'No entries found in CSV.'})

    count = len(creds)
    lines = [
        f'# {title}', '',
        f'*{count} entr{"y" if count == 1 else "ies"} imported from CSV '
        f'on {time.strftime("%Y-%m-%d %H:%M")}*', '',
        '| URL | Username | Password |',
        '|-----|----------|----------|',
    ]
    for c in creds:
        url  = c['url'].replace('|', '&#124;')
        user = c['username'].replace('|', '&#124;')
        pwd  = c['password'].replace('|', '&#124;')
        lines.append(f'| {url} | {user} | {pwd} |')

    body = '\n'.join(lines)
    fm   = '---\nid: ' + str(_uuid_mod.uuid4()) + '\ntags: [passwords, imported]\n---\n'

    safe = _sanitize_filename(title) or f'passwords_{int(time.time())}'
    nd   = paths.get_notes_dir()
    fp   = os.path.join(nd, safe + '.md')
    i    = 1
    while os.path.exists(fp):
        fp = os.path.join(nd, f'{safe}_{i}.md')
        i += 1
    note_name = os.path.basename(fp)

    with open(fp, 'w', encoding='utf-8') as fh:
        fh.write(fm + body)

    _sse_broadcast(_json.dumps({'type': 'notes-updated', 'name': note_name, 'appended': False}))
    return jsonify({'ok': True, 'count': count, 'note': note_name})


# ---------------------------------------------------------------------------
# (csv import fm already patched above)
# ---------------------------------------------------------------------------
# Notable import
# ---------------------------------------------------------------------------

def _parse_notable_fm(text: str):
    """Parse a Notable YAML front-matter block.
    Returns (frontmatter_dict, body_text)."""
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body   = text[end + 4:].lstrip('\n')

    fm = {}
    # title
    m = re.search(r"^title:\s*['\"]?(.*?)['\"]?\s*$", fm_raw, re.MULTILINE)
    if m:
        fm['title'] = m.group(1).strip()
    # tags: [tag1, tag2]
    m = re.search(r'^tags:\s*\[([^\]]*)\]', fm_raw, re.MULTILINE)
    if m:
        fm['tags'] = [t.strip().strip('"\'') for t in m.group(1).split(',') if t.strip()]
    else:
        m2 = re.search(r'^tags:\s*\n((?:[ \t]+-[ \t]+.+\n?)+)', fm_raw, re.MULTILINE)
        fm['tags'] = (
            [re.sub(r'^[ \t]+-[ \t]+', '', l).strip()
             for l in m2.group(1).splitlines() if l.strip()]
            if m2 else []
        )
    # attachments list
    m = re.search(r'^attachments:\s*\[([^\]]*)\]', fm_raw, re.MULTILINE)
    fm['attachments'] = (
        [a.strip() for a in m.group(1).split(',') if a.strip()] if m else []
    )
    # created timestamp — Notable stores as ISO 8601 with possible quotes
    m = re.search(r"^created:\s*['\"]?([^'\"\n]+)['\"]?\s*$", fm_raw, re.MULTILINE)
    if m:
        fm['created'] = m.group(1).strip()
    return fm, body


_NOTABLE_INVALID = r'<>:"/\|?*'
_NOTABLE_LOOKALIKE = str.maketrans({
    ':': '-',   # :
    '꞉': '-',   # ꞉ MODIFIER LETTER COLON
    '：': '-',   # ： FULLWIDTH COLON
    '‹': '-',   # ‹
    '›': '-',   # ›
    '«': '-',   # «
    '»': '-',   # »
})

def _sanitize_notable_name(name: str) -> str:
    """Return a Windows-safe filename stem from a Notable title."""
    name = name.translate(_NOTABLE_LOOKALIKE)
    out  = []
    for c in name:
        if ord(c) <= 0x1F:
            continue
        if c in _NOTABLE_INVALID:
            out.append('-')
            continue
        out.append(c)
    result = re.sub(r'-{2,}', '-', ''.join(out)).strip(' -.')
    return result or 'untitled'


@app.route('/api/import/notable', methods=['POST'])
def import_notable():
    data        = request.get_json(force=True)
    folder      = data.get('folder', '').strip()
    if not folder or not os.path.isdir(folder):
        return jsonify({'error': f'Folder not found: {folder}'}), 400

    notes_sub = os.path.join(folder, 'notes')
    if not os.path.isdir(notes_sub):
        notes_sub = folder
    attach_src = os.path.join(folder, 'attachments')

    img_dir = _ensure_attach_subdir('images')
    nd      = paths.get_notes_dir()

    # Recursive scan so notebook subdirectories are included
    md_files = sorted(glob_module.glob(os.path.join(notes_sub, '**', '*.md'), recursive=True))
    if not md_files:
        return jsonify({'error': 'No .md files found in the specified folder.'}), 400

    imported = []
    skipped  = []
    errors   = []

    for src_path in md_files:
        try:
            with open(src_path, 'r', encoding='utf-8-sig') as fh:
                raw = fh.read()

            fm, body = _parse_notable_fm(raw)
            title    = fm.get('title') or os.path.splitext(os.path.basename(src_path))[0]
            tags     = fm.get('tags', [])

            # Rewrite @attachment refs
            def _repl_img(m):
                fname = m.group(1)
                return f'![{fname}](/attachments/images/{fname})'
            body = re.sub(r'!\[\]\(@attachment/([^)]+)\)', _repl_img, body)

            def _repl_link(m):
                fname = m.group(1)
                return f'[{fname}](/attachments/images/{fname})'
            body = re.sub(r'\[@attachment/([^\]]+)\]', _repl_link, body)

            # Build output
            _nid     = str(_uuid_mod.uuid4())
            fm_block = ('---\nid: ' + _nid + '\ntags: [' + ', '.join(tags) + ']\n---\n') if tags \
                       else ('---\nid: ' + _nid + '\n---\n')
            content  = fm_block + body

            safe_name = _sanitize_notable_name(title)
            dest_md   = os.path.join(nd, safe_name + '.md')
            if os.path.exists(dest_md):
                skipped.append(safe_name)
                continue

            with open(dest_md, 'w', encoding='utf-8') as fh:
                fh.write(content)

            # Write history file so Created: shows the original Notable creation date.
            # Notable stores: created: '2023-01-01T12:00:00.000Z'
            created_str = fm.get('created', '')
            if created_str:
                try:
                    # Strip timezone suffix (Z or +HH:MM) before parsing
                    clean = re.sub(r'[Z+][^.]*$', '', created_str).rstrip('Z')
                    created_dt = _dtt.fromisoformat(clean)
                    hist_at = created_dt.strftime('%Y-%m-%dT%H:%M:%S')
                except Exception:
                    hist_at = _dtt.now().strftime('%Y-%m-%dT%H:%M:%S')
            else:
                hist_at = _dtt.now().strftime('%Y-%m-%dT%H:%M:%S')

            hist_entry = [{'at': hist_at, 'via': 'notable_import', 'act': 'created'}]
            try:
                with open(_history_path(dest_md), 'w', encoding='utf-8') as fh:
                    _json.dump(hist_entry, fh, ensure_ascii=False)
            except OSError:
                pass

            # Copy attachments
            for att in fm.get('attachments', []):
                src_att = os.path.join(attach_src, att)
                dst_att = os.path.join(img_dir, att)
                if os.path.isfile(src_att) and not os.path.exists(dst_att):
                    shutil.copy2(src_att, dst_att)

            imported.append(safe_name)
            _sse_broadcast(_json.dumps({
                'type': 'notes-updated', 'name': safe_name + '.md', 'appended': False
            }))

        except Exception as exc:
            errors.append(f'{os.path.basename(src_path)}: {exc}')

    # Wake sync threads so imported notes reach iCloud / GDrive without waiting
    # for the next scheduled interval.
    if imported:
        _icloud_wake.set()
        _gdrive_wake.set()

    return jsonify({
        'imported': len(imported),
        'skipped':  len(skipped),
        'errors':   errors,
        'notes':    imported,
    })


def _purge_deletion_log() -> None:
    """Remove entries older than 6 months from the local deletion log.
    Runs at startup so the log stays bounded even without GitHub sync."""
    log_path = _deletion_log_path()
    try:
        data   = _load_deletion_log(log_path)
        cutoff = time.time() - 180 * 86400
        before = len(data['deletions'])
        data['deletions'] = [e for e in data['deletions']
                             if e.get('deleted_at', 0) > cutoff]
        if len(data['deletions']) != before:
            _save_deletion_log(log_path, data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Window geometry
# ---------------------------------------------------------------------------

# The frameless maximize deliberately overshoots the work area by a pixel on
# each edge so DWM's compositor stub falls outside it and the content sits
# flush in the corner. That much slack is allowed through the clamp below.
_WORKAREA_PAD = 1


def _clamp_rect_to_work_area(x, y, cx, cy, work, moving, resizing):
    """Keep a window inside the work area. Returns (x, y, cx, cy).

    Size is clamped before position, and that order is the whole point: a
    window as tall as the monitor cannot be moved anywhere that clears the
    taskbar, so clamping position alone silently lets a full-screen rect
    through. Windows then hides the taskbar entirely, because its shell treats
    any window covering the whole monitor as a fullscreen app.
    """
    max_w = (work.right - work.left) + _WORKAREA_PAD * 2
    max_h = (work.bottom - work.top) + _WORKAREA_PAD * 2
    if cx > max_w:
        cx = max_w
    if cy > max_h:
        cy = max_h

    lo_x, lo_y = work.left - _WORKAREA_PAD, work.top - _WORKAREA_PAD
    hi_x, hi_y = work.right + _WORKAREA_PAD, work.bottom + _WORKAREA_PAD
    if x + cx > hi_x:
        x = hi_x - cx
    if y + cy > hi_y:
        y = hi_y - cy
    if x < lo_x:
        x = lo_x
    if y < lo_y:
        y = lo_y
    return x, y, cx, cy


if __name__ == '__main__':
    paths.ensure_dirs()
    _purge_deletion_log()

    _frozen = paths._is_frozen()
    # Debug mode only when running from source.  Frozen executables never use
    # the Werkzeug reloader (it cannot reload a compiled binary).
    _debug  = not _frozen

    # Start the Telegram poll thread:
    #   • frozen   → always (no reloader child process)
    #   • dev/debug→ only in the reloader child (WERKZEUG_RUN_MAIN=true)
    #   • dev/prod → always (no child process)
    if _frozen or os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not _debug:
        _tg_log.info('Launching Telegram poll thread …')
        _tg_thread = threading.Thread(target=_telegram_user_poll_loop, daemon=True)
        _tg_thread.start()

    if _frozen:
        # ── Standalone desktop window (pywebview, frameless) ───────────────
        # Flask runs in a background daemon thread; pywebview opens a native
        # frameless EdgeChromium window.  The custom #titlebar in the HTML
        # provides drag / minimize / maximize / close — injected only when
        # pywebviewready fires, so it stays hidden in browser dev mode.

        class _WindowAPI:
            """Methods callable from JS via window.pywebview.api.*"""
            def __init__(self):
                self._win        = None
                self._maximized  = False
                self._restore_rect = None   # (x, y, w, h) saved before maximize

            # ------------------------------------------------------------------
            # Win32 helpers — frameless windows bypass the normal work-area
            # clamping that keeps maximized windows above the taskbar.
            # We implement maximize ourselves using SetWindowPos + SPI_GETWORKAREA.
            # ------------------------------------------------------------------

            def _get_main_hwnd(self):
                """Find the HWND of the main pywebview window by PID + title."""
                our_pid = ctypes.windll.kernel32.GetCurrentProcessId()
                found = []

                EnumProc = ctypes.WINFUNCTYPE(
                    ctypes.c_bool,
                    ctypes.wintypes.HWND,
                    ctypes.wintypes.LPARAM,
                )

                def _cb(hwnd, _):
                    if not ctypes.windll.user32.IsWindowVisible(hwnd):
                        return True
                    pid = ctypes.wintypes.DWORD(0)
                    ctypes.windll.user32.GetWindowThreadProcessId(
                        hwnd, ctypes.byref(pid))
                    if pid.value == our_pid:
                        buf = ctypes.create_unicode_buffer(256)
                        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                        if 'myNote' in buf.value:
                            found.append(hwnd)
                    return True

                ctypes.windll.user32.EnumWindows(EnumProc(_cb), 0)
                return found[0] if found else None

            def close(self):
                if self._win:
                    self._win.destroy()

            def minimize(self):
                if self._win:
                    self._win.minimize()

            def toggle_maximize(self):
                if not self._win:
                    return
                hwnd = self._get_main_hwnd()
                if not hwnd:
                    # Fallback to pywebview built-ins if HWND lookup fails
                    if self._maximized:
                        self._win.restore()
                    else:
                        self._win.maximize()
                    self._maximized = not self._maximized
                    return

                SWP_SHOWWINDOW = 0x0040
                if self._maximized:
                    # Restore to saved rect
                    if self._restore_rect:
                        x, y, w, h = self._restore_rect
                        ctypes.windll.user32.SetWindowPos(
                            hwnd, 0, x, y, w, h, SWP_SHOWWINDOW)
                    self._maximized = False
                else:
                    # Save current rect before maximizing
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    self._restore_rect = (
                        rect.left, rect.top,
                        rect.right  - rect.left,
                        rect.bottom - rect.top,
                    )
                    # Get work area (screen minus taskbar) — SPI_GETWORKAREA = 0x30
                    work = ctypes.wintypes.RECT()
                    ctypes.windll.user32.SystemParametersInfoW(
                        0x0030, 0, ctypes.byref(work), 0)

                    # WS_POPUP frameless windows have a 1 px compositor stub on
                    # every edge that DWM owns and cannot be queried.  Shift the
                    # window 1 px past each work-area boundary so that stub lands
                    # off-screen and the visible content fills flush to the corner.
                    PAD = 1
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, 0,
                        work.left  - PAD,
                        work.top   - PAD,
                        work.right  - work.left + PAD * 2,
                        work.bottom - work.top  + PAD * 2,
                        SWP_SHOWWINDOW,
                    )
                    self._maximized = True

            def get_window_rect(self):
                """Return current window position so JS can compute drag deltas."""
                hwnd = self._get_main_hwnd()
                if not hwnd:
                    return None
                rect = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                return {'x': rect.left, 'y': rect.top,
                        'w': rect.right - rect.left, 'h': rect.bottom - rect.top}

            def move_window(self, x, y):
                """Move the window to absolute screen coordinates (called per mousemove)."""
                if self._maximized:
                    return
                hwnd = self._get_main_hwnd()
                if hwnd:
                    SWP_NOSIZE   = 0x0001
                    SWP_NOZORDER = 0x0004
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, 0, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER)

            def pick_folder(self):
                """Open a native OS folder-picker dialog; return the chosen path or None."""
                if self._win:
                    from webview import FileDialog
                    result = self._win.create_file_dialog(
                        FileDialog.FOLDER, allow_multiple=False
                    )
                    if result:
                        return result[0]
                return None

            def open_url(self, url):
                """Open a URL in the system default browser.

                Uses ShellExecute via os.startfile rather than `cmd /c start`:
                the shell form mangles URLs containing '&' and would execute
                anything a crafted link smuggled through — and links arrive here
                from note content, which syncs in from other devices.
                """
                try:
                    scheme = _urlparse.urlparse(url).scheme.lower()
                except Exception:
                    return
                if scheme not in ('http', 'https', 'mailto', 'tel', 'ftp'):
                    print(f'[myNote] refusing to open URL with scheme {scheme!r}', flush=True)
                    return
                try:
                    os.startfile(url)
                except OSError as exc:
                    print(f'[myNote] could not open {url}: {exc}', flush=True)

        # ── Workarea guard ────────────────────────────────────────────────────
        # frameless WS_POPUP windows are not automatically clamped to the work
        # area by Windows during user drag/resize, so the window can slide under
        # the taskbar.  We subclass the WndProc to intercept WM_WINDOWPOSCHANGING
        # and enforce the work area boundaries before the position is applied.

        _orig_wndproc_ptr: int = 0
        _new_wndproc_ref       = None   # held at module scope to prevent GC

        class _WINDOWPOS(ctypes.Structure):
            _fields_ = [
                ('hwnd',            ctypes.wintypes.HWND),
                ('hwndInsertAfter', ctypes.wintypes.HWND),
                ('x',   ctypes.c_int),
                ('y',   ctypes.c_int),
                ('cx',  ctypes.c_int),
                ('cy',  ctypes.c_int),
                ('flags', ctypes.c_uint),
            ]

        _WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.wintypes.HWND,
            ctypes.c_uint,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        )

        def _install_workarea_guard(hwnd: int) -> None:
            global _orig_wndproc_ptr, _new_wndproc_ref

            _GWLP_WNDPROC         = -4
            _SWP_NOMOVE           = 0x0002
            _SWP_NOSIZE           = 0x0001
            _WM_WINDOWPOSCHANGING = 0x0046
            _SPI_GETWORKAREA      = 0x0030

            u32 = ctypes.windll.user32
            u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            u32.CallWindowProcW.restype   = ctypes.c_long

            def _proc(hwnd, msg, wparam, lparam):
                if msg == _WM_WINDOWPOSCHANGING:
                    wp = ctypes.cast(lparam, ctypes.POINTER(_WINDOWPOS)).contents
                    work = ctypes.wintypes.RECT()
                    u32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(work), 0)

                    moving   = not (wp.flags & _SWP_NOMOVE)
                    resizing = not (wp.flags & _SWP_NOSIZE)

                    cur = ctypes.wintypes.RECT()
                    u32.GetWindowRect(hwnd, ctypes.byref(cur))
                    x  = wp.x  if moving   else cur.left
                    y  = wp.y  if moving   else cur.top
                    cx = wp.cx if resizing else (cur.right  - cur.left)
                    cy = wp.cy if resizing else (cur.bottom - cur.top)

                    nx, ny, ncx, ncy = _clamp_rect_to_work_area(
                        x, y, cx, cy, work, moving, resizing)

                    if moving:
                        wp.x,  wp.y  = nx,  ny
                    if resizing:
                        wp.cx, wp.cy = ncx, ncy

                return u32.CallWindowProcW(_orig_wndproc_ptr, hwnd, msg, wparam, lparam)

            _new_wndproc_ref  = _WNDPROC(_proc)
            _orig_wndproc_ptr = u32.GetWindowLongPtrW(hwnd, _GWLP_WNDPROC)
            u32.SetWindowLongPtrW(
                hwnd, _GWLP_WNDPROC,
                ctypes.cast(_new_wndproc_ref, ctypes.c_void_p).value)

        def _run_flask():
            app.run(
                host=paths.HOST,
                port=paths.PORT,
                debug=False,
                threaded=True,
                use_reloader=False,
            )

        _flask_thread = threading.Thread(target=_run_flask, daemon=True)
        _flask_thread.start()
        time.sleep(1.2)          # give Flask time to bind the port

        import webview           # bundled by PyInstaller via collect-all webview
        _icon_path = os.path.join(paths.STATIC_DIR, 'images', 'myNote.ico')
        _api = _WindowAPI()
        _wv_win = webview.create_window(
            title='myNote',
            url=f'http://{paths.HOST}:{paths.PORT}',
            width=1280,
            height=860,
            min_size=(700, 500),
            resizable=True,
            frameless=True,
            easy_drag=False,
            text_select=True,
            js_api=_api,
        )
        _api._win = _wv_win      # wire back-reference after creation

        def _on_window_loaded():
            hwnd = _api._get_main_hwnd()
            if not hwnd:
                return
            _install_workarea_guard(hwnd)
            # Nudge the window through the guard once. If it came up already
            # covering the whole monitor — restored geometry, or sized before
            # the hook existed — nothing else would trigger a clamp, and the
            # shell would keep the taskbar hidden for as long as it is focused.
            try:
                r = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, r.left, r.top,
                    r.right - r.left, r.bottom - r.top,
                    SWP_NOZORDER | SWP_NOACTIVATE)
            except Exception:
                pass
        _wv_win.events.loaded += _on_window_loaded

        webview.start(
            icon=_icon_path if os.path.exists(_icon_path) else None,
        )
        # Window closed → process ends naturally (daemon threads exit with it)
    else:
        app.run(
            host=paths.HOST,
            port=paths.PORT,
            debug=_debug,
            threaded=True,
            use_reloader=_debug,
        )
