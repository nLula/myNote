import os
import sys
import json


def _is_frozen() -> bool:
    return getattr(sys, 'frozen', False)


def _base_dir() -> str:
    """Root directory for bundled assets (templates/, static/).
    • PyInstaller --onefile  → sys._MEIPASS  (temp extraction dir)
    • PyInstaller --onedir   → sys._MEIPASS  (same as exe dir, but use _MEIPASS to be safe)
    • Development            → directory that contains this file
    """
    if _is_frozen():
        # sys._MEIPASS is set by PyInstaller for both --onefile and --onedir
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# --- AppData paths (config lives here, survives across installs) ---
APPDATA_DIR: str = os.path.join(
    os.environ.get('APPDATA', os.path.expanduser('~')), 'myNote'
)
CONFIG_FILE: str = os.path.join(APPDATA_DIR, 'config.json')

# --- Default user-data location (user's actual notes & attachments) ---
DEFAULT_NOTES_DIR: str = os.path.join(
    os.path.expanduser('~'), 'Documents', 'myNotes'
)

# --- Flask asset directories ---
TEMPLATES_DIR: str = os.path.join(_base_dir(), 'templates')
STATIC_DIR: str = os.path.join(_base_dir(), 'static')

# --- Flask server settings ---
HOST: str = '127.0.0.1'
PORT: int = 5000


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_config(cfg: dict) -> None:
    os.makedirs(APPDATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Public accessors — always read from config so the user can change the path
# ---------------------------------------------------------------------------

def get_notes_dir() -> str:
    return _load_config().get('notes_dir', DEFAULT_NOTES_DIR)


def set_notes_dir(path: str) -> None:
    cfg = _load_config()
    cfg['notes_dir'] = path
    _save_config(cfg)


def get_attachments_dir() -> str:
    return os.path.join(get_notes_dir(), 'attachments')


# ---------------------------------------------------------------------------
# Bootstrap — call once on startup
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    """Create AppData config dir + notes/attachments dirs if they don't exist."""
    os.makedirs(APPDATA_DIR, exist_ok=True)
    os.makedirs(get_notes_dir(), exist_ok=True)
    os.makedirs(get_attachments_dir(), exist_ok=True)
