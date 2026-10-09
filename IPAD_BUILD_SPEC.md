# myNote — iPad App Build Specification

> **Target reader:** Claude Code running on macOS.  
> **Goal:** Recreate every feature and visual design of the Windows desktop myNote app as a native iPad app using Expo SDK 54.  
> **Source of truth:** The Windows app lives at `C:\Users\user\Documents\myNote` (Flask + pywebview). This document fully specifies what to build — do not assume anything beyond what is written here.

---

## 1. Technology Stack

| Concern | Choice |
|---|---|
| Framework | Expo SDK 54 (managed workflow) |
| Language | TypeScript |
| Navigation | `expo-router` v4 (file-based routing) |
| State | Zustand (global) + React local state for UI ephemera |
| File storage | `expo-file-system` — notes as `.md` files in the app Documents directory |
| Secure storage | `expo-secure-store` — tokens, passwords, session keys |
| Markdown render | `react-native-markdown-display` |
| Markdown editor | Plain `TextInput` (multiline, monospace) |
| Media playback | `expo-av` for video and audio |
| File picker | `expo-document-picker` |
| Image picker | `expo-image-picker` |
| PDF export | `expo-print` + `expo-sharing` |
| Crypto | `expo-crypto` + native `SubtleCrypto`-equivalent (`react-native-aes-gcm-crypto` or pure-JS fallback) |
| Background tasks | `expo-background-fetch` + `expo-task-manager` for sync intervals |
| iCloud | `react-native-cloud-storage` (iCloud Drive documents) |
| HTTP (GDrive, GitHub, Telegram) | native `fetch` |
| Theme | dark/light via React Context + `Appearance` API |
| Icons | `@expo/vector-icons` (Feather set) |

> Initialise with: `npx create-expo-app myNoteApp --template blank-typescript`  
> Set `ios.supportsTablet: true` and `ios.requireFullScreen: false` in `app.json` so the app runs as a full-screen iPad app in both orientations.

---

## 2. Layout — Three-Column Split View

The iPad UI is a permanent three-column layout, matching the desktop app exactly. No tabs, no drawer — all three columns are always visible on iPad (landscape and portrait, with the tag column collapsible in portrait to save space).

```
┌──────────────┬────────────────────┬──────────────────────────────────────┐
│  Col 1       │  Col 2             │  Col 3                               │
│  Tags        │  Note list         │  Toolbar + content                   │
│  ~72 pt wide │  ~240 pt wide      │  flex: 1                             │
│              │  (resizable)       │                                       │
│  [sync icon] │  [search bar]      │  [toolbar row]                       │
│              │  [sort bar]        │  [note title + dates]                │
│  tag list    │  note list         │  [note view / note edit]             │
└──────────────┴────────────────────┴──────────────────────────────────────┘
```

Column widths are persisted in `AsyncStorage` so users can resize them. Implement drag handles between Col 1–2 and Col 2–3 using `PanResponder`.

---

## 3. Color Tokens (CSS Variables → React Native StyleSheet)

Implement as a theme object passed via React Context. Both dark (default) and light themes must be supported. Toggle via a sun/moon button in the Settings header.

```typescript
// dark theme
const dark = {
  bg:          '#1a1a1a',
  bgSecondary: '#222222',
  bgTertiary:  '#2a2a2a',
  border:      '#333333',
  accent:      '#4a9eff',
  text:        '#e0e0e0',
  textMuted:   '#777777',
  tagActive:   '#2a3a50',
  danger:      '#e05252',
  ok:          '#52b788',
};

// light theme
const light = {
  bg:          '#ffffff',
  bgSecondary: '#f5f5f5',
  bgTertiary:  '#ebebeb',
  border:      '#dddddd',
  accent:      '#2979d4',
  text:        '#1a1a1a',
  textMuted:   '#888888',
  tagActive:   '#e0ecff',
  danger:      '#cc3333',
  ok:          '#2d8653',
};
```

---

## 4. File System Layout

All data lives under `FileSystem.documentDirectory`:

