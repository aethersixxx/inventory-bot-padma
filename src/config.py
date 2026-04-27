"""
Konfigurasi aplikasi - load dari environment variables.
"""
import base64
import json
import os
import tempfile
from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _resolve_credentials_path() -> str:
    b64 = os.getenv("GOOGLE_CREDENTIALS_JSON_BASE64", "").strip()
    if b64:
        try:
            data = base64.b64decode(b64).decode("utf-8")
            return _write_creds_to_temp(data)
        except Exception as e:
            raise RuntimeError(f"GOOGLE_CREDENTIALS_JSON_BASE64 invalid: {e}")

    raw_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        try:
            json.loads(raw_json)
        except Exception as e:
            raise RuntimeError(f"GOOGLE_CREDENTIALS_JSON invalid JSON: {e}")
        return _write_creds_to_temp(raw_json)

    return os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/service-account.json")


def _write_creds_to_temp(json_content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="gcreds_")
    with os.fdopen(fd, "w") as f:
        f.write(json_content)
    return path


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")
GOOGLE_CREDENTIALS_PATH = _resolve_credentials_path()

ADMIN_USER_IDS: set[int] = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))
ALLOWED_USER_IDS: set[int] = _parse_ids(os.getenv("ALLOWED_USER_IDS", ""))

CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
AI_ENABLED = bool(GEMINI_API_KEY)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def is_allowed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def validate() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")
    if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        missing.append(f"GOOGLE_CREDENTIALS_PATH ({GOOGLE_CREDENTIALS_PATH} tidak ada)")
    if missing:
        raise RuntimeError(f"Konfigurasi tidak lengkap: {', '.join(missing)}")
