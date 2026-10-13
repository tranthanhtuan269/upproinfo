"""Quet lai toan bo offer-detail cho cac brand dang cho phep chay Search Ads / Google Ads.

Tu dong lay danh sach shop_id tu Database (hoac file JSON) dang co cờ allows_search_ads = 1.
Khong can truyen them bat ky tham so nao:
    python refresh_search_ads_brands.py

Hoac chay tu dau (bo qua checkpoint):
    python refresh_search_ads_brands.py --fresh
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scrape_brands import (
    ListingNotFound,
    MarketplaceClient,
    detail_url,
    safe_print,
    write_csv,
)
from storage import (
    BRANDS_DIR,
    DATA_DIR,
    clear_skipped,
    connect,
    export_details_csv,
    mark_skipped,
    save_detail_row,
    sync_allows_search_ads_from_json,
    sync_payment_methods_from_json,
)

PROGRESS_PATH = DATA_DIR / "refresh_ads_progress.json"
REPORT_PATH = DATA_DIR / "refresh_ads_latest.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_search_ads_shop_ids() -> list[int]:
    """Lay tat ca shop_id dang cho phep Search Ads tu database va JSON."""
    shop_ids: set[int] = set()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT shop_id FROM brand_metrics WHERE allows_search_ads = 1 OR search_ads_status IN ('no_google', 'brand_detects')"
        ).fetchall()
        shop_ids.update(int(r[0]) for r in rows)
    except Exception:
        pass
    finally:
        conn.close()

    # Quet them tu folder brands/*.json de dam bao khong bo sot
    if BRANDS_DIR.exists():
        for path in BRANDS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            channels = data.get("target_audience_customer_channels") or []
            has_sa = False
            if isinstance(channels, list):
                has_sa = any(
                    str(c).strip().lower() in ("search_ads", "search ads", "searchads")
                    for c in channels
                )
            if has_sa:
                try:
                    shop_ids.add(int(data.get("shop_id") or path.stem))
                except (ValueError, TypeError):
                    continue

    return sorted(shop_ids)


def load_progress(fresh: bool) -> dict[str, Any]:
    if fresh or not PROGRESS_PATH.exists():
        return {
            "started_at": now_iso(),
            "done_ids": [],
            "ok": 0,
            "missing": 0,
            "prohibited": 0,
            "cycle_done": False,
        }
    try:
        raw = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return load_progress(fresh=True)
    if not isinstance(raw, dict) or raw.get("cycle_done"):
        return load_progress(fresh=True)
    done = raw.get("done_ids") or []
    raw["done_ids"] = [int(sid) for sid in done]
    return raw


def save_progress(progress: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def refresh_one(client: MarketplaceClient, shop_id: int) -> str:
    try:
        payload = client.get_json(detail_url(shop_id), "detail")
    except ListingNotFound as exc:
        mark_skipped(shop_id, str(exc)[:240])
        return "missing"

    detail = payload.get("data")
    if not isinstance(detail, dict):
        detail = {"raw": payload, "shop_id": shop_id}
    if "shop_id" not in detail:
        detail["shop_id"] = shop_id

    # Luu va loc bo search_ads neu brand bat cam Google Ads
    save_detail_row(detail)
    clear_skipped(shop_id)

    # Kiem tra xem brand co bi loai bo Search Ads sau khi quet khong
    channels = detail.get("target_audience_customer_channels") or []
    still_allows = False
    if isinstance(channels, list):
        still_allows = any(
            str(c).strip().lower() in ("search_ads", "search ads", "searchads")
            for c in channels
        )

    if not still_allows:
        return "prohibited"
    return "ok"


def main() -> None:
    fresh = "--fresh" in sys.argv or "--refresh" in sys.argv
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    ads_ids = get_search_ads_shop_ids()
    total_target = len(ads_ids)
    if not total_target:
        safe_print("Khong tim thay brand nao dang cho phep Search Ads.")
        return

    progress = load_progress(fresh=fresh)
    done_set = set(progress.get("done_ids") or [])
    pending = [sid for sid in ads_ids if sid not in done_set]

    safe_print("=" * 60)
    safe_print(f"🎯 QUET LAI DETAIL CAC BRAND CHO PHEP SEARCH ADS")
    safe_print(f"- Tong so brand can quet: {total_target} brand")
    if done_set and not fresh:
        safe_print(f"- Da xong truoc do:      {len(done_set)} brand")
        safe_print(f"- Con lai can quet:      {len(pending)} brand")
    safe_print("=" * 60)

    if not pending:
        safe_print("✅ Toan bo brand Search Ads da duoc quet xong!")
        return

    client = MarketplaceClient()
    client.login()

    ok_count = int(progress.get("ok") or 0)
    missing_count = int(progress.get("missing") or 0)
    prohibited_count = int(progress.get("prohibited") or 0)

    try:
        for index, shop_id in enumerate(pending, start=1):
            status = refresh_one(client, shop_id)
            done_set.add(shop_id)
            progress["done_ids"] = list(done_set)

            if status == "missing":
                missing_count += 1
                safe_print(f"[{index}/{len(pending)}] Shop #{shop_id}: ❌ Khong con tren Marketplace (404)")
            elif status == "prohibited":
                prohibited_count += 1
                safe_print(f"[{index}/{len(pending)}] Shop #{shop_id}: ⚠️ PHAT HIEN CAM GOOGLE ADS -> Da loai bo Search Ads!")
            else:
                ok_count += 1
                safe_print(f"[{index}/{len(pending)}] Shop #{shop_id}: ✅ Van cho phep Search Ads")

            progress["ok"] = ok_count
            progress["missing"] = missing_count
            progress["prohibited"] = prohibited_count
            save_progress(progress)

            if index % 20 == 0:
                conn = connect()
                try:
                    export_details_csv(conn)
                finally:
                    conn.close()

    except KeyboardInterrupt:
        save_progress(progress)
        safe_print("\n⚠️ Dung giua chung. Chay lai 'python refresh_search_ads_brands.py' de tiep tuc.")
        raise SystemExit(1) from None

    progress["cycle_done"] = True
    save_progress(progress)

    # Dong bo lai toan bo database va CSV
    write_csv()
    sync_res = sync_allows_search_ads_from_json()
    sync_payment_methods_from_json()

    report = {
        "started_at": progress.get("started_at"),
        "finished_at": now_iso(),
        "total_target": total_target,
        "scanned": len(done_set),
        "still_allows": ok_count,
        "newly_prohibited": prohibited_count,
        "missing_404": missing_count,
        "total_active_search_ads_in_db": sync_res.get("allowed", 0),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    safe_print("\n" + "=" * 60)
    safe_print("🎉 HOAN TAT QUET DETAIL CAC BRAND SEARCH ADS")
    safe_print(f"- Tong da quet:                 {len(done_set)} brand")
    safe_print(f"- Brand van cho phep Search Ads: {ok_count}")
    safe_print(f"- Brand phat hien cam moi:      {prohibited_count}")
    safe_print(f"- Brand bien mat (404):         {missing_count}")
    safe_print(f"- Tong brand Search Ads trong DB:{sync_res.get('allowed', 0)}")
    safe_print("=" * 60)


if __name__ == "__main__":
    main()
