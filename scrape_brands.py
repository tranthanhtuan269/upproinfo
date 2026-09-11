"""Quet toan bo find-offers, roi lay offer-detail tung brand.

Chay cham, 1 request mot luc, tu refresh token, luu checkpoint de resume.
"""

from __future__ import annotations

import json
import random
import sys
import time
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from login_marketplace import login_and_get_tokens
from storage import (
    DATA_DIR,
    DETAIL_CSV,
    connect,
    done_detail_ids,
    export_details_csv,
    list_shop_ids,
    mark_skipped,
    save_detail_row,
    save_offer_row,
)


class ListingNotFound(Exception):
    """Brand khong con tren marketplace."""

SITE = "https://marketplace.uppromote.com"
API = "https://mkp-api.uppromote.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

PROGRESS_PATH = DATA_DIR / "progress.json"
CSV_PATH = DETAIL_CSV

PER_PAGE = 20
TAB = "all-offers"
TOKEN_REFRESH_AFTER = 360
LIST_DELAY = (1.8, 3.2)
DETAIL_DELAY = (2.4, 4.2)
BURST_EVERY = 20
BURST_PAUSE = (10, 18)
BLOCK_EVERY = 80
BLOCK_PAUSE = (25, 40)
MAX_RETRIES = 5


def sleep_range(bounds: tuple[float, float]) -> None:
    time.sleep(random.uniform(*bounds))


def safe_print(*args: Any) -> None:
    text = " ".join(str(item) for item in args)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def default_progress() -> dict[str, Any]:
    return {
        "list_next_page": 1,
        "list_done": False,
        "last_detail_shop_id": None,
        "detail_done_count": 0,
    }


def load_progress() -> dict[str, Any]:
    progress = default_progress()
    if not PROGRESS_PATH.exists():
        return progress
    try:
        raw = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        safe_print("progress.json bi hong, dung checkpoint mac dinh.")
        return progress
    if isinstance(raw, dict):
        progress.update(raw)
    return progress


def save_progress(progress: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slim = {
        "list_next_page": progress.get("list_next_page", 1),
        "list_done": bool(progress.get("list_done")),
        "last_detail_shop_id": progress.get("last_detail_shop_id"),
        "detail_done_count": int(progress.get("detail_done_count") or 0),
    }
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


class MarketplaceClient:
    def __init__(self) -> None:
        self.access = ""
        self.refresh = ""
        self.token_at = 0.0
        self.request_count = 0

    def login(self) -> None:
        safe_print("Dang dang nhap de lay token moi...")
        self.access, self.refresh = login_and_get_tokens(headless=True)
        self.token_at = time.time()
        safe_print("Dang nhap thanh cong.")

    def refresh_tokens(self) -> None:
        safe_print("Dang refresh token...")
        body = json.dumps({"refresh_token": self.refresh}).encode("utf-8")
        request = Request(
            f"{SITE}/api/auth/refresh",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access}",
                "Content-Type": "application/json",
                "Cookie": (
                    f"marketplace_access_token={self.access}; "
                    f"marketplace_refresh_token={self.refresh}"
                ),
                "Origin": SITE,
                "Referer": f"{SITE}/",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError:
            safe_print("Refresh that bai, dang nhap lai.")
            self.login()
            return
        data = payload.get("data") or {}
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            safe_print("Refresh khong tra token, dang nhap lai.")
            self.login()
            return
        self.access = access
        self.refresh = refresh
        self.token_at = time.time()
        safe_print("Refresh token thanh cong.")

    def ensure_token(self) -> None:
        if not self.access:
            self.login()
            return
        if time.time() - self.token_at >= TOKEN_REFRESH_AFTER:
            self.refresh_tokens()

    def pause(self, kind: str) -> None:
        sleep_range(LIST_DELAY if kind == "list" else DETAIL_DELAY)
        if self.request_count and self.request_count % BLOCK_EVERY == 0:
            extra = random.uniform(*BLOCK_PAUSE)
            safe_print(f"Nghi dai {extra:.1f}s sau {self.request_count} request...")
            time.sleep(extra)
        elif self.request_count and self.request_count % BURST_EVERY == 0:
            extra = random.uniform(*BURST_PAUSE)
            safe_print(f"Nghi ngan {extra:.1f}s sau {self.request_count} request...")
            time.sleep(extra)

    def get_json(self, url: str, kind: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            self.ensure_token()
            self.pause(kind)
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.access}",
                    "Content-Type": "application/json",
                    "Origin": SITE,
                    "Referer": f"{SITE}/",
                    "User-Agent": USER_AGENT,
                },
                method="GET",
            )
            try:
                with urlopen(request, timeout=40) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.request_count += 1
                if not isinstance(payload, dict):
                    raise RuntimeError("API khong tra JSON object")
                return payload
            except HTTPError as exc:
                last_error = exc
                body = exc.read().decode("utf-8", errors="replace")[:240]
                if exc.code == 401:
                    safe_print("HTTP 401, refresh token roi thu lai.")
                    self.refresh_tokens()
                    continue
                if exc.code == 429:
                    wait = min(180, 60 * attempt)
                    safe_print(f"HTTP 429, nghi {wait}s roi thu lai.")
                    time.sleep(wait)
                    continue
                if exc.code >= 500:
                    wait = 20 * attempt
                    safe_print(f"HTTP {exc.code}, nghi {wait}s. {body}")
                    time.sleep(wait)
                    continue
                if exc.code in (404, 410) or "MARKETPLACE_LISTING_NOT_FOUND" in body:
                    self.request_count += 1
                    raise ListingNotFound(body) from exc
                raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
            except (
                URLError,
                TimeoutError,
                IncompleteRead,
                json.JSONDecodeError,
                ConnectionError,
                OSError,
            ) as exc:
                last_error = exc
                wait = 15 * attempt
                safe_print(f"Loi mang/JSON ({type(exc).__name__}: {exc}), nghi {wait}s.")
                time.sleep(wait)
        raise RuntimeError(f"Het so lan thu: {last_error}")


