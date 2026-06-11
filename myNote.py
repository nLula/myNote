from flask import Flask, render_template, jsonify, request, abort, send_from_directory
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
import requests
import paths

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
        return {'tags': [], 'encrypted': False}
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
    encrypted = bool(re.search(r'^encrypted:\s*true', fm, re.MULTILINE))
    return {'tags': tags, 'encrypted': encrypted}


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


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_title(filepath: str) -> str:
    """Return text of the first # heading in the note body, or the filename."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return os.path.splitext(os.path.basename(filepath))[0]
    body = re.sub(r'^---\s*\n[\s\S]*?\n---\s*\n?', '', content).lstrip()
    m = re.search(r'^#\s+(.+)', body, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return os.path.splitext(os.path.basename(filepath))[0]


def _note_files():
    return sorted(glob_module.glob(os.path.join(paths.get_notes_dir(), '*.md')))


def _sanitize_filename(name: str) -> str:
    """Strip emoji, OS-reserved chars, and control chars; keep all scripts."""
    out = []
    for c in name:
        cp = ord(c)
        # Control characters
        if cp <= 0x1F:
            continue
        # Windows / POSIX reserved characters
        if c in r'<>:"/\|?*':
            continue
        # Above BMP (> U+FFFF) — virtually all emoji (😀 🏁 🙈 etc.)
        if cp > 0xFFFF:
            continue
        # Misc Symbols U+2600–U+26FF  (☀ ☎ ♥ ✓ …)
        # Dingbats     U+2700–U+27BF  (✂ ✈ ✉ …)
        if 0x2600 <= cp <= 0x27BF:
            continue
        # Variation selectors and ZWJ used in emoji sequences
        if cp in (0x200D, 0xFE0E, 0xFE0F) or 0xFE00 <= cp <= 0xFE0F:
            continue
        out.append(c)
    return ''.join(out).strip()[:100]


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
    existing = {os.path.splitext(os.path.basename(f))[0] for f in _note_files()}
    if 'Untitled' not in existing:
        return jsonify({'name': 'Untitled'})
    i = 2
    while f'Untitled{i}' in existing:
        i += 1
    return jsonify({'name': f'Untitled{i}'})


@app.route('/api/tags')
def api_tags():
    tags: set = set()
    for fp in _note_files():
        for t in _read_frontmatter(fp).get('tags', []):
            if t != TRASH_TAG:
                tags.add(t)
    return jsonify(sorted(tags))


@app.route('/api/notes')
def api_notes():
    tag = request.args.get('tag', '').strip()
    names = []
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
            if tag and tag not in fm_tags:
                continue
        name = os.path.splitext(os.path.basename(fp))[0]
        names.append({'name': name, 'title': _extract_title(fp), 'encrypted': fm.get('encrypted', False)})
    return jsonify(names)


@app.route('/api/search')
def api_search():
    q   = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip()
    results = []
    for fp in _note_files():
        fm_tags    = _read_frontmatter(fp).get('tags', [])
        is_trashed = TRASH_TAG in fm_tags
        if tag == TRASH_TAG:
            if not is_trashed:
                continue
        else:
            if is_trashed:
                continue
        name      = os.path.splitext(os.path.basename(fp))[0]
        encrypted = _read_frontmatter(fp).get('encrypted', False)
        if not q or q in name.lower():
            results.append({'name': name, 'title': _extract_title(fp), 'encrypted': encrypted})
            continue
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                if q in f.read().lower():
                    results.append({'name': name, 'title': _extract_title(fp), 'encrypted': encrypted})
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
    return jsonify({
        'name':    name,
        'content': content,
        'ctime':   stat.st_ctime,
        'mtime':   stat.st_mtime,
    })


@app.route('/api/note', methods=['POST'])
def api_create_note():
    data    = request.get_json(force=True)
    name    = (data.get('name') or '').strip()
    content = data.get('content', '')
    if not name or re.search(r'[/\\<>:"|?*]', name) or name.startswith('.'):
        abort(400)
    fp = os.path.join(paths.get_notes_dir(), name + '.md')
    if os.path.exists(fp):
        abort(409)
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({'name': name}), 201


@app.route('/api/note/<path:name>', methods=['PATCH'])
def api_save_note(name):
    fp = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    data    = request.get_json(force=True)
    content = data.get('content', '')
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({'ok': True})


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
    tags = _read_frontmatter(fp).get('tags', [])
    if TRASH_TAG not in tags:
        tags.append(TRASH_TAG)
    _write_tags(fp, tags)
    return jsonify({'ok': True})


@app.route('/api/note/<path:name>/restore', methods=['POST'])
def api_restore_note(name):
    fp   = _safe_path(name)
    if not os.path.isfile(fp):
        abort(404)
    tags = [t for t in _read_frontmatter(fp).get('tags', []) if t != TRASH_TAG]
    _write_tags(fp, tags)
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
                      skipped_html, marked_js, md_json, attach_map_json):
    """Assemble a self-contained shareable HTML string."""
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
        '  <div class="note-body" id="note-body"></div>\n'
        '</div>\n'
        '<footer class="note-footer">Shared with <strong>myNote</strong></footer>\n'
        '<script>' + marked_js + '</script>\n'
        '<script>\n'
        + _SHARE_JS_BEFORE + md_json
        + _SHARE_JS_MIDDLE + attach_map_json
        + _SHARE_JS_AFTER
        + '</script>\n'
        '</body>\n'
        '</html>\n'
    )


@app.route('/api/share', methods=['POST'])
def api_share():
    """Generate a self-contained shareable HTML file and save to Downloads."""
    import mimetypes as _mimetypes
    import urllib.parse as _urlparse
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

    # Load embedded marked.js
    marked_path = os.path.join(paths.STATIC_DIR, 'js', 'marked.min.js')
    with open(marked_path, 'r', encoding='utf-8') as f:
        marked_js = f.read()

    md_json         = _json.dumps(body_processed)
    attach_map_json = _json.dumps(data_map)
    html_out = _build_share_html(
        title_esc      = title_esc,
        created        = _fmt(stat.st_ctime),
        modified       = _fmt(stat.st_mtime),
        tags_html      = tags_html,
        skipped_html   = skipped_html,
        marked_js      = marked_js,
        md_json        = md_json,
        attach_map_json= attach_map_json,
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
        'email':           s.get('email', ''),
        'emailPasswordSet': bool(s.get('email_password', '')),
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
    new_fp    = os.path.realpath(os.path.join(notes_dir, new_name + '.md'))
    base      = os.path.realpath(notes_dir)
    if not new_fp.startswith(base + os.sep):
        abort(403)
    if os.path.exists(new_fp):
        abort(409)
    os.rename(fp, new_fp)
    return jsonify({'name': new_name})


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

_TG_DIVIDER = '\n\n' + '-' * 99 + '\n\n'

def _tg_create_note(title: str, body: str) -> None:
    """Create a new note or append to an existing one if the title matches."""
    safe      = _sanitize_filename(title) or f'tg_{_tg_ts_file()}'
    notes_dir = paths.get_notes_dir()
    fp        = os.path.join(notes_dir, safe + '.md')

    note_name = safe + '.md'

    if os.path.exists(fp):
        # Title matches an existing note — append content below a divider.
        # The repeated header itself is not written, only the body.
        if body.strip():
            with open(fp, 'r', encoding='utf-8') as f:
                existing = f.read()
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(existing.rstrip() + _TG_DIVIDER + body.strip())
            _tg_log.info('  appended to existing note: %r', note_name)
            _sse_broadcast(_json.dumps({'type': 'notes-updated', 'name': note_name, 'appended': True}))
        else:
            _tg_log.debug('  skipped append — no body content')
    else:
        # New note: write frontmatter + heading + body
        fm      = '---\ntags: [Telegram]\n---\n'
        content = fm + f'# {title}\n' + (('\n' + body.strip()) if body.strip() else '')
        with open(fp, 'w', encoding='utf-8') as f:
            f.write(content)
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

async def _tg_process_message(client, msg) -> None:
    """Convert one Telethon Message into a note + downloaded attachments."""
    try:
        from telethon.tl.types import DocumentAttributeFilename
    except ImportError:
        return

    # Service messages (photo changed, user joined/left, pin, channel create, etc.)
    # have msg.action set to a non-None TL object. They carry no user content.
    if getattr(msg, 'action', None) is not None:
        _tg_log.debug('  skipped — service message (action=%s)', type(msg.action).__name__)
        return

    # msg.text applies the client's parse_mode and returns e.g. **bold** for bold entities.
    # msg.raw_text is always the plain string the user typed, with no markdown markup added.
    text  = (msg.raw_text or '').strip()
    lines = text.split('\n') if text else []
    title = lines[0][:80].strip() if lines else _tg_ts_title()
    body  = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ''

    _tg_log.debug('  msg id=%-8s  photo=%-5s  doc=%-5s  text=%.50r',
                  msg.id, bool(msg.photo), bool(msg.document), text)

    # Skip entirely empty messages (no text, no media — nothing to save)
    if not text and not msg.photo and not msg.document:
        _tg_log.debug('  skipped — no text and no media')
        return

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

                last_id  = int(paths._load_config().get('telegram', {})
                               .get('last_message_id', 0))
                _tg_log.debug('Polling chat=%s  min_id=%s', chat_id, last_id)
                messages = await client.get_messages(target_entity, limit=50, min_id=last_id)
                msg_list = list(reversed(list(messages)))
                _tg_log.info('Fetched %d new message(s) from chat %s', len(msg_list), chat_id)
                for msg in msg_list:
                    _tg_log.debug('Processing msg id=%s', msg.id)
                    try:
                        await _tg_process_message(client, msg)
                    except Exception as e:
                        _tg_log.error('Error processing msg id=%s: %s', msg.id, e, exc_info=True)
                    c = paths._load_config()
                    c.setdefault('telegram', {})['last_message_id'] = msg.id
                    paths._save_config(c)
            finally:
                await client.disconnect()
                _tg_log.debug('Disconnected')

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_poll())
        except Exception as e:
            _tg_log.error('Poll loop error: %s', e, exc_info=True)
        finally:
            loop.close()

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
# Browser password import (Chrome / Edge, Windows only)
# ---------------------------------------------------------------------------

_bi_log = logging.getLogger('[BI]')
_bi_log.setLevel(logging.DEBUG)
if not _bi_log.handlers:
    _bih = logging.StreamHandler()
    _bih.setFormatter(logging.Formatter('%(name)s %(message)s'))
    _bi_log.addHandler(_bih)


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
    fm   = '---\ntags: [passwords, imported]\n---\n'

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
    fm   = '---\ntags: [passwords, imported]\n---\n'

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
        # Maybe the folder IS the notes folder already
        notes_sub = folder
    attach_src = os.path.join(folder, 'attachments')

    img_dir = _ensure_attach_subdir('images')
    nd      = paths.get_notes_dir()

    md_files = sorted(glob_module.glob(os.path.join(notes_sub, '*.md')))
    if not md_files:
        return jsonify({'error': 'No .md files found in the specified folder.'}), 400

    imported = []
    skipped  = []
    errors   = []

    for src_path in md_files:
        try:
            with open(src_path, 'r', encoding='utf-8') as fh:
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
            fm_block = ('---\ntags: [' + ', '.join(tags) + ']\n---\n') if tags else ''
            content  = fm_block + body

            safe_name = _sanitize_notable_name(title)
            dest_md   = os.path.join(nd, safe_name + '.md')
            if os.path.exists(dest_md):
                skipped.append(safe_name)
                continue

            with open(dest_md, 'w', encoding='utf-8') as fh:
                fh.write(content)

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

    return jsonify({
        'imported': len(imported),
        'skipped':  len(skipped),
        'errors':   errors,
        'notes':    imported,
    })


if __name__ == '__main__':
    paths.ensure_dirs()

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
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, 0,
                        work.left, work.top,
                        work.right  - work.left,
                        work.bottom - work.top,
                        SWP_SHOWWINDOW,
                    )
                    self._maximized = True

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
                """Open a URL in the system default browser."""
                import subprocess
                subprocess.Popen(['start', '', url], shell=True)

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
            js_api=_api,
        )
        _api._win = _wv_win      # wire back-reference after creation
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
