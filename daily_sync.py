"""Quy trinh hang ngay: quet listing, cap nhat catalog, lay detail, quet traffic/ads.

1. Quet find-offers (tab all-offers)
2. Brand moi -> them vao offers
3. Brand mat khoi listing -> danh dau skipped (dung hoat dong)
4. Brand quay lai -> go skipped
5. Lay offer-detail cho brand moi
6. Quet AITDK traffic + Google Ads cho brand moi

    python daily_sync.py
    python daily_sync.py --from-report
    python daily_sync.py --fresh
    python daily_sync.py --metrics-all
    python daily_sync.py --skip-metrics
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from check_brands import (
    REPORT_MISSING,
    REPORT_NEW,
    as_offer_row,
    catalog_diff,
    default_progress,
    finish_scan,
    rows_from_csv,
    save_progress,
    scan_list,
)
from scrape_brands import (
    ListingNotFound,
    MarketplaceClient,
    detail_url,
    safe_print,
)
from storage import (
    DATA_DIR,
    clear_skipped,
    connect,
    ensure_skipped,
    existing_detail_ids,
    export_details_csv,
    export_offers_csv,
    mark_skipped,
    save_detail_row,
    save_offer_row,
)
from scan_metrics import scan_pending, scan_shop_ids

DAILY_PROGRESS = DATA_DIR / "daily_progress.json"
DAILY_REPORT = DATA_DIR / "daily_latest.json"
SKIP_REASON = "Khong con tren find-offers"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_daily(fresh: bool) -> dict[str, Any]:
    progress = default_progress()
    progress.update(
        {
            "apply_done": False,
            "detail_queue": [],
            "detail_ok": [],
            "detail_missing": [],
            "metrics_done": False,
            "done": False,
            "from_report": False,
        }
    )
    if fresh or not DAILY_PROGRESS.exists():
        return progress
    try:
        raw = json.loads(DAILY_PROGRESS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return progress
    if not isinstance(raw, dict) or raw.get("done"):
        return progress
    progress.update(raw)
    live = progress.get("live")
    progress["live"] = live if isinstance(live, dict) else {}
    return progress


def save_daily(progress: dict[str, Any]) -> None:
    save_progress(progress, DAILY_PROGRESS)


def apply_catalog(
    live_map: dict[str, dict[str, Any]],
    new_ids: set[int],
    still_ids: set[int],
    missing_ids: set[int],
) -> dict[str, Any]:
    added = 0
    refreshed = 0
    returned_ids: list[int] = []
    newly_missing = 0

    for sid in sorted(new_ids):
        row = live_map.get(str(sid)) or {"shop_id": sid}
        save_offer_row(as_offer_row(row))
        added += 1

    for sid in sorted(still_ids):
        row = live_map.get(str(sid))
        if row:
            save_offer_row(as_offer_row(row))
            refreshed += 1
        if clear_skipped(sid):
            returned_ids.append(sid)
            safe_print(f"Brand quay lai shop_id={sid}")

    for sid in sorted(missing_ids):
        if ensure_skipped(sid, SKIP_REASON):
            newly_missing += 1

    conn = connect()
    try:
        export_offers_csv(conn)
    finally:
        conn.close()

    safe_print(
        f"Catalog: them {added} moi, cap nhat {refreshed} con, "
        f"danh dau {newly_missing} xoa moi, go skip {len(returned_ids)} quay lai."
    )
    return {
        "added": added,
        "refreshed": refreshed,
        "returned": len(returned_ids),
        "returned_ids": returned_ids,
        "newly_missing": newly_missing,
    }


def apply_from_report() -> tuple[dict[str, dict[str, Any]], set[int], dict[str, int]]:
    new_rows = rows_from_csv(REPORT_NEW)
    missing_rows = rows_from_csv(REPORT_MISSING)
    if not new_rows and not missing_rows:
        raise SystemExit(f"Khong co bao cao. Can {REPORT_NEW} hoac {REPORT_MISSING}.")

    live_map: dict[str, dict[str, Any]] = {}
    new_ids = set()
    for row in new_rows:
        sid = int(row["shop_id"])
        new_ids.add(sid)
        live_map[str(sid)] = as_offer_row(row)

    missing_ids = {int(row["shop_id"]) for row in missing_rows}
    applied = apply_catalog(live_map, new_ids, set(), missing_ids)
    return live_map, new_ids, applied


def detail_targets(new_ids: set[int], returned_ids: set[int] | None = None) -> list[int]:
    have = existing_detail_ids()
    wanted = set(new_ids) | set(returned_ids or [])
    return [sid for sid in sorted(wanted) if sid not in have]


def fetch_details(client: MarketplaceClient, progress: dict[str, Any]) -> None:
    queue = [int(sid) for sid in progress.get("detail_queue") or []]
    ok = [int(sid) for sid in progress.get("detail_ok") or []]
    missing = [int(sid) for sid in progress.get("detail_missing") or []]
    total = len(queue) + len(ok) + len(missing)
    if not queue:
        safe_print("Khong co brand moi can lay detail.")
        return

    safe_print(f"Lay detail {len(queue)} brand moi/quay lai.")
    pending = list(queue)
    for index, shop_id in enumerate(list(pending), start=1):
        try:
            payload = client.get_json(detail_url(shop_id), "detail")
        except ListingNotFound as exc:
            mark_skipped(shop_id, str(exc)[:240])
            missing.append(shop_id)
            queue = [sid for sid in queue if sid != shop_id]
            progress["detail_queue"] = queue
            progress["detail_missing"] = missing
            save_daily(progress)
            safe_print(f"Detail {index}/{len(pending)} shop_id={shop_id} (khong con tren marketplace)")
            continue
        detail = payload.get("data")
        if not isinstance(detail, dict):
            detail = {"raw": payload, "shop_id": shop_id}
        if "shop_id" not in detail:
            detail["shop_id"] = shop_id
        save_detail_row(detail)
        ok.append(shop_id)
        queue = [sid for sid in queue if sid != shop_id]
        progress["detail_queue"] = queue
        progress["detail_ok"] = ok
        save_daily(progress)
        name = detail.get("name") or ""
        safe_print(f"Detail {index}/{len(pending)} shop_id={shop_id} {name}")
        if index % 20 == 0:
            conn = connect()
            try:
                export_details_csv(conn)
            finally:
                conn.close()

    conn = connect()
    try:
        export_details_csv(conn)
    finally:
        conn.close()
    safe_print(f"Detail xong: ok={len(ok)}/{total}  404={len(missing)}")


def write_daily_report(progress: dict[str, Any], applied: dict[str, Any], summary: dict[str, int] | None) -> None:
    apply_out = {
        "added": applied.get("added", 0),
        "refreshed": applied.get("refreshed", 0),
        "returned": applied.get("returned", 0),
        "newly_missing": applied.get("newly_missing", 0),
    }
    report = {
        "started_at": progress.get("started_at"),
        "finished_at": now_iso(),
        "from_report": bool(progress.get("from_report")),
        "apply": apply_out,
        "scan": summary,
        "detail_ok": progress.get("detail_ok") or [],
        "detail_missing": progress.get("detail_missing") or [],
        "detail_pending": progress.get("detail_queue") or [],
        "metrics_count": progress.get("metrics_count") or 0,
    }
    DAILY_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def run_metrics(progress: dict[str, Any]) -> int:
    if "--skip-metrics" in sys.argv:
        safe_print("Bo qua quet traffic/ads (--skip-metrics).")
        progress["metrics_done"] = True
        progress["metrics_count"] = 0
        save_daily(progress)
        return 0
    ids = [int(sid) for sid in (progress.get("detail_ok") or [])]
    try:
        if "--metrics-all" in sys.argv:
            safe_print("Buoc 4/4: quet traffic/ads toan bo brand con thieu.")
            count = scan_pending()
        elif ids:
            safe_print(f"Buoc 4/4: quet traffic/ads {len(ids)} brand moi.")
            count = scan_shop_ids(ids)
        else:
            safe_print("Buoc 4/4: khong co brand moi de quet traffic.")
            count = 0
    except KeyboardInterrupt:
        save_daily(progress)
        safe_print("Dung giua chung. Chay lai python daily_sync.py de resume metrics.")
        raise SystemExit(1) from None
    progress["metrics_done"] = True
    progress["metrics_count"] = count
    save_daily(progress)
    return count


def run_from_report() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    progress = {
        "started_at": now_iso(),
        "from_report": True,
        "list_done": True,
        "apply_done": False,
        "detail_queue": [],
        "detail_ok": [],
        "detail_missing": [],
        "metrics_done": False,
        "done": False,
    }
    save_daily(progress)
    live_map, new_ids, applied = apply_from_report()
    queue = detail_targets(new_ids)
    progress["apply_done"] = True
    progress["detail_queue"] = queue
    save_daily(progress)

    client = MarketplaceClient()
    client.login()
    try:
        fetch_details(client, progress)
    except KeyboardInterrupt:
        save_daily(progress)
        safe_print("Dung giua chung. Chay lai python daily_sync.py --from-report de tiep tuc detail.")
        raise SystemExit(1) from None

    run_metrics(progress)
    progress["done"] = True
    save_daily(progress)
    write_daily_report(progress, applied, None)
    safe_print(f"Xong. Them {applied['added']} brand, danh dau {applied['newly_missing']} da xoa.")
    safe_print(f"Bao cao: {DAILY_REPORT}")


def run_full(fresh: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_daily(fresh=fresh)
    if fresh:
        safe_print("Bat dau daily sync moi (--fresh).")
    elif progress.get("live") and not progress.get("list_done"):
        safe_print(
            f"Resume listing tu page {progress.get('list_next_page')} "
            f"(live={len(progress.get('live') or {})})."
        )
    elif progress.get("apply_done") and progress.get("detail_queue"):
        safe_print(f"Resume detail, con {len(progress['detail_queue'])} brand.")

    client = MarketplaceClient()
    client.login()
    summary = None
    applied = {
        "added": 0,
        "refreshed": 0,
        "returned": 0,
        "newly_missing": 0,
    }

    if not progress.get("list_done"):
        safe_print("Buoc 1/4: quet find-offers.")
        try:
            live_map = scan_list(client, progress, DAILY_PROGRESS)
        except KeyboardInterrupt:
            save_daily(progress)
            safe_print("Dung giua chung. Chay lai python daily_sync.py de resume.")
            raise SystemExit(1) from None
        progress["live"] = live_map
        progress["list_done"] = True
        save_daily(progress)
        summary = finish_scan(progress, live_map)
        safe_print(
            f"Scan: Live={summary['live']}  Catalog={summary['known']}  "
            f"Con={summary['still']}  Moi={summary['new']}  Da xoa={summary['missing']}"
        )
    else:
        live_map = {
            str(key): value for key, value in (progress.get("live") or {}).items()
        }

    if not progress.get("apply_done"):
        safe_print("Buoc 2/4: cap nhat catalog (them moi, danh dau da xoa).")
        diff = catalog_diff(live_map)
        applied = apply_catalog(
            live_map,
            diff["new_ids"],
            diff["still_ids"],
            diff["missing_ids"],
        )
        queue = detail_targets(diff["new_ids"], set(applied["returned_ids"]))
        progress["apply_done"] = True
        progress["detail_queue"] = queue
        progress["new_ids"] = sorted(diff["new_ids"])
        save_daily(progress)
    elif not progress.get("detail_queue") and not progress.get("detail_ok"):
        diff = catalog_diff(live_map) if live_map else {"new_ids": set()}
        progress["detail_queue"] = detail_targets(set(progress.get("new_ids") or []) or diff["new_ids"])
        save_daily(progress)

    safe_print("Buoc 3/4: lay offer-detail brand moi.")
    try:
        fetch_details(client, progress)
    except KeyboardInterrupt:
        save_daily(progress)
        safe_print("Dung giua chung. Chay lai python daily_sync.py de resume detail.")
        raise SystemExit(1) from None

    if not progress.get("metrics_done"):
        run_metrics(progress)

    progress["done"] = True
    save_daily(progress)
    write_daily_report(progress, applied, summary)
    safe_print(
        "Xong daily sync. "
        f"Them {applied['added']}  Xoa moi {applied['newly_missing']}  "
        f"Quay lai {applied['returned']}  Metrics {progress.get('metrics_count') or 0}"
    )
    safe_print(f"Bao cao: {DAILY_REPORT}")


def main() -> None:
    from_report = "--from-report" in sys.argv
    fresh = "--fresh" in sys.argv
    if from_report:
        run_from_report()
        return
    run_full(fresh=fresh)


if __name__ == "__main__":
    main()
