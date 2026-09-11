"""Xuat lai CSV gon tu SQLite. Mo file bang Excel."""

from storage import connect, export_details_csv, export_offers_csv, DETAIL_CSV, LIST_CSV


def main() -> None:
    conn = connect()
    try:
        offers = export_offers_csv(conn)
        details = export_details_csv(conn)
    finally:
        conn.close()
    print(f"offers_list.csv: {offers} dong -> {LIST_CSV}")
    print(f"brand_details.csv: {details} dong -> {DETAIL_CSV}")


if __name__ == "__main__":
    main()
