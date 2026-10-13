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

Quy trình đồng bộ hàng ngày (Daily Sync):
- Khi bắt đầu, tự động đánh dấu toàn bộ dự án thành "ngưng hoạt động" (Inactive).
- Khi quét marketplace (find-offers), dự án nào còn hoạt động sẽ được cập nhật ngay thành "đang hoạt động" (Active).
- Dự án nào không còn tìm thấy trên sàn sẽ tự động nằm ở "Inactive brands" (Thương hiệu dừng hoạt động).
- Lấy detail và quét traffic/ads cho brand mới.

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

Cập nhật lại detail cho brand (hỗ trợ toàn bộ hoặc danh sách shop_id chỉ định). Dừng giữa chừng thì chạy lại để resume:

```bat
# Quét toàn bộ brand trong catalog (resume nếu dở dang):
python refresh_details.py

# Bắt đầu chu kỳ quét mới từ đầu:
python refresh_details.py --fresh

# Quét lại detail cho danh sách shop_id cụ thể:
python refresh_details.py 263411 43018
python refresh_details.py --fresh 263411 43018
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

---

## Kiểm tra và làm sạch Search Ads

Kiểm tra danh sách các brand cho phép chạy Google / Search Ads theo logic mới:

```bat
# Kiểm tra toàn bộ catalog:
python check_search_ads.py

# Tự động làm sạch các brand bị cấm nhưng còn sót tag search_ads và đồng bộ DB:
python check_search_ads.py --fix

# Kiểm tra riêng 1 hoặc nhiều shop_id cụ thể:
python check_search_ads.py 221103
```

---

## Quét lại chỉ riêng các Brand cho phép Search Ads

Tự động gom toàn bộ các brand đang cho phép Search Ads / Google Ads trên hệ thống để cào lại detail (kiểm tra xem shop có mới đổi sang cấm Google Ads hay không), không cần điền tham số:

```bat
python refresh_search_ads_brands.py
```

Hoặc double-click `refresh_search_ads_brands.bat` trên Windows.

- Tự động lấy danh sách brand cho phép Search Ads từ Database / JSON.
- Có tính năng lưu tiến trình (checkpoint), nếu tắt ngang thì chạy lại sẽ tiếp tục từ vị trí dở dang.
- Muốn quét lại từ đầu (bỏ checkpoint): `python refresh_search_ads_brands.py --fresh`.

---

## Tự động hóa định kỳ (Cronjob & Batch Pipeline)

Script chạy tuần tự 4 bước với tham số `--fresh` và ghi toàn bộ log ra thư mục `logs/`:
1. `daily_sync.py --fresh` (Đánh dấu ngưng hoạt động, quét listing, cập nhật brand active/inactive)
2. `refresh_details.py --fresh` (Quét lại toàn bộ detail các brand, lọc từ khóa cấm Google Ads)
3. `scan_metrics.py --fresh` (Quét traffic AITDK, thời lượng xem, bounce rate, Google Ads transparency)
4. `refresh_search_ads_brands.py --fresh` (Quét kiểm tra chuyên sâu riêng các brand Search Ads)

### 1. Trên Server Linux (Crontab):
Chạy tự động mỗi ngày vào lúc 02:00 sáng (hoặc giờ mong muốn):
```bash
0 2 * * * /var/www/upproinfo/run_cron_pipeline.sh >/dev/null 2>&1
```
Xem log trực tiếp:
```bash
tail -f /var/www/upproinfo/logs/cron_pipeline_latest.log
```

### 2. Trên Windows (Local):
Chạy thủ công hoặc đặt lịch trong **Task Scheduler**:
```bat
run_cron_pipeline.bat
```
Log được lưu riêng từng ngày tại `logs\cron_pipeline_YYYYMMDD_HHMMSS.log`.

---

## Đăng ký hàng loạt GoAffPro (site goaff.upproinfo.com)

Chức năng nằm ở project `/var/www/goaffpro` (DB dùng chung `data/goaffpro.db`).

- Doc chi tiết: [`/var/www/goaffpro/BULK_ENROLLMENT.md`](/var/www/goaffpro/BULK_ENROLLMENT.md)
- Engine: `web/bulk_enrollment.py` → gọi `POST /v1/user/enroll/{store_id}`
- **Lưu ý (2026-10):** API enroll chung của sàn đã không còn hoạt động cho bulk (job chạy xong vẫn `success=0`). Không dùng CAPTCHA bypass; xem mục hướng xử lý trong doc trên.

---

## Đồng bộ danh mục Store sàn GoAffPro (`goaffpro_sync.py`)

Hệ thống hỗ trợ tự động cào và đồng bộ toàn bộ **21.986+ store** từ Marketplace Directory của **GoAffPro** (https://goaffpro.com/affiliate/stores/search) về SQLite và CSV.

### Các lệnh thực thi:

```bash
# 1. Chạy đồng bộ thông thường (hoặc tiếp tục chu kỳ dở dang)
python goaffpro_sync.py

# 2. Làm mới toàn bộ danh mục từ đầu (đánh dấu inactive trước khi quét)
python goaffpro_sync.py --fresh

# 3. Chạy thử nghiệm 50 store đầu tiên
python goaffpro_sync.py --limit 50

# 4. Xem thống kê database hiện tại
python goaffpro_sync.py --status

# 5. Xuất lại file CSV từ database
python goaffpro_sync.py --export
```

* Trên Windows: Chạy nhấp đúp file `goaffpro_sync.bat`.

### Tệp dữ liệu sinh ra:
* **SQLite Database:** `data/goaffpro.db` (Bảng `stores` lưu đầy đủ ID, Name, Website, Domain, Logo, Currency, Commission, Cookie Days, Auto Approval, Portal URL, Status...).
* **File CSV:** `data/goaffpro_stores.csv` (21.986 dòng).
* **Báo cáo tóm tắt:** `data/goaffpro_latest.json`.
* **Tiến trình:** `data/goaffpro_progress.json`.


### Quét Traffic & Google Ads cho GoAffPro (`goaff_scan_metrics.py`):

Tự động quét Traffic 3 tháng gần nhất (API AITDK / SimilarWeb) cho danh mục store GoAffPro:

```bash
# 1. Quét backlog các store chưa có metrics (hoặc quá 7 ngày chưa quét)
python goaff_scan_metrics.py

# 2. Quét lại toàn bộ tất cả store từ đầu (--fresh)
python goaff_scan_metrics.py --fresh

# 3. Quét thử nghiệm giới hạn 50 store
python goaff_scan_metrics.py --limit 50

# 4. Quét theo danh sách store ID chỉ định
python goaff_scan_metrics.py 7193380 7194084 7194108
```

* Trên Windows: Nhấp đúp vào file `goaff_scan_metrics.bat`.
* Bảng dữ liệu lưu tại: `data/goaffpro.db` -> Bảng `store_metrics`.
