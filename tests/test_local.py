"""
Quick test script - jalankan: python -m tests.test_local

Test ini:
1. Cek konfigurasi
2. Test koneksi ke Google Sheets
3. Test fungsi search dengan beberapa keyword
4. Test fuzzy suggest
5. Print hasil ke console (tidak ngirim ke Telegram)
"""
from src import config
from src.sheets import format_item, sheets_client


def main() -> None:
    print("=" * 60)
    print("INVENTORY BOT - LOCAL TEST")
    print("=" * 60)

    # 1. Validate config
    try:
        config.validate()
        print("✅ Config OK")
    except Exception as e:
        print(f"❌ Config error: {e}")
        return

    # 2. Test connection + load data
    try:
        records = sheets_client.get_all_records()
        print(f"✅ Loaded {len(records)} baris dari Google Sheets")
        if records:
            print(f"   Headers: {list(records[0].keys())}")
    except Exception as e:
        print(f"❌ Gagal connect ke Google Sheets: {e}")
        return

    # 3. Test search
    test_queries = ["compressor", "BAUT", "xyz123notfound"]
    for q in test_queries:
        print(f"\n🔍 Search: '{q}'")
        results = sheets_client.search(q)
        print(f"   Hasil: {len(results)}")
        for r in results[:2]:
            print("   ---")
            print(format_item(r))

        if not results:
            sug = sheets_client.fuzzy_suggest(q)
            print(f"   Fuzzy suggest: {sug}")

    # 4. Test cache
    print("\n📦 Test cache...")
    import time
    t1 = time.perf_counter()
    sheets_client.get_all_records()
    t2 = time.perf_counter()
    sheets_client.get_all_records()
    t3 = time.perf_counter()
    print(f"   Call 1 (cached): {(t2-t1)*1000:.1f}ms")
    print(f"   Call 2 (cached): {(t3-t2)*1000:.1f}ms")

    print("\n✅ Semua test selesai")


if __name__ == "__main__":
    main()
