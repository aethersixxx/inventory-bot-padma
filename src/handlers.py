"""
Handler untuk Telegram bot (python-telegram-bot v21).
"""
from __future__ import annotations

import time
from cachetools import TTLCache

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src import config
from src.ai_search import (
    execute_filter,
    format_ai_response,
    is_natural_language_query,
    parse_query,
)
from src.logger import logger
from src.sheets import format_item, sheets_client

# ---------- session storage ----------
# Simpan hasil terakhir per user selama 10 menit, agar bisa kirim "tampilkan semua"
# tanpa search ulang. Key: user_id, Value: (query, results, last_shown_index)
_last_results: TTLCache = TTLCache(maxsize=500, ttl=600)

# Default page size & detail mode threshold
_PAGE_SIZE = 10               # berapa item per "page"
_COMPACT_THRESHOLD = 0        # > N hasil → pakai compact mode.
                              # Set 0 = compact mode SELALU dipakai (konsisten),
                              # naikkan jika mau detail penuh untuk hasil sedikit.

# Keyword yang dikenali sebagai "tampilkan semua" / "lanjut"
_SHOW_MORE_KEYWORDS = {
    "tampilkan semua", "tampilkan semuanya", "semua", "all", "show all",
    "lanjut", "lanjutkan", "next", "more", "selanjutnya", "berikutnya",
    "lainnya", "yang lain",
}

