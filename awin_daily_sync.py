"""Quet danh muc Awin moi ngay: programme moi, programme bien mat, detail cho programme moi.

Di cham, tung request, qua proxy. Dung giua chung thi chay lai de tiep tuc.

    python awin_daily_sync.py
    python awin_daily_sync.py --fresh
    python awin_daily_sync.py --listing-only
    python awin_daily_sync.py --max-pages 3
"""

from __future__ import annotations

import json
import sys
from typing import Any

from awin_client import AwinBlocked, AwinClient, AwinError
from awin_storage import (
    DAILY_PROGRESS,
    DAILY_REPORT,
    DATA_DIR,
    counts,
    finish_scan,
    now_iso,
    save_detail,
    upsert_programme,
)


def safe_print(message: str) -> None:
    print(message, flush=True)


def load_progress(fresh: bool) -> dict[str, Any]:
    progress: dict[str, Any] = {
        "started_at": now_iso(),
        "scan_id": now_iso(),
        "page": 1,
        "last_page": None,
        "listing_done": False,
        "seen": 0,
        "new": 0,
        "detail_queue": [],
        "detail_ok": [],
        "detail_fail": [],
        "done": False,
    }
    if fresh or not DAILY_PROGRESS.exists():
        return progress
    try:
        raw = json.loads(DAILY_PROGRESS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return progress
    if not isinstance(raw, dict) or raw.get("done"):
        return progress
    progress.update(raw)
    return progress


def save_progress(progress: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_PROGRESS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DAILY_PROGRESS)


def write_report(progress: dict[str, Any]) -> None:
    report = {
        "finished_at": now_iso(),
        "scan_id": progress.get("scan_id"),
        "seen": progress.get("seen") or 0,
        "new": progress.get("new") or 0,
        "listing_done": bool(progress.get("listing_done")),
        "detail_ok": len(progress.get("detail_ok") or []),
        "detail_fail": len(progress.get("detail_fail") or []),
        "done": bool(progress.get("done")),
        "catalog": counts(),
    }
    DAILY_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def scan_listing(client: AwinClient, progress: dict[str, Any], max_pages: int | None) -> None:
    if progress.get("listing_done"):
        safe_print("Listing da xong trong checkpoint. Bo qua.")
        return
    page = max(1, int(progress.get("page") or 1))
    last_page = progress.get("last_page")
    previous_ids: set[int] = set()
    safe_print(f"Quet All Programs tu trang {page}. Moi trang 40 programme, nghi giua cac request.")
    while True:
        if max_pages is not None and page > max_pages:
            safe_print(f"Dung vi --max-pages {max_pages}.")
            break
        parsed = client.directory_page(page)
        rows = parsed["rows"]
        if parsed.get("last_page"):
            last_page = int(parsed["last_page"])
            progress["last_page"] = last_page
        ids = [int(row["advertiser_id"]) for row in rows]
        if not ids:
            safe_print(f"Trang {page} trong. Ket thuc listing.")
            progress["listing_done"] = True
            break
        if previous_ids and set(ids) == previous_ids:
            safe_print(f"Trang {page} lap lai trang truoc. Ket thuc listing.")
            progress["listing_done"] = True
            break
        added = 0
        for row in rows:
            kind = upsert_programme(row, str(progress["scan_id"]))
            if kind == "new":
                added += 1
        progress["seen"] = int(progress.get("seen") or 0) + len(rows)
        progress["new"] = int(progress.get("new") or 0) + added
        progress["page"] = page + 1
        save_progress(progress)
        heading = parsed.get("heading") or ""
        safe_print(
            f"Trang {page}/{last_page or '?'}  {len(rows)} programme  moi {added}  {heading}"
        )
        previous_ids = set(ids)
        if last_page and page >= int(last_page):
            progress["listing_done"] = True
            break
        page += 1
    if progress.get("listing_done") and max_pages is None:
        hidden = finish_scan(str(progress["scan_id"]))
        safe_print(f"Listing xong. Programme khong con trong danh muc: {hidden}.")
    elif progress.get("listing_done"):
        safe_print("Listing dung som, chua danh dau programme bien mat.")
    save_progress(progress)


def scan_details(client: AwinClient, progress: dict[str, Any], max_details: int | None) -> None:
    queue = [int(item) for item in (progress.get("detail_queue") or [])]
    if not queue and progress.get("listing_done"):
        done = {int(item) for item in (progress.get("detail_ok") or [])}
        done.update(int(item) for item in (progress.get("detail_fail") or []))
        from awin_storage import connect

        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT p.advertiser_id
                FROM programmes p
                LEFT JOIN details d ON d.advertiser_id = p.advertiser_id AND COALESCE(d.error, '') = ''
                WHERE p.scan_id = ? AND d.advertiser_id IS NULL
                ORDER BY p.advertiser_id
                """,
                (str(progress.get("scan_id") or ""),),
            ).fetchall()
        finally:
            conn.close()
        queue = [int(row["advertiser_id"]) for row in rows if int(row["advertiser_id"]) not in done]
        progress["detail_queue"] = queue
        save_progress(progress)
    if not queue:
        safe_print("Khong co programme moi can lay detail.")
        return
    safe_print(f"Lay detail {len(queue)} programme moi. Moi programme 2 request, nghi dai.")
    taken = 0
    for index, advertiser_id in enumerate(list(queue), start=1):
        if max_details is not None and taken >= max_details:
            safe_print(f"Dung vi --max-details {max_details}.")
            break
        try:
            detail = client.fetch_detail(advertiser_id)
            save_detail(advertiser_id, detail)
            progress.setdefault("detail_ok", []).append(advertiser_id)
            safe_print(f"Detail {index}/{len(queue)} id={advertiser_id} {detail.get('name') or ''}")
        except AwinBlocked:
            save_progress(progress)
            raise
        except AwinError as exc:
            save_detail(advertiser_id, {}, error=str(exc))
            progress.setdefault("detail_fail", []).append(advertiser_id)
            safe_print(f"Detail {index}/{len(queue)} id={advertiser_id} loi: {exc}")
        queue = [item for item in queue if item != advertiser_id]
        progress["detail_queue"] = queue
        save_progress(progress)
        taken += 1


def parse_args() -> dict[str, Any]:
    fresh = False
    listing_only = False
    max_pages = None
    max_details = None
    args = sys.argv[1:]
    index = 0
    while index < len(args):
        arg = args[index].strip().lower()
        if arg in {"--fresh", "-f"}:
            fresh = True
        elif arg == "--listing-only":
            listing_only = True
        elif arg == "--max-pages" and index + 1 < len(args):
            max_pages = int(args[index + 1])
            index += 1
        elif arg == "--max-details" and index + 1 < len(args):
            max_details = int(args[index + 1])
            index += 1
        index += 1
    return {
        "fresh": fresh,
        "listing_only": listing_only,
        "max_pages": max_pages,
        "max_details": max_details,
    }


def main() -> None:
    options = parse_args()
    progress = load_progress(fresh=options["fresh"])
    client = AwinClient()
    safe_print("Dang nhap Awin qua proxy...")
    client.login()
    safe_print(f"Publisher {client.publisher_id}. Vung {', '.join(client.region_ids)}.")
    try:
        scan_listing(client, progress, options["max_pages"])
        if not options["listing_only"]:
            scan_details(client, progress, options["max_details"])
    except KeyboardInterrupt:
        save_progress(progress)
        write_report(progress)
        safe_print("Dung giua chung. Chay lai python awin_daily_sync.py de tiep tuc.")
        raise SystemExit(1) from None
    except AwinBlocked as exc:
        save_progress(progress)
        write_report(progress)
        safe_print(str(exc))
        raise SystemExit(2) from None
    finished_listing = bool(progress.get("listing_done")) and options["max_pages"] is None
    finished_details = not progress.get("detail_queue")
    progress["done"] = finished_listing and (options["listing_only"] or finished_details)
    save_progress(progress)
    write_report(progress)
    catalog = counts()
    safe_print(
        "Xong luot quet. "
        f"Tong {catalog['total']}  con {catalog['active']}  "
        f"moi hom nay {catalog['new_today']}  da co detail {catalog['detailed']}."
    )
    safe_print(f"Bao cao: {DAILY_REPORT}")


if __name__ == "__main__":
    main()
