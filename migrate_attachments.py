"""
One-time migration script:
  1. Creates images/ documents/ archives/ videos/ under myNotes/attachments/
  2. Moves every flat file in attachments/ to the correct subfolder
  3. Updates /attachments/filename  →  /attachments/subfolder/filename
     in every .md in myNotes/
  4. Imports all Notable notes from NotableWork/notes/ into myNotes/
  5. Copies their PNG attachments into myNotes/attachments/images/

Run from the myNote project root:
    python migrate_attachments.py
"""

import os
import re
import shutil
import glob

# ── Config ──────────────────────────────────────────────────────────────────

NOTES_DIR   = os.path.join(os.path.expanduser('~'), 'Documents', 'myNotes')
ATTACH_DIR  = os.path.join(NOTES_DIR, 'attachments')

NOTABLE_NOTES_DIR  = os.path.join(os.path.dirname(__file__), 'NotableWork', 'notes')
NOTABLE_ATTACH_DIR = os.path.join(os.path.dirname(__file__), 'NotableWork', 'attachments')

# ── Extension → subfolder ────────────────────────────────────────────────────

IMAGE_EXTS   = {'.png','.jpg','.jpeg','.gif','.bmp','.svg','.webp','.ico',
                '.tiff','.tif','.heic','.heif'}
ARCHIVE_EXTS = {'.zip','.rar','.7z','.tar','.gz','.bz2','.xz','.tgz'}
VIDEO_EXTS   = {'.mp4','.avi','.mov','.mkv','.wmv','.flv','.webm',
                '.m4v','.mpg','.mpeg'}

def subfolder_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXTS:   return 'images'
    if ext in ARCHIVE_EXTS: return 'archives'
    if ext in VIDEO_EXTS:   return 'videos'
    return 'documents'

# ── Step 1+2: Move existing flat attachments into subfolders ─────────────────

def migrate_existing():
    for sub in ('images', 'documents', 'archives', 'videos'):
        os.makedirs(os.path.join(ATTACH_DIR, sub), exist_ok=True)

    moved = {}   # old_name → new_rel_path  (e.g. "foo.png" → "images/foo.png")
    for fname in os.listdir(ATTACH_DIR):
        fpath = os.path.join(ATTACH_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        sub = subfolder_for(fname)
        dest_dir = os.path.join(ATTACH_DIR, sub)
        dest     = os.path.join(dest_dir, fname)
        if os.path.exists(dest):
            print(f'  skip (exists): {sub}/{fname}')
        else:
            shutil.move(fpath, dest)
            print(f'  moved → {sub}/{fname}')
        moved[fname] = f'{sub}/{fname}'
    return moved

# ── Step 3: Update .md references ────────────────────────────────────────────

def update_md_refs(moved: dict):
    """Replace /attachments/filename with /attachments/sub/filename in all notes."""
    for md_path in glob.glob(os.path.join(NOTES_DIR, '*.md')):
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
        new_content = content
        for old_name, new_rel in moved.items():
            # Match the bare /attachments/filename (not already subfoldered)
            old_pat = re.escape(f'/attachments/{old_name}')
            new_url = f'/attachments/{new_rel}'
            new_content = re.sub(old_pat, new_url, new_content)
        if new_content != content:
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f'  updated refs: {os.path.basename(md_path)}')

# ── Notable frontmatter parser ───────────────────────────────────────────────

def parse_notable_fm(text: str):
    """Return (frontmatter_dict, body_text)."""
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body    = text[end+4:].lstrip('\n')

    fm = {}
    # title
    m = re.search(r"^title:\s*['\"]?(.*?)['\"]?\s*$", fm_text, re.MULTILINE)
    if m:
        fm['title'] = m.group(1).strip()
    # tags  [tag1, tag2] or YAML list
    m = re.search(r'^tags:\s*\[([^\]]*)\]', fm_text, re.MULTILINE)
    if m:
        raw = m.group(1)
        fm['tags'] = [t.strip().strip('"\'') for t in raw.split(',') if t.strip()]
    else:
        m2 = re.search(r'^tags:\s*\n((?:[ \t]+-[ \t]+.+\n?)+)', fm_text, re.MULTILINE)
        if m2:
            fm['tags'] = [re.sub(r'^[ \t]+-[ \t]+','',l).strip()
                          for l in m2.group(1).splitlines() if l.strip()]
        else:
            fm['tags'] = []
    # attachments list
    m = re.search(r'^attachments:\s*\[([^\]]*)\]', fm_text, re.MULTILINE)
    if m:
        raw = m.group(1)
        fm['attachments'] = [a.strip() for a in raw.split(',') if a.strip()]
    else:
        fm['attachments'] = []

    return fm, body

