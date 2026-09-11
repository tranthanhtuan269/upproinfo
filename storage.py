"""Luu brand gon: CSV (Excel), SQLite, va 1 file JSON nho moi brand."""

from __future__ import annotations

import csv
import json
import sqlite3
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
    return conn


def upsert_offer(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    slim = slim_list_row(row)
    shop_id = slim.get("shop_id")
    if shop_id is None:
        return
    conn.execute(
        """
        INSERT INTO offers (
            shop_id, listing_id, program_id, name, website, myshopify_domain,
            categories, commission, cookie, payout_rate, approval_rate,
            offer_score, recommend_score, currency, apply_url, page
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        [slim[field] for field in LIST_CSV_FIELDS],
    )


def upsert_detail(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
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


def save_offer_row(row: dict[str, Any]) -> None:
    conn = connect()
    try:
        upsert_offer(conn, row)
        conn.commit()
    finally:
        conn.close()


def save_detail_row(row: dict[str, Any]) -> None:
    save_detail_json(row)
    conn = connect()
    try:
        upsert_detail(conn, row)
        conn.commit()
    finally:
        conn.close()


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


if __name__ == "__main__":
    cleaned = strip_existing_apply_urls()
    print(
        "stripped apply_url "
        f"offers={cleaned['offers']} details={cleaned['details']} "
        f"json={cleaned['json_files']}"
    )
