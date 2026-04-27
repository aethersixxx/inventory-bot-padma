"""
AI-powered search menggunakan Gemini.

Strategi: Gemini berperan sebagai NLU parser, BUKAN search engine.
- Input: pertanyaan natural user + sample data sheet (untuk konteks kolom)
- Output: JSON {filters: [...], aggregation: ..., limit: ...}
- Python eksekusi filter di cached data (cepat, hemat token)

Untuk pertanyaan agregat ("ada berapa", "total"), Gemini sekaligus
men-summary hasil setelah Python filter datanya.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from google import genai
from google.genai import types

from src import config
from src.logger import logger

# Initialize Gemini client (lazy)
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


# Trigger keywords (kalau muncul → kemungkinan besar pertanyaan natural)
_NL_KEYWORDS = {
    "apa", "apakah", "berapa", "yang", "ada", "saja", "mana", "dimana",
    "kenapa", "bagaimana", "siapa", "kapan", "tolong", "tampilkan",
    "tunjukkan", "carikan", "cari", "list", "daftar", "semua", "total",
    "jumlah", "rusak", "tersedia", "available", "broken", "in use",
    "lokasi", "gudang", "berfungsi", "kosong",
}


def is_natural_language_query(text: str) -> bool:
    """
    Heuristik: deteksi apakah query terlihat seperti pertanyaan natural.
    Kalau ya → pakai AI parser. Kalau enggak → search literal biasa.

    Logic:
    - Pendek (≤ 3 kata) DAN tidak ada keyword tanya → literal search
    - Selain itu → natural language
    """
    if not text or not text.strip():
        return False

    words = text.lower().split()
    word_count = len(words)

    # Cek keyword natural language
    has_nl_keyword = any(w in _NL_KEYWORDS for w in words)

    # Cek tanda tanya
    has_question_mark = "?" in text

    # Aturan
    if word_count <= 2:
        # "D320", "compressor abc" → literal
        return False
    if has_nl_keyword or has_question_mark:
        return True
    if word_count >= 4:
        # kalimat panjang → kemungkinan natural
        return True
    return False


def _extract_column_samples(
    records: list[dict], headers: list[str], max_per_col: int = 8
) -> dict[str, list[str]]:
    """
    Extract nilai unik per kolom (top N paling sering muncul).
    Membantu Gemini memahami tipe data tiap kolom — misal:
      "Lokasi" → ["Limus", "Cicurug", "Commpark", "Trial", "Ciracas"]
      "Status Terakhir" → ["Indolakto Cicurug 3", "PO Indofood NSF", ...]
    """
    from collections import Counter

    samples: dict[str, list[str]] = {}
    for header in headers:
        counter: Counter = Counter()
        for row in records:
            val = str(row.get(header, "")).strip()
            if val and val not in {"-", "N/A"}:
                counter[val] += 1
        # Top N nilai paling sering
        top = [v for v, _ in counter.most_common(max_per_col)]
        if top:
            samples[header] = top
    return samples


def _build_parser_prompt(
    query: str,
    headers: list[str],
    sample_rows: list[dict],
    column_samples: dict[str, list[str]],
) -> str:
    """Bangun prompt untuk Gemini parse intent dengan info nilai unik per kolom."""
    # Format kolom + sample values jadi readable
    col_info_lines = []
    for h in headers:
        samples = column_samples.get(h, [])
        if samples:
            preview = ", ".join(f'"{s}"' for s in samples[:6])
            extra = f" (+{len(samples)-6} lainnya)" if len(samples) > 6 else ""
            col_info_lines.append(f'  - "{h}" → contoh nilai: [{preview}]{extra}')
        else:
            col_info_lines.append(f'  - "{h}" → (mostly kosong)')
    columns_described = "\n".join(col_info_lines)

    return f"""Kamu adalah parser query inventory. Tugasmu mengubah pertanyaan user
menjadi JSON filter struktur. JANGAN menjawab pertanyaannya, JANGAN tambah
penjelasan, HANYA balas dengan JSON.

KOLOM YANG TERSEDIA (dengan contoh nilai untuk konteks):
{columns_described}

PENTING — Cara memilih kolom yang tepat:
- Lihat NILAI SAMPLE di tiap kolom untuk paham isi sebenarnya
- "Lokasi" biasanya berisi lokasi resmi/gudang utama
- "Status Terakhir" sering berisi keterangan posisi terakhir / proyek / perusahaan tujuan
- Kalau user sebut nama yang muncul di "Status Terakhir" (mis. nama perusahaan/proyek),
  pakai filter pada kolom "Status Terakhir", BUKAN "Lokasi"
- Kalau user tanya "di mana barang X" tanpa konteks lokasi, cari di semua kolom (free_text)

Skema JSON output:
{{
  "filters": [
    {{"field": "<nama kolom EXACT>", "op": "contains|equals|starts_with", "value": "<string>"}}
  ],
  "aggregation": "count|list|none",
  "free_text": "<keyword bebas, atau null>",
  "limit": <int, default 50>,
  "explanation": "<1 kalimat Bahasa Indonesia jelaskan apa yang dicari + dari kolom mana>"
}}

Aturan:
- "field" HARUS PERSIS sama dengan salah satu nama kolom di atas (case-sensitive!)
- Kalau user sebut kode/model spesifik (D320, ABC-123) tanpa info lokasi → pakai "free_text"
- Kalau user nyebut nama tempat/proyek/perusahaan → COCOKKAN dengan sample values:
  * Kalau nilainya muncul di "Lokasi" → filter Lokasi
  * Kalau muncul di "Status Terakhir" → filter Status Terakhir
  * Kalau ragu / muncul di dua-duanya → boleh pakai 2 filter terpisah ATAU free_text
