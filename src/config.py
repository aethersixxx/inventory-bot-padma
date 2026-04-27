"""
Konfigurasi aplikasi - load dari environment variables.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    """Parse string '123,456' menjadi set of int."""
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Google Sheets
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")
GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH", "credentials/service-account.json"
)

# Access Control
ADMIN_USER_IDS: set[int] = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))
ALLOWED_USER_IDS: set[int] = _parse_ids(os.getenv("ALLOWED_USER_IDS", ""))

# Cache
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log")

# Gemini AI
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
AI_ENABLED = bool(GEMINI_API_KEY)


def is_admin(user_id: int) -> bool:
    """Cek apakah user adalah admin."""
    return user_id in ADMIN_USER_IDS


def is_allowed(user_id: int) -> bool:
    """
    Cek apakah user diizinkan akses bot.
    - Jika ALLOWED_USER_IDS kosong → semua user boleh (read-only)
    - Admin selalu diizinkan
    """
    if is_admin(user_id):
        return True
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def validate() -> None:
    """Validasi konfigurasi minimum sebelum bot start."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")
    if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        missing.append(f"GOOGLE_CREDENTIALS_PATH ({GOOGLE_CREDENTIALS_PATH} tidak ada)")
    if missing:
        raise RuntimeError(f"Konfigurasi tidak lengkap: {', '.join(missing)}")
