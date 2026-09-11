"""Dang nhap Google / GitHub bang OAuth 2.0."""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WEB_DIR = Path(__file__).resolve().parent
OAUTH_PATH = WEB_DIR / "oauth.json"
OAUTH_SECRET_PATH = WEB_DIR / "oauth.secret.json"
PROVIDERS = ("google", "github")
USERNAME_CLEAN = re.compile(r"[^A-Za-z0-9._-]+")


def _read_oauth_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_oauth_file() -> dict:
    data = _read_oauth_json(OAUTH_PATH)
    secret = _read_oauth_json(OAUTH_SECRET_PATH)
    merged = dict(data)
    for key in PROVIDERS:
        item = {}
        if isinstance(data.get(key), dict):
            item.update(data[key])
        if isinstance(secret.get(key), dict):
            item.update(secret[key])
        if item:
            merged[key] = item
    return merged


def provider_config(provider: str) -> dict | None:
    if provider not in PROVIDERS:
        return None
    env_id = (os.environ.get(f"{provider.upper()}_CLIENT_ID") or "").strip()
    env_secret = (os.environ.get(f"{provider.upper()}_CLIENT_SECRET") or "").strip()
    file_data = load_oauth_file().get(provider) or {}
    client_id = env_id or str(file_data.get("client_id") or "").strip()
    client_secret = env_secret or str(file_data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return None
    return {"client_id": client_id, "client_secret": client_secret}


def http_json(url: str, method: str = "GET", data: dict | None = None, headers: dict | None = None):
    hdrs = {
        "Accept": "application/json",
        "User-Agent": "UpproInfo-OAuth",
    }
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = Request(url, data=body, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            message = payload.get("error_description") or payload.get("error") or payload.get("message") or f"HTTP {err.code}"
        else:
            message = f"HTTP {err.code}"
        raise RuntimeError(str(message)) from err
    except URLError as err:
        raise RuntimeError(str(err.reason or err)) from err
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise RuntimeError("Phản hồi OAuth không hợp lệ.") from err


def authorize_url(provider: str, client_id: str, redirect_uri: str, state: str) -> str:
    if provider == "google":
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "access_type": "online",
                "prompt": "select_account",
            }
        )
    return "https://github.com/login/oauth/authorize?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        }
    )


def fetch_identity(provider: str, cfg: dict, redirect_uri: str, code: str) -> dict:
    if provider == "google":
        token = http_json(
            "https://oauth2.googleapis.com/token",
            method="POST",
            data={
                "code": code,
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if not isinstance(token, dict):
            raise RuntimeError("Google không trả access token.")
        access = str(token.get("access_token") or "")
        if not access:
            raise RuntimeError("Google không trả access token.")
        profile = http_json(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access}"},
        )
        if not isinstance(profile, dict):
            raise RuntimeError("Không lấy được tài khoản Google.")
        provider_id = str(profile.get("sub") or "")
        email = str(profile.get("email") or "").strip()
        name = str(profile.get("name") or "").strip()
        base = email.split("@", 1)[0] if email else name or "google"
        if not provider_id:
            raise RuntimeError("Không lấy được tài khoản Google.")
        return {"provider_id": provider_id, "email": email, "name": name, "username_base": base}

    token = http_json(
        "https://github.com/login/oauth/access_token",
        method="POST",
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    if not isinstance(token, dict):
        raise RuntimeError("GitHub không trả access token.")
    access = str(token.get("access_token") or "")
    if not access:
        raise RuntimeError("GitHub không trả access token.")
    auth = {"Authorization": f"Bearer {access}"}
    profile = http_json("https://api.github.com/user", headers=auth)
    if not isinstance(profile, dict):
        raise RuntimeError("Không lấy được tài khoản GitHub.")
    provider_id = str(profile.get("id") or "")
    login_name = str(profile.get("login") or "").strip()
    name = str(profile.get("name") or login_name).strip()
    email = str(profile.get("email") or "").strip()
    if not email:
        emails = http_json("https://api.github.com/user/emails", headers=auth)
        if isinstance(emails, list):
            primary = next((item for item in emails if isinstance(item, dict) and item.get("primary") and item.get("email")), None)
            chosen = primary or next((item for item in emails if isinstance(item, dict) and item.get("email")), None)
            if chosen:
                email = str(chosen.get("email") or "").strip()
    if not provider_id:
        raise RuntimeError("Không lấy được tài khoản GitHub.")
    return {
        "provider_id": provider_id,
        "email": email,
        "name": name,
        "username_base": login_name or (email.split("@", 1)[0] if email else "github"),
    }


def new_state() -> str:
    return secrets.token_urlsafe(24)


def safe_username(base: str) -> str:
    cleaned = USERNAME_CLEAN.sub("", (base or "").replace(" ", "."))
    cleaned = cleaned.strip("._-")[:28]
    if len(cleaned) < 3:
        cleaned = (cleaned + "user")[:3]
        if len(cleaned) < 3:
            cleaned = "user"
    return cleaned
