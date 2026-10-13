"""Luu brand gon: CSV (Excel), SQLite, va 1 file JSON nho moi brand."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
BRANDS_DIR = DATA_DIR / "brands"
DB_PATH = DATA_DIR / "uppromote.db"
LIST_CSV = DATA_DIR / "offers_list.csv"
DETAIL_CSV = DATA_DIR / "brand_details.csv"
OLD_LIST_JSONL = DATA_DIR / "offers_list.jsonl"
OLD_DETAIL_JSONL = DATA_DIR / "brand_details.jsonl"

LIST_CSV_FIELDS = [
    "shop_id",
    "listing_id",
    "program_id",
    "name",
    "website",
    "myshopify_domain",
    "categories",
    "commission",
    "cookie",
    "payout_rate",
    "approval_rate",
    "offer_score",
    "recommend_score",
    "currency",
    "apply_url",
    "page",
]

DETAIL_CSV_FIELDS = [
    "shop_id",
    "name",
    "website",
    "myshopify_domain",
    "categories",
    "commission",
    "cookie",
    "payout_rate",
    "payout_period",
    "approval_rate",
    "offer_score",
    "recommend_score",
    "avg_order_value",
    "application_review",
    "offer_status",
    "program_id",
    "mkp_listing_id",
    "apply_url",
    "hashtags",
    "shop_plan",
]


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    BRANDS_DIR.mkdir(parents=True, exist_ok=True)


def brand_json_path(shop_id: int) -> Path:
    return BRANDS_DIR / f"{int(shop_id)}.json"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def strip_query(url: Any) -> Any:
    """Bo query/fragment, giu lai path dang ky. VD: .../register?p=1&key=... -> .../register"""
    if url is None:
        return None
    text = str(url).strip()
    if not text:
        return url
    parts = urlsplit(text)
    if not parts.scheme and not parts.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def slim_list_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "shop_id": row.get("shop_id"),
        "listing_id": row.get("id"),
        "program_id": row.get("program_id"),
        "name": row.get("name"),
        "website": row.get("website"),
        "myshopify_domain": row.get("myshopify_domain"),
        "categories": row.get("categories"),
        "commission": row.get("commission"),
        "cookie": row.get("cookie"),
        "payout_rate": row.get("payout_rate"),
        "approval_rate": row.get("approval_rate"),
        "offer_score": row.get("offer_score"),
        "recommend_score": row.get("recommend_score"),
        "currency": row.get("currency"),
        "apply_url": strip_query(row.get("apply_url")),
        "page": row.get("_page"),
    }


def slim_detail_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for field in DETAIL_CSV_FIELDS:
        value = row.get(field)
        if field == "apply_url":
            value = strip_query(value)
        out[field] = value
    return out


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offers (
            shop_id INTEGER PRIMARY KEY,
            listing_id INTEGER,
            program_id INTEGER,
            name TEXT,
            website TEXT,
            myshopify_domain TEXT,
            categories TEXT,
            commission TEXT,
            cookie TEXT,
            payout_rate TEXT,
            approval_rate TEXT,
            offer_score TEXT,
            recommend_score TEXT,
            currency TEXT,
            apply_url TEXT,
            page INTEGER,
            created_at TEXT
        )
        """
    )
    _add_missing_columns(conn, "offers", {"created_at": "TEXT"})
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS details (
            shop_id INTEGER PRIMARY KEY,
            name TEXT,
            website TEXT,
            myshopify_domain TEXT,
            categories TEXT,
            commission TEXT,
            cookie TEXT,
            payout_rate TEXT,
            payout_period TEXT,
            approval_rate TEXT,
            offer_score TEXT,
            recommend_score TEXT,
            avg_order_value TEXT,
            application_review TEXT,
            offer_status TEXT,
            program_id TEXT,
            mkp_listing_id TEXT,
            apply_url TEXT,
            hashtags TEXT,
            shop_plan TEXT,
            payment_methods TEXT
        )
        """
    )
    ensure_details_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skipped (
            shop_id INTEGER PRIMARY KEY,
            reason TEXT,
            skipped_at TEXT
        )
        """
    )
    ensure_brand_metrics(conn)
    ensure_presence_tables(conn)
    return conn


DETAILS_EXTRA_COLUMNS = {
    "payment_methods": "TEXT",
}


