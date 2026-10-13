"""Polite Awin publisher client. One request at a time, through the configured proxy."""

from __future__ import annotations

import json
import random
import re
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SECRET_PATH = ROOT / "awin.secret.json"
COOKIE_PATH = ROOT / "data" / "awin" / "session_cookies.json"
UI = "https://ui.awin.com"
LOGIN_URL = (
    "https://ui.awin.com/idp/en/awin/login/prelogin?redirect=%2Flogin%3FnetworkGroup%3Dawin"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

LIST_GAP = 4.5
DETAIL_GAP = 7.0
BATCH_EVERY = 15
BATCH_PAUSE = 25.0


class AwinError(RuntimeError):
    pass


class AwinBlocked(AwinError):
    """Awin asked us to slow down or challenged the session. Stop the scan."""


def load_secret() -> dict[str, Any]:
    if not SECRET_PATH.exists():
        raise AwinError(f"Thieu {SECRET_PATH.name}. Can email, password va proxy.")
    data = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AwinError("awin.secret.json khong hop le.")
    return data


def _clean(text: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _number(text: str) -> float | None:
    raw = (text or "").strip().lower()
    if raw in {"", "n/a", "na", "-", "—"}:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", raw.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _days(text: str) -> int | None:
    match = re.search(r"(\d+)", text or "")
    if not match or "n/a" in (text or "").lower():
        return None
    return int(match.group(1))


def _membership(action: str) -> str:
    low = (action or "").strip().lower()
    if "promote" in low or low == "leave":
        return "joined"
    if "pending" in low:
        return "pending"
    if "reject" in low:
        return "rejected"
    if "suspend" in low:
        return "suspended"
    if "closed" in low:
        return "closed"
    if "accept" in low or "invitation" in low:
        return "invited"
    if "join" in low:
        return "not_joined"
    return low


class _RowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._in_row = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._cell_status: list[str] = []
        self._cells: list[dict[str, Any]] = []
        self._href = ""
        self._title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key: value or "" for key, value in attrs}
        if tag == "tr" and "merchantDirectory" not in data.get("class", ""):
            classes = data.get("class", "")
            if "light" in classes or "basic" in classes:
                self._in_row = True
                self._cells = []
                self._href = ""
                self._title = ""
        if not self._in_row:
            return
        if tag == "td":
            self._in_cell = True
            self._cell_text = []
            self._cell_status = []
        if tag == "a" and "merchant-profile/" in data.get("href", ""):
            self._href = data.get("href", "")
        if tag == "div" and data.get("title"):
            self._title = data.get("title", "")
        classes = data.get("class", "")
        match = re.search(r"status-(green|amber|red)", classes)
        if match and self._in_cell:
            self._cell_status.append(match.group(1))

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_cell:
            self._cells.append(
                {"text": _clean(" ".join(self._cell_text)), "status": list(self._cell_status)}
            )
            self._in_cell = False
        if tag == "tr" and self._in_row:
            self._in_row = False
            row = self._build()
            if row:
                self.rows.append(row)

    def handle_data(self, data: str) -> None:
        if self._in_cell and data.strip():
            self._cell_text.append(data.strip())

    def _build(self) -> dict[str, Any] | None:
        match = re.search(r"merchant-profile/(\d+)", self._href)
        if not match or len(self._cells) < 8:
            return None
        cells = self._cells
        action = cells[-1]["text"] if cells else ""
        payment = ""
        link = ""
        statuses = [item for cell in cells for item in cell["status"]]
        if statuses:
            payment = statuses[0]
        if len(statuses) > 1:
            link = statuses[-1]
        feed_text = cells[6]["text"].lower() if len(cells) > 6 else ""
        feed = 1 if feed_text == "yes" else 0 if feed_text == "no" else None
        epc = cells[3]["text"] if len(cells) > 3 else ""
        return {
            "advertiser_id": int(match.group(1)),
            "name": unescape(self._title or cells[0]["text"]),
            "conversion_rate": cells[1]["text"],
            "approval_rate": cells[2]["text"],
            "epc": epc,
            "epc_value": _number(epc),
            "payment_status": payment,
            "avg_payment_text": cells[5]["text"] if len(cells) > 5 else "",
            "product_feed": feed,
            "launch_date": cells[7]["text"] if len(cells) > 7 else "",
            "link_status": link,
            "action": action,
            "membership": _membership(action),
        }


def parse_directory(html: str) -> dict[str, Any]:
    parser = _RowParser()
    parser.feed(html or "")
    pages = [int(item) for item in re.findall(r'data-page="(\d+)"', html or "")]
    heading = ""
    heading_match = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.S | re.I)
    if heading_match:
        heading = _clean(heading_match.group(1))
    return {
        "rows": parser.rows,
        "last_page": max(pages) if pages else 1,
        "heading": heading,
    }


