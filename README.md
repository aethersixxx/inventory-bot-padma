# 📦 Inventory Telegram Bot

Bot Telegram untuk monitoring inventory yang terhubung dengan Google Sheets sebagai database. Mendukung pencarian fuzzy, role-based access (Admin/User), in-memory cache, dan auto-suggest saat barang tidak ditemukan.

---

## ✨ Fitur

- 🔍 Pencarian case-insensitive + partial match
- 🤖 **AI Natural Language Search** (powered by Gemini) — tanya pakai bahasa natural
- 💡 Auto-suggest fuzzy matching jika barang tidak ketemu
- ⚡ In-memory cache 30 detik (bisa diatur) → response < 2 detik
- 👥 Multi-user dengan role: **Admin** & **User**
- 🔧 Admin bisa update stok via `/update <nama> <jumlah>`
- 📝 Logging request ke file dengan rotation (5MB × 3 backup)
- 🐳 Siap deploy ke Railway / Render / Fly.io / VPS

---

## 🤖 AI Natural Language Search

Bot bisa otomatis paham pertanyaan dalam bahasa natural. Contoh:

| Pertanyaan kamu | Yang dilakukan bot |
|---|---|
| `D320` | Literal search (cepat, tanpa AI) |
| `barang apa saja yang ada D320?` | AI parse → cari "D320" di semua kolom |
| `ada berapa mesin yang rusak?` | AI parse → filter Status="rusak" → tampilkan jumlah |
| `list semua di gudang A` | AI parse → filter Lokasi contains "gudang A" |
| `compressor yang available` | AI parse → free_text="compressor" + Status="available" |

**Cara kerja:**
- Bot deteksi otomatis: query pendek (≤2 kata, tanpa keyword tanya) → literal search; query panjang / pakai kata "apa", "berapa", "yang", dll → AI mode
- Gemini hanya **parse intent** jadi struktur filter (JSON), Python yang eksekusi filter di data lokal yang sudah di-cache → hemat token + cepat
- Kalau Gemini gagal/quota habis → fallback otomatis ke literal search

**Setup Gemini API key (gratis):**
1. Buka https://aistudio.google.com/app/apikey
2. Login pakai akun Google → klik **Create API key**
3. Copy key, taruh di `.env` sebagai `GEMINI_API_KEY=...`
4. Restart bot

Free tier Gemini: 15 request/menit, 1500 request/hari (per April 2026, cek dokumentasi terbaru). Cukup untuk bot internal team kecil-menengah.

Kalau `GEMINI_API_KEY` kosong di `.env`, fitur AI otomatis nonaktif dan bot tetap jalan dengan literal search saja.

---

## 📁 Struktur Folder

```
telegram-inventory-bot/
├── main.py                  # Entry point
├── requirements.txt         # Python dependencies
├── Dockerfile               # Untuk deployment containerized
├── Procfile                 # Untuk Railway/Render
├── .env.example             # Template environment vars
├── .gitignore
├── README.md
├── src/
│   ├── __init__.py
│   ├── config.py            # Load & validasi env vars, role helper
│   ├── logger.py            # Setup rotating file logger
│   ├── sheets.py            # Google Sheets client + cache + search
│   └── handlers.py          # Telegram command & message handlers
├── tests/
│   └── test_local.py        # Quick test koneksi + search lokal
├── credentials/
│   └── service-account.json # (kamu yang taruh, JANGAN di-commit)
└── logs/
    └── bot.log              # Auto-generated
```

---

## 🛠️ Setup dari Nol

### 1. Buat Telegram Bot