def ensure_details_columns(conn: sqlite3.Connection) -> None:
    _add_missing_columns(conn, "details", DETAILS_EXTRA_COLUMNS)
    conn.commit()


def payment_methods_token(payment_support: Any) -> str:
    methods: list[str] = []
    if isinstance(payment_support, dict):
        methods = [str(key).strip().lower() for key, val in payment_support.items() if val]
    elif isinstance(payment_support, list):
        methods = [str(item).strip().lower() for item in payment_support if str(item).strip()]
    elif isinstance(payment_support, str) and payment_support.strip():
        text = payment_support.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                return payment_methods_token(parsed)
            except json.JSONDecodeError:
                pass
        methods = [part.strip().lower() for part in text.split("|") if part.strip()]
    clean = sorted({item for item in methods if item})
    if not clean:
        return ""
    return "|" + "|".join(clean) + "|"


BRAND_METRICS_COLUMNS = {
    "domain": "TEXT",
    "traffic_m1": "INTEGER DEFAULT 0",
    "traffic_m2": "INTEGER DEFAULT 0",
    "traffic_m3": "INTEGER DEFAULT 0",
    "monthly_visits_json": "TEXT",
    "bounce_rate": "REAL DEFAULT 0",
    "time_on_site": "REAL DEFAULT 0",
    "pages_per_visit": "REAL DEFAULT 0",
    "global_rank": "INTEGER DEFAULT 0",
    "country_rank": "INTEGER DEFAULT 0",
    "top_keywords_json": "TEXT",
    "top_regions_json": "TEXT",
    "google_ads_running": "INTEGER DEFAULT 0",
    "google_advertisers_count": "INTEGER DEFAULT 0",
    "google_advertisers_json": "TEXT",
    "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "allows_search_ads": "INTEGER DEFAULT 0",
    "search_ads_status": "TEXT DEFAULT 'no'",
}


PRESENCE_COLUMNS = {
    "name": "TEXT",
    "website": "TEXT",
    "listing_id": "INTEGER",
    "page": "INTEGER",
    "status": "TEXT",
    "first_seen_at": "TEXT",
    "last_seen_at": "TEXT",
    "missing_at": "TEXT",
}

METRIC_WRITE_COLUMNS = [
    "domain",
    "traffic_m1",
    "traffic_m2",
    "traffic_m3",
    "monthly_visits_json",
    "bounce_rate",
    "time_on_site",
    "pages_per_visit",
    "global_rank",
    "country_rank",
    "top_keywords_json",
    "google_ads_running",
    "google_advertisers_count",
    "google_advertisers_json",
    "updated_at",
]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = set(_table_columns(conn, table))
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def ensure_brand_metrics(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brand_metrics (
            shop_id INTEGER PRIMARY KEY,
            domain TEXT,
            traffic_m1 INTEGER DEFAULT 0,
            traffic_m2 INTEGER DEFAULT 0,
            traffic_m3 INTEGER DEFAULT 0,
            monthly_visits_json TEXT,
            bounce_rate REAL DEFAULT 0,
            time_on_site REAL DEFAULT 0,
            pages_per_visit REAL DEFAULT 0,
            global_rank INTEGER DEFAULT 0,
            country_rank INTEGER DEFAULT 0,
            top_keywords_json TEXT,
            top_regions_json TEXT,
            google_ads_running INTEGER DEFAULT 0,
            google_advertisers_count INTEGER DEFAULT 0,
            google_advertisers_json TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            allows_search_ads INTEGER DEFAULT 0
        )
        """
    )
    _add_missing_columns(conn, "brand_metrics", BRAND_METRICS_COLUMNS)
    conn.commit()


def ensure_metrics_table(conn: sqlite3.Connection) -> None:
    ensure_brand_metrics(conn)


def ensure_presence_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brand_presence (
            shop_id INTEGER PRIMARY KEY,
            name TEXT,
            website TEXT,
            listing_id INTEGER,
            page INTEGER,
            status TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            missing_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            live_count INTEGER,
            known_count INTEGER,
            still_count INTEGER,
            new_count INTEGER,
            missing_count INTEGER
        )
        """
    )
    _add_missing_columns(conn, "brand_presence", PRESENCE_COLUMNS)
    conn.commit()


