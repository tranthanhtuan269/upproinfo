import time
import sqlite3
from pathlib import Path
from flask import request, abort, session, Response

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "billing.db"

def get_real_ip() -> str:
    # CF-Connecting-IP is set by Cloudflare, verified via Nginx
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"

def is_ip_blacklisted(ip: str) -> bool:
    now = int(time.time())
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
        row = conn.execute(
            "SELECT blocked_until FROM ip_blacklist WHERE ip = ?", (ip,)
        ).fetchone()
        conn.close()
        if row:
            if row[0] > now:
                return True
            else:
                # Expired ban, unban
                conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
                conn.execute("DELETE FROM ip_blacklist WHERE ip = ?", (ip,))
                conn.commit()
                conn.close()
    except Exception:
        pass
    return False

def blacklist_ip(ip: str, reason: str, ban_seconds: int = 86400):
    now = int(time.time())
    until = now + ban_seconds
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
        conn.execute(
            """
            INSERT OR REPLACE INTO ip_blacklist (ip, reason, blocked_until, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (ip, reason, until)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """Fixed-window counter in SQLite. Returns True if allowed, False if exceeded."""
    now = int(time.time())
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
        row = conn.execute(
            "SELECT count, window_start FROM rate_limits WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO rate_limits (key, count, window_start) VALUES (?, 1, ?)",
                (key, now)
            )
            conn.commit()
            conn.close()
            return True

        count, window_start = row
        if now - window_start >= window_seconds:
            conn.execute(
                "UPDATE rate_limits SET count = 1, window_start = ? WHERE key = ?",
                (now, key)
            )
            conn.commit()
            conn.close()
            return True

        if count >= max_requests:
            conn.close()
            return False

        conn.execute(
            "UPDATE rate_limits SET count = count + 1 WHERE key = ?", (key,)
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return True
