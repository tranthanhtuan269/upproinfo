#!/usr/bin/env python3
"""
GoAffPro Store Metrics Scanner (goaff_scan_metrics.py)
Quet chi so Traffic 3 thang (AITDK) va Google Ads Transparency cho cac store GoAffPro.

Cac lenh su dung:
    python goaff_scan_metrics.py                 # Quet backlog cac store chua co metrics
    python goaff_scan_metrics.py --fresh         # Quet lai toan bo tat ca store tu dau
    python goaff_scan_metrics.py --limit 50      # Quet thu nghiem 50 store
    python goaff_scan_metrics.py 7194108 7194087 # Quet store chi dinh theo ID
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sqlite3
import string
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "goaffpro.db"
PROGRESS_FILE = DATA_DIR / "goaff_metrics_progress.json"
LOG_FILE = DATA_DIR / "goaff_metrics.log"

SECRET_PATHS = [
    ROOT_DIR / "scan_metrics.secret.json",
    ROOT_DIR.parent / "upproinfo" / "scan_metrics.secret.json",
    Path("/var/www/upproinfo/scan_metrics.secret.json"),
    Path("/var/www/goaffpro/scan_metrics.secret.json"),
]

BATCH_SIZE = 20
logger = logging.getLogger("goaff_scan_metrics")


def setup_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(stream)


def load_secret() -> str:
    env = (os.environ.get("AITDK_SECRET") or "").strip()
    if env:
        return env
    for p in SECRET_PATHS:
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                secret = str(raw.get("aitdk_secret") or "").strip()
                if secret:
                    return secret
            except Exception:
                continue
    raise RuntimeError("Khong tim thay AITDK secret trong scan_metrics.secret.json hoac bien moi truong AITDK_SECRET.")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_metrics_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS store_metrics (
        store_id INTEGER PRIMARY KEY,
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
        google_ads_running INTEGER DEFAULT 0,
        google_advertisers_count INTEGER DEFAULT 0,
        google_advertisers_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_store_metrics_traffic ON store_metrics(traffic_m3)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_store_metrics_updated ON store_metrics(updated_at)")
    conn.commit()


def clean_domain(url: Any) -> str:
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    try:
        netloc = urllib.parse.urlparse(text).netloc or text.split("/")[0]
        netloc = netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.split(":")[0]
    except Exception:
        return ""


def get_aitdk_bulk(domains: list[str], secret: str, max_retries: int = 3) -> dict[str, Any]:
    chars = string.ascii_letters + string.digits
    for attempt in range(max_retries):
        nonce = "".join(random.choice(chars) for _ in range(16))
        timestamp = str(int(time.time()))
        params = {
            "domain": ",".join(domains),
            "view": "summary",
            "stream": "true",
        }
        keys = sorted(params.keys())
        normalized_q = urllib.parse.urlencode([(key, str(params[key])) for key in keys])
        sig_str = f"GET\n/api/v1/bulk\n{normalized_q}\n{timestamp}\n{nonce}\n{secret}"
        signature = hashlib.sha256(sig_str.encode("utf-8")).hexdigest()
        params["timestamp"] = timestamp
        params["nonce"] = nonce
        params["signature"] = signature
        url = "https://wapi.aitdk.com/api/v1/bulk?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/event-stream",
            },
        )
        results: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                current_event = None
                for line in resp:
                    line = line.decode("utf-8", errors="ignore").strip()
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and current_event == "traffic":
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        item = json.loads(data_str)
                        if isinstance(item, dict) and item.get("domain"):
                            results[item["domain"]] = item
            return results
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries - 1:
                wait_time = (attempt + 1) * 3.0
                logger.warning("AITDK 429 Rate Limit (lan %s/%s). Nghi %ss roi thu lai...", attempt + 1, max_retries, wait_time)
                time.sleep(wait_time)
                continue
            logger.warning("AITDK error for batch (%s domains): %s", len(domains), exc)
            break
        except Exception as exc:
            logger.warning("AITDK error for batch (%s domains): %s", len(domains), exc)
            break
    return results


def search_google_ads(domain: str) -> tuple[bool, int, list[Any]]:
    url = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser="
    payload = {"2": 40, "3": {"12": {"1": domain, "2": True}}, "7": {"1": 1}}
    data = urllib.parse.urlencode({"f.req": json.dumps(payload)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if not isinstance(res, dict):
                return False, 0, []
            creatives = res.get("1", []) or []
            adv_ids = list(
                {item.get("1") for item in creatives if isinstance(item, dict) and item.get("1")}
            )
            return len(creatives) > 0, len(adv_ids), adv_ids
    except Exception:
        return False, 0, []


def month_values(monthly: dict[str, Any]) -> tuple[int, int, int]:
    m1 = m2 = m3 = 0
    if not isinstance(monthly, dict) or not monthly:
        return m1, m2, m3
    sorted_months = sorted(monthly.keys())
    if len(sorted_months) >= 3:
        m1 = monthly.get(sorted_months[-3], 0) or 0
        m2 = monthly.get(sorted_months[-2], 0) or 0
        m3 = monthly.get(sorted_months[-1], 0) or 0
    elif len(sorted_months) == 2:
        m2 = monthly.get(sorted_months[-2], 0) or 0
        m3 = monthly.get(sorted_months[-1], 0) or 0
    elif len(sorted_months) == 1:
        m3 = monthly.get(sorted_months[-1], 0) or 0
    return int(m1), int(m2), int(m3)


def process_batch(items: list[tuple[int, str]], secret: str) -> list[tuple[Any, ...]]:
    domains = [domain for _sid, domain in items]
    traffic_map = get_aitdk_bulk(domains, secret)

    records = []
    for sid, domain in items:
        domain_item = traffic_map.get(domain) or {}
        t_info = domain_item.get("data") or {}
        overview = t_info.get("overview") or {}
        monthly = t_info.get("monthlyVisits") or {}
        keywords = t_info.get("topKeywords") or []
        m1, m2, m3 = month_values(monthly if isinstance(monthly, dict) else {})
        bounce_rate = float(overview.get("bounceRate") or 0)
        time_on_site = float(overview.get("timeOnSite") or 0)
        pages_per_visit = float(overview.get("pagesPerVisit") or 0)
        global_rank = int(overview.get("globalRank") or 0)
        country_rank = int(overview.get("countryRank") or 0)

        records.append((
            sid,
            domain,
            m1,
            m2,
            m3,
            json.dumps(monthly) if monthly else "",
            bounce_rate,
            time_on_site,
            pages_per_visit,
            global_rank,
            country_rank,
            json.dumps(keywords) if keywords else "",
            0,
            0,
            "",
        ))
    return records


def save_records(records: list[tuple[Any, ...]]) -> None:
    if not records:
        return
    conn = connect()
    try:
        ensure_metrics_table(conn)
        conn.executemany("""
            INSERT INTO store_metrics (
                store_id, domain, traffic_m1, traffic_m2, traffic_m3,
                monthly_visits_json, bounce_rate, time_on_site, pages_per_visit,
                global_rank, country_rank, top_keywords_json, google_ads_running,
                google_advertisers_count, google_advertisers_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(store_id) DO UPDATE SET
                domain = excluded.domain,
                traffic_m1 = excluded.traffic_m1,
                traffic_m2 = excluded.traffic_m2,
                traffic_m3 = excluded.traffic_m3,
                monthly_visits_json = excluded.monthly_visits_json,
                bounce_rate = excluded.bounce_rate,
                time_on_site = excluded.time_on_site,
                pages_per_visit = excluded.pages_per_visit,
                global_rank = excluded.global_rank,
                country_rank = excluded.country_rank,
                top_keywords_json = excluded.top_keywords_json,
                google_ads_running = excluded.google_ads_running,
                google_advertisers_count = excluded.google_advertisers_count,
                google_advertisers_json = excluded.google_advertisers_json,
                updated_at = CURRENT_TIMESTAMP
        """, records)
        conn.commit()
    finally:
        conn.close()


def load_tasks(store_ids: Iterable[int] | None = None, force: bool = False) -> list[tuple[int, str]]:
    conn = connect()
    try:
        ensure_metrics_table(conn)
        ids = [int(sid) for sid in (store_ids or [])]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, domain, website FROM stores WHERE id IN ({placeholders}) AND status = 'active'",
                ids
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, domain, website FROM stores WHERE status = 'active'"
            ).fetchall()

        processed: set[int] = set()
        if not force and not ids:
            processed = {
                int(row[0]) for row in conn.execute(
                    "SELECT store_id FROM store_metrics WHERE time_on_site > 0 OR (bounce_rate > 0 AND updated_at > datetime('now', '-7 days'))"
                )
            }
    finally:
        conn.close()

    tasks: list[tuple[int, str]] = []
    for sid, domain, website in rows:
        sid = int(sid)
        if sid in processed:
            continue
        dom = clean_domain(domain or website)
        if dom:
            tasks.append((sid, dom))
    return tasks


def scan_tasks(tasks: list[tuple[int, str]]) -> int:
    if not tasks:
        logger.info("Khong co store nao can quet metrics.")
        return 0
    secret = load_secret()
    batches = [tasks[i : i + BATCH_SIZE] for i in range(0, len(tasks), BATCH_SIZE)]
    start = time.time()
    done = 0
    logger.info("Bat dau quet metrics %s store (tong %s batch)...", len(tasks), len(batches))
    for index, batch in enumerate(batches, start=1):
        try:
            records = process_batch(batch, secret)
            save_records(records)
            done += len(batch)
            pct = round(done * 100 / len(tasks), 2)
            elapsed = round(time.time() - start, 1)
            logger.info(
                "Batch %s/%s (+%s). %s/%s (%s%%) %ss",
                index,
                len(batches),
                len(batch),
                done,
                len(tasks),
                pct,
                elapsed,
            )
            PROGRESS_FILE.write_text(
                json.dumps(
                    {
                        "total": len(tasks),
                        "processed": done,
                        "percent": pct,
                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            time.sleep(1.2)  # Gian cach an toan tranh Rate Limit 429
        except Exception:
            logger.exception("Loi o batch %s", index)
            time.sleep(2.0)
    logger.info("Hoan tat quet metrics: %s store.", done)
    return done


def main() -> None:
    setup_logging()
    force = "--refresh" in sys.argv or "--fresh" in sys.argv or "--all" in sys.argv or "--force" in sys.argv
    limit = 0
    skip_next = False
    raw_ids = []
    for i, arg in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if arg == "--limit":
            try:
                limit = int(sys.argv[i + 1])
                skip_next = True
            except (ValueError, IndexError):
                limit = 50
        elif arg.isdigit():
            raw_ids.append(arg)
    if raw_ids:
        store_ids = [int(arg) for arg in raw_ids]
        logger.info("Quet metrics %s store_id chi dinh: %s (force=%s).", len(store_ids), store_ids[:10], force)
        tasks = load_tasks(store_ids=store_ids, force=True)
    else:
        logger.info("Quet metrics backlog (force=%s, limit=%s).", force, limit)
        tasks = load_tasks(store_ids=None, force=force)

    if limit > 0 and len(tasks) > limit:
        tasks = tasks[:limit]

    scan_tasks(tasks)


if __name__ == "__main__":
    main()