```
DocumentDirectory/
  notes/
    My Note.md
    Another Note.md
  attachments/
    images/      ← .png .jpg .gif .webp .heic …
    documents/   ← .pdf .docx .xlsx .html …
    videos/      ← .mp4 .mov .m4v .mkv …
    audio/       ← .mp3 .m4a .wav .flac …
    archives/    ← .zip .rar .7z …
  .config.json   ← app-level settings (workfolder already IS DocumentDirectory)
  .deletions.json ← union-merged deletion log for cross-device sync
  .state.json    ← sync state (GitHub SHA map, last-sync timestamps)
```

### Note file format

Each note is a plain UTF-8 Markdown file. Optional YAML frontmatter between `---` delimiters:

```markdown
---
tags: [work, ideas]
created: 1718000000
id: abc123
encrypted: true
---
# Note title

Body content here.
```

- `tags` — list of string tags
- `created` — Unix timestamp (set once on creation, never changed)
- `id` — UUID4, set once on creation, used to match notes across devices during sync
- `encrypted: true` — body is AES-GCM Base64 ciphertext, not plaintext

File name on disk = sanitized title (no `/\<>:"|?*`, max 100 chars) + `.md`. When the `# heading` changes, the file is renamed automatically.

---

## 5. Note Service (`src/services/NoteService.ts`)

This is the complete backend replacement. No HTTP server — all note operations call these functions directly.

### File operations
```typescript
listNotes(): Promise<NoteListItem[]>
// Returns [{name, title, ctime, mtime, encrypted, tags}] sorted by parameter

readNote(name: string): Promise<{content: string, ctime: number, mtime: number}>

saveNote(name: string, content: string): Promise<void>
// Writes file; preserves ctime from frontmatter; updates mtime to now

renameNote(oldName: string, newTitle: string): Promise<string>
// Sanitizes title → new filename; moves file; returns new name

createNote(title?: string): Promise<string>
// Creates file with frontmatter (id, created); returns filename

trashNote(name: string): Promise<void>
// Sets trashed_at: <timestamp> in frontmatter (note stays in notes/ folder)

restoreNote(name: string): Promise<void>
// Removes trashed_at from frontmatter

deleteNote(name: string): Promise<void>
// Permanently deletes file; logs to .deletions.json

searchNotes(query: string, tag?: string): Promise<NoteListItem[]>
// Full-text search: match query against title + body; scored by frequency

getNextUntitledTitle(): Promise<string>
// Returns "Untitled", "Untitled 2", "Untitled 3", etc.
```

### Tag operations
```typescript
getTags(forTrash?: boolean): Promise<{tags: {name:string, count:number}[], all_count:number, trash_count:number}>
updateTags(name: string, tags: string[]): Promise<void>
```

### Attachment operations
```typescript
attachmentSubfolder(filename: string): 'images'|'documents'|'videos'|'audio'|'archives'
uploadAttachment(file: FileResult, noteName: string): Promise<{url: string, filename: string}>
// Copies picked file into correct attachments/ subfolder; returns URL path
openAttachment(relPath: string): Promise<void>
// Opens file using Sharing.shareAsync / IntentLauncher
deleteAttachment(relPath: string): Promise<void>
```

### History
```typescript
readHistory(name: string): Promise<HistoryEntry[]>
appendHistory(name: string, action: string, via: string): Promise<void>
// action: 'created'|'edited'|'renamed'|'trashed'|'restored'|'locked'|'unlocked'
// via: 'user'|'icloud'|'gdrive'|'github'|'telegram'
// History stored in notes/.history/<name>.json (max 200 entries)
```

### Trash auto-expire
```typescript
expireTrash(months: number): Promise<void>
// Delete notes where trashed_at < now - months*30*86400
// Run on app startup and after every sync
```

### Orphan attachment cleanup
```typescript
cleanOrphanAttachments(): Promise<number>
// Scan all .md files for /attachments/... links (URL-decode before comparing)
// Walk attachments/ tree; delete any file not referenced by any note
// Log each deletion to .deletions.json
// Returns count of files deleted
// Called after every sync operation completes
```