def determine_search_ads_status(row: dict[str, Any]) -> tuple[int, str]:
    """Xac dinh trang thai search ads:
    - ('allowed', 1): Cho phep tat ca search ads (ca Google va Bing)
    - ('no_google', 0): Cho chay ads nhung KHONG CHO CHAY GOOGLE ADS (is_detect=1 & action=2 hoac text cam doan)
    - ('brand_detects', 1): Cho chay ads nhung BRAND BIET (is_detect=1 & action=1 -> 'This brand is able to detect referrals that come through Google ads')
    - ('no', 0): Khong cho chay bat ky search ads nao
    """
    if not isinstance(row, dict):
        return 0, "no"

    channels = row.get("target_audience_customer_channels") or []
    has_sa = False
    if isinstance(channels, list):
        has_sa = any(str(c).strip().lower() in ("search_ads", "search ads", "searchads") for c in channels)
    elif isinstance(channels, dict):
        has_sa = any(str(k).strip().lower() in ("search_ads", "search ads", "searchads") for k in channels.keys())

    is_detect = row.get("is_detect_referral_google_ads") == 1
    action = row.get("action_referral_google_ads")

    has_prohibit_phrase = False
    for val in row.values():
        if isinstance(val, str) and GOOGLE_ADS_DISALLOWED_PHRASE in val.lower():
            has_prohibit_phrase = True
            break
        elif isinstance(val, (list, dict)):
            try:
                if GOOGLE_ADS_DISALLOWED_PHRASE in json.dumps(val).lower():
                    has_prohibit_phrase = True
                    break
            except Exception:
                pass

    # Anh 1: "Referrals from Google Ads are not allowed under this offer" -> is_detect=1 va action=2, hoac co doan text cam
    if (is_detect and action == 2) or has_prohibit_phrase:
        return 0, "no_google"

    # Anh 2: "This brand is able to detect referrals that come through Google ads" -> is_detect=1 va action=1
    if is_detect and action == 1:
        return 1, "brand_detects"

    if has_sa:
        return 1, "allowed"

    return 0, "no"


def _allows_search_ads(channels: Any) -> int:
    if isinstance(channels, dict):
        return 1 if channels.get("search_ads") else 0
    if isinstance(channels, list):
        return 1 if any(str(item).strip().lower() in ("search_ads", "search ads", "searchads") for item in channels) else 0
    return 0


