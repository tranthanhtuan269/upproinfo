"""Goi xem du lieu Awin. Database rieng, khong dung chung billing.db cua UpproInfo."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "awin_billing.db"

PLANS = {
    "month": {"id": "month", "amount": 100_000, "days": 30, "label": "Tháng", "currency": "VND"},
    "quarter": {"id": "quarter", "amount": 300_000, "days": 90, "label": "Quý", "currency": "VND"},
    "year": {"id": "year", "amount": 1_000_000, "days": 365, "label": "Năm", "currency": "VND"},
}
ADMINS = {"admin"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_vnd(amount: int | str) -> str:
    try:
        number = int(amount)
    except (TypeError, ValueError):
        return str(amount)
    return f"{number:,}".replace(",", ".")


def plan_of(plan_id: str) -> dict | None:
    return PLANS.get(str(plan_id or "").strip())


def is_admin(username: str | None) -> bool:
    return str(username or "").casefold() in ADMINS


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
            currency TEXT NOT NULL DEFAULT 'VND',
            merchant_trade_no TEXT NOT NULL UNIQUE,
            pay_content TEXT,
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
    conn.commit()
    return conn


def pay_code(order_id: int) -> str:
    return f"AW{int(order_id):08d}"


def create_order(username: str, plan_id: str, merchant_trade_no: str) -> dict:
    plan = plan_of(plan_id)
    if not plan:
        raise ValueError("plan")
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO orders (username, plan, amount, currency, merchant_trade_no, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (username, plan["id"], str(plan["amount"]), plan["currency"], merchant_trade_no, to_iso(utcnow())),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM orders WHERE merchant_trade_no = ?",
            (merchant_trade_no,),
        ).fetchone()
        order = dict(row)
        code = pay_code(int(order["id"]))
        conn.execute("UPDATE orders SET pay_content = ? WHERE id = ?", (code, order["id"]))
        conn.commit()
        order["pay_content"] = code
        return order
    finally:
        conn.close()


def update_checkout(merchant_trade_no: str, prepay_id: str, checkout_url: str, raw_json: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE orders SET prepay_id = ?, checkout_url = ?, raw_json = ?
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


def get_subscription(username: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def is_entitled(username: str | None) -> bool:
    if is_admin(username):
        return True
    if not username:
        return False
    sub = get_subscription(username)
    if not sub:
        return False
    try:
        return parse_iso(sub["expires_at"]) > utcnow()
    except ValueError:
        return False


def access_for(username: str | None) -> dict:
    admin = is_admin(username)
    sub = get_subscription(username or "") if username else None
    plan = ""
    expires = ""
    if sub:
        try:
            if parse_iso(sub["expires_at"]) > utcnow():
                plan = str(sub.get("plan") or "")
                expires = sub["expires_at"]
        except ValueError:
            plan = ""
    if admin and not plan:
        plan = "year"
    return {"is_admin": admin, "plan": plan, "expires_at": expires, "entitled": admin or bool(plan)}


def find_vietqr_order(content: str = "", order_id: str = "") -> dict | None:
    blob = f"{content} {order_id}".upper()
    if "AW" not in blob:
        return None
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'pending' ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        code = str(row["pay_content"] or "").upper()
        if code and code in blob:
            return dict(row)
    return None


def fulfill_paid_order(merchant_trade_no: str, raw_json: str = "") -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM orders WHERE merchant_trade_no = ?",
            (merchant_trade_no,),
        ).fetchone()
        if not row:
            return None
        order = dict(row)
        if order["status"] != "paid":
            conn.execute(
                "UPDATE orders SET status = 'paid', paid_at = ?, raw_json = ? WHERE merchant_trade_no = ?",
                (to_iso(utcnow()), raw_json, merchant_trade_no),
            )
        plan = plan_of(order["plan"]) or {"days": 30, "id": order["plan"]}
        current = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE username = ?",
            (order["username"],),
        ).fetchone()
        start = utcnow()
        if current:
            try:
                existing = parse_iso(current["expires_at"])
                if existing > start:
                    start = existing
            except ValueError:
                start = utcnow()
        expires = start + timedelta(days=int(plan["days"]))
        conn.execute(
            """
            INSERT INTO subscriptions (username, plan, expires_at, last_order_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                plan = excluded.plan,
                expires_at = excluded.expires_at,
                last_order_id = excluded.last_order_id
            """,
            (order["username"], plan["id"], to_iso(expires), order["id"]),
        )
        conn.commit()
        order["status"] = "paid"
        return order
    finally:
        conn.close()
