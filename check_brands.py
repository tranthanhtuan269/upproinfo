"""Quet find-offers de so sanh voi catalog local.

Khong lay offer-detail, khong ghi progress.json, khong sua offers/details/skipped.
Chi danh dau brand con/mat/moi trong bang presence va file bao cao.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scrape_brands import (
    MarketplaceClient,
    list_url,
    safe_print,
)
from storage import DATA_DIR, connect, ensure_presence_tables, list_shop_ids

PROGRESS_PATH = DATA_DIR / "presence_progress.json"
REPORT_JSON = DATA_DIR / "presence_latest.json"
REPORT_NEW = DATA_DIR / "presence_new.csv"
REPORT_STILL = DATA_DIR / "presence_still.csv"
REPORT_MISSING = DATA_DIR / "presence_missing.csv"

CSV_FIELDS = [
    "shop_id",
    "name",
    "website",
    "listing_id",
    "page",
    "status",
    "first_seen_at",
    "last_seen_at",
    "missing_at",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_progress() -> dict[str, Any]:
    return {
        "started_at": now_iso(),
        "list_next_page": 1,
        "list_done": False,
        "live": {},
    }


def load_progress(fresh: bool) -> dict[str, Any]:
    if fresh or not PROGRESS_PATH.exists():
        return default_progress()
    try:
        raw = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        safe_print("presence_progress.json bi hong, bat dau scan moi.")
        return default_progress()
    if not isinstance(raw, dict) or raw.get("list_done"):
        return default_progress()
    progress = default_progress()
    progress.update(raw)
    live = progress.get("live")
    progress["live"] = live if isinstance(live, dict) else {}
    progress["list_done"] = False
    return progress


def save_progress(progress: dict[str, Any], path: Path = PROGRESS_PATH) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def known_shop_ids(conn: sqlite3.Connection | None = None) -> set[int]:
    del conn
    return set(list_shop_ids())


def catalog_diff(live_map: dict[str, dict[str, Any]]) -> dict[str, set[int]]:
    live_ids = {int(item["shop_id"]) for item in live_map.values()}
    known = set(list_shop_ids())
    return {
        "live_ids": live_ids,
        "known": known,
        "still_ids": live_ids & known,
        "new_ids": live_ids - known,
        "missing_ids": known - live_ids,
    }


def rows_from_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            shop_id = raw.get("shop_id")
            if not shop_id:
                continue
            try:
                sid = int(shop_id)
            except (TypeError, ValueError):
                continue
            item = dict(raw)
            item["shop_id"] = sid
            rows.append(item)
        return rows


def lookup_names(conn: sqlite3.Connection, shop_ids: set[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not shop_ids:
        return out
    placeholders = ",".join("?" for _ in shop_ids)
    params = list(shop_ids)
    queries = (
        f"SELECT shop_id, name, website, listing_id, page FROM offers WHERE shop_id IN ({placeholders})",
        f"SELECT shop_id, name, website, NULL, NULL FROM details WHERE shop_id IN ({placeholders})",
        f"SELECT shop_id, name, website, listing_id, page FROM brand_presence WHERE shop_id IN ({placeholders})",
    )
    for sql in queries:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            continue
        for shop_id, name, website, listing_id, page in rows:
            sid = int(shop_id)
            prev = out.get(sid, {})
            out[sid] = {
                "shop_id": sid,
                "name": prev.get("name") or name or "",
                "website": prev.get("website") or website or "",
                "listing_id": prev.get("listing_id") or listing_id,
                "page": prev.get("page") or page,
            }
    return out


LIVE_FIELDS = (
    "shop_id",
    "id",
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
)


def slim_live_row(row: dict[str, Any], page: int) -> dict[str, Any] | None:
    shop_id = row.get("shop_id")
    if shop_id is None:
        return None
    try:
        sid = int(shop_id)
    except (TypeError, ValueError):
        return None
    out = {field: row.get(field) for field in LIVE_FIELDS}
    out["shop_id"] = sid
    out["listing_id"] = row.get("id")
    out["page"] = page
    out["_page"] = page
    return out


def as_offer_row(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    if row.get("id") is None and row.get("listing_id") is not None:
        row["id"] = row["listing_id"]
    page = row.get("_page") if row.get("_page") is not None else row.get("page")
    if page is not None:
        row["_page"] = page
    return row


def scan_list(
    client: MarketplaceClient,
    progress: dict[str, Any],
    path: Path = PROGRESS_PATH,
) -> dict[str, dict[str, Any]]:
    live: dict[str, dict[str, Any]] = {
        str(key): value for key, value in (progress.get("live") or {}).items()
    }
    page = int(progress.get("list_next_page") or 1)
    while True:
        payload = client.get_json(list_url(page), "list")
        data = payload.get("data") or {}
        rows = data.get("data") or []
        if not rows:
            progress["list_done"] = True
            progress["live"] = live
            save_progress(progress, path)
            safe_print(f"Het trang o page {page}.")
            return {key: value for key, value in live.items()}

        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            slim = slim_live_row(row, page)
            if slim is None:
                continue
            live[str(slim["shop_id"])] = slim
            added += 1

        nxt = data.get("next_page_url")
        safe_print(
            f"List page {page}: {added} brand "
            f"(from={data.get('from')} to={data.get('to')}, live={len(live)})"
        )
        progress["list_next_page"] = page + 1
        progress["live"] = live
        save_progress(progress, path)
        if not nxt:
            progress["list_done"] = True
            save_progress(progress, path)
            safe_print(f"Trang cuoi: {page}.")
            return live
        page += 1


def upsert_presence(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    status: str,
    seen_at: str,
) -> None:
    shop_id = int(row["shop_id"])
    existing = conn.execute(
        "SELECT first_seen_at, last_seen_at, missing_at FROM brand_presence WHERE shop_id = ?",
        (shop_id,),
    ).fetchone()
    first_seen = existing[0] if existing and existing[0] else seen_at
    if status == "missing":
        last_seen = existing[1] if existing else None
        missing_at = existing[2] if existing and existing[2] else seen_at
    else:
        last_seen = seen_at
        missing_at = None
    row["first_seen_at"] = first_seen
    row["last_seen_at"] = last_seen or ""
    row["missing_at"] = missing_at or ""
    conn.execute(
        """
        INSERT INTO brand_presence (
            shop_id, name, website, listing_id, page, status,
            first_seen_at, last_seen_at, missing_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shop_id) DO UPDATE SET
            name=COALESCE(NULLIF(excluded.name, ''), brand_presence.name),
            website=COALESCE(NULLIF(excluded.website, ''), brand_presence.website),
            listing_id=COALESCE(excluded.listing_id, brand_presence.listing_id),
            page=COALESCE(excluded.page, brand_presence.page),
            status=excluded.status,
            first_seen_at=brand_presence.first_seen_at,
            last_seen_at=excluded.last_seen_at,
            missing_at=excluded.missing_at
        """,
        (
            shop_id,
            row.get("name") or "",
            row.get("website") or "",
            row.get("listing_id"),
            row.get("page"),
            status,
            first_seen,
            last_seen,
            missing_at,
        ),
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda item: (item.get("name") or "", int(item["shop_id"]))):
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def finish_scan(progress: dict[str, Any], live_map: dict[str, dict[str, Any]]) -> dict[str, int]:
    seen_at = now_iso()
    live_ids = {int(item["shop_id"]) for item in live_map.values()}
    conn = connect()
    try:
        ensure_presence_tables(conn)
        known = known_shop_ids(conn)
        still_ids = live_ids & known
        new_ids = live_ids - known
        missing_ids = known - live_ids
        names = lookup_names(conn, missing_ids | still_ids)

        still_rows: list[dict[str, Any]] = []
        new_rows: list[dict[str, Any]] = []
        missing_rows: list[dict[str, Any]] = []

        for sid in still_ids:
            row = dict(live_map[str(sid)])
            row.update({k: v for k, v in names.get(sid, {}).items() if not row.get(k)})
            row["status"] = "still"
            row["last_seen_at"] = seen_at
            upsert_presence(conn, row, "still", seen_at)
            still_rows.append(row)

        for sid in new_ids:
            row = dict(live_map[str(sid)])
            row["status"] = "new"
            row["first_seen_at"] = seen_at
            row["last_seen_at"] = seen_at
            upsert_presence(conn, row, "new", seen_at)
            new_rows.append(row)

        for sid in missing_ids:
            row = names.get(sid) or {"shop_id": sid, "name": "", "website": ""}
            row["status"] = "missing"
            row["missing_at"] = seen_at
            upsert_presence(conn, row, "missing", seen_at)
            missing_rows.append(row)

        conn.execute(
            """
            INSERT INTO presence_scans (
                started_at, finished_at, live_count, known_count,
                still_count, new_count, missing_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                progress.get("started_at"),
                seen_at,
                len(live_ids),
                len(known),
                len(still_ids),
                len(new_ids),
                len(missing_ids),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    write_csv(REPORT_STILL, still_rows)
    write_csv(REPORT_NEW, new_rows)
    write_csv(REPORT_MISSING, missing_rows)
    report = {
        "started_at": progress.get("started_at"),
        "finished_at": seen_at,
        "live": len(live_ids),
        "known": len(known),
        "still": len(still_ids),
        "new": len(new_ids),
        "missing": len(missing_ids),
        "files": {
            "still": str(REPORT_STILL),
            "new": str(REPORT_NEW),
            "missing": str(REPORT_MISSING),
        },
        "new_brands": [
            {"shop_id": row["shop_id"], "name": row.get("name") or "", "website": row.get("website") or ""}
            for row in sorted(new_rows, key=lambda item: (item.get("name") or "", item["shop_id"]))
        ],
        "missing_brands": [
            {"shop_id": row["shop_id"], "name": row.get("name") or "", "website": row.get("website") or ""}
            for row in sorted(missing_rows, key=lambda item: (item.get("name") or "", item["shop_id"]))
        ],
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "live": len(live_ids),
        "known": len(known),
        "still": len(still_ids),
        "new": len(new_ids),
        "missing": len(missing_ids),
    }


def main() -> None:
    fresh = "--fresh" in sys.argv
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_progress(fresh=fresh)
    if fresh:
        safe_print("Bat dau scan moi (--fresh).")
    elif PROGRESS_PATH.exists() and not progress.get("list_done") and progress.get("live"):
        safe_print(
            f"Resume tu page {progress.get('list_next_page')} "
            f"(da co {len(progress.get('live') or {})} brand live)."
        )

    client = MarketplaceClient()
    client.login()
    safe_print("Chi quet find-offers (tab all-offers). Khong lay detail, khong ghi catalog.")
    try:
        live_map = scan_list(client, progress)
    except KeyboardInterrupt:
        save_progress(progress)
        safe_print("Dung giua chung. Chay lai python check_brands.py de resume.")
        raise SystemExit(1) from None

    summary = finish_scan(progress, live_map)
    safe_print(
        "Xong. "
        f"Live={summary['live']}  Catalog={summary['known']}  "
        f"Con={summary['still']}  Moi={summary['new']}  Da xoa={summary['missing']}"
    )
    safe_print(f"Brand moi: {REPORT_NEW}")
    safe_print(f"Con ton tai: {REPORT_STILL}")
    safe_print(f"Da xoa: {REPORT_MISSING}")
    safe_print(f"Tom tat: {REPORT_JSON}")


if __name__ == "__main__":
    main()