---

## 6. Encryption (`src/services/CryptoService.ts`)

Identical algorithm to the PC app — encrypt/decrypt must be cross-compatible with notes created on the PC.

```
PBKDF2(SHA-256, 100000 iterations, 16-byte random salt) → AES-256-GCM key
Encrypt: [16-byte salt][12-byte IV][ciphertext]  → Base64
Decrypt: parse Base64 → split salt/IV/ct → derive key → decrypt
```

Use `expo-crypto` for random bytes and `react-native-aes-gcm-crypto` or a pure-JS implementation of AES-GCM that is byte-compatible with the Web Crypto API output.

Session password map: `Map<noteName, password>` in Zustand store.  
Auto-lock: 60 seconds after unlock (clear session password, re-render view mode).

---

## 7. Column 1 — Tags Panel

### Visual
- Width: 72 pt (default), user-resizable
- Background: `bgSecondary`
- Top: sync icon button (the myNote logo icon, tappable)
- Below: scrollable `FlatList` of tag items

### Tag list items
```
[All]         count-badge
[tag name]    count-badge   ← user tags, sorted alphabetically
[Trash]       count-badge
```

- `All` and `Trash` are system tags styled differently (muted, slightly smaller)
- Active tag: left border accent stripe + `tagActive` background
- Tap to select tag → Col 2 reloads note list

### Sync icon
- The myNote logo image (`myNote.ico` equivalent as PNG on iOS)
- Tap → trigger manual sync for all enabled sync sources
- States:
  - **Idle** — static icon
  - **Syncing** — rotating animation (spin indefinitely while any sync active)
  - **Flash OK** — border turns green for 2 s, tooltip shows `↑N ↓N`
  - **Flash Error** — border turns red for 2 s, tooltip shows exact error string
- Multiple sync sources tracked in a `Set`; icon only stops spinning when all report 'end'

---

## 8. Column 2 — Note List Panel

### Search bar (top, replaces col header)
- Search icon on left
- `TextInput` — placeholder "Search notes…"
- `+` button on right → create new note
- Debounce 250 ms; clear button appears when non-empty

### Sort bar
Three buttons: `Created` | `Alphabetic` | `Changed`  
Active sort shows a direction arrow (↑/↓) toggled by tapping the active button again.  
Sort persisted in `AsyncStorage`.

### Note list (`FlatList`)

**Normal sort (Created / Changed):** flat list of note items

**Alphabetic sort:** collapsible letter-group headers for letters with ≥ 6 notes; fewer notes appear ungrouped. Groups expand/collapse on tap showing a ▸/▾ chevron.

**Locked notes group:** always rendered at bottom. Collapsed by default. Collapses again automatically after 5 minutes of inactivity.

### Note list item
```
[lock icon, hidden if not encrypted]  [note title text]
```
- Active note: `accent` left border + slightly lighter background
- Multi-select: long-press to enter multi-select mode; tap others to extend selection
- Multi-select with Shift: range select from last tapped item
- Multi-select active: shows `N notes selected — tap Trash/Restore to confirm` in Col 3 title bar
- Multi-select: Trash and Restore buttons only; all other toolbar buttons disabled

---

## 9. Column 3 — Content Panel

### Toolbar row

Left side (actions):
```
[Edit]  [Tag]  |  [Export]  [Share]  |  [Lock]  |  [Trash/Restore]
```

Center:
```
[note title]
[Created: date  Last change: date]
```

Right side:
```
[Settings gear]
```

- **Edit** button: enter edit mode (disabled in Trash view, disabled if note locked)
- **Tag** button: open tag popover (disabled in Trash view, disabled if note locked)
- **Export** button: export note as PDF (disabled if note locked)
- **Share** button: share note as self-contained HTML file via iOS Share Sheet (disabled if encrypted)
- **Lock** button: toggle lock dialog (always enabled when note is open)
- **Trash/Restore**: moves note to trash (in Trash view → restore). Disabled for locked notes not in Trash.
- **Settings**: open Settings modal

