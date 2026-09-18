# Hướng dẫn chạy UpproInfo (local)

Mọi lệnh chạy trong thư mục project:

```bat
cd /d C:\xampp\htdocs\upproinfo
```

Cài sẵn: `python -m pip install -r requirements.txt` rồi `python -m playwright install chromium` (nếu chưa có).

---

## An toàn database

Các lệnh crawl **không tạo lại** file `uppromote.db`, **không xóa bảng**, **không `INSERT OR REPLACE`** lên `brand_metrics`.

- Chỉ `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` nếu thiếu.
- `offers` / `details` / `skipped` dùng upsert theo `shop_id`.
- Traffic/Google Ads chỉ ghi các cột metrics, **giữ** `allows_search_ads`.
- Detail mới tự cập nhật `allows_search_ads` từ `target_audience_customer_channels` trong JSON.
- `update.bat` **gop** catalog vào DB trên server, không đè nguyên file (tránh mất cột extra). Không đụng `billing.db`.

---

## Việc hàng ngày (dùng lệnh này)

Quét brand mới, đánh dấu brand đã xóa, lấy detail brand mới, rồi quét traffic + Google Ads:

```bat
python daily_sync.py
```

Hoặc double-click `daily_sync.bat` (log ghi vào `data\daily_sync.log`).

Dừng giữa chừng thì chạy lại cùng lệnh — script tự resume.

Cờ thêm:

| Lệnh | Khi nào dùng |
|---|---|
| `python daily_sync.py --fresh` | Bỏ checkpoint, quét listing từ đầu |
| `python daily_sync.py --from-report` | Dùng báo cáo `check_brands` vừa xong, không quét listing lại |
| `python daily_sync.py --metrics-all` | Daily sync + quét traffic cho **mọi** brand còn thiếu |
| `python daily_sync.py --skip-metrics` | Daily sync, bỏ bước traffic/ads |

Lịch Windows: Task Scheduler → chạy `C:\xampp\htdocs\upproinfo\daily_sync.bat` mỗi ngày 1 lần.

---

## Lần đầu / bắt kịp dữ liệu cũ

Chỉ cần khi catalog chưa có, hoặc muốn lấy lại toàn bộ:

```bat
python scrape_brands.py
```

Chỉ lấy offer-detail cho brand **còn thiếu** (bỏ qua brand đã có file/detail):

```bat
python scrape_details.py
```

Cập nhật lại detail **toàn bộ** brand (chạy 3 ngày/lần). Brand đã có vẫn lấy lại. Dừng giữa chừng thì chạy lại để resume:

```bat
python refresh_details.py
python refresh_details.py --fresh
```

Hoặc `refresh_details.bat`. Khoảng 9.000 brand nên mất nhiều giờ.

Chỉ quét listing để xem mới / còn / đã xóa — **không** ghi catalog, **không** lấy detail:

```bat
python check_brands.py
python check_brands.py --fresh
```

Quét traffic + Google Ads cho brand chưa có trong `brand_metrics`:

```bat
python scan_metrics.py
```

Quét lại metrics một list shop_id (ghi đè traffic, giữ `allows_search_ads`):

```bat
python scan_metrics.py --refresh 263411 43018
```

Cần file `scan_metrics.secret.json` (git bỏ qua) hoặc biến môi trường `AITDK_SECRET`.

---

## Website local

```bat
python -m web.app
```

Hoặc `run_web.bat`. Mở http://127.0.0.1:5050

Tài khoản admin nằm trong `web\users.json`.

Trên danh sách: lọc **Traffic tối thiểu**, **Search Ads**, sắp xếp theo traffic.

---

## File kết quả (thư mục `data\`)

| File | Nội dung |
|---|---|
| `uppromote.db` | Catalog + detail + skipped + traffic/ads + presence |
| `brands\{shop_id}.json` | Chi tiết từng brand |
| `presence_latest.json` | Tóm tắt lần quét listing |
| `presence_new.csv` | Brand mới |
| `presence_still.csv` | Brand còn trên marketplace |
| `presence_missing.csv` | Brand đã biến khỏi listing |
| `daily_latest.json` | Tóm tắt lần daily sync |
| `daily_progress.json` | Checkpoint daily (xóa nếu muốn chạy lại từ đầu) |
| `refresh_details_progress.json` | Checkpoint refresh detail toàn bộ |
| `refresh_details_latest.json` | Tóm tắt lần refresh detail |

---

## Local và server

Các lệnh crawl **chỉ chạy máy local**, không tự đổi [upproinfo.com](https://upproinfo.com).

Đẩy catalog lên server (backup theo ngày, rồi **gop** `uppromote.db` + upload `brands/`):

```bat
update.bat
```

Hoặc `python update_data.py`.

Không đè `billing.db`. Không thay nguyên file `uppromote.db` trên server — chỉ upsert bảng catalog. Backup nằm ở `/var/backups/upproinfo/YYYY-MM-DD` trên VPS.

Cần file `web\_push_vietqr.py` (git bỏ qua) để SSH. Copy từ máy đã có file này nếu local chưa có.

Không commit: `scan_metrics.secret.json`, `web\vietqr.secret.json`, `web\users.json` (mật khẩu), `web\_push_vietqr.py`.

python scan_metrics.py --refresh
