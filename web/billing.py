"""Goi thang/nam, don hang va han su dung (SQLite rieng)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "billing.db"
ADMIN_NAMES = {"admin"}

PLANS = {
    "month": {
        "id": "month",
        "amount": "6.00",
        "days": 30,
        "currency": "USDT",
        "goods_id": "plan_month",
    },
    "year": {
        "id": "year",
        "amount": "60.00",
        "days": 365,
        "currency": "USDT",
        "goods_id": "plan_year",
    },
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    text = (value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_admin(username: str | None) -> bool:
    return str(username or "").casefold() in ADMIN_NAMES


def plan_of(plan_id: str) -> dict | None:
    return PLANS.get(str(plan_id or "").strip())


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            plan TEXT NOT NULL,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USDT',
            merchant_trade_no TEXT NOT NULL UNIQUE,
            prepay_id TEXT,
            checkout_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            paid_at TEXT,
            raw_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            username TEXT PRIMARY KEY,
            plan TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_order_id INTEGER
        )
        """
    )
    return conn


def get_subscription(username: str | None) -> dict | None:
    if not username:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username, plan, expires_at, last_order_id FROM subscriptions WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def is_entitled(username: str | None) -> bool:
    if not username:
        return False
    if is_admin(username):
        return True
    sub = get_subscription(username)
    if not sub:
        return False
    try:
        return parse_iso(sub["expires_at"]) > utcnow()
    except ValueError:
        return False


def access_for(username: str | None) -> dict:
    sub = get_subscription(username)
    entitled = is_entitled(username)
    expires_date = ""
    if sub:
        expires_date = str(sub.get("expires_at") or "")[:10]
    return {
        "entitled": entitled,
        "is_admin": is_admin(username),
        "subscription": sub,
        "plan": (sub or {}).get("plan") if entitled else "",
        "expires_date": expires_date if entitled and not is_admin(username) else expires_date,
    }


def create_order(username: str, plan_id: str, merchant_trade_no: str) -> dict:
    plan = plan_of(plan_id)
    if not plan:
        raise ValueError("plan")
    now = to_iso(utcnow())
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO orders (username, plan, amount, currency, merchant_trade_no, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (username, plan["id"], plan["amount"], plan["currency"], merchant_trade_no, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM orders WHERE merchant_trade_no = ?",
            (merchant_trade_no,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def update_order_checkout(merchant_trade_no: str, prepay_id: str, checkout_url: str, raw_json: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE orders
            SET prepay_id = ?, checkout_url = ?, raw_json = ?
            WHERE merchant_trade_no = ?
            """,
            (prepay_id, checkout_url, raw_json, merchant_trade_no),
        )
        conn.commit()
    finally:
        conn.close()


def get_order(merchant_trade_no: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM orders WHERE merchant_trade_no = ?",
            (merchant_trade_no,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_order_closed(merchant_trade_no: str, raw_json: str = "") -> None:
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE orders
            SET status = CASE WHEN status = 'paid' THEN status ELSE 'closed' END,
                raw_json = CASE WHEN ? != '' THEN ? ELSE raw_json END
            WHERE merchant_trade_no = ?
            """,
            (raw_json, raw_json, merchant_trade_no),
        )
        conn.commit()
    finally:
        conn.close()


def fulfill_paid_order(merchant_trade_no: str, raw_json: str = "") -> dict | None:
    """Idempotent: paid lan sau khong cong them ngay."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT * FROM orders WHERE merchant_trade_no = ?",
            (merchant_trade_no,),
        ).fetchone()
        if not order:
            conn.rollback()
            return None
        if order["status"] == "paid":
            conn.commit()
            return dict(order)
        plan = plan_of(order["plan"])
        if not plan:
            conn.rollback()
            return None
        now = utcnow()
        paid_at = to_iso(now)
        conn.execute(
            """
            UPDATE orders
            SET status = 'paid', paid_at = ?, raw_json = CASE WHEN ? != '' THEN ? ELSE raw_json END
            WHERE id = ?
            """,
            (paid_at, raw_json, raw_json, order["id"]),
        )
        existing = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE username = ?",
            (order["username"],),
        ).fetchone()
        base = now
        if existing:
            try:
                current = parse_iso(existing["expires_at"])
                if current > base:
                    base = current
            except ValueError:
                pass
        expires_at = to_iso(base + timedelta(days=int(plan["days"])))
        conn.execute(
            """
            INSERT INTO subscriptions (username, plan, expires_at, last_order_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                plan = excluded.plan,
                expires_at = excluded.expires_at,
                last_order_id = excluded.last_order_id
            """,
            (order["username"], order["plan"], expires_at, order["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