### Title bar — history tooltip
Tap the note title area → show a scrollable tooltip listing the note's edit history.  
Format per entry: `action — via — date/time`  
Example: `edited — user — 2024 June 12 at 14:03:22`  
Max height: 75% of screen height. Scrollable. Dismiss on tap-outside or tap-again.

### View mode
Rendered markdown using `react-native-markdown-display`. Custom rules:
- `/attachments/…` image links → inline `Image` component
- `/attachments/…` video links → `expo-av` `Video` component, `shouldPlay: false`, `useNativeControls: true`
- `/attachments/…` audio links → `expo-av` `Audio` player row with play/pause/seek
- Other `/attachments/…` links → tappable row showing file icon + filename → calls `openAttachment()`
- Fenced code blocks → add a "Copy" button overlay (top-right of code block)
- Attachment links: URL-encode filename segment before parsing (spaces/Cyrillic/parens in filenames)

**Encrypted note view mode (locked):** show a lock icon + "This note is encrypted. Tap the lock button to unlock."

### Edit mode
- Full-screen `TextInput`, monospace font (Menlo / Courier New), multiline
- Auto-saves 300 ms after last keystroke
- Auto-renames file when `# Heading` title changes
- Heartbeat: notify backend (Zustand state) that editing is active → sync services wait for a 30-second grace period after last keystroke before running
- **Paste handling:**
  - Clipboard image → upload as attachment → insert markdown link
  - HTML table (from Excel/Google Sheets) → convert to markdown GFM table and insert
  - TSV (tab-separated) → convert to markdown table and insert
- **Drag & drop:** drag files from Files app onto edit area → upload as attachment

---

## 10. Tag Popover

Appears anchored below the `[Tag]` toolbar button. Not a full-screen modal — a floating card.

Contents:
- Horizontal scrollable chip list of current note's tags (each chip has an × to remove)
- Text input: "Add tag" with autocomplete suggestions from all existing tags
- Cannot create a tag named "Telegram" if it doesn't already exist (reserved)
- Dismiss on tap-outside

---

## 11. Settings Modal

Full-screen modal (on iPad: centered dialog 600 × 700 pt, dimmed overlay).

Left panel: navigation list  
Right panel: active settings panel

Navigation items:
1. myNotes folder
2. *(divider)*
3. Email *(store email/password in Keychain, not implemented beyond storage)*
4. Connect Telegram
5. iCloud Storage
6. Google Drive
7. Connect GitHub
8. *(divider)*
9. Locked Notes
10. Import Notable
11. Trash
12. *(divider)*
13. Manage Space

Theme toggle button (sun/moon) always visible in top-right of modal header.  
Close (×) button top-right.

---

### 11.1 myNotes Folder

On iOS, data always lives in `FileSystem.documentDirectory`. There is no user-chosen folder.  
Show the path as a read-only label.  
Explain that notes are stored in the app's private documents directory.  
Add a button: **"Export all notes as ZIP"** → zip the entire `notes/` and `attachments/` tree and share via iOS Share Sheet.

---

### 11.2 Telegram

4-step wizard identical to the PC app but adapted for Telegram Bot API (HTTP) instead of MTProto:

**Step 1 — Bot token**  
- Instruction: create a bot via @BotFather, copy the token
- Field: Bot Token (stored in Keychain via `expo-secure-store`)
- Field: Your Telegram User ID (the numeric ID of your own account — needed to restrict who can send to the bot)
- Button: "Test & Continue →"
- Calls `GET https://api.telegram.org/bot<token>/getMe` to verify token

**Step 2 — Connect channel**  
- Instruction: forward any message from your target channel to the bot, OR paste channel username
- Field: Channel username or numeric ID
- Button: "Connect Channel →"
- Calls `POST /sendMessage` to verify the bot can post to the channel

**Step 3 — Connected**  
- Show bot name, channel ID
- Interval drag-slider (same stops as PC: Never / 30 sec / 1 min / 5 min / 15 min / 30 min / 1 h)
- `Save` button, `Disconnect` button, `Reset sync` button

