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
    {{
      "field": "<nama kolom EXACT>",
      "op": "contains|equals|starts_with|in",
      "value": "<string, untuk op selain 'in'>",
      "values": ["<string>", ...]   // hanya untuk op="in" (multi-value OR)
    }}
  ],
  "aggregation": "count|list|none",
  "free_text": "<keyword bebas, atau null>",
  "limit": <int, default 50>,
  "explanation": "<MAX 12 kata dalam Bahasa Indonesia: apa yang dicari>"
}}

Aturan:
- "field" HARUS PERSIS sama dengan salah satu nama kolom di atas (case-sensitive!)
- MULTI-VALUE / OR LOGIC: kalau user nyebut beberapa pilihan dengan kata "atau" / "or" / koma
  pada satu kolom, gunakan op="in" dengan array "values".
  Contoh: "di limus atau commpark" → {{"field": "Lokasi", "op": "in", "values": ["limus", "commpark"]}}
- MULTI-FILTER (AND): filters yang berbeda field di-AND-kan (mis. status rusak DAN lokasi limus)
- KEYWORD SPESIFIK (kode/model/serial seperti D320, Cypress2, ABC-123, HXDM251004):
  * SELALU pakai "free_text", JANGAN bikin filter struktur
  * Karena kita tidak tahu pasti keyword itu ada di kolom Nama Mesin / Model / Serial Number / dll
  * Biarkan sistem cari di semua kolom searchable
- Kalau user nyebut nama tempat/proyek/perusahaan → COCOKKAN dengan sample values:
  * Kalau nilainya muncul di "Lokasi" → filter Lokasi
  * Kalau muncul di "Status Terakhir" → filter Status Terakhir
- "berapa" / "ada berapa" / "total" / "jumlah" → aggregation = "count"
- "list" / "daftar" / "apa saja" / "semua" → aggregation = "list"
- Cari satu barang spesifik → aggregation = "none"
- "rusak" / "broken" → filter Status contains "rusak"
- Output HARUS valid JSON, TANPA markdown ```json``` atau teks lain
- explanation MAKSIMUM 12 kata supaya output hemat ruang

CONTOH PARSING (singkat):
- "berapa santak ups di limus atau commpark"
  → {{"filters":[{{"field":"Lokasi","op":"in","values":["limus","commpark"]}}],"free_text":"santak ups","aggregation":"count","limit":50,"explanation":"Total Santak UPS di Limus atau Commpark"}}
- "list barang rusak di gudang A"
  → {{"filters":[{{"field":"Status","op":"contains","value":"rusak"}},{{"field":"Lokasi","op":"contains","value":"gudang A"}}],"free_text":null,"aggregation":"list","limit":50,"explanation":"Daftar barang rusak di Gudang A"}}
- "mesin yang mengandung nama Cypress2"
  → {{"filters":[],"free_text":"Cypress2","aggregation":"list","limit":50,"explanation":"Mesin yang mengandung Cypress2"}}
- "barang dengan kode HXDM251004"
  → {{"filters":[],"free_text":"HXDM251004","aggregation":"none","limit":50,"explanation":"Barang dengan kode HXDM251004"}}

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
        column_samples = _extract_column_samples(all_records, headers)
        prompt = _build_parser_prompt(query, headers, all_records, column_samples)

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1500,  # Naik dari 600 - prompt baru lebih kompleks
                response_mime_type="application/json",
            ),
        )

        text = (response.text or "").strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

        # Coba parse normal dulu
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # JSON mungkin terpotong (truncated) — coba repair
            repaired = _repair_truncated_json(text)
            if repaired is not None:
                parsed = repaired
                logger.warning("JSON Gemini terpotong, berhasil di-repair")
            else:
                logger.warning(
                    "Gemini balas non-JSON dan tidak bisa di-repair: text=%r",
                    text[:300]
                )
                return None

        logger.info("AI parsed: %s", parsed.get("explanation", "(no explanation)"))
        logger.debug("AI filters: %s | free_text=%s",
                     parsed.get("filters"), parsed.get("free_text"))
        return parsed

    except Exception as e:
        # Quota / rate limit error → log ringkas saja
        err_str = str(e)
        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str or "quota" in err_str.lower():
            logger.warning(
                "Gemini quota habis / belum aktif. Fallback ke literal search. "
                "Cek: https://aistudio.google.com/app/apikey"
            )
        else:
            logger.exception("Gagal panggil Gemini: %s", e)
        return None


