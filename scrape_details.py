"""Chi chay buoc lay offer-detail. Resume tu SQLite/CSV/JSON nho.

Hang ngay dung python daily_sync.py (quet listing + danh dau xoa + detail brand moi).
File nay chi lay detail cho brand da co trong offers ma chua co detail.
"""

from scrape_brands import (
    DATA_DIR,
    MarketplaceClient,
    load_progress,
    safe_print,
    scrape_details,
    write_csv,
)
from storage import (
    BRANDS_DIR,
    filter_disallowed_search_ads,
    save_detail_json,
    sync_allows_search_ads_from_json,
    sync_payment_methods_from_json,
)
import json


def clean_disallowed_google_ads_in_files() -> int:
    """Kiem tra va loai bo Search ads trong cac file JSON neu co cau cam Google Ads."""
    modified = 0
    if not BRANDS_DIR.exists():
        return 0
    for path in BRANDS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and filter_disallowed_search_ads(data):
            save_detail_json(data)
            modified += 1
    return modified



def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_progress()
    if not progress.get("list_done"):
        safe_print("Danh sach brand chua xong. Chay python scrape_brands.py truoc.")
        return
    client = MarketplaceClient()
    client.login()
    safe_print("Chi chay buoc offer-detail. Brand da co se bi bo qua.")
    scrape_details(client, progress)
    cleaned_ads = clean_disallowed_google_ads_in_files()
    if cleaned_ads:
        safe_print(f"Da loc bo Search Ads cho {cleaned_ads} brand co thong bao cam Google Ads.")
    write_csv()
    ads = sync_allows_search_ads_from_json()
    safe_print(
        f"Da cap nhat allows_search_ads tu JSON: {ads['allowed']}/{ads['updated']} brand."
    )
    pays = sync_payment_methods_from_json()
    safe_print(
        f"Da cap nhat payment_methods tu JSON: {pays['with_payment']}/{pays['updated']} brand."
    )
    safe_print("Xong buoc detail.")


if __name__ == "__main__":
    main()