1. Buka Telegram, chat ke [@BotFather](https://t.me/BotFather)
2. Kirim `/newbot`, ikuti petunjuknya
3. Simpan token yang diberikan (format: `123456789:ABCdef...`)
4. Untuk dapat `User ID` Telegram kamu, chat ke [@userinfobot](https://t.me/userinfobot)

### 2. Setup Google Cloud (Service Account)

1. Buka [Google Cloud Console](https://console.cloud.google.com/)
2. Buat project baru (atau pilih existing)
3. Aktifkan **Google Sheets API**:
   - Menu: **APIs & Services → Library**
   - Cari "Google Sheets API" → klik **Enable**
   - Lakukan hal yang sama untuk "Google Drive API"
4. Buat **Service Account**:
   - Menu: **IAM & Admin → Service Accounts → Create Service Account**
   - Nama bebas, misal `inventory-bot`
   - Role: **Viewer** (cukup; nanti permission sheet di-grant manual)
   - Klik **Done**
5. Buat key JSON:
   - Klik service account yang baru dibuat
   - Tab **Keys → Add Key → Create New Key → JSON → Create**
   - File JSON otomatis ter-download
   - Rename jadi `service-account.json`, taruh di folder `credentials/`
6. Share Google Sheet ke service account:
   - Buka file JSON, copy nilai field `client_email` (contoh: `inventory-bot@xxx.iam.gserviceaccount.com`)
   - Buka [Google Sheet kamu](https://docs.google.com/spreadsheets/d/1yzuj-YffTuFPhFPPucTN6D9EDv1Qfg05OxFuRg4fr18/edit)
   - Klik **Share**, paste email service account, beri akses **Editor** (kalau mau update) atau **Viewer** (read-only)

### 3. Install Lokal

```bash
# Clone / extract proyek
cd telegram-inventory-bot

# Buat virtual env
python3 -m venv venv
source venv/bin/activate          # Linux/Mac
# venv\Scripts\activate           # Windows

# Install deps
pip install -r requirements.txt

# Copy env template
cp .env.example .env
# Edit .env, isi: TELEGRAM_BOT_TOKEN, ADMIN_USER_IDS, dll
```

### 4. Test Koneksi (Tanpa Telegram)

```bash
python -m tests.test_local
```

Output yang diharapkan:
```
✅ Config OK
✅ Loaded N baris dari Google Sheets
🔍 Search: 'compressor' → ...
```

### 5. Jalankan Bot

```bash
python main.py
```

Bot akan polling Telegram. Buka chat dengan bot, kirim `/start`.

---

## 🧪 Cara Testing

### Manual (di Telegram)

Sebagai **User biasa**:
- Kirim: `compressor` → harusnya muncul detail barang
- Kirim: `BAUT` → match case-insensitive
- Kirim: `comp` → match partial
- Kirim: `barang_random_xyz` → response "Barang tidak ditemukan" + suggest
- Kirim: `/start`, `/help`

Sebagai **Admin** (User ID kamu di `ADMIN_USER_IDS`):
- `/update compressor 25` → update kolom Status/Stok
- `/refresh` → invalidate cache
- `/whoami` → cek role

### Otomatis (Lokal)

```bash
python -m tests.test_local
```

---

## 🚀 Deployment

### Opsi A: Railway (paling mudah, free tier)

1. Push proyek ke GitHub (jangan ikutkan `credentials/` & `.env`)
2. Buka [railway.app](https://railway.app), login pakai GitHub
3. **New Project → Deploy from GitHub repo**
4. Set environment variables di tab **Variables**:
   - `TELEGRAM_BOT_TOKEN`
   - `GOOGLE_SHEET_ID`
   - `ADMIN_USER_IDS`
   - dst (sesuai `.env.example`)
5. Untuk credentials JSON, ada 2 cara:
   - **Cara 1:** Upload via Railway Volumes ke `/app/credentials/service-account.json`
   - **Cara 2 (recommended):** Encode JSON jadi base64, set env var `GOOGLE_CREDENTIALS_JSON`, lalu modif `config.py` untuk decode (lihat catatan di bawah)
6. Railway auto-detect `Procfile` dan run `worker: python main.py`

### Opsi B: Render

Mirip Railway:
1. **New → Background Worker** (bukan Web Service, karena bot polling)
2. Connect repo, set env vars, deploy

### Opsi C: VPS (DigitalOcean / AWS EC2 Free Tier)

```bash
# SSH ke VPS
sudo apt update && sudo apt install -y python3 python3-venv git

git clone <repo-kamu> && cd telegram-inventory-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Upload service-account.json via scp ke folder credentials/
# scp -i key.pem service-account.json user@vps-ip:~/telegram-inventory-bot/credentials/

cp .env.example .env && nano .env   # isi confignya

# Run pakai systemd (recommended) atau tmux/screen
# Contoh systemd:
sudo nano /etc/systemd/system/inventory-bot.service
```

Isi `inventory-bot.service`:
```ini
[Unit]
Description=Inventory Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/telegram-inventory-bot
ExecStart=/home/ubuntu/telegram-inventory-bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable inventory-bot
sudo systemctl start inventory-bot
sudo systemctl status inventory-bot     # cek status
journalctl -u inventory-bot -f          # cek log realtime
```

### Opsi D: Docker

```bash
# Build
docker build -t inventory-bot .

# Run (mount credentials dari host)
docker run -d \
  --name inventory-bot \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/credentials:/app/credentials \
  -v $(pwd)/logs:/app/logs \
  inventory-bot
```

---

## 📈 Cara Scaling untuk Data Besar

Cache 30 detik sudah handle traffic kecil-menengah. Untuk data > 10.000 baris atau traffic tinggi:

1. **Tambah TTL cache** (misal 5 menit) jika data jarang berubah → set `CACHE_TTL=300`.
2. **Pakai Redis** sebagai cache eksternal supaya bisa share antar instance:
   - Ganti `cachetools.TTLCache` di `src/sheets.py` dengan Redis client.
3. **Indexing in-memory:** saat load data, build dict `{nama_lower: row}` untuk O(1) lookup eksak. Untuk partial match, build inverted index per token.
4. **Migrasi ke database asli** (PostgreSQL/SQLite) jika data > 50K baris:
   - Sync Google Sheets → DB tiap 5 menit (background job / cron).
   - Sheet jadi "source of truth" untuk admin, DB jadi cache untuk read.
5. **Pagination & rate limiting** Google Sheets API:
   - Pakai `batch_get` untuk fetch beberapa range sekaligus.
   - Default quota: 60 read req/menit per user → cache wajib.
6. **Webhook mode** menggantikan polling untuk latency lebih rendah (perlu domain + HTTPS).

---

## 🔐 Keamanan

- ❌ **Jangan pernah commit** `credentials/service-account.json` atau `.env` ke git.
- ✅ Set `ALLOWED_USER_IDS` di production agar bot tidak terbuka untuk publik.
- ✅ Service account dengan permission **minimal** (hanya sheet yang dipakai, bukan semua Drive).
- ✅ Rotate token Telegram & service account key secara berkala.

---

## 📝 Lisensi & Catatan

Built dengan:
- [python-telegram-bot](https://python-telegram-bot.org/) v21
- [gspread](https://docs.gspread.org/)
- [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) untuk fuzzy matching
- [cachetools](https://cachetools.readthedocs.io/) untuk TTL cache
