"""Goi thang/nam, don hang va han su dung (SQLite rieng)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "billing.db"
ADMIN_NAMES = {"admin"}

PLANS = {
    "month": {
        "id": "month",
        "amount": 100000,
        "days": 30,
        "currency": "VND",
        "goods_id": "plan_month",
    },
    "quarter": {
        "id": "quarter",
        "amount": 300000,
        "days": 90,
        "currency": "VND",
        "goods_id": "plan_quarter",
    },
    "year": {
        "id": "year",
        "amount": 1000000,
        "days": 365,
        "currency": "VND",
        "goods_id": "plan_year",
    },
}


def pay_code(order_id: int | str) -> str:
    try:
        return str(int(order_id))
    except (TypeError, ValueError):
        return str(order_id or "").strip()


def format_vnd(amount: int | str) -> str:
    try:
        number = int(amount)
    except (TypeError, ValueError):
        return str(amount)
    return f"{number:,}".replace(",", ".")


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
            currency TEXT NOT NULL DEFAULT 'VND',
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vietqr_tokens (
            token TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vietqr_callbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transactionid TEXT NOT NULL UNIQUE,
            referencenumber TEXT,
            order_id TEXT,
            amount TEXT,
            trans_type TEXT,
            content TEXT,
            bankaccount TEXT,
            transactiontime TEXT,
            status TEXT NOT NULL,
            merchant_trade_no TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'success',
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            key TEXT PRIMARY KEY,
            count INTEGER NOT NULL,
            window_start INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ip_blacklist (
            ip TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            blocked_until INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    if "pay_content" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN pay_content TEXT")
    if "received_by" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN received_by TEXT")
    if "received_at" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN received_at TEXT")
    conn.commit()
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
            (username, plan["id"], str(plan["amount"]), plan["currency"], merchant_trade_no, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM orders WHERE merchant_trade_no = ?",
            (merchant_trade_no,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def update_order_checkout(
    merchant_trade_no: str,
    prepay_id: str,
    checkout_url: str,
    raw_json: str,
    pay_content: str = "",
) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE orders
            SET prepay_id = ?, checkout_url = ?, raw_json = ?, pay_content = ?
            WHERE merchant_trade_no = ?
            """,
            (prepay_id, checkout_url, raw_json, pay_content, merchant_trade_no),
        )
        conn.commit()
    finally:
        conn.close()


