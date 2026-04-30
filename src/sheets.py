"""
Modul untuk akses Google Sheets sebagai database inventory.

Fitur:
- Service account authentication
- In-memory cache (TTLCache) untuk minimalkan request ke Google
- Pencarian case-insensitive + partial match
- Fuzzy matching untuk auto-suggest
- Update kolom (untuk admin)

Asumsi struktur sheet (header di row 1):
    Nama Mesin | Model | Merk | Tipe Mesin | Part Number |
    Serial Number | Lokasi | Status | Keterangan | Status Terakhir
"""
from __future__ import annotations

import threading
from typing import Optional

import gspread
from cachetools import TTLCache
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

from src import config
from src.logger import logger

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Field yang akan ditampilkan ke user (urutan sesuai requirement)
DISPLAY_FIELDS = [
    "Nama Mesin",
    "Model",
    "Merk",
    "Tipe Mesin",
    "Part Number",
    "Serial Number",
    "Lokasi",
    "Status",
    "Keterangan",
    "Status Terakhir",
]

# Field yang dapat dipakai untuk matching saat user mencari
# Termasuk Lokasi & Status Terakhir karena user sering tanya "barang di X"
# di mana X bisa lokasi resmi (kolom Lokasi) atau status/posisi (kolom Status Terakhir)
SEARCHABLE_FIELDS = [
    "Nama Mesin",
    "Model",
    "Merk",
    "Part Number",
    "Serial Number",
    "Lokasi",
    "Status Terakhir",
]

# Stop words bahasa Indonesia/Inggris - di-skip saat tokenize multi-word query
# (supaya "barang apa yang ada D320?" → cari "D320", bukan "barang"/"apa"/"yang")
_STOP_WORDS = {
    "apa", "apakah", "yang", "ada", "saja", "berapa", "mana", "dimana",
    "kenapa", "bagaimana", "siapa", "kapan", "tolong", "tampilkan",
    "tunjukkan", "carikan", "cari", "list", "daftar", "semua", "total",
    "jumlah", "barang", "mesin", "alat", "item", "punya", "punyai",
    "dengan", "untuk", "dari", "ke", "di", "pada", "atau", "dan",
    "is", "are", "the", "a", "an", "of", "for", "with", "what", "which",
    "show", "find", "search", "all", "any", "have", "has",
}


def _tokenize(query: str) -> list[str]:
    """
    Pecah query jadi token signifikan.
    - Lowercase
    - Buang tanda baca (kecuali strip dari ujung)
    - Buang stop words
    - Buang token sangat pendek (1 char), kecuali angka/kode
    """
    import re
    # Pisah by whitespace, hilangkan tanda baca di ujung tiap kata
    raw = re.findall(r"[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*", query.lower())
    return [
        t for t in raw
        if t not in _STOP_WORDS and (len(t) >= 2 or t.isdigit())
    ]



