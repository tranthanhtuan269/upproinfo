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
from storage import sync_allows_search_ads_from_json, sync_payment_methods_from_json


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