def save_vietqr_token(token: str, expires_at: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM vietqr_tokens WHERE expires_at < ?", (to_iso(utcnow()),))
        conn.execute(
            "INSERT OR REPLACE INTO vietqr_tokens (token, expires_at) VALUES (?, ?)",
            (token, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def vietqr_token_ok(token: str) -> bool:
    if not token:
        return False
    conn = connect()
    try:
        row = conn.execute(
            "SELECT expires_at FROM vietqr_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return False
        try:
            return parse_iso(row["expires_at"]) > utcnow()
        except ValueError:
            return False
    finally:
        conn.close()


def expire_stale_orders(minutes: int = 10080) -> None:
    cutoff = to_iso(utcnow() - timedelta(minutes=minutes))
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE orders
            SET status = 'closed'
            WHERE status = 'pending' AND created_at < ?
            """,
            (cutoff,),
        )
        conn.commit()
    finally:
        conn.close()


def _pay_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def pay_needles(*texts: str) -> set[str]:
    keys: set[str] = set()
    for text in texts:
        raw = str(text or "").strip().upper()
        if not raw:
            continue
        keys.add(raw)
        compact = _pay_key(raw)
        if compact:
            keys.add(compact)
        for part in re.split(r"[^A-Z0-9]+", raw):
            if len(part) >= 4:
                keys.add(part)
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            keys.add(digits)
            try:
                number = int(digits)
                keys.add(str(number))
                keys.add(f"{number:05d}")
            except ValueError:
                pass
    return {key for key in keys if key}


def get_vietqr_callback(transactionid: str) -> dict | None:
    key = str(transactionid or "").strip()
    if not key:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM vietqr_callbacks WHERE transactionid = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_vietqr_callback(payload: dict) -> dict:
    now = to_iso(utcnow())
    transactionid = str(payload.get("transactionid") or "").strip()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO vietqr_callbacks (
                transactionid, referencenumber, order_id, amount, trans_type, content,
                bankaccount, transactiontime, status, merchant_trade_no, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transactionid) DO UPDATE SET
                referencenumber = excluded.referencenumber,
                order_id = excluded.order_id,
                amount = excluded.amount,
                trans_type = excluded.trans_type,
                content = excluded.content,
                bankaccount = excluded.bankaccount,
                transactiontime = excluded.transactiontime,
                status = excluded.status,
                merchant_trade_no = excluded.merchant_trade_no,
                raw_json = excluded.raw_json
            """,
            (
                transactionid,
                str(payload.get("referencenumber") or ""),
                str(payload.get("order_id") or ""),
                str(payload.get("amount") or ""),
                str(payload.get("trans_type") or ""),
                str(payload.get("content") or ""),
                str(payload.get("bankaccount") or ""),
                str(payload.get("transactiontime") or ""),
                str(payload.get("status") or "received"),
                str(payload.get("merchant_trade_no") or ""),
                str(payload.get("raw_json") or ""),
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM vietqr_callbacks WHERE transactionid = ?",
            (transactionid,),
        ).fetchone()
        return dict(row) if row else payload
    finally:
        conn.close()


def find_vietqr_order(content: str = "", order_id: str = "") -> dict | None:
    needles = pay_needles(content, order_id)
    if not needles:
        return None
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    matches: list[dict] = []
    for row in rows:
        item = dict(row)
        haystack = pay_needles(
            item.get("pay_content") or "",
            item.get("merchant_trade_no") or "",
            str(item.get("id") or ""),
            pay_code(item.get("id") or ""),
        )
        if needles.intersection(haystack):
            matches.append(item)
            continue
        if any(
            (needle in hay or hay in needle)
            for needle in needles
            for hay in haystack
            if len(needle) >= 8 and len(hay) >= 8
        ):
            matches.append(item)
    rank = {"pending": 0, "paid": 1, "closed": 2}
    matches.sort(key=lambda item: rank.get(str(item.get("status") or ""), 9))
    return matches[0] if matches else None


def fulfill_vietqr_payment(
    content: str = "",
    order_id: str = "",
    amount: int | None = None,
    raw_json: str = "",
) -> dict | None:
    expire_stale_orders()
    order = find_vietqr_order(content, order_id)
    if not order:
        return None
    if amount is not None:
        try:
            if int(order["amount"]) != int(amount):
                return None
        except (TypeError, ValueError):
            return None
    return fulfill_paid_order(order["merchant_trade_no"], raw_json)


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


def format_local(value: str) -> str:
    if not value:
        return ""
    try:
        return parse_iso(value).astimezone(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(value)


def list_orders(limit: int = 80) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        items = [dict(row) for row in rows]
    finally:
        conn.close()
    for item in items:
        item["created_local"] = format_local(str(item.get("created_at") or ""))
        item["paid_local"] = format_local(str(item.get("paid_at") or ""))
        item["amount_text"] = format_vnd(item.get("amount") or 0)
    return items


def pending_count() -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE status = 'pending'").fetchone()
        return int(row["n"] if row else 0)
    finally:
        conn.close()


def mark_received(merchant_trade_no: str, admin_user: str) -> dict | None:
    trade = str(merchant_trade_no or "").strip()
    if not trade:
        return None
    order = get_order(trade)
    if not order:
        return None
    fulfilled = fulfill_paid_order(trade, "", source=f"Admin {admin_user} duyệt")
    if not fulfilled:
        return None
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE orders
            SET received_by = ?, received_at = ?
            WHERE merchant_trade_no = ?
            """,
            (str(admin_user or "").strip(), to_iso(utcnow()), trade),
        )
        conn.commit()
    finally:
        conn.close()
    return get_order(trade)


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


def fulfill_paid_order(merchant_trade_no: str, raw_json: str = "", source: str = "Tự động VietQR") -> dict | None:
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
        res = dict(row) if row else None
        if res:
            try:
                plan_label = "Gói 1 Tháng" if res.get("plan") == "month" else "Gói 1 Năm"
                add_user_notification(
                    username=res["username"],
                    title="Thanh toán thành công!",
                    message=f"Đơn hàng #{res['id']} ({plan_label}) đã được duyệt thành công! Hạn dùng VIP của bạn đã được gia hạn đến ngày {expires_at[:10]}.",
                    notif_type="success"
                )
            except Exception as e:
                pass
            try:
                import notifier
                notifier.notify_order_paid(res, expires_at=expires_at, source=source)
            except Exception:
                pass
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def list_subscriptions() -> dict[str, dict]:
    conn = connect()
    try:
        rows = conn.execute('SELECT username, plan, expires_at, last_order_id FROM subscriptions').fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            result[d['username']] = d
        return result
    finally:
        conn.close()

def set_subscription_days(username: str, days: int, plan: str = 'manual') -> dict:
    conn = connect()
    try:
        now = utcnow()
        base = now
        existing = conn.execute('SELECT expires_at FROM subscriptions WHERE username = ?', (username,)).fetchone()
        if existing:
            try:
                curr = parse_iso(existing['expires_at'])
                if curr > base:
                    base = curr
            except Exception:
                pass
        expires_at = to_iso(base + timedelta(days=max(1, int(days))))
        conn.execute('''
            INSERT INTO subscriptions (username, plan, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                plan = excluded.plan,
                expires_at = excluded.expires_at
        ''', (username, plan, expires_at))
        conn.commit()
        return {'username': username, 'plan': plan, 'expires_at': expires_at}
    finally:
        conn.close()

def revoke_subscription(username: str) -> None:
    conn = connect()
    try:
        conn.execute('DELETE FROM subscriptions WHERE username = ?', (username,))
        conn.commit()
    finally:
        conn.close()

def add_user_notification(username: str, title: str, message: str, notif_type: str = "success") -> int:
    conn = connect()
    try:
        now = to_iso(utcnow())
        cur = conn.execute(
            """
            INSERT INTO user_notifications (username, title, message, type, is_read, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (username, title, message, notif_type, now)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def get_user_notifications(username: str, limit: int = 15) -> list[dict]:
    if not username:
        return []
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, username, title, message, type, is_read, created_at
            FROM user_notifications
            WHERE username = ?
            ORDER BY id DESC LIMIT ?
            """,
            (username, max(1, int(limit)))
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["created_local"] = format_local(d.get("created_at") or "")
            items.append(d)
        return items
    finally:
        conn.close()

def get_unread_notification_count(username: str) -> int:
    if not username:
        return 0
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM user_notifications WHERE username = ? AND is_read = 0",
            (username,)
        ).fetchone()
        return int(row["n"] if row else 0)
    finally:
        conn.close()

def mark_notification_read(notif_id: int, username: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE user_notifications SET is_read = 1 WHERE id = ? AND username = ?",
            (notif_id, username)
        )
        conn.commit()
    finally:
        conn.close()

def mark_all_notifications_read(username: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE user_notifications SET is_read = 1 WHERE username = ?",
            (username,)
        )
        conn.commit()
    finally:
        conn.close()