**Sync behaviour:**  
- Poll `getUpdates` with `offset` tracking
- Messages with `#note` hashtag or sent by the configured user → create or update a note tagged `Telegram`
- Files/photos attached to messages → download and attach to the created note
- Background poll using `expo-background-fetch` at the configured interval

---

### 11.3 iCloud Storage

On macOS/iOS, iCloud is natively available. No "install iCloud for Windows" steps.

**Step 1 — Enable iCloud**  
- Check if `react-native-cloud-storage` (or equivalent) can access iCloud Drive
- If yes, advance automatically
- If no, show instructions: Settings → [Your Name] → iCloud → enable iCloud Drive

**Step 2 — Sync folder**  
- Default sync folder name: `myNotes` inside iCloud Drive
- Show folder tree preview as on PC
- Button: "Create & Connect →"

**Step 3 — Connected**  
- Show iCloud Drive path, folder name, last sync time
- Interval drag-slider (same stops)
- `Sync Now`, `Disconnect`, `Save` buttons

**Sync logic** (identical merge strategy as PC app):
- Enumerate remote `notes/` and `attachments/`
- Three-way merge for each note: base (last-known-common), local, remote → merged text
- `.deletions.json` union-merge: fetch remote, merge with local, push merged back
- Apply deletions from merged log
- Run `cleanOrphanAttachments()` after every sync
- Append to `.history` for each file changed via sync

---

### 11.4 Google Drive

Same wizard structure as iCloud.

**Step 1 — OAuth**  
- Use `expo-auth-session` with Google OAuth 2.0
- Scopes: `https://www.googleapis.com/auth/drive.file`
- Store refresh token in `expo-secure-store`

**Step 2 — Sync folder**  
- Find or create a `myNotes` folder in Drive root
- Show folder ID

**Step 3 — Connected**  
- Show folder name, last sync time
- Interval drag-slider (same stops)
- `Sync Now`, `Disconnect`, `Save` buttons

**Sync logic** (port directly from `_gdrive_do_sync()` in `myNote.py`):
- Use Drive v3 REST API with refresh-token auth
- List all files in remote notes/ and attachments/ subfolders
- Fetch modified-time from Drive, compare to local mtime
- Upload newer-local, download newer-remote
- Conflict: three-way text merge for `.md` files; remote wins for binary attachments if newer
- `.deletions.json` union-merge (same as iCloud)
- Monotonic-clock guard: do not run two syncs within `max(interval - 5, interval * 0.9)` seconds (prevents double-sync race)
- Run `cleanOrphanAttachments()` after sync

---

### 11.5 GitHub

Same wizard structure.

**Step 1 — Setup**  
- Field: Repository URL (`https://github.com/user/repo.git`)
- Field: Personal Access Token (PAT, stored in Keychain)
- Instructions as shown in PC app
- Disclaimer: "⚠ Files larger than 47 MB will not be synchronized." — show in BOTH the setup step and the connected step
- Button: "Connect →" — calls GitHub API to verify PAT and find default branch

**Step 2 — Connected**  
- Show repo URL, last sync time
- Interval drag-slider (same stops)
- `Sync Now`, `Disconnect`, `Save` buttons

**Sync logic** (port from `_gh_do_sync()` in `myNote.py`):
- Never sync dotfiles except `.deletions.json` (which has its own separate fetch/push)
- Never sync files > 47 MB (log to a "too-large" set; skip silently)
- Blob SHA (`git hash-object` style: `sha1("blob " + len + "\0" + content)`) — only push if SHA changed
- State file (`.state.json`) tracks last-known SHA per file
- `.deletions.json` sync: fetch separately → union-merge → push merged → apply deletions locally before main sync loop
- Build `_deleted_rels` set from deletion log; strip those files from `local_files` before main loop
- Three-way text merge for `.md` files with concurrent edits
- SSE-equivalent on iOS: use Zustand actions to fire sync start/end events that update the sync icon UI

---

### 11.6 Locked Notes

