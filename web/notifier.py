import json
import logging
import threading
from pathlib import Path
import requests

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "telegram_notify.json"

def _load_config():
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error loading telegram notify config: {e}")
        return None

def send_telegram_message(text: str):
    config = _load_config()
    if not config or not config.get("enabled"):
        return
    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not token or not chat_id:
        return

    def _worker():
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            requests.post(url, json=payload, timeout=8)
        except Exception as ex:
            logger.error(f"Failed to send Telegram notification: {ex}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def notify_new_order(order: dict):
    amount_str = f"{int(order.get('amount', 0)):,} đ"
    plan_name = "Gói 1 Tháng (30 ngày)" if order.get("plan") == "month" else "Gói 1 Năm (365 ngày)"
    pay_code = order.get("pay_content") or str(order.get("id") or "")
    
    msg = (
        "🛒 <b>[UpproInfo] CÓ ĐƠN HÀNG MỚI ĐANG CHỜ</b>\n\n"
        f"👤 <b>Khách hàng:</b> <code>{order.get('username')}</code>\n"
        f"📦 <b>Gói dịch vụ:</b> {plan_name}\n"
        f"💰 <b>Số tiền:</b> <b>{amount_str}</b>\n"
        f"🏷️ <b>Nội dung CK:</b> <code>{pay_code}</code>\n"
        f"🔖 <b>Mã đơn:</b> <code>{order.get('merchant_trade_no')}</code>\n\n"
        "<i>Khách đang ở trang quét mã QR thanh toán...</i>"
    )
    send_telegram_message(msg)

def notify_order_paid(order: dict, expires_at: str = "", source: str = "Tự động VietQR"):
    amount_str = f"{int(order.get('amount', 0)):,} đ"
    plan_name = "Gói 1 Tháng (30 ngày)" if order.get("plan") == "month" else "Gói 1 Năm (365 ngày)"
    exp_display = expires_at[:10] if expires_at else "Đã kích hoạt"
    
    msg = (
        "🎉 <b>[UpproInfo] THANH TOÁN THÀNH CÔNG!</b>\n\n"
        f"👤 <b>Khách hàng:</b> <code>{order.get('username')}</code>\n"
        f"📦 <b>Gói:</b> {plan_name}\n"
        f"💰 <b>Số tiền:</b> <b>+{amount_str}</b>\n"
        f"📅 <b>Hạn dùng mới:</b> <code>{exp_display}</code>\n"
        f"⚡ <b>Nguồn xác nhận:</b> {source}\n"
        f"🔖 <b>Mã đơn:</b> <code>{order.get('merchant_trade_no')}</code>"
    )
    send_telegram_message(msg)

def notify_unmatched_payment(amount: str, content: str, trans_id: str):
    msg = (
        "⚠️ <b>[UpproInfo] CÓ TIỀN VÀO NHƯNG CHƯA KHỚP ĐƠN!</b>\n\n"
        f"💰 <b>Số tiền:</b> <b>+{amount} đ</b>\n"
        f"📝 <b>Nội dung chuyển khoản:</b> <code>{content}</code>\n"
        f"🏦 <b>Mã GD ngân hàng:</b> <code>{trans_id}</code>\n\n"
        "👉 <i>Anh kiểm tra mục Giao dịch Admin để duyệt tay cho khách nếu cần nhé:</i>\n"
        "https://upproinfo.com/admin/transactions"
    )
    send_telegram_message(msg)