def upsert_allows_search_ads(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    shop_id = row.get("shop_id")
    if shop_id is None:
        return
    flag, status = determine_search_ads_status(row)
    conn.execute(
        """
        INSERT INTO brand_metrics (shop_id, allows_search_ads, search_ads_status)
        VALUES (?, ?, ?)
        ON CONFLICT(shop_id) DO UPDATE SET
            allows_search_ads=excluded.allows_search_ads,
            search_ads_status=excluded.search_ads_status
        """,
        (int(shop_id), flag, status),
    )


def sync_allows_search_ads_from_json(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    own = conn is None
    if own:
        conn = connect()
    ensure_brand_metrics(conn)
    files = 0
    updated = 0
    allowed = 0
    no_google = 0
    brand_detects = 0
    try:
        for path in BRANDS_DIR.glob("*.json"):
            files += 1
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            try:
                shop_id = int(row.get("shop_id") or path.stem)
            except (TypeError, ValueError):
                continue
            filter_disallowed_search_ads(row)
            flag, status = determine_search_ads_status(row)
            conn.execute(
                """
                INSERT INTO brand_metrics (shop_id, allows_search_ads, search_ads_status)
                VALUES (?, ?, ?)
                ON CONFLICT(shop_id) DO UPDATE SET
                    allows_search_ads=excluded.allows_search_ads,
                    search_ads_status=excluded.search_ads_status
                """,
                (shop_id, flag, status),
            )
            updated += 1
            if status == "allowed":
                allowed += 1
            elif status == "no_google":
                no_google += 1
            elif status == "brand_detects":
                brand_detects += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"files": files, "updated": updated, "allowed": allowed, "no_google": no_google, "brand_detects": brand_detects}


def upsert_metrics_record(conn: sqlite3.Connection, record: tuple[Any, ...]) -> None:
    """Ghi traffic/ads. Khong dung INSERT OR REPLACE de giu allows_search_ads."""
    if len(record) == 16:
        conn.execute(
            """
            INSERT INTO brand_metrics (
                shop_id, domain, traffic_m1, traffic_m2, traffic_m3,
                monthly_visits_json, bounce_rate, time_on_site, pages_per_visit,
                global_rank, country_rank, top_keywords_json, top_regions_json, google_ads_running,
                google_advertisers_count, google_advertisers_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(shop_id) DO UPDATE SET
                domain=excluded.domain,
                traffic_m1=excluded.traffic_m1,
                traffic_m2=excluded.traffic_m2,
                traffic_m3=excluded.traffic_m3,
                monthly_visits_json=excluded.monthly_visits_json,
                bounce_rate=excluded.bounce_rate,
                time_on_site=excluded.time_on_site,
                pages_per_visit=excluded.pages_per_visit,
                global_rank=excluded.global_rank,
                country_rank=excluded.country_rank,
                top_keywords_json=excluded.top_keywords_json,
                top_regions_json=excluded.top_regions_json,
                google_ads_running=excluded.google_ads_running,
                google_advertisers_count=excluded.google_advertisers_count,
                google_advertisers_json=excluded.google_advertisers_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            record,
        )
    else:
        conn.execute(
            """
            INSERT INTO brand_metrics (
                shop_id, domain, traffic_m1, traffic_m2, traffic_m3,
                monthly_visits_json, bounce_rate, time_on_site, pages_per_visit,
                global_rank, country_rank, top_keywords_json, google_ads_running,
                google_advertisers_count, google_advertisers_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(shop_id) DO UPDATE SET
                domain=excluded.domain,
                traffic_m1=excluded.traffic_m1,
                traffic_m2=excluded.traffic_m2,
                traffic_m3=excluded.traffic_m3,
                monthly_visits_json=excluded.monthly_visits_json,
                bounce_rate=excluded.bounce_rate,
                time_on_site=excluded.time_on_site,
                pages_per_visit=excluded.pages_per_visit,
                global_rank=excluded.global_rank,
                country_rank=excluded.country_rank,
                top_keywords_json=excluded.top_keywords_json,
                google_ads_running=excluded.google_ads_running,
                google_advertisers_count=excluded.google_advertisers_count,
                google_advertisers_json=excluded.google_advertisers_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            record,
        )


def upsert_offer(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    slim = slim_list_row(row)
    shop_id = slim.get("shop_id")
    if shop_id is None:
        return
    created_at = row.get("created_at") or datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """
        INSERT INTO offers (
            shop_id, listing_id, program_id, name, website, myshopify_domain,
            categories, commission, cookie, payout_rate, approval_rate,
            offer_score, recommend_score, currency, apply_url, page, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shop_id) DO UPDATE SET
            listing_id=excluded.listing_id,
            program_id=excluded.program_id,
            name=excluded.name,
            website=excluded.website,
            myshopify_domain=excluded.myshopify_domain,
            categories=excluded.categories,
            commission=excluded.commission,
            cookie=excluded.cookie,
            payout_rate=excluded.payout_rate,
            approval_rate=excluded.approval_rate,
            offer_score=excluded.offer_score,
            recommend_score=excluded.recommend_score,
            currency=excluded.currency,
            apply_url=excluded.apply_url,
            page=excluded.page
        """,
        [slim[field] for field in LIST_CSV_FIELDS] + [created_at],
    )
    if slim.get("categories"):
        sync_offer_categories(conn, shop_id, slim.get("categories"))


def upsert_detail(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    ensure_details_columns(conn)
    slim = slim_detail_row(row)
    shop_id = slim.get("shop_id")
    if shop_id is None:
        return
    conn.execute(
        f"""
        INSERT INTO details ({", ".join(DETAIL_CSV_FIELDS)})
        VALUES ({", ".join("?" for _ in DETAIL_CSV_FIELDS)})
        ON CONFLICT(shop_id) DO UPDATE SET
        {", ".join(f"{field}=excluded.{field}" for field in DETAIL_CSV_FIELDS if field != "shop_id")}
        """,
        [_cell(slim[field]) if field != "shop_id" else slim[field] for field in DETAIL_CSV_FIELDS],
    )
    conn.execute(
        "UPDATE details SET payment_methods = ? WHERE shop_id = ?",
        (payment_methods_token(row.get("payment_support")), int(shop_id)),
    )


def save_detail_json(row: dict[str, Any]) -> None:
    shop_id = row.get("shop_id")
    if shop_id is None:
        return
    ensure_dirs()
    cleaned = dict(row)
    if "apply_url" in cleaned:
        cleaned["apply_url"] = strip_query(cleaned.get("apply_url"))
    path = brand_json_path(int(shop_id))
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")


def export_offers_csv(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        f"SELECT {', '.join(LIST_CSV_FIELDS)} FROM offers ORDER BY page, shop_id"
    ).fetchall()
    with LIST_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(LIST_CSV_FIELDS)
        writer.writerows(rows)
    return len(rows)


def export_details_csv(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        f"SELECT {', '.join(DETAIL_CSV_FIELDS)} FROM details ORDER BY name, shop_id"
    ).fetchall()
    with DETAIL_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(DETAIL_CSV_FIELDS)
        writer.writerows(rows)
    return len(rows)


def list_shop_ids() -> list[int]:
    conn = connect()
    try:
        rows = conn.execute("SELECT shop_id FROM offers ORDER BY page, shop_id").fetchall()
        if rows:
            return [int(row[0]) for row in rows]
    finally:
        conn.close()
    return []


def existing_detail_ids() -> set[int]:
    """Brand da co file/detail. Khong gom skipped."""
    ids: set[int] = set()
    conn = connect()
    try:
        rows = conn.execute("SELECT shop_id FROM details").fetchall()
        ids.update(int(row[0]) for row in rows)
    finally:
        conn.close()
    if BRANDS_DIR.exists():
        for path in BRANDS_DIR.glob("*.json"):
            try:
                ids.add(int(path.stem))
            except ValueError:
                continue
    return ids


def done_detail_ids() -> set[int]:
    ids: set[int] = set()
    conn = connect()
    try:
        rows = conn.execute("SELECT shop_id FROM details").fetchall()
        ids.update(int(row[0]) for row in rows)
        skipped = conn.execute("SELECT shop_id FROM skipped").fetchall()
        ids.update(int(row[0]) for row in skipped)
    finally:
        conn.close()
    if BRANDS_DIR.exists():
        for path in BRANDS_DIR.glob("*.json"):
            try:
                ids.add(int(path.stem))
            except ValueError:
                continue
    return ids


def mark_skipped(shop_id: int, reason: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO skipped (shop_id, reason, skipped_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(shop_id) DO UPDATE SET
                reason=excluded.reason,
                skipped_at=excluded.skipped_at
            """,
            (int(shop_id), reason[:240]),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_skipped(shop_id: int, reason: str) -> bool:
    """Danh dau skipped neu chua co. True khi moi them."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT shop_id FROM skipped WHERE shop_id = ?",
            (int(shop_id),),
        ).fetchone()
        if row:
            return False
        conn.execute(
            """
            INSERT INTO skipped (shop_id, reason, skipped_at)
            VALUES (?, ?, datetime('now'))
            """,
            (int(shop_id), reason[:240]),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def mark_all_as_inactive() -> int:
    """Cap nhat toan bo brand trong offers thanh ngung hoat dong (vao bang skipped)."""
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT OR REPLACE INTO skipped (shop_id, reason, skipped_at)
            SELECT shop_id, 'Khong con tren find-offers', datetime('now')
            FROM offers
            """
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_skipped_batch(shop_ids: list[int | str]) -> int:
    """Go khoi bang skipped (cap nhat thanh dang hoat dong) cho danh sach shop_id."""
    clean_ids = [int(sid) for sid in shop_ids if sid]
    if not clean_ids:
        return 0
    conn = connect()
    try:
        placeholders = ",".join("?" for _ in clean_ids)
        cur = conn.execute(
            f"DELETE FROM skipped WHERE shop_id IN ({placeholders})",
            clean_ids,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_skipped(shop_id: int) -> bool:
    """Go skipped khi brand quay lai listing. True khi co dong bi xoa."""
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM skipped WHERE shop_id = ?", (int(shop_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_offer_row(row: dict[str, Any]) -> None:
    conn = connect()
    try:
        upsert_offer(conn, row)
        conn.commit()
    finally:
        conn.close()


GOOGLE_ADS_DISALLOWED_PHRASE = (
    "referrals from google ads are not allowed under this offer. "
    "please avoid using this traffic source to ensure your referrals remain valid."
)


def filter_disallowed_search_ads(row: dict[str, Any]) -> bool:
    """Neu brand cam Google Ads qua bien is_detect_referral_google_ads hoac qua text, xoa Search ads."""
    if not isinstance(row, dict):
        return False

    disallowed = False
    # Truong hop 1: UpPromote bat tinh nang chan referral Google Ads
    if row.get("is_detect_referral_google_ads") == 1:
        disallowed = True

    # Truong hop 2: Doan text canh bao trong description / terms
    if not disallowed:
        for val in row.values():
            if isinstance(val, str) and GOOGLE_ADS_DISALLOWED_PHRASE in val.lower():
                disallowed = True
                break
            elif isinstance(val, (list, dict)):
                try:
                    raw = json.dumps(val).lower()
                    if GOOGLE_ADS_DISALLOWED_PHRASE in raw:
                        disallowed = True
                        break
                except Exception:
                    pass

    if not disallowed:
        return False

    channels = row.get("target_audience_customer_channels")
    modified = False
    if isinstance(channels, list):
        filtered = [
            ch for ch in channels
            if str(ch).strip().lower() not in ("search_ads", "search ads", "searchads")
        ]
        if len(filtered) != len(channels):
            row["target_audience_customer_channels"] = filtered
            modified = True
    elif isinstance(channels, dict):
        for k in list(channels.keys()):
            if str(k).strip().lower() in ("search_ads", "search ads", "searchads"):
                del channels[k]
                modified = True
    return modified


def save_detail_row(row: dict[str, Any]) -> None:
    filter_disallowed_search_ads(row)
    save_detail_json(row)
    conn = connect()
    try:
        upsert_detail(conn, row)
        upsert_allows_search_ads(conn, row)
        conn.commit()
    finally:
        conn.close()


def sync_payment_methods_from_json(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    own = conn is None
    if own:
        conn = connect()
    ensure_details_columns(conn)
    files = 0
    updated = 0
    with_pay = 0
    try:
        for path in BRANDS_DIR.glob("*.json"):
            files += 1
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            try:
                shop_id = int(row.get("shop_id") or path.stem)
            except (TypeError, ValueError):
                continue
            token = payment_methods_token(row.get("payment_support"))
            cur = conn.execute(
                """
                UPDATE details SET payment_methods = ? WHERE shop_id = ?
                """,
                (token, shop_id),
            )
            if cur.rowcount:
                updated += 1
                if token:
                    with_pay += 1
            elif token:
                conn.execute(
                    """
                    INSERT INTO details (shop_id, payment_methods)
                    VALUES (?, ?)
                    ON CONFLICT(shop_id) DO UPDATE SET
                        payment_methods=excluded.payment_methods
                    """,
                    (shop_id, token),
                )
                updated += 1
                with_pay += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"files": files, "updated": updated, "with_payment": with_pay}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def migrate_existing() -> dict[str, int]:
    ensure_dirs()
    conn = connect()
    offers = 0
    details = 0
    try:
        if OLD_LIST_JSONL.exists():
            for row in iter_jsonl(OLD_LIST_JSONL):
                upsert_offer(conn, row)
            offers = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        if OLD_DETAIL_JSONL.exists():
            for row in iter_jsonl(OLD_DETAIL_JSONL):
                upsert_detail(conn, row)
                save_detail_json(row)
            details = conn.execute("SELECT COUNT(*) FROM details").fetchone()[0]
        conn.commit()
        export_offers_csv(conn)
        export_details_csv(conn)
    finally:
        conn.close()

    if OLD_LIST_JSONL.exists():
        OLD_LIST_JSONL.replace(ARCHIVE_DIR / "offers_list.jsonl")
    if OLD_DETAIL_JSONL.exists():
        OLD_DETAIL_JSONL.replace(ARCHIVE_DIR / "brand_details.jsonl")
    return {"offers": offers, "details": details}


def strip_existing_apply_urls() -> dict[str, int]:
    """Cat query string tren apply_url da luu (SQLite + JSON + CSV)."""
    conn = connect()
    offers = 0
    details = 0
    files = 0
    try:
        for shop_id, url in conn.execute(
            "SELECT shop_id, apply_url FROM offers WHERE apply_url LIKE '%?%'"
        ).fetchall():
            cleaned = strip_query(url)
            if cleaned != url:
                conn.execute(
                    "UPDATE offers SET apply_url = ? WHERE shop_id = ?",
                    (cleaned, shop_id),
                )
                offers += 1
        for shop_id, url in conn.execute(
            "SELECT shop_id, apply_url FROM details WHERE apply_url LIKE '%?%'"
        ).fetchall():
            cleaned = strip_query(url)
            if cleaned != url:
                conn.execute(
                    "UPDATE details SET apply_url = ? WHERE shop_id = ?",
                    (cleaned, shop_id),
                )
                details += 1
        conn.commit()
        export_offers_csv(conn)
        export_details_csv(conn)
    finally:
        conn.close()

    if BRANDS_DIR.exists():
        for path in BRANDS_DIR.glob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            url = row.get("apply_url")
            cleaned = strip_query(url)
            if cleaned != url:
                row["apply_url"] = cleaned
                path.write_text(
                    json.dumps(row, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                files += 1
    return {"offers": offers, "details": details, "json_files": files}


REQUIRED_CATALOG = {
    "offers": LIST_CSV_FIELDS,
    "details": [*DETAIL_CSV_FIELDS, "payment_methods"],
    "skipped": ["shop_id", "reason", "skipped_at"],
    "brand_metrics": ["shop_id", "allows_search_ads", *METRIC_WRITE_COLUMNS],
}

CATALOG_UPSERTS = (
    ("offers", "shop_id", LIST_CSV_FIELDS),
    ("details", "shop_id", [*DETAIL_CSV_FIELDS, "payment_methods"]),
    ("skipped", "shop_id", ["shop_id", "reason", "skipped_at"]),
    ("brand_metrics", "shop_id", ["shop_id", "allows_search_ads", *METRIC_WRITE_COLUMNS]),
    ("brand_presence", "shop_id", ["shop_id", *PRESENCE_COLUMNS]),
)


def catalog_schema_problems(path: Path | None = None) -> list[str]:
    db_path = Path(path or DB_PATH)
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        problems: list[str] = []
        for table, columns in REQUIRED_CATALOG.items():
            if table not in tables:
                problems.append(f"thieu bang {table}")
                continue
            have = set(_table_columns(conn, table))
            for col in columns:
                if col not in have:
                    problems.append(f"{table}.{col}")
        return problems
    finally:
        conn.close()


def merge_catalog(incoming: Path, live: Path | None = None) -> dict[str, int]:
    """Gop catalog vao DB dich. Khong xoa cot extra, khong de file DB."""
    incoming_path = Path(incoming)
    live_path = Path(live or DB_PATH)
    if not incoming_path.exists():
        raise FileNotFoundError(incoming_path)
    conn = sqlite3.connect(live_path)
    stats: dict[str, int] = {}
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS offers (
                shop_id INTEGER PRIMARY KEY,
                listing_id INTEGER,
                program_id INTEGER,
                name TEXT,
                website TEXT,
                myshopify_domain TEXT,
                categories TEXT,
                commission TEXT,
                cookie TEXT,
                payout_rate TEXT,
                approval_rate TEXT,
                offer_score TEXT,
                recommend_score TEXT,
                currency TEXT,
                apply_url TEXT,
                page INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS details (
                shop_id INTEGER PRIMARY KEY,
                name TEXT,
                website TEXT,
                myshopify_domain TEXT,
                categories TEXT,
                commission TEXT,
                cookie TEXT,
                payout_rate TEXT,
                payout_period TEXT,
                approval_rate TEXT,
                offer_score TEXT,
                recommend_score TEXT,
                avg_order_value TEXT,
                application_review TEXT,
                offer_status TEXT,
                program_id TEXT,
                mkp_listing_id TEXT,
                apply_url TEXT,
                hashtags TEXT,
                shop_plan TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skipped (
                shop_id INTEGER PRIMARY KEY,
                reason TEXT,
                skipped_at TEXT
            )
            """
        )
        ensure_brand_metrics(conn)
        ensure_presence_tables(conn)
        conn.execute("ATTACH DATABASE ? AS incoming", (str(incoming_path),))
        incoming_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM incoming.sqlite_master WHERE type='table'")
        }
        for table, pk, columns in CATALOG_UPSERTS:
            if table not in incoming_tables:
                stats[table] = 0
                continue
            live_cols = set(_table_columns(conn, table))
            incoming_cols = {row[1] for row in conn.execute(f"PRAGMA incoming.table_info({table})")}
            use = [col for col in columns if col in live_cols and col in incoming_cols]
            if pk not in use:
                stats[table] = 0
                continue
            updates = [col for col in use if col != pk]
            col_sql = ", ".join(use)
            if updates:
                set_sql = ", ".join(f"{col}=excluded.{col}" for col in updates)
                conn.execute(
                    f"""
                    INSERT INTO {table} ({col_sql})
                    SELECT {col_sql} FROM incoming.{table} WHERE true
                    ON CONFLICT({pk}) DO UPDATE SET {set_sql}
                    """
                )
            else:
                conn.execute(
                    f"""
                    INSERT INTO {table} ({col_sql})
                    SELECT {col_sql} FROM incoming.{table} WHERE true
                    ON CONFLICT({pk}) DO NOTHING
                    """
                )
            stats[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if "skipped" in incoming_tables:
            conn.execute(
                "DELETE FROM skipped WHERE shop_id NOT IN (SELECT shop_id FROM incoming.skipped)"
            )
        if "presence_scans" in incoming_tables:
            live_scan = [col for col in _table_columns(conn, "presence_scans") if col != "id"]
            incoming_scan = {
                row[1] for row in conn.execute("PRAGMA incoming.table_info(presence_scans)")
            }
            use = [col for col in live_scan if col in incoming_scan]
            if use:
                col_sql = ", ".join(use)
                conn.execute(
                    f"INSERT INTO presence_scans ({col_sql}) SELECT {col_sql} FROM incoming.presence_scans"
                )
        conn.commit()
        conn.execute("DETACH DATABASE incoming")
        return stats
    finally:
        conn.close()


if __name__ == "__main__":
    cleaned = strip_existing_apply_urls()
    print(
        "stripped apply_url "
        f"offers={cleaned['offers']} details={cleaned['details']} "
        f"json={cleaned['json_files']}"
    )


def split_categories(cat_str: str) -> list[str]:
    """Tach chuoi categories chua dau phay va dau & thanh danh sach category rieng biet."""
    if not cat_str:
        return []
    parts = []
    for chunk in str(cat_str).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "&" in chunk:
            for sub in chunk.split("&"):
                sub_clean = sub.strip()
                if sub_clean:
                    parts.append(sub_clean)
        else:
            parts.append(chunk)

    cleaned = []
    for p in parts:
        p_clean = p.strip()
        low = p_clean.lower()
        if low in ("pet supplies", "pet supply"):
            std = "Pet Supplies"
        elif low in ("consumer goods",):
            std = "Consumer Goods"
        elif low in ("digital products",):
            std = "Digital Products"
        elif low in ("professional services",):
            std = "Professional Services"
        elif low in ("product add-ons",):
            std = "Product Add-Ons"
        elif low in ("tobacco products", "tobacco"):
            std = "Tobacco"
        elif low in ("adult products",):
            std = "Adult Products"
        elif low in ("sporting goods",):
            std = "Sporting Goods"
        elif low in ("office supplies",):
            std = "Office Supplies"
        else:
            std = p_clean.title() if p_clean.islower() else p_clean
        if std and std not in cleaned:
            cleaned.append(std)
    return cleaned


def sync_offer_categories(conn: sqlite3.Connection, shop_id: int, raw_categories: str) -> None:
    """Dong bo danh muc many-to-many cho mot store."""
    cats = split_categories(raw_categories)
    if not cats:
        return
    conn.execute("DELETE FROM offer_categories WHERE shop_id = ?", (int(shop_id),))
    for c in cats:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, slug) VALUES (?, lower(replace(replace(?, ' ', '-'), '&', 'and')))",
            (c, c),
        )
        cur = conn.execute("SELECT id FROM categories WHERE name = ?", (c,))
        row = cur.fetchone()
        if row:
            cid = row[0] if isinstance(row, (tuple, list)) else row["id"]
            conn.execute(
                "INSERT OR IGNORE INTO offer_categories (shop_id, category_id) VALUES (?, ?)",
                (int(shop_id), int(cid)),
            )