List all notes with `encrypted: true` in their frontmatter.  
Each row: lock icon + title + `×` button.  
Tapping `×` → show decrypt dialog → on correct password: strip `encrypted: true`, write plaintext back, refresh list.

---

### 11.7 Import Notable

- Browse for a folder (must contain `notes/` and `attachments/` subdirectories)
- Use `expo-document-picker` to pick a zip or `react-native-fs` directory access
- Convert Notable frontmatter to myNote frontmatter format:
  - `title:` → becomes `# title` heading if missing from body
  - `tags:` → preserved as-is
  - `created:` / `modified:` → preserved
  - `attachments:` Notable field → ignored (use body links instead)
- Copy images to `attachments/images/`
- Skip notes that already exist (same filename)
- Show progress log

---

### 11.8 Trash

- Dropdown: "Auto-delete after" 1–12 months (default 3)
- Save button
- Divider
- "Empty Trash" button → confirmation dialog → permanently delete all trashed notes and their orphaned attachments

---

### 11.9 Manage Space

Stacked colored bar chart showing disk usage by category.  
Categories: Notes (.md files), Images, Documents, Videos, Audio, Archives.  
Each category: color swatch + label + size + file count.  
Grand total row at bottom.  
Tap any category row → show a list of files in that category with individual sizes.  
Long-press a file → delete it (with "Deleting this attachment will break notes that link to it." warning).

---

## 12. Interval Drag-Slider Component

Used in all four sync settings panels. Must be a reusable component.

```typescript
interface IntervalBarProps {
  stops: number[];          // e.g. [0, 30, 60, 300, 900, 1800, 3600]
  labels: string[];         // e.g. ['Never', '30 sec', '1 min', '5 min', '15 min', '30 min', '1 h']
  value: number;            // current stop value
  title?: string;           // e.g. 'Auto-sync interval:'
  onChange: (value: number) => void;
}
```

Visual structure:
```
Auto-sync interval:  [5 min]         ← label + current value
─────────────●───────────────────    ← track + fill + draggable thumb
|    |    |    |    |    |    |      ← tick marks
N   30s   1m   5m  15m  30m   1h   ← labels (active one in accent color)
```

- Thumb snaps to nearest stop on drag release
- Tapping a tick label jumps to that stop
- Dispatches `onChange` on snap

---

## 13. Sync State & Icon

### Global sync state (Zustand)

```typescript
interface SyncStore {
  activeSources: Set<string>;   // sources currently syncing
  addSource: (s: string) => void;
  removeSource: (s: string) => void;
}
```

### Sync icon behaviour
- **Spinning**: `activeSources.size > 0`
- **Flash OK** (2 s, green border): `↑N ↓N` where N = files pushed/pulled
- **Flash Error** (2 s, red border): exact error message string
- Long-press the sync icon → show last sync result for each source as a tooltip list

### Manual sync (tap sync icon)
- Triggers all enabled sync sources in parallel
- Each source runs independently; icon keeps spinning until all finish

### Auto-sync (background)
- `expo-background-fetch` task registered for each enabled source
- Minimum interval enforced by iOS (15 min for background fetch); in-app intervals use `setInterval` while app is active
- Grace period: if a note edit happened within 30 seconds, delay sync
- After sync completes: call `cleanOrphanAttachments()`, then `expireTrash()`

---

## 14. Deletions Log (`.deletions.json`)

Shared across all sync sources. Format:

```json
{
  "settings": {},
  "deletions": [
    { "path": "My Note.md", "type": "note", "deleted_at": "2024-06-01T12:00:00Z" },
    { "path": "images/photo.jpg", "type": "attachment", "deleted_at": "2024-06-02T09:00:00Z" }
  ]
}
```

**Union-merge strategy:** when syncing, fetch remote `.deletions.json`, merge with local (keep all unique entries, deduplicate by path+type+deleted_at). Push merged back. Apply any deletions not yet applied locally.

**Apply deletion:** delete local file if it exists; do not error if already gone.