def list_url(page: int) -> str:
    return (
        f"{API}/api/v1/marketplace-offer/find-offer/datatable/data?"
        + urlencode(
            {
                "page": str(page),
                "per_page": str(PER_PAGE),
                "keyword": "",
                "sort_by": "most_relevant",
                "sort": "",
                "tab[0]": TAB,
                "pathPage": "/offers/find-offers",
                "mobile": "false",
            }
        )
    )


def detail_url(shop_id: int) -> str:
    return (
        f"{API}/api/v1/marketplace-offer/offer-detail/{shop_id}?"
        + urlencode(
            {
                "pathPage": "/brand/[id]/[[...affiliate_id]]",
                "mobile": "false",
            }
        )
    )


def scrape_list(client: MarketplaceClient, progress: dict[str, Any]) -> None:
    if progress.get("list_done"):
        safe_print("Danh sach brand da quet xong tu lan truoc.")
        return

    page = int(progress.get("list_next_page") or 1)
    while True:
        payload = client.get_json(list_url(page), "list")
        data = payload.get("data") or {}
        rows = data.get("data") or []
        if not rows:
            progress["list_done"] = True
            save_progress(progress)
            safe_print(f"Het trang o page {page}.")
            return

        for row in rows:
            if isinstance(row, dict):
                row["_page"] = page
                save_offer_row(row)

        nxt = data.get("next_page_url")
        safe_print(
            f"List page {page}: {len(rows)} brand "
            f"(from={data.get('from')} to={data.get('to')})"
        )
        progress["list_next_page"] = page + 1
        save_progress(progress)
        if not nxt:
            progress["list_done"] = True
            save_progress(progress)
            safe_print(f"Trang cuoi: {page}.")
            return
        page += 1


def scrape_details(client: MarketplaceClient, progress: dict[str, Any]) -> None:
    shop_ids = list_shop_ids()
    done = done_detail_ids()
    pending = [shop_id for shop_id in shop_ids if shop_id not in done]
    safe_print(f"Can lay detail: {len(pending)}/{len(shop_ids)} brand.")

    for index, shop_id in enumerate(pending, start=1):
        try:
            payload = client.get_json(detail_url(shop_id), "detail")
        except ListingNotFound as exc:
            mark_skipped(shop_id, str(exc)[:240])
            done.add(shop_id)
            progress["last_detail_shop_id"] = shop_id
            progress["detail_done_count"] = len(done)
            save_progress(progress)
            safe_print(f"Bo qua {index}/{len(pending)} shop_id={shop_id} (khong con tren marketplace)")
            continue
        detail = payload.get("data")
        if not isinstance(detail, dict):
            detail = {"raw": payload, "shop_id": shop_id}
        if "shop_id" not in detail:
            detail["shop_id"] = shop_id
        save_detail_row(detail)
        done.add(shop_id)
        progress["last_detail_shop_id"] = shop_id
        progress["detail_done_count"] = len(done)
        save_progress(progress)
        if index % 20 == 0:
            conn = connect()
            try:
                export_details_csv(conn)
            finally:
                conn.close()
        name = detail.get("name") or ""
        safe_print(f"Detail {index}/{len(pending)} shop_id={shop_id} {name}")


def write_csv() -> None:
    conn = connect()
    try:
        count = export_details_csv(conn)
    finally:
        conn.close()
    safe_print(f"Da ghi CSV: {CSV_PATH} ({count} dong)")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_progress()
    client = MarketplaceClient()
    client.login()
    safe_print("Buoc 1: quet toan bo trang find-offers (tab all-offers, 20/trang).")
    scrape_list(client, progress)
    safe_print("Buoc 2: lay offer-detail tung brand.")
    scrape_details(client, progress)
    write_csv()
    safe_print("Xong.")


if __name__ == "__main__":
    main()