# ── Sanitize a filename for Windows ──────────────────────────────────────────

INVALID_WIN = r'<>:"/\|?*'

def sanitize(name: str) -> str:
    out = []
    for c in name:
        cp = ord(c)
        if cp <= 0x1F:
            continue
        if c in INVALID_WIN:
            out.append('-')
            continue
        # Replace Unicode look-alike colons / special punctuation
        if c in ('꞉', '：', '﹕'):
            out.append('-')
            continue
        if c in ('‹', '›', '«', '»'):
            out.append('-')
            continue
        out.append(c)
    return re.sub(r'-{2,}', '-', ''.join(out)).strip(' -.')

# ── Step 4+5: Import Notable notes ───────────────────────────────────────────

def import_notable():
    img_dir = os.path.join(ATTACH_DIR, 'images')
    os.makedirs(img_dir, exist_ok=True)

    md_files = glob.glob(os.path.join(NOTABLE_NOTES_DIR, '*.md'))
    imported = skipped = 0

    for src_path in sorted(md_files):
        with open(src_path, 'r', encoding='utf-8') as f:
            raw = f.read()

        fm, body = parse_notable_fm(raw)
        title    = fm.get('title') or os.path.splitext(os.path.basename(src_path))[0]
        tags     = fm.get('tags', [])

        # Convert @attachment refs to /attachments/images/filename
        def repl_attach(m):
            fname = m.group(1)
            return f'![{fname}](/attachments/images/{fname})'
        body = re.sub(r'!\[\]\(@attachment/([^)]+)\)', repl_attach, body)
        # Also handle non-image Notable links: [@attachment/file]
        def repl_link(m):
            fname = m.group(1)
            return f'[{fname}](/attachments/images/{fname})'
        body = re.sub(r'\[@attachment/([^\]]+)\]', repl_link, body)

        # Build myNote frontmatter
        if tags:
            tag_line = 'tags: [' + ', '.join(tags) + ']'
            fm_block = f'---\n{tag_line}\n---\n'
        else:
            fm_block = ''

        note_content = fm_block + body

        # Choose output filename
        safe_name = sanitize(title)
        if not safe_name:
            safe_name = sanitize(os.path.splitext(os.path.basename(src_path))[0])
        dest_md = os.path.join(NOTES_DIR, safe_name + '.md')
        # Avoid overwriting existing notes
        if os.path.exists(dest_md):
            print(f'  skip (exists): {safe_name}.md')
            skipped += 1
            continue

        with open(dest_md, 'w', encoding='utf-8') as f:
            f.write(note_content)
        imported += 1

        # Copy attachments
        for att_name in fm.get('attachments', []):
            src_att = os.path.join(NOTABLE_ATTACH_DIR, att_name)
            dst_att = os.path.join(img_dir, att_name)
            if os.path.exists(src_att) and not os.path.exists(dst_att):
                shutil.copy2(src_att, dst_att)

        print(f'  imported: {safe_name}.md  [{", ".join(tags) or "no tags"}]')

    print(f'\nNotable import done: {imported} imported, {skipped} skipped.')

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('=== Step 1-2: Migrating existing flat attachments ===')
    moved = migrate_existing()

    print(f'\n=== Step 3: Updating .md references ({len(moved)} files moved) ===')
    update_md_refs(moved)

    print('\n=== Step 4-5: Importing Notable notes ===')
    import_notable()

    print('\nAll done.')