All sync sources (iCloud, GDrive, GitHub) use this same file and merge strategy. The Android app (KosalmaApp) also reads/writes this file — do not change the format.

---

## 15. Note History Tooltip

Tap the note title area in the toolbar to show/hide.  
Rendered as an absolutely-positioned `ScrollView` overlay anchored below the title.  
Max height: 75% of screen.  
Each entry: `{action} via {source} — {formatted date}`  
Dismiss by tapping the title again or tapping anywhere else.

---

## 16. PDF Export

Tap the Export button → generate PDF → share via iOS Share Sheet.

Process:
1. Convert Markdown body to HTML (use `marked.js` logic or a Swift-compatible markdown→HTML library)
2. Embed images as base64 data URIs
3. Call `expo-print`'s `printToFileAsync({ html })` → produces a `.pdf` file in cache
4. Call `expo-sharing`'s `shareAsync(uri)`

If note is encrypted and locked: disabled.  
If note is encrypted and unlocked in session: ask for password again (mode 'export') before exporting. This matches the PC behaviour.

---

## 17. Share as HTML

Tap the Share button → build self-contained HTML file → share via iOS Share Sheet.

The HTML includes:
- Note title as `<h1>`
- Created/modified timestamps
- Tags as styled chips
- Rendered markdown body (same CSS variables → embed inline CSS)
- Embedded attachment images as base64 data URIs
- Non-image attachments as download links (may not work on all platforms)

Output filename: `{note title}.html`

---

## 18. Note Locking Flow

Matches PC app exactly:

| State | Lock button shows | Tap action |
|---|---|---|
| Note not encrypted | "Lock note" | Open lock dialog (set password) |
| Encrypted, no session password | "Unlock note" | Open unlock dialog → on success: 60s auto-lock timer starts |
| Encrypted, session active | "Lock note" | Immediately re-lock (clear session password) |

**Lock dialog:** password field + confirm field → AES-GCM encrypt body → write `encrypted: true` to frontmatter → save.  
**Unlock dialog:** password field → decrypt → show plaintext view → start 60-second auto-lock timer.  
**Export encrypted note:** re-verify password even if session is active.

---

## 19. Attachment Subfolders

Auto-determined from file extension:

| Subfolder | Extensions |
|---|---|
| `images` | .png .jpg .jpeg .gif .bmp .svg .webp .ico .tiff .heic .heif |
| `videos` | .mp4 .avi .mov .mkv .wmv .flv .webm .m4v .mpg .mpeg .3gp |
| `audio` | .mp3 .m4a .ogg .oga .wav .flac .aac .opus .weba .wma |
| `archives` | .zip .rar .7z .tar .gz .bz2 .xz .tgz |
| `documents` | everything else |

When an attachment is uploaded, its markdown link is inserted at cursor:
- Image: `![filename](../../attachments/images/filename.png)`  
- Other: `[filename](../../attachments/documents/filename.pdf)`

Filename segment must be URL-encoded so markdown parsers handle spaces and special chars.

---

## 20. Keyboard Shortcuts (iPad hardware keyboard)

| Shortcut | Action |
|---|---|
| `⌘N` | New note |
| `⌘S` | Save (force-flush pending save) |
| `⌘E` | Toggle edit/view mode |
| `⌘F` | Focus search bar |
| `⌘⇧T` | Add tag |
| `Escape` | Close popover / cancel edit |
| `⌘,` | Open Settings |
| `⌘⇧P` | Export as PDF |

---

## 21. Toasts & Notifications

Two toast styles (matching PC app):

**General notification toast** (bottom centre, dismissable):  
Used for sync warnings, connectivity issues, Telegram events.  
Auto-dismiss after 5 s or user taps ×.

**Export toast** (bottom right, with 📄 icon):  
Appears after PDF/HTML export completes.  
Shows filename.  
Auto-dismiss after 4 s.

---

## 22. Confirmation Dialogs

**Send to Trash:**  
"Send to trash?" with Yes / No.  
Appears when tapping the Trash button for a non-trashed note.

**Empty Trash:**  
"Attention! This will remove all notes and files from Trash forever." with Cancel / OK.