# ---------- helper ----------
def _user_info(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "unknown"
    return f"{u.id} ({u.username or u.first_name or 'no-name'})"


def _check_access(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    return config.is_allowed(u.id)


# ---------- commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("START dari %s", _user_info(update))

    role = "Admin" if user and config.is_admin(user.id) else "User"
    ai_status = "🤖 AI search: AKTIF" if config.AI_ENABLED else "🤖 AI search: nonaktif"
    text = (
        f"👋 Halo *{user.first_name if user else ''}*!\n\n"
        f"Selamat datang di *Inventory Bot*. Role kamu: *{role}*\n"
        f"{ai_status}\n\n"
        "📖 *Cara pakai:*\n"
        "• Ketik kode/nama spesifik → cari literal\n"
        "  `D320` · `compressor` · `BAUT`\n"
        "• Ketik pertanyaan → AI akan parse otomatis\n"
        "  `barang apa saja yang ada D320?`\n"
        "  `ada berapa mesin yang rusak?`\n"
        "  `list semua di gudang A`\n\n"
        "Pencarian tidak case-sensitive & support partial match.\n"
        "Ketik /help untuk command lengkap."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("HELP dari %s", _user_info(update))
    user = update.effective_user
    is_admin = user and config.is_admin(user.id)

    base = (
        "📋 *Daftar Command*\n\n"
        "👤 *User:*\n"
        "• /start - Panduan penggunaan\n"
        "• /help - Daftar command\n"
        "• /search <nama> - Cari barang (literal)\n"
        "• /all - Tampilkan semua hasil pencarian terakhir\n"
        "• Ketik nama langsung → cari literal\n"
        "• Ketik pertanyaan → AI mode\n\n"
        "💬 *Contoh pertanyaan AI:*\n"
        "• _barang apa yang ada D320?_\n"
        "• _berapa total mesin yang rusak?_\n"
        "• _list semua di gudang A_\n"
        "• _list barang di indolakto cicurug 3_\n\n"
        "📄 *Hasil banyak?*\n"
        "Ketik `lanjut` atau /all untuk lihat sisanya.\n"
    )
    admin_extra = (
        "\n🔧 *Admin:*\n"
        "• /update <nama barang> <jumlah> - Update stok/status\n"
        "• /refresh - Force refresh cache\n"
        "• /whoami - Cek role kamu\n"
    )
    text = base + (admin_extra if is_admin else "\n💡 Ketik /whoami untuk cek role.")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    role = "Admin ✅" if config.is_admin(user.id) else "User 👤"
    await update.message.reply_text(
        f"User ID: `{user.id}`\nUsername: @{user.username or '-'}\nRole: *{role}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        await update.message.reply_text("⛔ Kamu tidak diizinkan akses bot ini.")
        return

    if not context.args:
        await update.message.reply_text(
            "Format: `/search <nama barang>`", parse_mode=ParseMode.MARKDOWN
        )
        return

    query = " ".join(context.args)
    await _do_search(update, query)


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan halaman berikutnya dari hasil pencarian terakhir."""
    if not _check_access(update):
        await update.message.reply_text("⛔ Kamu tidak diizinkan akses bot ini.")
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in _last_results:
        await update.message.reply_text(
            "_Belum ada pencarian sebelumnya. Cari dulu, lalu ketik /all "
            "untuk lihat semua hasilnya._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await _send_more_results(update, user_id)


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not config.is_admin(user.id):
        await update.message.reply_text(
            "⛔ Command ini hanya untuk *Admin*.", parse_mode=ParseMode.MARKDOWN
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Format: `/update <nama barang> <jumlah>`\n"
            "Contoh: `/update compressor 15`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # jumlah = argumen terakhir, sisanya = nama barang
    quantity = context.args[-1]
    item_name = " ".join(context.args[:-1])

    logger.info("UPDATE request: '%s' → %s oleh %s", item_name, quantity, _user_info(update))

    try:
        ok = sheets_client.update_quantity(item_name, quantity)
    except Exception as e:
        logger.exception("Gagal update")
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    if ok:
        await update.message.reply_text(
            f"✅ Berhasil update *{item_name}* → `{quantity}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ Barang *{item_name}* tidak ditemukan.", parse_mode=ParseMode.MARKDOWN
        )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not config.is_admin(user.id):
        await update.message.reply_text("⛔ Hanya Admin.")
        return
    sheets_client.invalidate_cache()
    await update.message.reply_text("🔄 Cache di-refresh.")


# ---------- text handler (free text search) ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        await update.message.reply_text("⛔ Kamu tidak diizinkan akses bot ini.")
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await _do_search(update, text)


async def _do_search(update: Update, query: str) -> None:
    """
    Logic pencarian + response.

    Routing:
    - Query "tampilkan semua" / "lanjut" → kirim sisa hasil dari search sebelumnya
    - Query natural (kalimat panjang) → AI parser
    - Query pendek/literal → literal search
    """
    user_id = update.effective_user.id if update.effective_user else 0

    # 0. Cek apakah ini permintaan continuation ("tampilkan semua", dll)
    if _is_show_more_query(query) and user_id in _last_results:
        await _send_more_results(update, user_id)
        return

    start = time.perf_counter()
    logger.info("SEARCH '%s' dari %s", query, _user_info(update))

    use_ai = config.AI_ENABLED and is_natural_language_query(query)
    aggregation = ""  # Set saat AI search berhasil

    try:
        if use_ai:
            results, header_msg, aggregation = await _ai_search(query)
        else:
            results, header_msg = _literal_search(query)
    except Exception as e:
        logger.exception("Error saat search")
        await update.message.reply_text(
            f"⚠️ Error: `{e}`", parse_mode=ParseMode.MARKDOWN
        )
        return

    elapsed = (time.perf_counter() - start) * 1000

    # Tidak ada hasil
    if not results:
        # Bersihkan session lama agar "tampilkan semua" berikutnya tidak ngacau
        _last_results.pop(user_id, None)
        suggestions = sheets_client.fuzzy_suggest(query, limit=5)
        msg = "❌ *Barang tidak ditemukan di inventory.*"
        if suggestions:
            msg += "\n\n💡 *Mungkin maksud kamu:*\n"
            msg += "\n".join(f"• `{s}`" for s in suggestions)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        logger.info("SEARCH '%s' → 0 hasil (%.0fms, ai=%s)", query, elapsed, use_ai)
        return

    # ========== Special path: aggregation=count ==========
    # Gabung header (jumlah + breakdown) + compact list jadi 1 pesan saja.
    # User minta jawaban ringkas dalam 1 bubble untuk pertanyaan "berapa".
    if aggregation == "count":
        await _send_count_response(update, user_id, query, results, header_msg)
        logger.info(
            "SEARCH '%s' → %d unit (count, %.0fms, ai=%s)",
            query, len(results), elapsed, use_ai,
        )
        return

    # ========== Default path: list / none — interactive UI ==========
    # Simpan ke session dengan struktur baru untuk support tombol interaktif
    _last_results[user_id] = {
        "query": query,
        "results": results,
        "header_msg": header_msg,
        "page": 1,
        # filter & filtered_results di-set saat user tap tombol filter
    }

    # Render dengan inline keyboard
    await _render_search_result(update, _last_results[user_id], user_id, edit=False)

    logger.info("SEARCH '%s' → %d hasil (%.0fms, ai=%s)", query, len(results), elapsed, use_ai)


def _is_show_more_query(query: str) -> bool:
    """Cek apakah query adalah perintah lanjutkan / tampilkan semua."""
    q = query.strip().lower().rstrip(" !.?,").strip()
    return q in _SHOW_MORE_KEYWORDS


async def _send_count_response(
    update: Update,
    user_id: int,
    query: str,
    results: list[dict],
    header_msg: str,
) -> None:
    """
    Kirim response untuk aggregation=count:
    - Header (total + breakdown per lokasi)
    - Compact list semua item DI BUBBLE YANG SAMA (kalau muat)
    - Kalau lebih dari ~3500 char, split: bubble 1 = header + batch awal,
      sisanya pakai mekanisme paging.
    """
    # Telegram message hard limit ~4096 char; pakai 3500 untuk margin aman
    MAX_CHARS = 3500

    # Coba muat semua dalam 1 bubble dulu
    full_compact = _format_compact(results, start_index=1)
    combined = f"{header_msg}\n\n*Daftar barang:*\n{full_compact}"

    if len(combined) <= MAX_CHARS:
        # Fits in 1 bubble
        await update.message.reply_text(combined, parse_mode=ParseMode.MARKDOWN)
        # Tidak perlu session karena sudah ditampilkan semua
        _last_results.pop(user_id, None)
        return

    # Tidak muat — bagi: bubble 1 = header + sebanyak mungkin item,
    # sisanya simpan ke session untuk continuation via "lanjut"
    fitted_count = 0
    accumulated = f"{header_msg}\n\n*Daftar barang:*\n"
    for i, row in enumerate(results, start=1):
        line = _format_compact([row], start_index=i)
        # +2 untuk newline pemisah
        if len(accumulated) + len(line) + 2 > MAX_CHARS:
            break
        accumulated += line + "\n\n"
        fitted_count += 1

    accumulated = accumulated.rstrip()

    if fitted_count < len(results):
        accumulated += (
            f"\n\n_Menampilkan {fitted_count}/{len(results)}. "
            f"Ketik *lanjut* untuk lihat sisanya._"
        )
        # Simpan session dengan offset = jumlah yang sudah di-render
        _last_results[user_id] = {
            "query": query,
            "results": results,
            "shown": fitted_count,
        }
    else:
        _last_results.pop(user_id, None)

    await update.message.reply_text(accumulated, parse_mode=ParseMode.MARKDOWN)


async def _send_results_page(update: Update, user_id: int) -> None:
    """
    Kirim halaman berikutnya dari hasil yang tersimpan.
    Mode dipilih otomatis:
    - Hasil ≤ _COMPACT_THRESHOLD → kirim detail penuh per item
    - Hasil > _COMPACT_THRESHOLD → kirim compact (banyak item per pesan)
    """
    session = _last_results.get(user_id)
    if not session:
        return

    results = session["results"]
    shown = session["shown"]
    total = len(results)

    if shown >= total:
        await update.message.reply_text("✅ Semua hasil sudah ditampilkan.")
        _last_results.pop(user_id, None)
        return

    # Tentukan mode tampilan berdasarkan total hasil
    use_compact = total > _COMPACT_THRESHOLD

    page = results[shown : shown + _PAGE_SIZE]
    new_shown = shown + len(page)
    session["shown"] = new_shown
    remaining = total - new_shown

    if use_compact:
        # Compact: gabung jadi 1 pesan ringkas
        await update.message.reply_text(
            _format_compact(page, start_index=shown + 1),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Detail penuh per item
        for row in page:
            await update.message.reply_text(
                format_item(row), parse_mode=ParseMode.MARKDOWN
            )

    # Footer: ada sisa atau habis
    if remaining > 0:
        await update.message.reply_text(
            f"_Menampilkan {new_shown}/{total}. "
            f"Ketik *lanjut* untuk lihat {min(remaining, _PAGE_SIZE)} hasil "
            f"berikutnya._",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Habis, hapus session
        _last_results.pop(user_id, None)
        if total > _PAGE_SIZE:
            await update.message.reply_text(
                f"✅ Selesai — total {total} hasil ditampilkan."
            )


async def _send_more_results(update: Update, user_id: int) -> None:
    """Handler untuk command 'tampilkan semua' / 'lanjut'."""
    logger.info("CONTINUE dari %s", _user_info(update))
    await _send_results_page(update, user_id)


def _status_emoji(status: str) -> str:
    """Map status text → emoji status indicator."""
    s = status.lower().strip()
    if not s or s == "-":
        return "⚪"
    if "aktif" in s or "available" in s or "tersedia" in s:
        return "🟢"
    if "rusak" in s or "broken" in s or "error" in s:
        return "🔴"
    if "trial" in s or "pinjam" in s or "rental" in s:
        return "🟡"
    if "stok" in s or "stock" in s:
        return "🟢"
    return "⚪"


def _highlight_keyword(text: str, keyword: str) -> str:
    """
    Bold-kan keyword di text (case-insensitive).
    Pakai markdown bold *...*. Skip kalau keyword < 2 char (terlalu generic).
    """
    if not keyword or len(keyword) < 2 or not text:
        return text
    # Find case-insensitive
    text_lower = text.lower()
    kw_lower = keyword.lower()
    if kw_lower not in text_lower:
        return text
    # Replace dengan preserve case asli
    result = ""
    i = 0
    while i < len(text):
        if text_lower[i:i+len(kw_lower)] == kw_lower:
            result += f"*{text[i:i+len(kw_lower)]}*"
            i += len(kw_lower)
        else:
            result += text[i]
            i += 1
    return result


def _format_compact(
    rows: list[dict], start_index: int = 1, highlight: str = ""
) -> str:
    """
    Format compact dengan:
    - Emoji status (🟢 Aktif, 🔴 Rusak, 🟡 Trial, ⚪ Lainnya)
    - Separator divider antar item
    - Highlight keyword pencarian (bold)
    - Layout rapi
    """
    if not rows:
        return ""

    lines = []
    for i, row in enumerate(rows, start=start_index):
        nama = str(row.get("Nama Mesin", "")).strip() or "-"
        model = str(row.get("Model", "")).strip()
        tipe = str(row.get("Tipe Mesin", "")).strip()
        lokasi = str(row.get("Lokasi", "")).strip() or "-"
        pn = str(row.get("Part Number", "")).strip()
        sn = str(row.get("Serial Number", "")).strip()
        status = str(row.get("Status", "")).strip()
        status_terakhir = str(row.get("Status Terakhir", "")).strip()

        emoji = _status_emoji(status)

        # Apply highlight ke field yang sering match keyword
        nama_h = _highlight_keyword(_md(nama), highlight) if highlight else _md(nama)
        model_h = _highlight_keyword(_md(model), highlight) if highlight else _md(model)
        pn_h = _highlight_keyword(_md(pn), highlight) if highlight else _md(pn)

        # Baris 1: emoji + nomor + nama (highlighted)
        header_parts = [f"{emoji} *{i}.* {nama_h}"]
        if model and model != "-":
            header_parts.append(model_h)
        if tipe and tipe != "-":
            header_parts.append(_md(tipe))
        line = " · ".join(header_parts)

        # Baris 2: lokasi · status
        meta_parts = [f"📍 {_md(lokasi)}"]
        if status and status != "-":
            meta_parts.append(_md(status))
        line += "\n   " + " · ".join(meta_parts)

        # Baris 3+: PN, SN
        if pn and pn != "-":
            line += f"\n   🏷️ `{pn_h if highlight else _md(pn)}`"
        if sn and sn != "-":
            line += f"\n   🔢 `{_md(sn)}`"

        # Baris terakhir: Status Terakhir / Keterangan
        if status_terakhir and status_terakhir != "-":
            line += f"\n   💬 _{_md(status_terakhir)}_"

        lines.append(line)

    # Pisah antar item dengan divider tipis
    return "\n\n".join(lines)


def _format_detail(row: dict) -> str:
    """
    Format detail penuh untuk satu item. Dipakai saat user tap tombol "Detail".
    Menampilkan semua 10 field rapi.
    """
    nama = str(row.get("Nama Mesin", "")).strip() or "-"
    status = str(row.get("Status", "")).strip()
    emoji = _status_emoji(status)

    fields = [
        ("Nama Mesin", "Nama Mesin"),
        ("Model", "Model"),
        ("Merk", "Merk"),
        ("Tipe Mesin", "Tipe Mesin"),
        ("Part Number", "Part Number"),
        ("Serial Number", "Serial Number"),
        ("Lokasi", "Lokasi"),
        ("Status", "Status"),
        ("Keterangan", "Keterangan"),
        ("Status Terakhir", "Status Terakhir"),
    ]

    lines = [f"{emoji} *Detail Barang*", "━━━━━━━━━━━━━━━━━━"]
    for label, key in fields:
        value = str(row.get(key, "")).strip()
        if not value:
            value = "-"
        # PN & SN pakai code formatting
        if key in ("Part Number", "Serial Number") and value != "-":
            lines.append(f"*{label}:* `{_md(value)}`")
        else:
            lines.append(f"*{label}:* {_md(value)}")

    return "\n".join(lines)


def _build_search_keyboard(
    user_id: int, page: int, total_pages: int,
    items_on_page: int, has_filter: bool = False,
) -> InlineKeyboardMarkup | None:
    """
    Build inline keyboard untuk hasil pencarian.

    Layout:
      Row 1: tombol detail per item ([1] [2] [3] [4] [5]) -- max 5
      Row 2: ⬅️ Sebelumnya | ➡️ Lanjut
      Row 3: 🟢 Aktif | 🔴 Rusak  (atau ✖️ Hapus Filter kalau sudah filter)
      Row 4: 🔄 Refresh | ❌ Tutup
    """
    rows: list[list[InlineKeyboardButton]] = []

    # Row 1: Tombol detail per item (max 5 di halaman ini)
    if items_on_page > 0:
        start_idx = (page - 1) * _PAGE_SIZE
        max_detail = min(items_on_page, 5)
        detail_buttons = []
        for i in range(max_detail):
            global_idx = start_idx + i
            local_num = i + 1 + start_idx
            detail_buttons.append(
                InlineKeyboardButton(
                    f"📄 #{local_num}",
                    callback_data=f"detail:{user_id}:{global_idx}",
                )
            )
        rows.append(detail_buttons)

    # Row 2: Pagination
    pagination_row = []
    if page > 1:
        pagination_row.append(
            InlineKeyboardButton(
                "⬅️ Sebelumnya",
                callback_data=f"page:{user_id}:{page - 1}",
            )
        )
    if page < total_pages:
        pagination_row.append(
            InlineKeyboardButton(
                "➡️ Lanjut",
                callback_data=f"page:{user_id}:{page + 1}",
            )
        )
    if pagination_row:
        rows.append(pagination_row)

    # Row 3: Filter cepat
    if not has_filter:
        rows.append([
            InlineKeyboardButton("🟢 Aktif", callback_data=f"filter:{user_id}:aktif"),
            InlineKeyboardButton("🔴 Rusak", callback_data=f"filter:{user_id}:rusak"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "✖️ Hapus Filter",
                callback_data=f"filter:{user_id}:reset",
            ),
        ])

    # Row 4: Action buttons
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{user_id}:0"),
        InlineKeyboardButton("❌ Tutup", callback_data=f"close:{user_id}:0"),
    ])

    return InlineKeyboardMarkup(rows) if rows else None


def _build_detail_keyboard(user_id: int, item_index: int) -> InlineKeyboardMarkup:
    """Keyboard untuk view detail satu item — tombol kembali ke list."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Kembali ke Hasil",
                callback_data=f"back:{user_id}:0",
            )
        ]
    ])


