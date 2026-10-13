import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

SECRET_PATH = Path(__file__).resolve().parent / "telegram_alert.secret.json"


def send_telegram_alert(message: str) -> bool:
    """Gui thong bao den nhom Telegram khi phat hien vi pham thiet bi."""
    if not SECRET_PATH.exists():
        return False
    try:
        cfg = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
        bot_token = cfg.get("bot_token")
        chat_id = cfg.get("group_chat_id")
        topic_id = cfg.get("topic_id")
        if not bot_token or not chat_id:
            return False

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if topic_id:
            payload["message_thread_id"] = topic_id

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return bool(res.get("ok"))
    except Exception as exc:
        logger.warning("Loi gui telegram alert: %s", exc)
        return False