- "berapa" / "ada berapa" / "total" → aggregation = "count"
- "list" / "daftar" / "apa saja" / "semua" → aggregation = "list"
- Cari satu barang spesifik → aggregation = "none"
- "rusak" / "broken" → filter Status contains "rusak"
- Output HARUS valid JSON, TANPA markdown ```json``` atau teks lain

Pertanyaan user: "{query}"

JSON:"""


def parse_query(
    query: str, headers: list[str], all_records: list[dict]
) -> Optional[dict]:
    """
    Pakai Gemini untuk parse query natural → struktur filter.
    Return None kalau gagal (caller harus fallback ke literal search).
    """
    if not config.AI_ENABLED:
        return None

    try:
        client = _get_client()
        # Extract sample values per kolom (cached-able tapi tiap call cepat)
        column_samples = _extract_column_samples(all_records, headers)
        prompt = _build_parser_prompt(query, headers, all_records, column_samples)

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=600,
                response_mime_type="application/json",
            ),
        )

        text = (response.text or "").strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

        parsed = json.loads(text)
        logger.info("AI parsed: %s", parsed.get("explanation", "(no explanation)"))
        logger.debug("AI filters: %s | free_text=%s",
                     parsed.get("filters"), parsed.get("free_text"))
        return parsed

    except json.JSONDecodeError as e:
        logger.warning("Gemini balas non-JSON: %s | text=%r", e, text[:200])
        return None
    except Exception as e:
        # Quota / rate limit error → log ringkas saja (jangan spam stack trace)
        err_str = str(e)
        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str or "quota" in err_str.lower():
            logger.warning(
                "Gemini quota habis / belum aktif. Fallback ke literal search. "
                "Cek: https://aistudio.google.com/app/apikey"
            )
        else:
            logger.exception("Gagal panggil Gemini: %s", e)
        return None


def execute_filter(parsed: dict, all_records: list[dict]) -> list[dict]:
    """
    Eksekusi filter hasil parsing di data lokal (cached).

    Strategy:
    1. Apply filter sesuai instruksi Gemini.
    2. Kalau hasil kosong tapi ada filter dengan value, coba SAFETY NET:
       cari value tersebut di kolom-kolom alternatif (Lokasi, Status Terakhir,
       Keterangan, Nama Mesin) — karena Gemini bisa salah pilih kolom.
    """
    results = list(all_records)

    # Apply structured filters
    filter_values: list[str] = []  # untuk safety net
    for f in parsed.get("filters") or []:
        field = f.get("field")
        op = f.get("op", "contains")
        value = str(f.get("value", "")).lower().strip()
        if not field or not value:
            continue
        filter_values.append(value)

        filtered = []
        for row in results:
            cell = str(row.get(field, "")).lower().strip()
            if not cell:
                continue
            if op == "equals" and cell == value:
                filtered.append(row)
            elif op == "starts_with" and cell.startswith(value):
                filtered.append(row)
            elif op == "contains" and value in cell:
                filtered.append(row)
        results = filtered

    # Apply free_text search (di semua kolom searchable)
    free_text = (parsed.get("free_text") or "").lower().strip()
    if free_text:
        from src.sheets import SEARCHABLE_FIELDS
        filtered = []
        for row in results:
            for field in SEARCHABLE_FIELDS:
                if free_text in str(row.get(field, "")).lower():
                    filtered.append(row)
                    break
        results = filtered

    # SAFETY NET: kalau hasil 0 tapi punya filter values, coba cari value
    # tersebut di kolom-kolom yang sering jadi tempat info lokasi/keterangan.
    # Ini menyelamatkan kasus Gemini salah pilih kolom (mis. pilih Lokasi
    # padahal seharusnya Status Terakhir).
    if not results and filter_values:
        FALLBACK_FIELDS = [
            "Lokasi", "Status Terakhir", "Keterangan", "Nama Mesin", "Model"
        ]
        # Gabungkan semua filter value jadi satu list token
        for row in all_records:
            blob = " ".join(
                str(row.get(f, "")).lower() for f in FALLBACK_FIELDS
            )
            if all(v in blob for v in filter_values):
                results.append(row)
        if results:
            logger.info(
                "Safety net aktif: filter Gemini 0 hasil, fallback ke multi-field "
                "match menemukan %d hasil",
                len(results),
            )

    # Apply limit
    limit = parsed.get("limit") or 50
    return results[:limit]


def format_ai_response(parsed: dict, results: list[dict], total_data: int) -> str:
    """
    Format hasil filter jadi pesan ringkas berdasarkan tipe agregasi.
    Detail per-item nanti di-render terpisah oleh handler.
    """
    aggregation = parsed.get("aggregation", "none")
    explanation = parsed.get("explanation", "")
    count = len(results)

    if aggregation == "count":
        return (
            f"📊 *Hasil:*\n"
            f"_{explanation}_\n\n"
            f"Ditemukan *{count}* barang"
            f"{' (dari ' + str(total_data) + ' total)' if count != total_data else ''}."
        )

    if count == 0:
        return f"❌ Tidak ada barang yang cocok.\n_{explanation}_"

    return (
        f"🔍 _{explanation}_\n"
        f"Ditemukan *{count}* hasil:"
    )
