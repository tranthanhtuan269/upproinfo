"""Lay lai toan bo chi tiet programme Awin. Resume neu dung giua chung.

    python awin_refresh_details.py
    python awin_refresh_details.py --fresh
    python awin_refresh_details.py 95509 130491
"""

from __future__ import annotations

import json
import sys
from typing import Any

from awin_client import AwinBlocked, AwinClient, AwinError
from awin_storage import (
    DATA_DIR,
    REFRESH_PROGRESS,
    REFRESH_REPORT,
    list_advertiser_ids,
    now_iso,
    save_detail,
)


def safe_print(message: str) -> None:
    print(message, flush=True)


def load_progress(fresh: bool) -> dict[str, Any]:
    progress: dict[str, Any] = {
        "started_at": now_iso(),
        "done_ids": [],
        "ok": 0,
        "fail": 0,
        "cycle_done": False,
    }
    if fresh or not REFRESH_PROGRESS.exists():
        return progress
    try:
        raw = json.loads(REFRESH_PROGRESS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return progress
    if not isinstance(raw, dict) or raw.get("cycle_done"):
        return progress
    progress.update(raw)
    progress["done_ids"] = [int(item) for item in (progress.get("done_ids") or [])]
    return progress


def save_progress(progress: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REFRESH_PROGRESS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REFRESH_PROGRESS)


def refresh_one(client: AwinClient, advertiser_id: int) -> str:
    try:
        detail = client.fetch_detail(advertiser_id)
    except AwinError as exc:
        save_detail(advertiser_id, {}, error=str(exc))
        return "fail"
    save_detail(advertiser_id, detail)
    return "ok"


def parse_args() -> tuple[bool, list[int]]:
    fresh = False
    ids: list[int] = []
    for arg in sys.argv[1:]:
        if arg.strip().lower() in {"--fresh", "-f"}:
            fresh = True
            continue
        try:
            ids.append(int(arg.strip()))
        except ValueError:
            continue
    return fresh, ids


def main() -> None:
    fresh, explicit = parse_args()
    client = AwinClient()
    safe_print("Dang nhap Awin qua proxy...")
    client.login()
    if explicit:
        ok = 0
        fail = 0
        for index, advertiser_id in enumerate(explicit, start=1):
            status = refresh_one(client, advertiser_id)
            if status == "ok":
                ok += 1
                safe_print(f"[{index}/{len(explicit)}] {advertiser_id} cap nhat")
            else:
                fail += 1
                safe_print(f"[{index}/{len(explicit)}] {advertiser_id} loi")
        safe_print(f"Xong {len(explicit)} programme. Ok={ok} Loi={fail}.")
        return

    advertiser_ids = list_advertiser_ids(active_only=True)
    if not advertiser_ids:
        safe_print("Chua co catalog. Chay python awin_daily_sync.py truoc.")
        return
    progress = load_progress(fresh)
    done = {int(item) for item in (progress.get("done_ids") or [])}
    pending = [item for item in advertiser_ids if item not in done]
    safe_print(f"Refresh detail {len(pending)}/{len(advertiser_ids)} programme dang hoat dong.")
    try:
        for index, advertiser_id in enumerate(pending, start=1):
            status = refresh_one(client, advertiser_id)
            done.add(advertiser_id)
            progress["done_ids"] = list(done)
            if status == "ok":
                progress["ok"] = int(progress.get("ok") or 0) + 1
                safe_print(f"Refresh {index}/{len(pending)} id={advertiser_id}")
            else:
                progress["fail"] = int(progress.get("fail") or 0) + 1
                safe_print(f"Refresh {index}/{len(pending)} id={advertiser_id} loi")
            save_progress(progress)
    except KeyboardInterrupt:
        save_progress(progress)
        safe_print("Dung giua chung. Chay lai python awin_refresh_details.py de tiep tuc.")
        raise SystemExit(1) from None
    except AwinBlocked as exc:
        save_progress(progress)
        safe_print(str(exc))
        raise SystemExit(2) from None
    progress["cycle_done"] = True
    save_progress(progress)
    report = {
        "finished_at": now_iso(),
        "total": len(advertiser_ids),
        "ok": progress.get("ok") or 0,
        "fail": progress.get("fail") or 0,
    }
    REFRESH_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_print(f"Xong refresh. Ok={report['ok']} Loi={report['fail']}. Bao cao: {REFRESH_REPORT}")


if __name__ == "__main__":
    main()