def _repair_truncated_json(text: str) -> Optional[dict]:
    """
    Coba repair JSON yang terpotong di tengah (karena hit max_output_tokens).

    Strategi:
    1. Buang trailing yang tidak lengkap (ke koma terakhir)
    2. Tutup bracket/brace yang masih terbuka
    3. Coba parse ulang

    Return dict kalau berhasil, None kalau tidak.
    """
    if not text:
        return None

    # Hitung bracket yang masih terbuka
    open_curly = text.count("{") - text.count("}")
    open_square = text.count("[") - text.count("]")

    if open_curly < 0 or open_square < 0:
        return None  # JSON malformed, bukan sekadar terpotong

    # Cari posisi aman untuk truncate: setelah karakter ", }, ], atau angka/true/false
    # Buang semuanya setelah koma terakhir untuk hindari ", terpotong"
    last_safe = max(
        text.rfind('",'),
        text.rfind('},'),
        text.rfind('],'),
        text.rfind('"\n'),
        text.rfind('}\n'),
        text.rfind(']\n'),
    )
    if last_safe > 0:
        text = text[:last_safe + 1]

    # Buang trailing comma
    text = text.rstrip(",\n \t")

    # Tutup bracket yang masih terbuka
    open_curly = text.count("{") - text.count("}")
    open_square = text.count("[") - text.count("]")
    text = text + ("]" * open_square) + ("}" * open_curly)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
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
    filter_values: list[str] = []  # untuk safety net (gabungan semua filter values)
    for f in parsed.get("filters") or []:
        field = f.get("field")
        op = f.get("op", "contains")
        if not field:
            continue

        # Multi-value (OR) untuk op="in"
        if op == "in":
            raw_values = f.get("values") or []
            values = [str(v).lower().strip() for v in raw_values if v]
            if not values:
                continue
            filter_values.extend(values)

            filtered = []
            for row in results:
                cell = str(row.get(field, "")).lower().strip()
                if not cell:
                    continue
                # Match kalau cell contains salah satu value (OR logic)
                if any(v in cell for v in values):
                    filtered.append(row)
            results = filtered
            continue

        # Single-value operators
        value = str(f.get("value", "")).lower().strip()
        if not value:
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
        from src.sheets import SEARCHABLE_FIELDS, _tokenize
        # Tokenize free_text agar "santak ups" match dengan "Santak UPS" tanpa peduli urutan
        ft_tokens = _tokenize(free_text)
        if ft_tokens:
            filtered = []
            for row in results:
                blob = " ".join(
                    str(row.get(f, "")).lower() for f in SEARCHABLE_FIELDS
                )
                if all(t in blob for t in ft_tokens):
                    filtered.append(row)
            results = filtered
        else:
            # Fallback: substring search lama
            filtered = []
            for row in results:
                for field in SEARCHABLE_FIELDS:
                    if free_text in str(row.get(field, "")).lower():
                        filtered.append(row)
                        break
            results = filtered

    # SAFETY NET: kalau hasil 0 tapi punya filter values, coba retry dengan
    # mencari value tersebut di SEMUA kolom searchable (bukan cuma kolom yang
    # ditarget Gemini). Berguna kalau Gemini salah pilih kolom — misal cari
    # "Cypress2" di kolom Nama Mesin padahal datanya di kolom Model.
    if not results and filter_values:
        from src.sheets import SEARCHABLE_FIELDS, _tokenize

        # Gabungkan semua filter values jadi tokens
        combined_tokens: list[str] = []
        for v in filter_values:
            combined_tokens.extend(_tokenize(v) or [v])

        if combined_tokens:
            for row in all_records:
                blob = " ".join(
                    str(row.get(f, "")).lower() for f in SEARCHABLE_FIELDS
                )
                # Match kalau salah satu token muncul (lebih permisif untuk safety net)
                if any(t in blob for t in combined_tokens):
                    # Kalau ada free_text juga, cek juga
                    if free_text:
                        ft_tokens = _tokenize(free_text)
                        if ft_tokens and not all(t in blob for t in ft_tokens):
                            continue
                    results.append(row)
            if results:
                logger.info(
                    "Safety net aktif: filter Gemini 0 hasil, fallback ke "
                    "multi-field search menemukan %d hasil",
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
        msg = (
            f"📊 *Hasil:*\n"
            f"_{explanation}_\n\n"
            f"Ditemukan total *{count}* unit"
            f"{' (dari ' + str(total_data) + ' total inventori)' if count != total_data else ''}."
        )

        # Tambah breakdown per Lokasi/Status (multi-value) atau per Model
        breakdown = _breakdown_by_filter(parsed, results)
        if breakdown:
            msg += "\n\n" + breakdown

        return msg

    if count == 0:
        return f"❌ Tidak ada barang yang cocok.\n_{explanation}_"

    return (
        f"🔍 _{explanation}_\n"
        f"Ditemukan *{count}* hasil:"
    )


def _breakdown_by_filter(parsed: dict, results: list[dict]) -> str:
    """
    Bikin rincian count untuk hasil:
    - Multi-lokasi (op=in dengan ≥2 values) → breakdown per lokasi
    - Single-lokasi tapi free_text ada → breakdown per Model
      (agar user bisa verify isi: misal "12 di Commpark" itu campuran model apa)
    - Selain itu (no filter) → tidak tampil

    Output contoh:
       Per Lokasi:
       • Limus: 3 unit
       • Commpark: 2 unit

    atau:

       Per Model:
       • C3K(2021): 8 unit
       • C2K: 3 unit
       • Lainnya: 1 unit
    """
    from collections import Counter

    filters = parsed.get("filters") or []
    # Cari filter lokasi/status terakhir
    target_field = None
    target_values: list[str] = []
    for f in filters:
        field = f.get("field", "")
        if field in ("Lokasi", "Status Terakhir"):
            target_field = field
            if f.get("op") == "in":
                target_values = [str(v).lower() for v in (f.get("values") or [])]
            else:
                target_values = [str(f.get("value", "")).lower()]
            break

    # === Case 1: multi-value pada Lokasi/Status Terakhir → breakdown per group ===
    if target_field and len(target_values) >= 2:
        counter: Counter = Counter()
        for row in results:
            cell = str(row.get(target_field, "")).lower()
            for v in target_values:
                if v in cell:
                    counter[v] += 1
                    break
        if counter:
            lines = [f"*Per {target_field}:*"]
            for v in target_values:
                label = v.title() if v.islower() else v
                n = counter.get(v, 0)
                lines.append(f"• {label}: *{n}* unit")
            return "\n".join(lines)

    # === Case 2: single lokasi/status terakhir + ada free_text → breakdown per Model ===
    # Berguna agar user bisa verify "12 unit di Commpark" itu mix model apa.
    free_text = (parsed.get("free_text") or "").strip()
    if free_text and len(results) >= 3:
        model_counter: Counter = Counter()
        for row in results:
            model = str(row.get("Model", "")).strip()
            if not model or model == "-":
                model = "(tanpa model)"
            model_counter[model] += 1

        # Hanya tampilkan kalau ada variasi (>1 model unik)
        if len(model_counter) > 1:
            lines = ["*Per Model:*"]
            # Top 5 model + sisanya
            top = model_counter.most_common(5)
            for model, n in top:
                lines.append(f"• {model}: *{n}* unit")
            others_count = sum(model_counter.values()) - sum(n for _, n in top)
            if others_count > 0:
                lines.append(f"• Lainnya: *{others_count}* unit")
            return "\n".join(lines)

    return ""
