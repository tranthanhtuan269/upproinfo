"""VietQR: tao ma QR dong, doi soat callback BĐSD theo api.vietqr.vn."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import qrcode
import io
import base64
import os
import re
import secrets
import unicodedata
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import billing

WEB_DIR = Path(__file__).resolve().parent
CONFIG_PATH = WEB_DIR / "vietqr.json"
SECRET_PATH = WEB_DIR / "vietqr.secret.json"
DEFAULT_BASE = "https://dev.vietqr.org"
PUBLIC_GENERATE = "https://api.vietqr.io/v2/generate"
CONTENT_RE = re.compile(r"[^A-Za-z0-9]+")
BANK_BINS = {
    "ABB": "970425",
    "ACB": "970416",
    "BAB": "970409",
    "BIDV": "970418",
    "BVB": "970438",
    "EIB": "970431",
    "HDB": "970437",
    "ICB": "970415",
    "KLB": "970452",
    "LPB": "970449",
    "MB": "970422",
    "MSB": "970426",
    "NAB": "970428",
    "OCB": "970448",
    "PGB": "970430",
    "PVCB": "970412",
    "SEAB": "970440",
    "SHB": "970443",
    "STB": "970403",
    "TCB": "970407",
    "TPB": "970423",
    "VAB": "970427",
    "VBA": "970405",
    "VCB": "970436",
    "VCCB": "970454",
    "VIB": "970441",
    "VPB": "970432",
}


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict:
    data = _read_json(CONFIG_PATH)
    secret = _read_json(SECRET_PATH)
    merged = dict(data)
    merged.update({key: value for key, value in secret.items() if value not in ("", None)})
    for key in (
        "username",
        "password",
        "bank_code",
        "bank_account",
        "user_bank_name",
        "user_bank_name_display",
        "acq_id",
        "callback_username",
        "callback_password",
        "base_url",
    ):
        env = os.environ.get(f"VIETQR_{key.upper()}")
        if env:
            merged[key] = env
    return merged


def _cfg_text(cfg: dict, key: str) -> str:
    return str(cfg.get(key) or "").strip()


def is_configured() -> bool:
    cfg = load_config()
    return bool(_cfg_text(cfg, "bank_code") and _cfg_text(cfg, "bank_account") and _cfg_text(cfg, "user_bank_name"))


def has_merchant_api() -> bool:
    cfg = load_config()
    return bool(_cfg_text(cfg, "username") and _cfg_text(cfg, "password"))


def no_diacritics(text: str) -> str:
    norm = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")


def safe_content(text: str, limit: int = 23) -> str:
    cleaned = CONTENT_RE.sub("", no_diacritics(text or "").upper())
    return cleaned[:limit] or "UPPRO"


def is_sandbox(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return "dev.vietqr.org" in (_cfg_text(cfg, "base_url") or DEFAULT_BASE).lower()


def is_image_url(url: str) -> bool:
    text = str(url or "").strip()
    if text.startswith("data:image/"):
        return True
    lower = text.lower()
    if "qr-generated" in lower:
        return False
    if lower.startswith("/static/") or lower.startswith("/img/"):
        return any(token in lower for token in (".png", ".jpg", ".jpeg", ".webp", ".svg"))
    if not lower.startswith(("http://", "https://")):
        return False
    return any(token in lower for token in (".png", ".jpg", ".jpeg", ".webp", ".svg"))


def qr_data_uri(qr_code: str) -> str:
    payload = str(qr_code or "").strip()
    if not payload:
        return ""
    try:
        import io

        import qrcode

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(payload)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return (
            "https://api.qrserver.com/v1/create-qr-code/?size=320x320&ecc=M&margin=8&data="
            + quote(payload, safe="")
        )


def new_order_id() -> str:
    stamp = format(int(billing.utcnow().timestamp()), "x")[-7:]
    return safe_content(f"UP{stamp}{secrets.token_hex(2)}", 13)


def acq_id_of(cfg: dict) -> str:
    explicit = _cfg_text(cfg, "acq_id")
    if explicit:
        return explicit
    return BANK_BINS.get(_cfg_text(cfg, "bank_code").upper(), "")


def _http_json(url: str, payload: dict | None, headers: dict, timeout: int = 20) -> dict:
    body = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("desc") or data.get("error") or "")
        raise RuntimeError(message or f"HTTP {err.code}") from err
    except URLError as err:
        raise RuntimeError(str(err.reason or err)) from err
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise RuntimeError("Phản hồi VietQR không hợp lệ.") from err
    return data if isinstance(data, dict) else {}


def merchant_token(cfg: dict) -> str:
    user = _cfg_text(cfg, "username")
    password = _cfg_text(cfg, "password")
    if not user or not password:
        raise RuntimeError("Chưa có username/password VietQR.")
    raw = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    base = (_cfg_text(cfg, "base_url") or DEFAULT_BASE).rstrip("/")
    data = _http_json(
        f"{base}/vqr/api/token_generate",
        {},
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Basic {raw}",
            "User-Agent": "UpproInfo-VietQR",
        },
    )
    token = str(data.get("access_token") or data.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("VietQR không trả access token.")
    return token


def _from_official(cfg: dict, amount: int, order_id: str, content: str, return_url: str) -> dict:
    token = merchant_token(cfg)
    base = (_cfg_text(cfg, "base_url") or DEFAULT_BASE).rstrip("/")
    data = _http_json(
        f"{base}/vqr/api/qr/generate-customer",
        {
            "bankCode": _cfg_text(cfg, "bank_code").upper(),
            "bankAccount": _cfg_text(cfg, "bank_account"),
            "userBankName": no_diacritics(_cfg_text(cfg, "user_bank_name")).upper(),
            "content": content,
            "qrType": 0,
            "amount": amount,
            "orderId": order_id,
            "transType": "C",
            "urlLink": return_url,
            "note": "UpproInfo",
        },
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "UpproInfo-VietQR",
        },
    )
    if str(data.get("status") or "").upper() == "FAILED":
        raise RuntimeError(str(data.get("message") or "VietQR generate failed"))
    return data


def _from_public(cfg: dict, amount: int, content: str) -> dict:
    acq = acq_id_of(cfg)
    if not acq:
        raise RuntimeError("Thiếu acq_id / mã BIN ngân hàng.")
    data = _http_json(
        PUBLIC_GENERATE,
        {
            "accountNo": _cfg_text(cfg, "bank_account"),
            "accountName": no_diacritics(_cfg_text(cfg, "user_bank_name")).upper(),
            "acqId": acq,
            "amount": amount,
            "addInfo": content,
            "template": "compact2",
        },
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "UpproInfo-VietQR",
        },
    )
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict) or not (inner.get("qrDataURL") or inner.get("qrCode")):
        raise RuntimeError("Không tạo được mã VietQR.")
    return {
        "qrCode": inner.get("qrCode") or "",
        "qrLink": inner.get("qrDataURL") or "",
        "imgId": "",
        "transactionId": "",
        "orderId": content,
        "amount": amount,
        "content": content,
        "bankAccount": _cfg_text(cfg, "bank_account"),
        "bankCode": _cfg_text(cfg, "bank_code"),
        "userBankName": _cfg_text(cfg, "user_bank_name"),
    }


def account_display(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    return _cfg_text(cfg, "user_bank_name_display") or _cfg_text(cfg, "user_bank_name")


def static_qr_url() -> str:
    path = WEB_DIR / "static" / "img" / "vietqr-mb.jpg"
    version = int(path.stat().st_mtime) if path.is_file() else 0
    return f"/static/img/vietqr-mb.jpg?v={version}"



def _build_tlv(tag: str, val: str) -> str:
    return f"{tag}{len(val):02d}{val}"

def _crc16_ccitt(data: str) -> str:
    crc = 0xFFFF
    for ch in data.encode('ascii'):
        crc ^= (ch << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"

def generate_mb_vietqr_payload(acq_id: str, account_no: str, amount: int | str, content: str = "") -> str:
    guid = _build_tlv("00", "A000000727")
    beneficiary = _build_tlv("00", str(acq_id)) + _build_tlv("01", str(account_no))
    service = _build_tlv("02", "QRIBFTTA")
    tag38_val = guid + _build_tlv("01", beneficiary) + service
    tag38 = _build_tlv("38", tag38_val)

    payload = (
        _build_tlv("00", "01") +
        _build_tlv("01", "12") +
        tag38 +
        _build_tlv("53", "704")
    )
    if amount and int(amount) > 0:
        payload += _build_tlv("54", str(int(amount)))
    payload += _build_tlv("58", "VN")
    if content:
        payload += _build_tlv("62", _build_tlv("08", str(content)))
    payload += "6304"
    return payload + _crc16_ccitt(payload)

def generate_mb_qr_base64(amount: int, content: str, cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or load_config()
    acq = acq_id_of(cfg) or "970422"
    account = _cfg_text(cfg, "bank_account")
    if not account:
        return "", ""
    qr_payload = generate_mb_vietqr_payload(acq, account, amount, content)
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(qr_payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return data_uri, qr_payload

def branded_qr_url(amount: int, content: str, cfg: dict | None = None) -> str:
    data_uri, _ = generate_mb_qr_base64(amount, content, cfg)
    return data_uri


def create_payment(amount: int, return_url: str, order_id: str = "", content: str = "") -> dict:
    if not is_configured():
        raise RuntimeError("Chưa cấu hình VietQR.")
    cfg = load_config()
    content = str(content or order_id or "").strip() or (safe_content(order_id, 13) if order_id else new_order_id())
    order_id = str(order_id or content).strip()
    image, raw_payload = generate_mb_qr_base64(int(amount), content, cfg)
    if not image:
        raise RuntimeError("Không tạo được mã QR MBBank.")
    return {
        "order_id": order_id,
        "content": content,
        "amount": int(amount),
        "qr_image": image,
        "qr_code": raw_payload,
        "qr_link": image,
        "transaction_id": order_id,
        "bank_code": _cfg_text(cfg, "bank_code").upper(),
        "bank_account": _cfg_text(cfg, "bank_account"),
        "user_bank_name": account_display(cfg),
        "raw": {"source": "MBBank/NAPAS EMVCo (Local)", "account": _cfg_text(cfg, "bank_account")},
    }


def simulate_payment(content: str, amount: int) -> dict:
    cfg = load_config()
    if not is_sandbox(cfg):
        raise RuntimeError("Test callback chỉ dùng trên sandbox VietQR.")
    token = merchant_token(cfg)
    base = (_cfg_text(cfg, "base_url") or DEFAULT_BASE).rstrip("/")
    data = _http_json(
        f"{base}/vqr/bank/api/test/transaction-callback",
        {
            "bankAccount": _cfg_text(cfg, "bank_account"),
            "content": str(content or "").strip(),
            "amount": int(amount),
            "transType": "C",
            "bankCode": _cfg_text(cfg, "bank_code").upper(),
        },
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "UpproInfo-VietQR",
        },
    )
    if str(data.get("status") or "").upper() == "FAILED":
        raise RuntimeError(str(data.get("message") or "Test callback failed"))
    return data


def check_order(order_id: str) -> dict:
    if not has_merchant_api():
        return {"paid": False, "raw": {}}
    cfg = load_config()
    token = merchant_token(cfg)
    base = (_cfg_text(cfg, "base_url") or DEFAULT_BASE).rstrip("/")
    account = _cfg_text(cfg, "bank_account")
    checksum = hashlib.md5(f"{account}{_cfg_text(cfg, 'username')}".encode("utf-8")).hexdigest()
    data = _http_json(
        f"{base}/vqr/api/transactions/check-order",
        {
            "bankAccount": account,
            "type": "0",
            "value": order_id,
            "checkSum": checksum,
        },
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "UpproInfo-VietQR",
        },
    )
    rows = data.get("data")
    items = rows if isinstance(rows, list) else []
    paid = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("timePaid") or str(item.get("transType") or "").upper() == "C":
            paid = True
            break
    if str(data.get("status") or "").upper() == "SUCCESS" and items:
        paid = True
    return {"paid": paid, "raw": data}


TOKEN_TTL_SEC = 300
SYNC_REQUIRED = (
    ("bankaccount", ("bankaccount", "bankAccount", "bank_account")),
    ("amount", ("amount",)),
    ("transType", ("transType", "transtype", "trans_type")),
    ("content", ("content",)),
    ("transactionid", ("transactionid", "transactionId", "transaction_id")),
    ("transactiontime", ("transactiontime", "transactionTime", "transaction_time")),
    ("referencenumber", ("referencenumber", "referenceNumber", "reference_number")),
    ("orderId", ("orderId", "orderid", "order_id")),
)


def parse_basic_auth(header: str) -> tuple[str, str]:
    text = (header or "").strip()
    if not text.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(text[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return "", ""
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else ("", "")


def callback_auth_ok(header: str) -> bool:
    cfg = load_config()
    expected_user = _cfg_text(cfg, "callback_username")
    expected_pass = _cfg_text(cfg, "callback_password")
    if not expected_user or not expected_pass:
        return False
    user, password = parse_basic_auth(header)
    try:
        return hmac.compare_digest(user, expected_user) and hmac.compare_digest(password, expected_pass)
    except ValueError:
        return False


def token_failed(message: str) -> tuple[dict, int]:
    return {"status": "FAILED", "message": message}, 400


def issue_callback_token() -> dict:
    token = secrets.token_urlsafe(32)
    expires = billing.utcnow() + timedelta(seconds=TOKEN_TTL_SEC)
    billing.save_vietqr_token(token, billing.to_iso(expires))
    return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL_SEC}


def handle_token_request(header: str) -> tuple[dict, int]:
    text = (header or "").strip()
    if not text or not text.lower().startswith("basic "):
        return token_failed("INVALID_AUTH_HEADER")
    if not callback_auth_ok(text):
        return token_failed("INVALID_CREDENTIALS")
    return issue_callback_token(), 200


def bearer_ok(header: str) -> bool:
    text = (header or "").strip()
    if not text.lower().startswith("bearer "):
        return False
    return billing.vietqr_token_ok(text[7:].strip())


def _payload_field(payload: dict, *names: str):
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return ""


def sync_amount(payload: dict) -> int | None:
    raw = _payload_field(payload, "amount")
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    text = str(raw).strip().replace(",", "").split(" ")[0]
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return None


def looks_like_order(text: str) -> bool:
    compact = "".join(ch for ch in str(text or "").upper() if ch.isalnum())
    return compact.startswith("UP") or compact.startswith("VQR") or "VQR" in compact


def sync_success(ref: str, message: str = "Transaction processed successfully") -> tuple[dict, int]:
    return {
        "error": False,
        "errorReason": "",
        "toastMessage": message,
        "object": {"reftransactionid": str(ref or "")},
    }, 200


def sync_error(reason: str, message: str, code: int = 400) -> tuple[dict, int]:
    return {
        "error": True,
        "errorReason": reason,
        "toastMessage": message,
        "object": None,
    }, code


def parse_sync_payload(payload: dict) -> tuple[dict | None, str]:
    if not isinstance(payload, dict) or not payload:
        return None, "bankaccount"
    parsed: dict[str, object] = {}
    for canonical, names in SYNC_REQUIRED:
        value = _payload_field(payload, *names)
        if value in (None, ""):
            return None, canonical
        parsed[canonical] = value
    parsed["terminalCode"] = _payload_field(payload, "terminalCode", "terminal_code")
    parsed["subTerminalCode"] = _payload_field(payload, "subTerminalCode", "sub_terminal_code")
    parsed["serviceCode"] = _payload_field(payload, "serviceCode", "service_code")
    parsed["urlLink"] = _payload_field(payload, "urlLink", "url_link")
    parsed["sign"] = _payload_field(payload, "sign")
    parsed["amount"] = sync_amount(parsed)
    if parsed["amount"] is None:
        return None, "amount"
    parsed["transType"] = str(parsed["transType"]).strip().upper()
    parsed["transactionid"] = str(parsed["transactionid"]).strip()
    parsed["referencenumber"] = str(parsed["referencenumber"]).strip()
    parsed["orderId"] = str(parsed["orderId"]).strip()
    parsed["content"] = str(parsed["content"]).strip()
    parsed["bankaccount"] = str(parsed["bankaccount"]).strip()
    return parsed, ""


def _store_callback(parsed: dict, status: str, trade_no: str = "", raw: dict | None = None) -> None:
    if not parsed.get("transactionid"):
        return
    billing.save_vietqr_callback(
        {
            "transactionid": parsed["transactionid"],
            "referencenumber": parsed.get("referencenumber") or "",
            "order_id": parsed.get("orderId") or "",
            "amount": parsed.get("amount") or "",
            "trans_type": parsed.get("transType") or "",
            "content": parsed.get("content") or "",
            "bankaccount": parsed.get("bankaccount") or "",
            "transactiontime": parsed.get("transactiontime") or "",
            "status": status,
            "merchant_trade_no": trade_no,
            "raw_json": json.dumps(raw or parsed, ensure_ascii=False, default=str),
        }
    )


def apply_sync(payload: dict) -> dict | None:
    parsed, missing = parse_sync_payload(payload if isinstance(payload, dict) else {})
    if missing or not parsed or str(parsed.get("transType") or "").upper() != "C":
        return None
    return billing.fulfill_vietqr_payment(
        content=str(parsed.get("content") or "").strip(),
        order_id=str(parsed.get("orderId") or "").strip(),
        amount=parsed.get("amount") if isinstance(parsed.get("amount"), int) else None,
        raw_json=json.dumps(payload, ensure_ascii=False),
    )


def handle_sync_request(header: str, payload: dict) -> tuple[dict, int]:
    text = (header or "").strip()
    if not text or not text.lower().startswith("bearer "):
        return sync_error("INVALID_AUTH_HEADER", "Authorization header is missing or invalid", 401)
    if not bearer_ok(text):
        return sync_error("INVALID_TOKEN", "Invalid or expired token", 401)
    parsed, missing = parse_sync_payload(payload if isinstance(payload, dict) else {})
    if missing or not parsed:
        return sync_error("MISSING_FIELD", f"Missing required field: {missing or 'body'}")

    existing = billing.get_vietqr_callback(str(parsed["transactionid"]))
    if existing and existing.get("status") in {"paid", "ignored"}:
        return sync_success(existing.get("merchant_trade_no") or existing.get("transactionid") or "")

    trans_type = str(parsed["transType"]).upper()
    if trans_type == "D":
        _store_callback(parsed, "ignored", raw=payload)
        return sync_success(str(parsed["transactionid"]), "Debit transaction ignored")
    if trans_type != "C":
        return sync_error("INVALID_TRANSTYPE", "transType must be C or D")

    billing.expire_stale_orders()
    content = str(parsed.get("content") or "").strip()
    order_id = str(parsed.get("orderId") or "").strip()
    amount = parsed["amount"] if isinstance(parsed["amount"], int) else None
    order = billing.find_vietqr_order(content, order_id)
    if order and amount is not None:
        try:
            if int(order["amount"]) != int(amount):
                _store_callback(parsed, "amount_mismatch", order["merchant_trade_no"], payload)
                return sync_error("INVALID_AMOUNT", "Amount does not match the order")
        except (TypeError, ValueError):
            return sync_error("INVALID_AMOUNT", "Amount does not match the order")
    if not order:
        _store_callback(parsed, "unmatched", raw=payload)
        try:
            import notifier
            notifier.notify_unmatched_payment(
                amount=f"{amount:,}" if amount else str(parsed.get("amount")),
                content=content,
                trans_id=str(parsed.get("transactionid"))
            )
        except Exception:
            pass
        if looks_like_order(content) or looks_like_order(order_id):
            return sync_error("ORDER_NOT_FOUND", "Order not found")
        return sync_success(str(parsed["transactionid"]), "Transaction received")

    fulfilled = billing.fulfill_paid_order(
        order["merchant_trade_no"],
        json.dumps(payload, ensure_ascii=False),
    )
    trade = (fulfilled or order).get("merchant_trade_no") or ""
    _store_callback(parsed, "paid", trade, payload)
    return sync_success(trade)