class SheetsClient:
    """Wrapper gspread dengan cache."""

    def __init__(self) -> None:
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None
        # Cache key "all_records" → list[dict]
        self._cache: TTLCache = TTLCache(maxsize=8, ttl=config.CACHE_TTL)
        self._lock = threading.Lock()

    # ---------- koneksi ----------
    def _get_worksheet(self) -> gspread.Worksheet:
        if self._worksheet is not None:
            return self._worksheet

        creds = Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_PATH, scopes=SCOPES
        )
        self._client = gspread.authorize(creds)
        spreadsheet = self._client.open_by_key(config.GOOGLE_SHEET_ID)
        try:
            self._worksheet = spreadsheet.worksheet(config.GOOGLE_SHEET_NAME)
        except gspread.WorksheetNotFound:
            # fallback ke sheet pertama
            self._worksheet = spreadsheet.sheet1
            logger.warning(
                "Worksheet '%s' tidak ditemukan, fallback ke sheet pertama: '%s'",
                config.GOOGLE_SHEET_NAME,
                self._worksheet.title,
            )
        logger.info("Terhubung ke sheet: %s", self._worksheet.title)
        return self._worksheet

    # ---------- data ----------
    def get_all_records(self, force_refresh: bool = False) -> list[dict]:
        """
        Ambil semua data dari sheet (dengan cache).

        PENTING: Pakai get_all_values() langsung (raw string) lalu construct
        dict manual. gspread.get_all_records() — bahkan dengan FORMATTED_VALUE
        — kadang masih convert cell yang match pola scientific notation
        (mis. '507E91404305') jadi infinity. Cara aman: ambil sebagai string
        mentah, biarkan Python jangan auto-convert.
        """
        with self._lock:
            if not force_refresh and "all_records" in self._cache:
                logger.debug("Cache HIT: all_records")
                return self._cache["all_records"]

            logger.debug("Cache MISS: fetching dari Google Sheets")
            ws = self._get_worksheet()

            # Ambil semua cell sebagai string (FORMATTED_VALUE dijamin string)
            all_values = ws.get_all_values(
                value_render_option="FORMATTED_VALUE"
            )

            if not all_values:
                logger.warning("Sheet kosong")
                self._cache["all_records"] = []
                return []

            # Row pertama = header
            headers = [str(h).strip() for h in all_values[0]]
            data_rows = all_values[1:]

            # Build list of dict, semua value sebagai string
            records: list[dict] = []
            for row in data_rows:
                # Pad row dengan empty string kalau kolom kurang
                padded = list(row) + [""] * (len(headers) - len(row))
                rec = {
                    headers[i]: str(padded[i]).strip()
                    for i in range(len(headers))
                }
                records.append(rec)

            self._cache["all_records"] = records
            logger.info("Loaded %d baris dari Google Sheets", len(records))

            # Debug: log 1 sample SN supaya kita tahu data ke-load benar
            if records and "Serial Number" in headers:
                samples = [
                    r.get("Serial Number", "")
                    for r in records[:5]
                    if r.get("Serial Number")
                ]
                logger.debug("Sample SN: %s", samples)

            return records

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            logger.info("Cache di-invalidate")

    # ---------- metadata ----------
    def get_last_modified(self) -> Optional[str]:
        """
        Ambil timestamp terakhir sheet diubah (dari Google Drive API).
        Return string format: "30 Apr 2026 14:30 WIB"
        Return None kalau gagal (gak block bot, optional info).

        Cache dipisah dari data records, TTL 60 detik agar tidak spam API.
        """
        with self._lock:
            cache_key = "last_modified_str"
            if cache_key in self._cache:
                return self._cache[cache_key]

            try:
                # Ambil credentials yang sama dengan gspread
                creds = Credentials.from_service_account_file(
                    config.GOOGLE_CREDENTIALS_PATH, scopes=SCOPES
                )
                # Build Drive API client lazily
                from googleapiclient.discovery import build
                drive = build("drive", "v3", credentials=creds, cache_discovery=False)

                # Ambil metadata file
                file_meta = drive.files().get(
                    fileId=config.GOOGLE_SHEET_ID,
                    fields="modifiedTime",
                    supportsAllDrives=True,
                ).execute()

                modified_iso = file_meta.get("modifiedTime", "")
                if not modified_iso:
                    return None

                # Parse ISO8601 timestamp (UTC) → format WIB
                formatted = _format_jakarta_time(modified_iso)
                self._cache[cache_key] = formatted
                logger.debug("Sheet last modified: %s", formatted)
                return formatted

            except ImportError:
                logger.warning(
                    "google-api-python-client tidak terinstall. "
                    "Last modified tidak tersedia."
                )
                return None
            except Exception as e:
                logger.warning("Gagal ambil sheet metadata: %s", e)
                return None

    # ---------- pencarian ----------
    def search(self, query: str) -> list[dict]:
        """
        Pencarian case-insensitive + partial match pada field searchable.

        Strategi:
        1. Coba whole-string match dulu (untuk query pendek seperti "D320")
        2. Kalau hasil 0 atau query > 2 kata → tokenize, cari baris yang match
           SEMUA token signifikan (AND logic) di kombinasi field searchable.

        Return list barang yang match, urut by relevance.
        """
        if not query or not query.strip():
            return []

        q = query.strip().lower()
        records = self.get_all_records()

        # --- Mode 1: whole-string match (untuk query pendek/literal) ---
        exact: list[dict] = []
        prefix: list[dict] = []
        contains: list[dict] = []

        for row in records:
            best_score = 0
            for field in SEARCHABLE_FIELDS:
                value = str(row.get(field, "")).lower()
                if not value:
                    continue
                if value == q:
                    best_score = 3
                    break
                if value.startswith(q):
                    best_score = max(best_score, 2)
                elif q in value:
                    best_score = max(best_score, 1)

            if best_score == 3:
                exact.append(row)
            elif best_score == 2:
                prefix.append(row)
            elif best_score == 1:
                contains.append(row)

        whole_match = exact + prefix + contains

        # Kalau whole-string match sudah dapat hasil DAN query pendek (≤2 kata),
        # return langsung tanpa tokenize.
        word_count = len(q.split())
        if whole_match and word_count <= 2:
            return whole_match

        # --- Mode 2: multi-token match (untuk pertanyaan natural) ---
        tokens = _tokenize(query)
        if not tokens:
            return whole_match

        token_match: list[dict] = []
        for row in records:
            # Gabung semua field searchable jadi satu blob teks
            blob = " ".join(
                str(row.get(f, "")).lower() for f in SEARCHABLE_FIELDS
            )
            # Semua token harus muncul di blob (AND logic)
            if all(t in blob for t in tokens):
                token_match.append(row)

        # Merge: whole_match dulu (lebih relevan), lalu token_match yang belum ada
        seen_ids = {id(r) for r in whole_match}
        combined = list(whole_match)
        for r in token_match:
            if id(r) not in seen_ids:
                combined.append(r)

        return combined

    def fuzzy_suggest(self, query: str, limit: int = 5) -> list[str]:
        """
        Saran nama mesin via fuzzy matching - dipakai saat hasil search kosong.
        Return list nama (string) yang paling mirip.
        """
        records = self.get_all_records()
        choices: list[str] = []
        for row in records:
            name = str(row.get("Nama Mesin", "")).strip()
            if name:
                choices.append(name)

        if not choices:
            return []

        # rapidfuzz.process.extract → return [(choice, score, idx), ...]
        results = process.extract(
            query, choices, scorer=fuzz.WRatio, limit=limit, score_cutoff=50
        )
        return [r[0] for r in results]

    # ---------- update (admin) ----------
    def update_quantity(self, item_name: str, quantity: int | str) -> bool:
        """
        Update kolom 'Status' (atau 'Stok' jika ada) untuk barang yang match.
        Return True jika sukses, False jika barang tidak ditemukan.

        Note: Sesuai struktur sheet di requirement, "stok" disimulasikan via
        kolom 'Status'. Jika sheet punya kolom khusus 'Stok', kode akan
        otomatis pakai itu.
        """
        ws = self._get_worksheet()
        records = self.get_all_records(force_refresh=True)
        if not records:
            return False

        headers = list(records[0].keys())
        # Cari kolom target: prioritas 'Stok' > 'Status'
        target_col_name = None
        for candidate in ("Stok", "Stock", "Jumlah", "Status"):
            if candidate in headers:
                target_col_name = candidate
                break
        if not target_col_name:
            logger.error("Tidak ada kolom Stok/Status di sheet")
            return False

        target_col_idx = headers.index(target_col_name) + 1  # 1-based untuk gspread

        # Cari row yang match (case-insensitive, exact match dulu)
        q = item_name.strip().lower()
        target_row_idx = None
        for i, row in enumerate(records, start=2):  # start=2 karena row 1 = header
            if str(row.get("Nama Mesin", "")).strip().lower() == q:
                target_row_idx = i
                break

        # fallback: partial match
        if target_row_idx is None:
            for i, row in enumerate(records, start=2):
                if q in str(row.get("Nama Mesin", "")).strip().lower():
                    target_row_idx = i
                    break

        if target_row_idx is None:
            return False

        ws.update_cell(target_row_idx, target_col_idx, quantity)
        self.invalidate_cache()
        logger.info(
            "UPDATE: row %d, kolom %s → %s", target_row_idx, target_col_name, quantity
        )
        return True


