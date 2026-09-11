"""Binance Pay: tao/query don USDT va webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WEB_DIR = Path(__file__).resolve().parent
CONFIG_PATH = WEB_DIR / "binance_pay.json"
DEFAULT_BASE = "https://bpay.binanceapi.com"

PAID_STATUSES = {"PAID", "PAY_SUCCESS"}
CLOSED_STATUSES = {"CANCELED", "CANCELLED", "EXPIRED", "ERROR", "PAY_CLOSED", "CLOSED"}


def load_config() -> dict:
    env_key = (os.environ.get("BINANCE_PAY_API_KEY") or "").strip()
    env_secret = (os.environ.get("BINANCE_PAY_API_SECRET") or "").strip()
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            parsed = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {}
    api_key = env_key or str(data.get("api_key") or "").strip()
    api_secret = env_secret or str(data.get("api_secret") or "").strip()
    base_url = str(data.get("base_url") or DEFAULT_BASE).strip().rstrip("/") or DEFAULT_BASE
    webhook = str(data.get("webhook_public_url") or "").strip()
    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "base_url": base_url,
        "webhook_public_url": webhook,
    }


def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg["api_key"] and cfg["api_secret"])


def _sign(timestamp: str, nonce: str, body: str, secret: str) -> str:
    payload = f"{timestamp}\n{nonce}\n{body}\n"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512)
    return digest.hexdigest().upper()


def _post(path: str, payload: dict) -> dict:
    cfg = load_config()
    if not cfg["api_key"] or not cfg["api_secret"]:
        raise RuntimeError("not_configured")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    timestamp = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    signature = _sign(timestamp, nonce, body, cfg["api_secret"])
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "BinancePay-Timestamp": timestamp,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": cfg["api_key"],
        "BinancePay-Signature": signature,
    }
    url = cfg["base_url"] + path
    req = Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        message = ""
        if isinstance(data, dict):
            message = str(data.get("errorMessage") or data.get("message") or "")
        raise RuntimeError(message or f"HTTP {err.code}") from err
    except URLError as err:
        raise RuntimeError(str(err.reason or err)) from err
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as err:
        raise RuntimeError("invalid_json") from err
    if not isinstance(data, dict):
        raise RuntimeError("invalid_json")
    status = str(data.get("status") or "")
    if status and status != "SUCCESS":
        raise RuntimeError(str(data.get("errorMessage") or status))
    return data


def create_order(
    *,
    merchant_trade_no: str,
    amount: str,
    goods_id: str,
    goods_name: str,
    description: str,
    return_url: str,
    cancel_url: str,
) -> dict:
    cfg = load_config()
    payload = {
        "env": {"terminalType": "WEB"},
        "merchantTradeNo": merchant_trade_no,
        "orderAmount": float(amount),
        "currency": "USDT",
        "description": description,
        "goodsDetails": [
            {
                "goodsType": "02",
                "goodsCategory": "Z0000",
                "referenceGoodsId": goods_id,
                "goodsName": goods_name[:250],
            }
        ],
        "returnUrl": return_url,
        "cancelUrl": cancel_url,
    }
    webhook = cfg.get("webhook_public_url") or ""
    if webhook.startswith("https://"):
        payload["webhookUrl"] = webhook
    result = _post("/binancepay/openapi/v3/order", payload)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    checkout = str(data.get("checkoutUrl") or data.get("universalUrl") or "")
    return {
        "prepay_id": str(data.get("prepayId") or ""),
        "checkout_url": checkout,
        "qrcode_link": str(data.get("qrcodeLink") or ""),
        "raw": result,
    }


def query_order(merchant_trade_no: str) -> dict:
    result = _post(
        "/binancepay/openapi/v2/order/query",
        {"merchantTradeNo": merchant_trade_no},
    )
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    status = str(data.get("status") or "").upper()
    return {
        "status": status,
        "paid": status in PAID_STATUSES,
        "closed": status in CLOSED_STATUSES,
        "raw": result,
    }


def parse_webhook_trade_no(body: dict) -> str:
    payload = body.get("data")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = body if isinstance(body, dict) else {}
    return str(payload.get("merchantTradeNo") or body.get("merchantTradeNo") or "").strip()


def webhook_biz_status(body: dict) -> str:
    return str(body.get("bizStatus") or "").upper()


def verify_webhook_signature(timestamp: str, nonce: str, raw_body: str, signature_b64: str, cert_sn: str) -> bool:
    """RSA SHA256 neu co cryptography; neu khong thi False (van query lai Binance)."""
    if not timestamp or not nonce or not signature_b64:
        return False
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        return False
    try:
        certs = _post("/binancepay/openapi/certificates", {})
    except RuntimeError:
        return False
    rows = certs.get("data") if isinstance(certs.get("data"), list) else []
    pem = ""
    for item in rows:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("certSerial") or item.get("certSn") or "")
        if cert_sn and serial and serial != cert_sn:
            continue
        pem = str(item.get("certPublic") or "")
        if pem:
            break
    if not pem:
        return False
    try:
        import base64

        payload = f"{timestamp}\n{nonce}\n{raw_body}\n".encode("utf-8")
        signature = base64.b64decode(signature_b64)
        key = serialization.load_pem_public_key(pem.encode("utf-8"))
        key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False
