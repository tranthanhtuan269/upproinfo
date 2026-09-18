#!/usr/bin/env python3
"""
Quet toan bo Traffic (AITDK / SimilarWeb) va Google Ads Transparency cho hon 9.000 brand
Luu tru vao SQLite: /var/www/upproinfo/data/uppromote.db (bang brand_metrics)
"""

import os
import sys
import json
import time
import string
import random
import hashlib
import sqlite3
import logging
from pathlib import Path
from urllib.parse import urlparse
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = "/var/www/upproinfo/data/uppromote.db"
PROGRESS_FILE = "/var/www/upproinfo/data/scan_metrics_progress.json"
LOG_FILE = "/var/www/upproinfo/data/scan_metrics.log"
SECRET_KEY = "541737bb-02ce-4fb6-8157-3c7166873777"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS brand_metrics (
        shop_id INTEGER PRIMARY KEY,
        domain TEXT,
        traffic_m1 INTEGER DEFAULT 0,
        traffic_m2 INTEGER DEFAULT 0,
        traffic_m3 INTEGER DEFAULT 0,
        monthly_visits_json TEXT,
        bounce_rate REAL DEFAULT 0,
        global_rank INTEGER DEFAULT 0,
        country_rank INTEGER DEFAULT 0,
        top_keywords_json TEXT,
        google_ads_running INTEGER DEFAULT 0,
        google_advertisers_count INTEGER DEFAULT 0,
        google_advertisers_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()

def clean_domain(url):
    if not url:
        return ''
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    try:
        netloc = urlparse(url).netloc
        if not netloc:
            netloc = url.split('/')[0]
        netloc = netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        netloc = netloc.split(':')[0]
        return netloc
    except Exception:
        return ''

def get_aitdk_bulk(domains):
    """Goi API AITDK SSE cho 1 batch domains (toi da 20 domain/request)."""
    chars = string.ascii_letters + string.digits
    nonce = ''.join(random.choice(chars) for _ in range(16))
    timestamp = str(int(time.time()))
    
    domain_param = ','.join(domains)
    params = {
        'domain': domain_param,
        'view': 'summary',
        'stream': 'true'
    }
    
    keys = sorted(params.keys())
    normalized_q = urllib.parse.urlencode([(k, str(params[k])) for k in keys])
    sig_str = f'GET\n/api/v1/bulk\n{normalized_q}\n{timestamp}\n{nonce}\n{SECRET_KEY}'
    signature = hashlib.sha256(sig_str.encode('utf-8')).hexdigest()
    
    params['timestamp'] = timestamp
    params['nonce'] = nonce
    params['signature'] = signature
    
    url = 'https://wapi.aitdk.com/api/v1/bulk?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/event-stream'
    })
    
    results = {}
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            current_event = None
            for line in resp:
                line = line.decode('utf-8', errors='ignore').strip()
                if line.startswith('event:'):
                    current_event = line.split(':', 1)[1].strip()
                elif line.startswith('data:') and current_event == 'traffic':
                    data_str = line[5:].strip()
                    if data_str:
                        item = json.loads(data_str)
                        if isinstance(item, dict) and 'domain' in item:
                            results[item['domain']] = item
    except Exception as e:
        logging.warning(f"AITDK error for batch ({len(domains)} domains): {e}")
    return results

def search_google_ads(domain):
    """Kiem tra xem domain co dang chay Google Ads khong qua Ads Transparency."""
    url = 'https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser='
    payload = {
        '2': 40,
        '3': {
            '12': {
                '1': domain,
                '2': True
            }
        },
        '7': {
            '1': 1
        }
    }
    data = urllib.parse.urlencode({'f.req': json.dumps(payload)}).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Content-Type': 'application/x-www-form-urlencoded'
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode('utf-8')
            res = json.loads(content)
            if not isinstance(res, dict):
                return False, 0, []
            creatives = res.get('1', []) or []
            adv_ids = list(set(c.get('1') for c in creatives if isinstance(c, dict) and c.get('1')))
            return len(creatives) > 0, len(adv_ids), adv_ids
    except Exception:
        return False, 0, []

