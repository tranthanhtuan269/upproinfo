"""Quet traffic (AITDK / SimilarWeb) va Google Ads Transparency.

Luu bang brand_metrics trong data/uppromote.db.
Hang ngay: daily_sync.py goi scan_shop_ids() cho brand moi.
Backlog: python scan_metrics.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import string
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from storage import DATA_DIR, DB_PATH, connect, ensure_metrics_table, upsert_metrics_record

PROGRESS_FILE = DATA_DIR / "scan_metrics_progress.json"
LOG_FILE = DATA_DIR / "scan_metrics.log"
SECRET_PATH = Path(__file__).resolve().parent / "scan_metrics.secret.json"
BATCH_SIZE = 20

logger = logging.getLogger("scan_metrics")


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    if SECRET_PATH.exists():
        try:
            raw = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("scan_metrics.secret.json khong hop le.") from exc
        secret = str(raw.get("aitdk_secret") or "").strip()
        if secret:
            return secret
    raise RuntimeError(
        "Thieu AITDK secret. Tao scan_metrics.secret.json "
        '{"aitdk_secret":"..."} hoac set AITDK_SECRET."'
    )


def clean_domain(url: Any) -> str:
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    try:
        netloc = urlparse(text).netloc or text.split("/")[0]
        netloc = netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.split(":")[0]
    except Exception:
        return ""


def get_aitdk_bulk(domains: list[str], secret: str) -> dict[str, Any]:
    chars = string.ascii_letters + string.digits
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
    except Exception as exc:
        logger.warning("AITDK error for batch (%s domains): %s", len(domains), exc)
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
    ads_map: dict[str, tuple[bool, int, list[Any]]] = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_domain = {executor.submit(search_google_ads, domain): domain for domain in domains}
        for future in as_completed(future_to_domain):
            domain = future_to_domain[future]
            try:
                ads_map[domain] = future.result()
            except Exception:
                ads_map[domain] = (False, 0, [])

    records = []
    for sid, domain in items:
        domain_item = traffic_map.get(domain) or {}
        t_info = domain_item.get("data") or {}
        overview = t_info.get("overview") or {}
        monthly = t_info.get("monthlyVisits") or {}
        keywords = t_info.get("topKeywords") or []
        m1, m2, m3 = month_values(monthly if isinstance(monthly, dict) else {})
        ads_running, ads_count, ads_ids = ads_map.get(domain, (False, 0, []))
        records.append(
            (
                sid,
                domain,
                m1,
                m2,
                m3,
                json.dumps(monthly) if monthly else "{}",
                float(overview.get("bounceRate") or 0),
                int(overview.get("globalRank") or 0),
                int(overview.get("countryRank") or 0),
                json.dumps(keywords) if keywords else "[]",
                1 if ads_running else 0,
                ads_count,
                json.dumps(ads_ids) if ads_ids else "[]",
            )
        )
    return records


def save_records(records: list[tuple[Any, ...]]) -> None:
    if not records:
        return
    conn = connect()
    try:
        ensure_metrics_table(conn)
        for record in records:
            upsert_metrics_record(conn, record)
        conn.commit()
    finally:
        conn.close()


def load_tasks(shop_ids: Iterable[int] | None = None, force: bool = False) -> list[tuple[int, str]]:
    conn = connect()
    try:
        ensure_metrics_table(conn)
        ids = [int(sid) for sid in (shop_ids or [])]
        if ids:
            tasks: list[tuple[int, str]] = []
            for offset in range(0, len(ids), 400):
                chunk = ids[offset : offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT shop_id, website FROM details
                    WHERE shop_id IN ({placeholders})
                      AND website IS NOT NULL AND website != ''
                    """,
                    chunk,
                ).fetchall()
                tasks.extend((int(row[0]), row[1]) for row in rows)
        else:
            rows = conn.execute(
                "SELECT shop_id, website FROM details WHERE website IS NOT NULL AND website != ''"
            ).fetchall()
            tasks = [(int(row[0]), row[1]) for row in rows]
        processed: set[int] = set()
        if not force:
            processed = {int(row[0]) for row in conn.execute("SELECT shop_id FROM brand_metrics")}
    finally:
        conn.close()

    out: list[tuple[int, str]] = []
    for sid, website in tasks:
        if sid in processed:
            continue
        domain = clean_domain(website)
        if domain and "." in domain:
            out.append((sid, domain))
    return out


def scan_tasks(tasks: list[tuple[int, str]]) -> int:
    setup_logging()
    if not tasks:
        logger.info("Khong co brand can quet metrics.")
        return 0
    secret = load_secret()
    batches = [tasks[i : i + BATCH_SIZE] for i in range(0, len(tasks), BATCH_SIZE)]
    start = time.time()
    done = 0
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
            time.sleep(0.5)
        except Exception:
            logger.exception("Error in batch %s", index)
            time.sleep(2.0)
    logger.info("Scan metrics xong: %s brand.", done)
    return done


def scan_shop_ids(shop_ids: Iterable[int], force: bool = False) -> int:
    return scan_tasks(load_tasks(shop_ids=shop_ids, force=force))


def scan_pending(force: bool = False) -> int:
    return scan_tasks(load_tasks(shop_ids=None, force=force))


def main() -> None:
    setup_logging()
    force = "--refresh" in sys.argv
    ids: list[int] = []
    args = [item for item in sys.argv[1:] if item not in {"--refresh"}]
    for item in args:
        try:
            ids.append(int(item))
        except ValueError:
            continue
    if ids:
        logger.info("Quet metrics %s shop_id (force=%s).", len(ids), force)
        scan_shop_ids(ids, force=force)
        return
    logger.info("Quet metrics backlog (force=%s).", force)
    scan_pending(force=force)


if __name__ == "__main__":
    main()
