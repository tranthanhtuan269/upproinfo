"""Cap nhat lai offer-detail cho TAT CA brand trong catalog.

Khac scrape_details.py: khong bo qua brand da co detail.
Chay 3 ngay/lan. Resume neu dung giua chung.

    python refresh_details.py
    python refresh_details.py --fresh
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from scrape_brands import (
    ListingNotFound,
    MarketplaceClient,
    detail_url,
    safe_print,
    write_csv,
)
from storage import (
    DATA_DIR,
    clear_skipped,
    connect,
    export_details_csv,
    list_shop_ids,
    mark_skipped,
    save_detail_row,
)

PROGRESS_PATH = DATA_DIR / "refresh_details_progress.json"
REPORT_PATH = DATA_DIR / "refresh_details_latest.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_progress() -> dict[str, Any]:
    return {
        "started_at": now_iso(),
        "done_ids": [],
        "ok": 0,
        "missing": 0,
        "returned": 0,
        "last_shop_id": None,
        "cycle_done": False,
    }


def load_progress(fresh: bool) -> dict[str, Any]:
    if fresh or not PROGRESS_PATH.exists():
        return default_progress()
    try:
        raw = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        safe_print("refresh_details_progress.json bi hong, bat dau chu ky moi.")
        return default_progress()
    if not isinstance(raw, dict) or raw.get("cycle_done"):
        return default_progress()
    progress = default_progress()
    progress.update(raw)
    done = progress.get("done_ids") or []
    progress["done_ids"] = [int(sid) for sid in done]
    progress["cycle_done"] = False
    return progress


def save_progress(progress: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def write_report(progress: dict[str, Any], total: int) -> None:
    report = {
        "started_at": progress.get("started_at"),
        "finished_at": now_iso(),
        "total": total,
        "done": len(progress.get("done_ids") or []),
        "ok": progress.get("ok") or 0,
        "missing": progress.get("missing") or 0,
        "returned": progress.get("returned") or 0,
        "cycle_done": bool(progress.get("cycle_done")),
        "last_shop_id": progress.get("last_shop_id"),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


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
    save_detail_row(detail)
    if clear_skipped(shop_id):
        return "returned"
    return "ok"


def main() -> None:
    fresh = "--fresh" in sys.argv
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shop_ids = list_shop_ids()
    if not shop_ids:
        safe_print("Chua co catalog. Chay python scrape_brands.py truoc.")
        return

    progress = load_progress(fresh=fresh)
    done = set(progress.get("done_ids") or [])
    pending = [sid for sid in shop_ids if sid not in done]
    if fresh:
        safe_print("Bat dau chu ky refresh moi (--fresh).")
    elif done:
        safe_print(f"Resume: da xong {len(done)}/{len(shop_ids)}, con {len(pending)}.")
    safe_print(f"Cap nhat detail toan bo: {len(pending)}/{len(shop_ids)} brand.")

    client = MarketplaceClient()
    client.login()
    try:
        for index, shop_id in enumerate(pending, start=1):
            status = refresh_one(client, shop_id)
            done.add(shop_id)
            progress["done_ids"] = list(done)
            progress["last_shop_id"] = shop_id
            if status == "missing":
                progress["missing"] = int(progress.get("missing") or 0) + 1
                safe_print(
                    f"Refresh {index}/{len(pending)} shop_id={shop_id} (khong con tren marketplace)"
                )
            elif status == "returned":
                progress["ok"] = int(progress.get("ok") or 0) + 1
                progress["returned"] = int(progress.get("returned") or 0) + 1
                safe_print(f"Refresh {index}/{len(pending)} shop_id={shop_id} (quay lai)")
            else:
                progress["ok"] = int(progress.get("ok") or 0) + 1
                safe_print(f"Refresh {index}/{len(pending)} shop_id={shop_id}")
            save_progress(progress)
            if index % 20 == 0:
                conn = connect()
                try:
                    export_details_csv(conn)
                finally:
                    conn.close()
    except KeyboardInterrupt:
        save_progress(progress)
        write_report(progress, len(shop_ids))
        safe_print("Dung giua chung. Chay lai python refresh_details.py de resume.")
        raise SystemExit(1) from None

    progress["cycle_done"] = True
    save_progress(progress)
    write_csv()
    write_report(progress, len(shop_ids))
    safe_print(
        "Xong refresh detail. "
        f"Ok={progress.get('ok') or 0}  "
        f"Da xoa={progress.get('missing') or 0}  "
        f"Quay lai={progress.get('returned') or 0}"
    )
    safe_print(f"Bao cao: {REPORT_PATH}")


if __name__ == "__main__":
    main()