def process_batch(items):
    """
    items: list of (shop_id, domain)
    """
    domains = [d for sid, d in items]
    
    # 1. Fetch Traffic from AITDK for this batch
    traffic_map = get_aitdk_bulk(domains)
    
    # 2. Fetch Google Ads in parallel
    ads_map = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_domain = {executor.submit(search_google_ads, d): d for d in domains}
        for future in as_completed(future_to_domain):
            d = future_to_domain[future]
            try:
                ads_map[d] = future.result()
            except Exception:
                ads_map[d] = (False, 0, [])

    # 3. Parse and prepare records
    records = []
    for sid, d in items:
        domain_item = traffic_map.get(d) or {}
        t_info = domain_item.get('data') or {}
        overview = t_info.get('overview') or {}
        monthly = t_info.get('monthlyVisits') or {}
        keywords = t_info.get('topKeywords') or []
        
        # Sort months to get 3 latest months
        m1, m2, m3 = 0, 0, 0
        if isinstance(monthly, dict) and monthly:
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

        bounce_rate = float(overview.get('bounceRate') or 0)
        global_rank = int(overview.get('globalRank') or 0)
        country_rank = int(overview.get('countryRank') or 0)

        ads_running, ads_count, ads_ids = ads_map.get(d, (False, 0, []))

        records.append((
            sid,
            d,
            int(m1),
            int(m2),
            int(m3),
            json.dumps(monthly) if monthly else '{}',
            bounce_rate,
            global_rank,
            country_rank,
            json.dumps(keywords) if keywords else '[]',
            1 if ads_running else 0,
            ads_count,
            json.dumps(ads_ids) if ads_ids else '[]'
        ))

    return records

def main():
    init_db()
    logging.info("Starting Batch Scan for Brand Metrics...")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT shop_id, website FROM details WHERE website IS NOT NULL AND website != \'\'')
    all_rows = cur.fetchall()

    # Get already processed shop_ids
    cur.execute('SELECT shop_id FROM brand_metrics')
    processed_ids = set(r[0] for r in cur.fetchall())
    conn.close()

    tasks = []
    for sid, w in all_rows:
        if sid in processed_ids:
            continue
        d = clean_domain(w)
        if d and '.' in d:
            tasks.append((sid, d))

    logging.info(f"Total brands: {len(all_rows)}. Already processed: {len(processed_ids)}. Remaining to scan: {len(tasks)}")

    if not tasks:
        logging.info("All brands are already processed!")
        return

    # Process in batches of 20
    BATCH_SIZE = 20
    batches = [tasks[i:i + BATCH_SIZE] for i in range(0, len(tasks), BATCH_SIZE)]
    
    total_processed = len(processed_ids)
    total_tasks = len(all_rows)

    start_time = time.time()
    for b_idx, b in enumerate(batches, 1):
        try:
            records = process_batch(b)
            if records:
                db_conn = sqlite3.connect(DB_PATH)
                db_cur = db_conn.cursor()
                db_cur.executemany('''
                INSERT OR REPLACE INTO brand_metrics (
                    shop_id, domain, traffic_m1, traffic_m2, traffic_m3,
                    monthly_visits_json, bounce_rate, global_rank, country_rank,
                    top_keywords_json, google_ads_running, google_advertisers_count,
                    google_advertisers_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', records)
                db_conn.commit()
                db_conn.close()

            total_processed += len(b)
            pct = round(total_processed * 100 / total_tasks, 2)
            elapsed = round(time.time() - start_time, 1)
            logging.info(f"Batch {b_idx}/{len(batches)} done (+{len(b)} brands). Total: {total_processed}/{total_tasks} ({pct}%). Elapsed: {elapsed}s")

            # Update progress file
            with open(PROGRESS_FILE, "w", encoding="utf-8") as pf:
                json.dump({
                    "total": total_tasks,
                    "processed": total_processed,
                    "percent": pct,
                    "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
                }, pf, indent=2)

            time.sleep(0.5)
        except Exception as e:
            logging.error(f"Error in batch {b_idx}: {e}", exc_info=True)
            time.sleep(2.0)

    logging.info("Scan completed successfully!")

if __name__ == "__main__":
    main()