# ---------- helper untuk format timezone ----------
# Bulan dalam Bahasa Indonesia (singkat)
_MONTH_NAMES_ID = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "Mei", 6: "Jun",
    7: "Jul", 8: "Agu", 9: "Sep", 10: "Okt", 11: "Nov", 12: "Des",
}


def _format_jakarta_time(iso_utc: str) -> str:
    """
    Convert ISO8601 UTC timestamp → format WIB.

    Input:  "2026-04-30T07:30:45.123Z"
    Output: "30 Apr 2026 14:30 WIB"
    """
    from datetime import datetime, timezone, timedelta

    try:
        # Parse ISO8601 (handle baik dengan/tanpa milliseconds)
        # Trim 'Z' dan replace dengan timezone offset eksplisit
        clean = iso_utc.rstrip("Z")
        # fromisoformat handle milliseconds otomatis di Python 3.11+
        # tapi untuk safety, drop milliseconds kalau ada
        if "." in clean:
            clean = clean.split(".")[0]
        dt_utc = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)

        # Convert ke WIB (UTC+7)
        wib = timezone(timedelta(hours=7))
        dt_wib = dt_utc.astimezone(wib)

        # Format: "30 Apr 2026 14:30 WIB"
        bulan = _MONTH_NAMES_ID.get(dt_wib.month, dt_wib.strftime("%b"))
        return f"{dt_wib.day} {bulan} {dt_wib.year} {dt_wib.strftime('%H:%M')} WIB"
    except Exception:
        return iso_utc  # fallback: return raw


