#!/usr/bin/env python3
"""
GoAffPro Store Sync Tool (goaffpro_sync.py)
Tu dong cao va dong bo toan bo 21.986+ store tu marketplace GoAffPro.

Cac lenh su dung:
    python goaffpro_sync.py            # Chay dong bo day du (hoac resume)
    python goaffpro_sync.py --fresh    # Lam moi tu dau (danh dau inactive, reset progress)
    python goaffpro_sync.py --limit 50 # Chay thu nghiem 50 store
    python goaffpro_sync.py --export   # Xuat CSV va JSON tu database hien co
    python goaffpro_sync.py --status   # Xem thong ke database hien tai
"""

import csv
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Thu muc du lieu
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "goaffpro.db"
CSV_PATH = DATA_DIR / "goaffpro_stores.csv"
PROGRESS_PATH = DATA_DIR / "goaffpro_progress.json"
REPORT_PATH = DATA_DIR / "goaffpro_latest.json"

API_BASE_URL = "https://api-server-3.goaffpro.com/v1/public/sites"
BATCH_SIZE = 1000  # API ho tro toi da 1.000 store / 1 request


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_print(*args: Any) -> None:
    text = " ".join(str(a) for a in args)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY,
        name TEXT,
        website TEXT,
        domain TEXT,
        logo TEXT,
        currency TEXT,
        affiliate_portal TEXT,
        portal_url TEXT,
        cookie_duration INTEGER,
        cookie_days REAL,
        are_registrations_open INTEGER,
        is_approved_automatically INTEGER,
        commission_type TEXT,
        commission_amount REAL,
        commission_on TEXT,
        commission_text TEXT,
        status TEXT DEFAULT 'active',
        first_seen_at TEXT,
        last_seen_at TEXT,
        updated_at TEXT
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goaffpro_domain ON stores(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goaffpro_status ON stores(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goaffpro_auto ON stores(is_approved_automatically)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goaffpro_currency ON stores(currency)")
    conn.commit()


def clean_domain(url: str | None) -> str:
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    try:
        netloc = urllib.parse.urlparse(text).netloc or text.split("/")[0]
        netloc = netloc.lower().split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def format_commission(comm: dict[str, Any] | None, currency: str = "") -> tuple[str, float, str, str]:
    if not isinstance(comm, dict):
        return "", 0.0, "", ""
    c_type = str(comm.get("type") or "").strip().lower()
    c_on = str(comm.get("on") or "").strip().lower()
    try:
        amount = float(comm.get("amount") or 0)
    except (ValueError, TypeError):
        amount = 0.0

    if c_type in ("percentage", "percent"):
        amt_str = f"{int(amount) if amount.is_integer() else amount}%"
    elif amount > 0:
        amt_str = f"{amount} {currency}".strip()
    else:
        amt_str = "0"

    on_str = f"on {c_on}" if c_on else ""
    full_text = f"{amt_str} {on_str}".strip()
    return c_type, amount, c_on, full_text


def fetch_batch_from_api(offset: int, limit: int = BATCH_SIZE, max_retries: int = 3) -> dict[str, Any]:
    url = f"{API_BASE_URL}?limit={limit}&offset={offset}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except Exception as e:
            if attempt < max_retries:
                wait_time = attempt * 2.0
                safe_print(f"  [Canh bao] Loi API offset={offset} (lan {attempt}/{max_retries}): {e}. Thu lai sau {wait_time}s...")
                time.sleep(wait_time)
            else:
                raise RuntimeError(f"Khong the tai du lieu tu GoAffPro API tai offset {offset}: {e}") from e
    return {}


def upsert_store_batch(conn: sqlite3.Connection, stores: list[dict[str, Any]], now: str) -> tuple[int, int]:
    added = 0
    updated = 0
    cur = conn.cursor()
    for s in stores:
        sid = s.get("id")
        if not sid:
            continue
        sid = int(sid)
        name = str(s.get("name") or "").strip()
        website = str(s.get("website") or "").strip()
        domain = clean_domain(website)
        logo = str(s.get("logo") or "").strip()
        currency = str(s.get("currency") or "").strip().upper()
        aff_portal = str(s.get("affiliatePortal") or "").strip()
        portal_url = ""
        # Chi tao link portal neu co subdomain rieng (khong phai goaffpro.com chung)
        if aff_portal and aff_portal != "goaffpro.com":
            prefix = "" if aff_portal.startswith("http") else "https://"
            clean_p = f"{prefix}{aff_portal}".rstrip("/")
            if clean_p not in ("https://goaffpro.com", "http://goaffpro.com"):
                portal_url = f"{clean_p}/create-account"

        cookie_dur = int(s.get("cookieDuration") or 0)
        cookie_days = round(cookie_dur / 86400, 1) if cookie_dur > 0 else 0.0

        reg_open = 1 if s.get("areRegistrationsOpen") else 0
        auto_app = 1 if s.get("isApprovedAutomatically") else 0

        c_type, c_amount, c_on, c_text = format_commission(s.get("commission"), currency)

        cur.execute("SELECT portal_url, affiliate_portal FROM stores WHERE id = ?", (sid,))
        existing = cur.fetchone()

        if existing:
            existing_portal_url, existing_aff_portal = existing
            # Kiem tra xem link cu co phai link chung khong
            is_generic_existing = (existing_portal_url or "").rstrip("/") in (
                "https://goaffpro.com/create-account",
                "https://goaffpro.com",
                "http://goaffpro.com/create-account",
                "http://goaffpro.com",
                "",
            )
            if not portal_url:
                if not is_generic_existing:
                    portal_url = existing_portal_url
                    if existing_aff_portal and existing_aff_portal != "goaffpro.com":
                        aff_portal = existing_aff_portal
                else:
                    portal_url = ""

            cur.execute("""
                UPDATE stores SET
                    name = ?, website = ?, domain = ?, logo = ?, currency = ?,
                    affiliate_portal = ?, portal_url = ?, cookie_duration = ?, cookie_days = ?,
                    are_registrations_open = ?, is_approved_automatically = ?,
                    commission_type = ?, commission_amount = ?, commission_on = ?, commission_text = ?,
                    status = 'active', last_seen_at = ?, updated_at = ?
                WHERE id = ?
            """, (
                name, website, domain, logo, currency,
                aff_portal, portal_url, cookie_dur, cookie_days,
                reg_open, auto_app,
                c_type, c_amount, c_on, c_text,
                now, now, sid
            ))
            updated += 1
        else:
            cur.execute("""
                INSERT INTO stores (
                    id, name, website, domain, logo, currency,
                    affiliate_portal, portal_url, cookie_duration, cookie_days,
                    are_registrations_open, is_approved_automatically,
                    commission_type, commission_amount, commission_on, commission_text,
                    status, first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """, (
                sid, name, website, domain, logo, currency,
                aff_portal, portal_url, cookie_dur, cookie_days,
                reg_open, auto_app,
                c_type, c_amount, c_on, c_text,
                now, now, now
            ))
            added += 1

    conn.commit()
    return added, updated


def mark_all_inactive(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("UPDATE stores SET status = 'inactive' WHERE status = 'active'")
    conn.commit()
    return cur.rowcount


def export_csv(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, website, domain, status, currency, commission_text,
               cookie_days, is_approved_automatically, are_registrations_open,
               portal_url, updated_at
        FROM stores
        ORDER BY (CASE WHEN portal_url IS NOT NULL AND portal_url != '' THEN 0 ELSE 1 END) ASC, status ASC, is_approved_automatically DESC, name ASC
    """)
    rows = cur.fetchall()
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Store ID", "Tên Store", "Website", "Domain", "Trạng thái", "Tiền tệ",
            "Mức hoa hồng", "Thời hạn Cookie (ngày)", "Duyệt tự động (Instant)", "Đang mở đăng ký",
            "Link đăng ký Affiliate", "Cập nhật lúc"
        ])
        for r in rows:
            auto_str = "Có (Tự động)" if r[8] == 1 else "Chờ duyệt"
            reg_str = "Đang mở" if r[9] == 1 else "Đã đóng"
            writer.writerow([
                r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                r[7] if r[7] else 0, auto_str, reg_str,
                r[10], r[11]
            ])
    return len(rows)


def print_status(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM stores")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stores WHERE status = 'active'")
    active = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stores WHERE is_approved_automatically = 1")
    auto = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stores WHERE status = 'inactive'")
    inactive = cur.fetchone()[0]
    safe_print(f"📊 THỐNG KÊ DATABASE GOAFFPRO ({DB_PATH.name}):")
    safe_print(f"  - Tổng số store trong DB: {total:,}")
    safe_print(f"  - Đang hoạt động (active): {active:,}")
    safe_print(f"  - Duyệt tự động (Instant access): {auto:,}")
    safe_print(f"  - Ngưng hoạt động (inactive): {inactive:,}")


def run_sync(fresh: bool = False, limit_max: int = 0) -> None:
    start_time = time.time()
    conn = connect()
    init_db(conn)

    safe_print("=" * 60)
    safe_print(f"🚀 BẮT ĐẦU ĐỒNG BỘ GOAFFPRO STORES ({now_iso()})")
    safe_print(f"- Cờ fresh: {fresh} | Giới hạn test: {limit_max if limit_max else 'Toàn bộ'}")
    safe_print("=" * 60)

    # 1. Khoi tao progress
    offset = 0
    if fresh:
        marked = mark_all_inactive(conn)
        safe_print(f"Khoi tao: Da danh dau {marked:,} store cu thanh 'inactive' truoc khi quet moi.")
        if PROGRESS_PATH.exists():
            PROGRESS_PATH.unlink()
    elif PROGRESS_PATH.exists():
        try:
            saved_p = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
            if not saved_p.get("done"):
                offset = int(saved_p.get("offset") or 0)
                safe_print(f"Tiep tuc chu ky truoc tu offset={offset:,}...")
        except Exception:
            offset = 0

    # 2. Goi lay trang dau de biet tong so
    first_resp = fetch_batch_from_api(offset=offset, limit=1)
    total_in_api = int(first_resp.get("count") or 0)
    target_total = min(total_in_api, limit_max) if limit_max > 0 else total_in_api
    safe_print(f"Tong so store tren GoAffPro API: {total_in_api:,} store (Muc tieu can lay: {target_total:,})")

    total_added = 0
    total_updated = 0
    processed_count = offset
    now_str = now_iso()

    # 3. Vong lap quet batch 1000 store
    while processed_count < target_total:
        batch_limit = min(BATCH_SIZE, target_total - processed_count)
        if batch_limit <= 0:
            break

        safe_print(f"\n[Batch offset={processed_count:,}] Dang tai {batch_limit:,} store...")
        batch_data = fetch_batch_from_api(offset=processed_count, limit=batch_limit)
        stores = batch_data.get("stores") or []
        if not stores:
            safe_print("  -> Khong co them store nao. Ket thuc.")
            break

        added, updated = upsert_store_batch(conn, stores, now_str)
        total_added += added
        total_updated += updated
        processed_count += len(stores)

        pct = round(processed_count * 100 / target_total, 2)
        elapsed = round(time.time() - start_time, 1)
        safe_print(f"  -> Da luu: +{added} moi, ~{updated} cap nhat. Tien do: {processed_count:,}/{target_total:,} ({pct}%) trong {elapsed}s")

        # Luu progress file
        PROGRESS_PATH.write_text(json.dumps({
            "total": target_total,
            "offset": processed_count,
            "added": total_added,
            "updated": total_updated,
            "percent": pct,
            "done": processed_count >= target_total,
            "updated_at": now_iso()
        }, indent=2), encoding="utf-8")

        time.sleep(0.5)

    # 4. Xuat file CSV va JSON bao cao
    safe_print("\nDang xuat file CSV va bao cao tong hop...")
    exported_csv_count = export_csv(conn)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM stores WHERE status = 'active'")
    active_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stores WHERE is_approved_automatically = 1 AND status = 'active'")
    auto_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stores WHERE status = 'inactive'")
    inactive_count = cur.fetchone()[0]

    duration = round(time.time() - start_time, 1)
    report = {
        "sync_time": now_str,
        "duration_seconds": duration,
        "total_in_api": total_in_api,
        "total_processed": processed_count,
        "newly_added": total_added,
        "updated": total_updated,
        "total_active": active_count,
        "total_inactive": inactive_count,
        "auto_approved_active": auto_count,
        "csv_path": str(CSV_PATH),
        "db_path": str(DB_PATH)
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if PROGRESS_PATH.exists():
        PROGRESS_PATH.write_text(json.dumps({"done": True, "offset": processed_count, "total": target_total}, indent=2), encoding="utf-8")

    safe_print("=" * 60)
    safe_print(f"🎉 HOÀN TẤT ĐỒNG BỘ GOAFFPRO TRONG {duration} GIÂY!")
    safe_print(f"- Store them moi: {total_added:,}")
    safe_print(f"- Store cap nhat: {total_updated:,}")
    safe_print(f"- Tong store active: {active_count:,} (Duyet tu dong: {auto_count:,})")
    safe_print(f"- File CSV xuat: {CSV_PATH} ({exported_csv_count:,} dong)")
    safe_print(f"- Bao cao JSON: {REPORT_PATH}")
    safe_print("=" * 60)
    conn.close()


def main() -> None:
    fresh = "--fresh" in sys.argv
    if "--status" in sys.argv:
        conn = connect()
        init_db(conn)
        print_status(conn)
        conn.close()
        return

    if "--export" in sys.argv:
        conn = connect()
        init_db(conn)
        cnt = export_csv(conn)
        safe_print(f"Da xuat thanh cong {cnt:,} store ra {CSV_PATH}")
        conn.close()
        return

    limit_max = 0
    if "--limit" in sys.argv:
        try:
            l_idx = sys.argv.index("--limit")
            limit_max = int(sys.argv[l_idx + 1])
        except (ValueError, IndexError):
            limit_max = 50

    run_sync(fresh=fresh, limit_max=limit_max)


if __name__ == "__main__":
    main()
