#!/usr/bin/env bash
set -uo pipefail

APP_DIR="/var/www/upproinfo"
PYTHON="${APP_DIR}/.venv/bin/python"
LOG_DIR="${APP_DIR}/logs"
LOCK_FILE="/tmp/upproinfo_cron.lock"

mkdir -p "${LOG_DIR}"
DATE_TAG=$(date +"%Y%m%d_%H%M%S")
RUN_LOG="${LOG_DIR}/cron_pipeline_${DATE_TAG}.log"
LATEST_LOG="${LOG_DIR}/cron_pipeline_latest.log"

exec > >(tee -a "${RUN_LOG}" > "${LATEST_LOG}") 2>&1

echo "=================================================="
echo "🚀 BẮT ĐẦU CHU KỲ QUÉT TỰ ĐỘNG UPPROINFO: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================="

# Check lock (chống chạy đè nếu chu kỳ trước chưa xong)
if [ -f "${LOCK_FILE}" ]; then
    PID=$(cat "${LOCK_FILE}" 2>/dev/null || echo "")
    if [ -n "${PID}" ] && kill -0 "${PID}" 2>/dev/null; then
        echo "⚠️ Chu kỳ trước (PID: ${PID}) vẫn đang chạy. Bỏ qua lần này."
        exit 0
    else
        echo "ℹ️ Lock file cũ bị treo. Xóa lock để tiếp tục."
        rm -f "${LOCK_FILE}"
    fi
fi

echo "$$" > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"; echo "Đã dọn dẹp lock file."' EXIT

cd "${APP_DIR}" || exit 1

# 1. daily_sync.py --fresh
echo -e "\n[1/4] Chạy daily_sync.py --fresh..."
"${PYTHON}" daily_sync.py --fresh
STEP1_EC=$?
echo "[1/4] Hoàn tất daily_sync (Exit Code: ${STEP1_EC})"

# 2. refresh_details.py --fresh
echo -e "\n[2/4] Chạy refresh_details.py --fresh..."
"${PYTHON}" refresh_details.py --fresh
STEP2_EC=$?
echo "[2/4] Hoàn tất refresh_details (Exit Code: ${STEP2_EC})"

# 3. scan_metrics.py --fresh
echo -e "\n[3/4] Chạy scan_metrics.py --fresh..."
"${PYTHON}" scan_metrics.py --fresh
STEP3_EC=$?
echo "[3/4] Hoàn tất scan_metrics (Exit Code: ${STEP3_EC})"

# 4. refresh_search_ads_brands.py --fresh
echo -e "\n[4/4] Chạy refresh_search_ads_brands.py --fresh..."
"${PYTHON}" refresh_search_ads_brands.py --fresh
STEP4_EC=$?
echo "[4/4] Hoàn tất refresh_search_ads_brands (Exit Code: ${STEP4_EC})"

echo -e "\n=================================================="
echo "🎉 TẤT CẢ CÁC BƯỚC ĐÃ HOÀN TẤT TUẦN TỰ: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================="