def _field_after(html: str, label: str) -> str:
    pattern = (
        rf"<h4[^>]*>\s*{re.escape(label)}.*?</h4>\s*(?:<div[^>]*>.*?</div>\s*)*"
        rf"<(?:p|div)[^>]*>(.*?)</(?:p|div)>"
    )
    match = re.search(pattern, html or "", re.S | re.I)
    return _clean(match.group(1)) if match else ""


def _percent(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_cell = False
        self._cell: list[str] = []
        self._row: list[str] = []
        self._table: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = True
            self._table = []
        elif self._in_table and tag in {"td", "th"}:
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell:
            text = _clean(" ".join(self._cell))
            if text:
                self._row.append(text)
            self._in_cell = False
        elif tag == "tr" and self._in_table:
            if self._row:
                self._table.append(self._row)
            self._row = []
        elif tag == "table" and self._in_table:
            if self._table:
                self.tables.append(self._table)
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell and data.strip():
            self._cell.append(data.strip())


def parse_profile(html: str, groups_html: str = "") -> dict[str, Any]:
    name_match = re.search(r'id="accountName"[^>]*>(.*?)</h1>', html or "", re.S)
    membership_match = re.search(r'id="accountName".*?</h1>\s*<div>\s*\(([^)]+)\)', html or "", re.S)
    summary_match = re.search(r'id="descriptionShort"[^>]*>(.*?)</div>', html or "", re.S)
    long_match = re.search(r'id="descriptionLongContentView"[^>]*>(.*?)</div>', html or "", re.S)
    logo_match = re.search(r'id="viewProfilePicture".*?<img[^>]+src="([^"]+)"', html or "", re.S)
    site_match = re.search(
        r'<a[^>]+href="(https?://[^"]+)"[^>]*>\s*(?:<i[^>]*>\s*</i>\s*)?Website\s*</a>',
        html or "",
        re.S | re.I,
    )
    regions = [
        _clean(item)
        for item in re.findall(r'id="targetRegions".*?</ul>', html or "", re.S)
    ]
    region_items = re.findall(r'id="targetRegions"[\s\S]*?<ul[^>]*>([\s\S]*?)</ul>', html or "")
    region_text = ""
    if region_items:
        region_text = ", ".join(
            item for item in (_clean(part) for part in re.findall(r"<li[\s\S]*?</li>", region_items[0])) if item
        )
    emails = sorted(set(re.findall(r"mailto:([^\"'?]+)", html or "")))
    cookie_text = _field_after(html, "Attribution Period (Cookie Length)")
    validation = _field_after(html, "Auto Validation Period")
    payment_note = _field_after(html, "Payment Status") or ""
    if not payment_note:
        pay_match = re.search(r'id="paymentStatus"[^>]*>(.*?)</p>', html or "", re.S)
        payment_note = _clean(pay_match.group(1)) if pay_match else ""
    mobile_match = re.search(r"Mobile Optimised</h4>([\s\S]{0,400})", html or "", re.I)
    app_match = re.search(r"App Tracking</h4>([\s\S]{0,500})", html or "", re.I)
    tables = _TableParser()
    tables.feed(groups_html or "")
    group_rows = [row for table in tables.tables for row in table if any("%" in cell for cell in row)]
    percents = [item for item in (_percent(cell) for row in group_rows for cell in row) if item is not None]
    content_match = re.search(r'id="content"([\s\S]*?)<footer', html or "")
    detail_text = _clean(content_match.group(1))[:12000] if content_match else ""
    description = _clean(long_match.group(1)) if long_match else ""
    summary = _clean(summary_match.group(1)) if summary_match else ""
    commission_percent = percents[0] if percents else (_percent(description) or _percent(summary))
    return {
        "name": _clean(name_match.group(1)) if name_match else "",
        "profile_membership": _clean(membership_match.group(1)).lower() if membership_match else "",
        "summary": summary,
        "description": description,
        "logo": logo_match.group(1) if logo_match else "",
        "website": site_match.group(1) if site_match else "",
        "cookie_text": cookie_text,
        "cookie_days": _days(cookie_text),
        "validation_text": validation,
        "commission_text": " | ".join(" — ".join(row) for row in group_rows[:12]),
        "commission_percent": commission_percent,
        "commission_groups": group_rows[:40],
        "regions": region_text or (", ".join(regions) if regions else ""),
        "emails": ", ".join(emails),
        "mobile_optimised": "yes" if mobile_match and re.search(r"\bYes\b", mobile_match.group(1)) else (
            "no" if mobile_match else ""
        ),
        "app_tracking": "yes" if app_match and re.search(r"\bYes\b", app_match.group(1)) else (
            "no" if app_match else ""
        ),
        "payment_note": payment_note,
        "date_joined": _field_after(html, "Date Joined"),
        "detail_text": detail_text,
    }


class AwinClient:
    def __init__(self) -> None:
        secret = load_secret()
        self.email = str(secret.get("email") or "").strip()
        self.password = str(secret.get("password") or "")
        self.publisher_id = str(secret.get("publisher_id") or "3115188").strip()
        self.region_ids = [str(item) for item in (secret.get("region_ids") or ["3"])]
        host = str(secret.get("proxy_host") or "").strip()
        port = str(secret.get("proxy_port") or "").strip()
        user = str(secret.get("proxy_user") or "").strip()
        proxy_password = str(secret.get("proxy_password") or "")
        if not (self.email and self.password and host and port):
            raise AwinError("awin.secret.json thieu email, password hoac proxy.")
        proxy = f"http://{user}:{proxy_password}@{host}:{port}" if user else f"http://{host}:{port}"
        self.http = requests.Session()
        self.http.proxies = {"http": proxy, "https": proxy}
        self.http.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        self._last = 0.0
        self._calls = 0
        self._logged_in = False
        self._session_token = ""

    def _gap(self, kind: str) -> None:
        base = DETAIL_GAP if kind == "detail" else LIST_GAP
        wait = base - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        time.sleep(random.uniform(0.6, 1.8))
        self._calls += 1
        if self._calls % BATCH_EVERY == 0:
            time.sleep(BATCH_PAUSE + random.uniform(0, 8))
        self._last = time.time()

    def _blocked(self, response: requests.Response) -> bool:
        if response.status_code in {403, 429, 503}:
            return True
        sample = (response.text or "")[:4000].lower()
        return any(
            token in sample
            for token in ("captcha", "verify you are human", "access denied", "too many requests", "unusual activity")
        )

    def _looks_logged_out(self, response: requests.Response) -> bool:
        url = (response.url or "").lower()
        if "/idp/" in url or "id.awin.com" in url:
            return True
        return 'id="email"' in (response.text or "")[:5000] and "password" in (response.text or "")[:8000]

    def login(self, force: bool = False) -> None:
        if not force and self._load_cookies():
            self._logged_in = True
            return
        self._playwright_login()
        self._logged_in = True

    def _load_cookies(self) -> bool:
        if not COOKIE_PATH.exists():
            return False
        try:
            saved = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        if not isinstance(saved, list) or not saved:
            return False
        age = time.time() - COOKIE_PATH.stat().st_mtime
        if age > 6 * 3600:
            return False
        for cookie in saved:
            self.http.cookies.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain=cookie.get("domain"),
                path=cookie.get("path") or "/",
            )
        return True

    def _playwright_login(self) -> None:
        secret = load_secret()
        proxy = {
            "server": f"http://{secret['proxy_host']}:{secret['proxy_port']}",
            "username": str(secret.get("proxy_user") or ""),
            "password": str(secret.get("proxy_password") or ""),
        }
        with sync_playwright() as playwright:
            browser = None
            last_error: Exception | None = None
            for kwargs in ({"channel": "chrome", "headless": True}, {"headless": True}):
                try:
                    browser = playwright.chromium.launch(proxy=proxy, **kwargs)
                    break
                except Exception as exc:
                    last_error = exc
            if browser is None:
                raise AwinError("Khong mo duoc trinh duyet de dang nhap Awin.") from last_error
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            page.locator("#email").wait_for(timeout=30_000)
            page.locator("#email").fill(self.email)
            page.get_by_role("button", name="Continue").click()
            page.locator("#password").wait_for(timeout=30_000)
            page.locator("#password").fill(self.password)
            page.locator("button[type=submit], button[name=action]").first.click()
            page.wait_for_url("**/dashboard/**", timeout=60_000)
            cookies = context.cookies()
            browser.close()
        COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_PATH.write_text(json.dumps(cookies), encoding="utf-8")
        self.http.cookies.clear()
        for cookie in cookies:
            self.http.cookies.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain=cookie.get("domain"),
                path=cookie.get("path") or "/",
            )

    def _request(self, method: str, url: str, kind: str, **kwargs: Any) -> requests.Response:
        if not self._logged_in:
            self.login()
        last_error: Exception | None = None
        for attempt in range(4):
            self._gap(kind)
            try:
                response = self.http.request(method, url, timeout=60, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(20 + attempt * 20)
                continue
            if self._looks_logged_out(response):
                self.login(force=True)
                continue
            if self._blocked(response):
                time.sleep(90 + random.uniform(0, 30))
                continue
            if response.status_code >= 500:
                last_error = AwinError(f"Awin loi {response.status_code}")
                time.sleep(20)
                continue
            response.encoding = response.encoding or "utf-8"
            return response
        raise AwinBlocked(
            "Proxy hoac Awin ngat ket noi sau nhieu lan thu. Dung de tranh khoa tai khoan. Chay lai lenh cu de tiep tuc."
        ) from last_error

    def _ensure_token(self) -> str:
        if self._session_token:
            return self._session_token
        page_url = (
            f"{UI}/awin/affiliate/{self.publisher_id}/merchant-directory/index/tab/notJoined/page/1"
        )
        response = self._request("GET", page_url, "list")
        match = re.search(r"setSessionToken\('([^']+)'\)", response.text or "")
        if not match:
            raise AwinError("Khong lay duoc session token cua Awin.")
        self._session_token = match.group(1)
        return self._session_token

    def directory_page(
        self,
        page: int,
        page_length: int = 40,
        membership: str = "all",
        regions: list[str] | None = None,
    ) -> dict[str, Any]:
        region_ids = self.region_ids if regions is None else regions
        token = self._ensure_token()
        url = (
            f"{UI}/awin/affiliate/{self.publisher_id}/merchant-directory/xhr-directory/"
            "sortColumn/awinIndex/sortMode/desc/"
        )
        payload: list[tuple[str, str]] = [
            ("sortColumn", "awinIndex"),
            ("sortMode", "desc"),
            ("pageNumber", str(page)),
            ("pageLength", str(page_length)),
            ("selectedTab", ""),
            ("showDetailedView", "false"),
            ("isNewSearch", "false" if page > 1 else "true"),
            ("membershipStatus", membership),
            ("page", str(page)),
            ("searchTerms", ""),
            ("feedEnabled", ""),
            ("paymentStatus", ""),
            ("mobileOptimised", "0"),
            ("leadGenerationEnabled", "0"),
            ("goldStandard", "0"),
            ("accessAccount", "0"),
            ("appTracking", "0"),
            ("highRelianceOnThirdPartyCookies", "0"),
        ]
        for region_id in region_ids:
            payload.append(("targetRegions[]", region_id))
        response = self._request(
            "POST",
            url,
            "list",
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "sessiontoken": token,
                "Origin": UI,
                "Referer": f"{UI}/awin/affiliate/{self.publisher_id}/merchant-directory/",
            },
        )
        parsed = parse_directory(response.text)
        parsed["page"] = page
        return parsed

    def fetch_detail(self, advertiser_id: int) -> dict[str, Any]:
        profile_url = f"{UI}/awin/affiliate/{self.publisher_id}/merchant-profile/{advertiser_id}"
        profile = self._request("GET", profile_url, "detail")
        if profile.status_code == 404:
            raise AwinError(f"Khong thay advertiser {advertiser_id}")
        detail = parse_profile(profile.text)
        if not detail.get("name"):
            name_match = re.search(r"<h1[^>]*>(.*?)</h1>", profile.text, re.S)
            detail["name"] = _clean(name_match.group(1)) if name_match else ""
        return detail