# Singleton instance
sheets_client = SheetsClient()


# ---------- formatting helper ----------
# Set nilai-nilai yang dianggap "tidak valid" dan harus ditampilkan sebagai "-"
# (terjadi kalau data sheet ke-cache sebelum fix FORMATTED_VALUE)
_INVALID_VALUES = {"inf", "-inf", "nan", "infinity", "-infinity"}


def _clean_value(value) -> str:
    """
    Bersihkan nilai cell. Return string ter-strip.
    Kalau nilai adalah sentinel invalid (inf/nan dari scientific notation
    yang ke-corrupt), return string kosong.
    """
    s = str(value).strip()
    if s.lower() in _INVALID_VALUES:
        return ""
    return s


def format_item(row: dict) -> str:
    """
    Format satu baris data jadi pesan Telegram (Markdown).

    Logika 'Status Terakhir':
    - Jika kolom 'Status Terakhir' berisi teks → tampilkan teksnya
    - Jika kosong → tampilkan nilai kolom 'Lokasi'
    """
    lines = ["📦 *Detail Barang*", ""]
    for field in DISPLAY_FIELDS:
        value = _clean_value(row.get(field, ""))

        # Logika khusus Status Terakhir
        if field == "Status Terakhir" and not value:
            lokasi = _clean_value(row.get("Lokasi", ""))
            value = lokasi if lokasi else "-"

        if not value:
            value = "-"

        # Escape underscore untuk markdown
        safe_value = value.replace("*", "").replace("_", "\\_")
        lines.append(f"*{field}:* {safe_value}")

    return "\n".join(lines)
