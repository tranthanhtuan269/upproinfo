"""SQLite catalog for Awin programmes. Separate from uppromote.db."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "awin.db"
DAILY_PROGRESS = DATA_DIR / "awin_daily_progress.json"
DAILY_REPORT = DATA_DIR / "awin_daily_latest.json"
REFRESH_PROGRESS = DATA_DIR / "awin_refresh_progress.json"
REFRESH_REPORT = DATA_DIR / "awin_refresh_latest.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _write(operation):
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "readonly" not in str(exc).lower() and "locked" not in str(exc).lower():
                raise
            time.sleep(1.5 * (attempt + 1))
    raise last_error  # type: ignore[misc]


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS programmes (
            advertiser_id INTEGER PRIMARY KEY,
            name TEXT,
            membership TEXT,
            action TEXT,
            conversion_rate TEXT,
            approval_rate TEXT,
            epc TEXT,
            epc_value REAL,
            payment_status TEXT,
            avg_payment_text TEXT,
            product_feed INTEGER,
            launch_date TEXT,
            link_status TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT,
            last_seen TEXT,
            created_at TEXT,
            scan_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS details (
            advertiser_id INTEGER PRIMARY KEY,
            summary TEXT,
            description TEXT,
            website TEXT,
            logo TEXT,
            cookie_text TEXT,
            cookie_days INTEGER,
            validation_text TEXT,
            commission_text TEXT,
            commission_percent REAL,
            commission_groups TEXT,
            regions TEXT,
            emails TEXT,
            mobile_optimised TEXT,
            app_tracking TEXT,
            payment_note TEXT,
            date_joined TEXT,
            profile_membership TEXT,
            detail_text TEXT,
            fetched_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_programmes_active ON programmes(active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_programmes_created ON programmes(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_programmes_name ON programmes(name)")
    conn.commit()
    return conn


def upsert_programme(row: dict[str, Any], scan_id: str) -> str:
    """Insert or update a listing row. Returns 'new' or 'updated'."""
    advertiser_id = int(row["advertiser_id"])
    seen = now_iso()

    def operation() -> str:
        conn = connect()
        try:
            return _upsert(conn, advertiser_id, row, scan_id, seen)
        finally:
            conn.close()

    return _write(operation)


def _upsert(conn: sqlite3.Connection, advertiser_id: int, row: dict[str, Any], scan_id: str, seen: str) -> str:
    try:
        existing = conn.execute(
            "SELECT created_at FROM programmes WHERE advertiser_id = ?",
            (advertiser_id,),
        ).fetchone()
        created = existing["created_at"] if existing and existing["created_at"] else today()
        conn.execute(
            """
            INSERT INTO programmes (
                advertiser_id, name, membership, action, conversion_rate, approval_rate,
                epc, epc_value, payment_status, avg_payment_text, product_feed, launch_date,
                link_status, active, first_seen, last_seen, created_at, scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(advertiser_id) DO UPDATE SET
                name = excluded.name,
                membership = excluded.membership,
                action = excluded.action,
                conversion_rate = excluded.conversion_rate,
                approval_rate = excluded.approval_rate,
                epc = excluded.epc,
                epc_value = excluded.epc_value,
                payment_status = excluded.payment_status,
                avg_payment_text = excluded.avg_payment_text,
                product_feed = excluded.product_feed,
                launch_date = excluded.launch_date,
                link_status = excluded.link_status,
                active = 1,
                last_seen = excluded.last_seen,
                scan_id = excluded.scan_id
            """,
            (
                advertiser_id,
                row.get("name") or "",
                row.get("membership") or "",
                row.get("action") or "",
                row.get("conversion_rate") or "",
                row.get("approval_rate") or "",
                row.get("epc") or "",
                row.get("epc_value"),
                row.get("payment_status") or "",
                row.get("avg_payment_text") or "",
                row.get("product_feed"),
                row.get("launch_date") or "",
                row.get("link_status") or "",
                seen,
                seen,
                created,
                scan_id,
            ),
        )
        conn.commit()
        return "new" if existing is None else "updated"
    finally:
        conn.close()


def finish_scan(scan_id: str) -> int:
    """Mark programmes missing from this completed listing as inactive."""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE programmes SET active = 0 WHERE COALESCE(scan_id, '') != ?",
            (scan_id,),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def list_advertiser_ids(active_only: bool = True) -> list[int]:
    conn = connect()
    try:
        if active_only:
            rows = conn.execute(
                "SELECT advertiser_id FROM programmes WHERE active = 1 ORDER BY advertiser_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT advertiser_id FROM programmes ORDER BY advertiser_id"
            ).fetchall()
        return [int(row["advertiser_id"]) for row in rows]
    finally:
        conn.close()


def missing_detail_ids(advertiser_ids: list[int]) -> list[int]:
    if not advertiser_ids:
        return []
    conn = connect()
    try:
        have = {
            int(row["advertiser_id"])
            for row in conn.execute(
                "SELECT advertiser_id FROM details WHERE error IS NULL OR error = ''"
            ).fetchall()
        }
    finally:
        conn.close()
    return [item for item in advertiser_ids if item not in have]


def save_detail(advertiser_id: int, detail: dict[str, Any], error: str = "") -> None:
    def operation() -> None:
        _save_detail(advertiser_id, detail, error)

    _write(operation)


def _save_detail(advertiser_id: int, detail: dict[str, Any], error: str = "") -> None:
    conn = connect()
    try:
        groups = detail.get("commission_groups")
        if not isinstance(groups, str):
            groups = json.dumps(groups or [], ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO details (
                advertiser_id, summary, description, website, logo, cookie_text, cookie_days,
                validation_text, commission_text, commission_percent, commission_groups, regions,
                emails, mobile_optimised, app_tracking, payment_note, date_joined,
                profile_membership, detail_text, fetched_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(advertiser_id) DO UPDATE SET
                summary = excluded.summary,
                description = excluded.description,
                website = excluded.website,
                logo = excluded.logo,
                cookie_text = excluded.cookie_text,
                cookie_days = excluded.cookie_days,
                validation_text = excluded.validation_text,
                commission_text = excluded.commission_text,
                commission_percent = excluded.commission_percent,
                commission_groups = excluded.commission_groups,
                regions = excluded.regions,
                emails = excluded.emails,
                mobile_optimised = excluded.mobile_optimised,
                app_tracking = excluded.app_tracking,
                payment_note = excluded.payment_note,
                date_joined = excluded.date_joined,
                profile_membership = excluded.profile_membership,
                detail_text = excluded.detail_text,
                fetched_at = excluded.fetched_at,
                error = excluded.error
            """,
            (
                int(advertiser_id),
                detail.get("summary") or "",
                detail.get("description") or "",
                detail.get("website") or "",
                detail.get("logo") or "",
                detail.get("cookie_text") or "",
                detail.get("cookie_days"),
                detail.get("validation_text") or "",
                detail.get("commission_text") or "",
                detail.get("commission_percent"),
                groups,
                detail.get("regions") or "",
                detail.get("emails") or "",
                detail.get("mobile_optimised") or "",
                detail.get("app_tracking") or "",
                detail.get("payment_note") or "",
                detail.get("date_joined") or "",
                detail.get("profile_membership") or "",
                detail.get("detail_text") or "",
                now_iso(),
                error[:500],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def counts() -> dict[str, int]:
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM programmes").fetchone()["n"]
        active = conn.execute("SELECT COUNT(*) AS n FROM programmes WHERE active = 1").fetchone()["n"]
        detailed = conn.execute(
            "SELECT COUNT(*) AS n FROM details WHERE COALESCE(error, '') = ''"
        ).fetchone()["n"]
        new_today = conn.execute(
            "SELECT COUNT(*) AS n FROM programmes WHERE created_at = ?",
            (today(),),
        ).fetchone()["n"]
        return {
            "total": int(total),
            "active": int(active),
            "inactive": int(total) - int(active),
            "detailed": int(detailed),
            "new_today": int(new_today),
        }
    finally:
        conn.close()
