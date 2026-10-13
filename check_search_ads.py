"""Kiem tra va lam sach cac brand cho phep chay Google / Search Ads tren he thong.

Cach dung:
    python check_search_ads.py               # Quet kiem tra toan bo va in thong ke
    python check_search_ads.py --fix         # Tu dong lam sach JSON va dong bo vao DB
    python check_search_ads.py <shop_id>     # Kiem tra 1 brand cu the (VD: python check_search_ads.py 221103)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BRANDS_DIR = DATA_DIR / "brands"

from storage import (
    GOOGLE_ADS_DISALLOWED_PHRASE,
    connect,
    filter_disallowed_search_ads,
    save_detail_json,
    sync_allows_search_ads_from_json,
)


def inspect_brand_ads(data: dict) -> dict:
    """Kiem tra chi tiet trang thai Search Ads va ly do cam cua mot brand."""
    shop_id = data.get("shop_id")
    name = data.get("name") or f"Brand #{shop_id}"
    channels = data.get("target_audience_customer_channels") or []

    has_search_ads_in_channels = False
    if isinstance(channels, list):
        has_search_ads_in_channels = any(
            str(c).strip().lower() in ("search_ads", "search ads", "searchads") for c in channels
        )
    elif isinstance(channels, dict):
        has_search_ads_in_channels = any(
            str(k).strip().lower() in ("search_ads", "search ads", "searchads") for k in channels.keys()
        )

    is_detect = data.get("is_detect_referral_google_ads") == 1
    has_text_warning = False
    for val in data.values():
        if isinstance(val, str) and GOOGLE_ADS_DISALLOWED_PHRASE in val.lower():
            has_text_warning = True
            break
        elif isinstance(val, (list, dict)):
            try:
                if GOOGLE_ADS_DISALLOWED_PHRASE in json.dumps(val).lower():
                    has_text_warning = True
                    break
            except Exception:
                pass

    is_disallowed = is_detect or has_text_warning
    reasons = []
    if is_detect:
        reasons.append("Bat tinh nang UpPromote cam Google Ads (is_detect_referral_google_ads=1)")
    if has_text_warning:
        reasons.append("Co doan text canh bao cam Google Ads trong noi dung")

    # Trang thai thuc su cho phep
    truly_allows = has_search_ads_in_channels and not is_disallowed

    return {
        "shop_id": shop_id,
        "name": name,
        "has_search_ads_in_channels": has_search_ads_in_channels,
        "is_disallowed": is_disallowed,
        "reasons": reasons,
        "truly_allows": truly_allows,
    }


def main() -> None:
    fix_mode = "--fix" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--fix"]

    # 1. Kiem tra cac shop_id cu the neu co truyen tham so
    specific_ids = []
    for a in args:
        try:
            specific_ids.append(int(a))
        except ValueError:
            continue

    if specific_ids:
        print(f"=== KIEM TRA {len(specific_ids)} SHOP_ID CU THE ===")
        for sid in specific_ids:
            path = BRANDS_DIR / f"{sid}.json"
            if not path.exists():
                print(f"❌ Shop ID #{sid}: Khong tim thay file JSON trong {BRANDS_DIR}")
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            info = inspect_brand_ads(data)
            print(f"\n[Shop #{info['shop_id']}] {info['name']}")
            print(f"  - Kenh search_ads trong JSON: {'CO' if info['has_search_ads_in_channels'] else 'KHONG'}")
            print(f"  - Co bi cam boi UpPromote/Text: {'CO' if info['is_disallowed'] else 'KHONG'}")
            if info["reasons"]:
                for r in info["reasons"]:
                    print(f"    * {r}")
            print(f"  ==> KET QUA CHO PHEP SEARCH ADS: {'✅ DƯỢC PHÉP' if info['truly_allows'] else '❌ KHÔNG ĐƯỢC PHÉP'}")

            if fix_mode and info["is_disallowed"] and info["has_search_ads_in_channels"]:
                filter_disallowed_search_ads(data)
                save_detail_json(data)
                print("  ==> Da xoa search_ads khoi file JSON.")
        if fix_mode:
            sync_allows_search_ads_from_json()
            print("\nDa dong bo lai vao database.")
        return

    # 2. Quet toan bo catalog
    print("Dang quet toan bo brand trong data/brands/...")
    files = list(BRANDS_DIR.glob("*.json"))
    total_brands = len(files)

    clean_needed = []
    allowed_count = 0
    disallowed_count = 0

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        info = inspect_brand_ads(data)
        if info["truly_allows"]:
            allowed_count += 1
        elif info["is_disallowed"]:
            disallowed_count += 1
            if info["has_search_ads_in_channels"]:
                clean_needed.append(info)

    print("\n" + "=" * 50)
    print("📊 KET QUA KIEM TRA TRANG THAI SEARCH ADS")
    print("=" * 50)
    print(f"- Tong so brand kiem tra:          {total_brands}")
    print(f"- Brand cho phep TAT CA Search Ads (Google + Bing): {allowed_count}")
    print(f"- Brand cho chay Search Ads nhung CAM Google Ads:   {disallowed_count}")
    print(f"- Brand can lam sach:                              {len(clean_needed)}")
    print("=" * 50)

    if clean_needed:
        print(f"\n⚠️ Phat hien {len(clean_needed)} brand bi cam nhung van con chua tag 'search_ads':")
        for item in clean_needed[:10]:
            print(f"  - #{item['shop_id']}: {item['name']} ({', '.join(item['reasons'])})")
        if len(clean_needed) > 10:
            print(f"  ... va {len(clean_needed) - 10} brand khac.")

        if fix_mode:
            print("\nDang tu dong lam sach va cap nhat lai he thong...")
            updated_count = 0
            for item in clean_needed:
                path = BRANDS_DIR / f"{item['shop_id']}.json"
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if filter_disallowed_search_ads(data):
                        save_detail_json(data)
                        updated_count += 1
                except Exception:
                    continue
            res = sync_allows_search_ads_from_json()
            print(f"✅ Da lam sach {updated_count} file JSON.")
            print(f"✅ Da dong bo DB: con lai {res['allowed']} brand duoc phep Search Ads.")
        else:
            print("\n💡 Chay lenh sau de tu dong lam sach va cap nhat vao Database:")
            print("   python check_search_ads.py --fix")
    else:
        print("\n✅ Tuyet voi! Toan bo brand tren he thong deu da sach va dong bo chuan xac 100%.")


if __name__ == "__main__":
    main()