**Attachment delete:**  
"Deleting this attachment will break notes that link to it." with Cancel / Delete.

All dialogs are `Modal` components with dimmed overlay.

---

## 23. Multi-Select

- Long-press a note item → enter multi-select mode (note becomes selected)
- Tap additional notes to add/remove from selection (on iPad can also use Shift+tap via keyboard)
- Col 3 title bar shows: `N notes selected — tap Trash/Restore to confirm`
- Only Trash/Restore button active
- Tapping Trash/Restore with N notes selected → trash/restore all N → exit multi-select

---

## 24. What NOT to Build

These PC features do not exist on iOS and must not be built:

- Window title bar, minimize/maximize/close buttons (iOS handles window chrome)
- Windows DPAPI encryption (replaced by Keychain)
- Browser password import (iOS sandbox forbids browser database access; omit the entire panel)
- Folder picker for notes folder (DocumentDirectory is fixed)
- Column drag handles (use gestures on iPad instead of mousedown)

---

## 25. Build & Run

```bash
# macOS prerequisites
brew install node
brew install watchman
npm install -g expo-cli eas-cli

# Create project
npx create-expo-app myNoteApp --template blank-typescript
cd myNoteApp

# Install dependencies
npx expo install expo-file-system expo-secure-store expo-document-picker \
  expo-sharing expo-print expo-av expo-image-picker \
  expo-auth-session expo-crypto expo-background-fetch expo-task-manager \
  @react-navigation/native @react-navigation/stack \
  react-native-safe-area-context react-native-screens \
  react-native-markdown-display \
  zustand

# For iCloud sync:
# npm install react-native-cloud-storage

# Run on iPad simulator
npx expo run:ios --device

# Or start dev server (scan QR with Expo Go on iPad):
npx expo start
```

Set in `app.json`:
```json
{
  "expo": {
    "name": "myNote",
    "slug": "mynote",
    "ios": {
      "supportsTablet": true,
      "requireFullScreen": false,
      "bundleIdentifier": "com.yourname.mynote",
      "entitlements": {
        "com.apple.developer.icloud-container-identifiers": ["iCloud.com.yourname.mynote"],
        "com.apple.developer.ubiquity-kvstore-identifier": "$(TeamIdentifierPrefix)com.yourname.mynote"
      }
    }
  }
}
```

---

## 26. Implementation Order

Build in this sequence so each step is testable:

1. **NoteService + file system** — create/read/save/list notes; verify `.md` files appear in Files app
2. **Three-column layout** — static scaffold with theme, column widths, drag handles
3. **Tags col + note list col** — render from NoteService; tag filter; search; sort
4. **View mode** — markdown render, media players, attachment links
5. **Edit mode** — `TextInput`, auto-save, rename, paste/drop attachments
6. **Encryption** — lock/unlock dialogs, AES-GCM round-trip test
7. **Settings modal** — navigation shell + all panels (can stub sync panels)
8. **GitHub sync** — REST port of `_gh_do_sync`; test against a real repo
9. **Google Drive sync** — OAuth + REST port of `_gdrive_do_sync`
10. **iCloud sync** — CloudKit/iCloud Drive integration
11. **Telegram sync** — Bot API polling
12. **PDF export + HTML share**
13. **Manage Space panel**
14. **Background fetch + sync icon animations**
15. **Keyboard shortcuts**
16. **Deletions log cross-sync + orphan cleanup**

---

## 27. Cross-Compatibility Requirements

Notes created on the iPad MUST open correctly on the PC app and vice versa:

- Frontmatter format: identical YAML between `---` delimiters
- Encryption: identical AES-GCM + PBKDF2 scheme (same salt/IV layout, same Base64 encoding)
- Attachment URLs in note body: `/attachments/{subfolder}/{filename}` — same path scheme
- `.deletions.json`: same JSON format, same union-merge strategy
- `.state.json` (GitHub sync): same SHA-tracking format
- History files: `notes/.history/{notename}.json`, same entry format `{action, via, at}`

