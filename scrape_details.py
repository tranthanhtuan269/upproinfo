"""Chi chay buoc lay offer-detail. Resume tu SQLite/CSV/JSON nho."""

from scrape_brands import (
    DATA_DIR,
    MarketplaceClient,
    load_progress,
    safe_print,
    scrape_details,
    write_csv,
)


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
    safe_print("Xong buoc detail.")


if __name__ == "__main__":
    main()