# ---------- Callback handler (handler tap tombol inline keyboard) ----------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler untuk tap tombol inline keyboard.
    Format callback_data: "action:user_id:param"
    """
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()  # Hilangkan loading spinner

    try:
        action, owner_id_str, param = query.data.split(":", 2)
        owner_id = int(owner_id_str)
    except (ValueError, IndexError):
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if user_id != owner_id:
        await query.answer(
            "⛔ Tombol ini bukan untuk kamu. Search sendiri ya.",
            show_alert=True,
        )
        return

    session = _last_results.get(user_id)
    if not session:
        await query.answer(
            "⏰ Hasil pencarian sudah expired. Silakan search ulang.",
            show_alert=True,
        )
        try:
            await query.edit_message_text(
                "⏰ _Hasil pencarian sudah expired. Silakan search ulang._",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        return

    logger.info("CALLBACK %s param=%s dari %s", action, param, _user_info(update))

    if action == "page":
        try:
            new_page = int(param)
        except ValueError:
            return
        session["page"] = new_page
        await _render_search_result(query, session, user_id, edit=True)

    elif action == "detail":
        try:
            idx = int(param)
        except ValueError:
            return
        results = session.get("filtered_results") or session["results"]
        if 0 <= idx < len(results):
            row = results[idx]
            text = _format_detail(row)
            keyboard = _build_detail_keyboard(user_id, idx)
            try:
                await query.edit_message_text(
                    text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
                )
            except Exception as e:
                logger.warning("edit_message gagal: %s", e)

    elif action == "back":
        await _render_search_result(query, session, user_id, edit=True)

    elif action == "filter":
        if param == "reset":
            session.pop("filter", None)
            session.pop("filtered_results", None)
        else:
            session["filter"] = param
            base_results = session["results"]
            session["filtered_results"] = [
                r for r in base_results
                if param.lower() in str(r.get("Status", "")).lower()
            ]
        session["page"] = 1
        await _render_search_result(query, session, user_id, edit=True)

    elif action == "refresh":
        sheets_client.invalidate_cache()
        original_query = session.get("query", "")
        session.pop("filter", None)
        session.pop("filtered_results", None)
        try:
            await query.edit_message_text(
                "🔄 _Refreshing data..._", parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
        # Re-execute search
        if config.AI_ENABLED and is_natural_language_query(original_query):
            results, header_msg, _ = await _ai_search(original_query)
        else:
            results, header_msg = _literal_search(original_query)
        session["results"] = results
        session["header_msg"] = header_msg
        session["page"] = 1
        await _render_search_result(query, session, user_id, edit=True)

    elif action == "close":
        try:
            await query.edit_message_text(
                "✅ _Pencarian ditutup._", parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
        _last_results.pop(user_id, None)


async def _render_search_result(
    callback_or_update,
    session: dict,
    user_id: int,
    edit: bool = False,
) -> None:
    """
    Render hasil pencarian dengan inline keyboard.
    edit=False → reply baru. edit=True → edit pesan existing (untuk callback).
    """
    results = session.get("filtered_results") or session["results"]
    page = session.get("page", 1)
    has_filter = bool(session.get("filter"))
    filter_label = session.get("filter", "")
    query_text = session.get("query", "")

    total = len(results)
    if total == 0:
        text = "❌ *Tidak ada hasil yang cocok dengan filter ini.*"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✖️ Hapus Filter",
                callback_data=f"filter:{user_id}:reset",
            )]
        ])
        if edit:
            try:
                await callback_or_update.edit_message_text(
                    text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
                )
            except Exception:
                pass
        else:
            await callback_or_update.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
        return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    session["page"] = page

    start_idx = (page - 1) * _PAGE_SIZE
    end_idx = min(start_idx + _PAGE_SIZE, total)
    page_items = results[start_idx:end_idx]

    # Header dengan separator
    header = "━━━━━━━━━━━━━━━━━━━━\n"
    header += f"🔍 *Hasil pencarian:* `{_md(query_text)}`\n"
    if has_filter:
        header += f"🏷️ *Filter:* {filter_label.title()}\n"
    header += f"📊 *{total}* item ditemukan"
    if total_pages > 1:
        header += f"  ·  Halaman *{page}/{total_pages}*"
    header += "\n━━━━━━━━━━━━━━━━━━━━\n\n"

    # Body
    body = _format_compact(page_items, start_index=start_idx + 1, highlight=query_text)
    text = header + body

    # Truncate kalau lebih dari Telegram limit
    if len(text) > 4000:
        text = text[:3990] + "\n\n_...terpotong_"

    keyboard = _build_search_keyboard(
        user_id=user_id,
        page=page,
        total_pages=total_pages,
        items_on_page=len(page_items),
        has_filter=has_filter,
    )

    if edit:
        try:
            await callback_or_update.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
        except Exception as e:
            logger.debug("edit_message gagal: %s", e)
    else:
        await callback_or_update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )


def _md(s: str) -> str:
    """Escape karakter markdown yang sering bikin parse error."""
    return s.replace("*", "").replace("_", "\\_").replace("[", "(").replace("]", ")")


def _literal_search(query: str) -> tuple[list[dict], str]:
    """Literal search: case-insensitive + partial match."""
    results = sheets_client.search(query)
    if len(results) <= 1:
        return results, ""
    return results, f"🔍 Ditemukan *{len(results)}* hasil untuk *{query}*"


async def _ai_search(query: str) -> tuple[list[dict], str, str]:
    """
    AI-powered search via Gemini parser.
    Fallback ke literal search jika Gemini gagal.

    Return: (results, header_msg, aggregation)
      - results: list dict hasil filter
      - header_msg: pesan header (count + breakdown / "Ditemukan N hasil")
      - aggregation: "count" | "list" | "none" | "" (kalau fallback)
    """
    all_records = sheets_client.get_all_records()
    if not all_records:
        return [], "", ""

    headers = list(all_records[0].keys())
    parsed = parse_query(query, headers, all_records)

    # Fallback kalau Gemini gagal
    if not parsed:
        logger.info("AI parse gagal, fallback ke literal search")
        results, header_msg = _literal_search(query)
        return results, header_msg, ""

    results = execute_filter(parsed, all_records)
    header_msg = format_ai_response(parsed, results, len(all_records))
    aggregation = parsed.get("aggregation", "none")
    return results, header_msg, aggregation


# ---------- error handler ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception: %s", context.error, exc_info=context.error)
