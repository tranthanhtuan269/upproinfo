"""Website xem du lieu brand da crawl tu SQLite."""

from __future__ import annotations

import csv
import hmac
import io
import json
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))

from storage import strip_query
from i18n import LANG_COOKIE, texts_for
from oauth_login import PROVIDERS, authorize_url, fetch_identity, new_state, provider_config, safe_username
import billing
import vietqr
import legal
import security_guard

DB_PATH = ROOT / "data" / "uppromote.db"
BRANDS_DIR = ROOT / "data" / "brands"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
PAGE_SIZE = 40
USERS_PATH = WEB_DIR / "users.json"
SECRET_PATH = WEB_DIR / "secret.key"
OPEN_ENDPOINTS = {
    "login",
    "register",
    "set_lang",
    "static",
    "logout",
    "public_img",
    "oauth_start",
    "oauth_callback",
    "vietqr_token",
    "vietqr_sync",
    "policy",
    "term_of_service",
    "honeypot_trap",
    "favicon",
}
LOGO_FILES = ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp", "logo.gif", "logo.svg")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD_LEN = 6
SORTS = {
    "name": "o.name COLLATE NOCASE ASC",
    "score": "CAST(o.offer_score AS REAL) DESC",
    "recommend": "CAST(o.recommend_score AS REAL) DESC",
    "approval": "CAST(o.approval_rate AS REAL) DESC",
    "payout": "CAST(replace(o.payout_rate, '%', '') AS REAL) DESC",
}

PAYMENT_METHODS = (
    ("paypal", "PayPal"),
    ("bank", "Bank"),
    ("debit", "Debit"),
    ("venmo", "Venmo"),
    ("store_credit", "Store credit"),
    ("cheque", "Cheque"),
    ("other", "Other"),
    ("upi", "UPI"),
    ("paytm", "Paytm"),
)
PAYMENT_CODES = {code for code, _label in PAYMENT_METHODS}


def load_secret_key() -> str:
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    SECRET_PATH.write_text(key, encoding="utf-8")
    return key


app.secret_key = load_secret_key()
app.permanent_session_lifetime = timedelta(days=30)


def default_users() -> list[dict]:
    return [{"username": "admin", "password": "upproinfo"}]


def load_users() -> list[dict]:
    if not USERS_PATH.exists():
        return default_users()
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_users()
    raw: list = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict) and isinstance(data.get("users"), list):
        raw = data["users"]
    elif isinstance(data, dict) and data.get("username"):
        raw = [data]
    else:
        return default_users()
    users: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "").strip()
        if not username:
            continue
        users.append(
            {
                "username": username,
                "password": str(item.get("password") or ""),
                "provider": str(item.get("provider") or "password"),
                "provider_id": str(item.get("provider_id") or ""),
                "email": str(item.get("email") or ""),
                "name": str(item.get("name") or ""),
            }
        )
    return users or default_users()


def save_users(users: list[dict]) -> None:
    payload = json.dumps({"users": users}, ensure_ascii=False, indent=2) + "\n"
    tmp_path = USERS_PATH.with_name("users.json.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(USERS_PATH)


def user_exists(username: str, users: list[dict]) -> bool:
    needle = username.casefold()
    return any(str(item.get("username") or "").casefold() == needle for item in users)


def passwords_match(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left, right)
    except ValueError:
        return False


def check_credentials(username: str, password: str, users: list[dict]) -> bool:
    if not username or not password:
        return False
    matched = False
    for item in users:
        expected_user = str(item.get("username") or "")
        expected_pass = str(item.get("password") or "")
        if not expected_pass:
            continue
        user_ok = passwords_match(username, expected_user)
        pass_ok = passwords_match(password, expected_pass)
        if user_ok and pass_ok:
            matched = True
    return matched


def find_oauth_user(provider: str, provider_id: str, users: list[dict]) -> dict | None:
    for item in users:
        if str(item.get("provider") or "") == provider and str(item.get("provider_id") or "") == str(provider_id):
            return item
    return None


def unique_username(base: str, users: list[dict]) -> str:
    candidate = safe_username(base)
    if not user_exists(candidate, users):
        return candidate
    for index in range(2, 1000):
        suffix = str(index)
        name = f"{candidate[: 32 - len(suffix)]}{suffix}"
        if not user_exists(name, users):
            return name
    return safe_username(f"user{secrets.token_hex(4)}")


def login_oauth_identity(provider: str, identity: dict) -> str:
    users = load_users()
    existing = find_oauth_user(provider, identity["provider_id"], users)
    if existing:
        existing["email"] = identity.get("email") or existing.get("email") or ""
        existing["name"] = identity.get("name") or existing.get("name") or ""
        save_users(users)
        return str(existing["username"])
    username = unique_username(str(identity.get("username_base") or provider), users)
    users.append(
        {
            "username": username,
            "password": "",
            "provider": provider,
            "provider_id": str(identity["provider_id"]),
            "email": str(identity.get("email") or ""),
            "name": str(identity.get("name") or ""),
        }
    )
    save_users(users)
    return username


def safe_next_url(value: str | None) -> str:
    if not value:
        return url_for("index")
    parsed = urlparse(value)
    if parsed.netloc or not value.startswith("/"):
        return url_for("index")
    if parsed.path in ("/login", "/logout", "/register") or parsed.path.startswith("/login/") or parsed.path.startswith("/auth/"):
        return url_for("index")
    return value


def current_lang() -> str:
    query_lang = (request.args.get("lang") or "").lower()
    if query_lang in ("vi", "en"):
        return query_lang
    cookie_lang = (request.cookies.get(LANG_COOKIE) or "").lower()
    if cookie_lang in ("vi", "en"):
        return cookie_lang
    return "vi"


def find_logo() -> tuple[Path, str] | None:
    folders = (WEB_DIR / "static" / "img", WEB_DIR / "static", WEB_DIR / "img")
    for folder in folders:
        for name in LOGO_FILES:
            path = folder / name
            if path.is_file():
                return path, name
    return None


def logo_url() -> str:
    found = find_logo()
    if not found:
        return ""
    path, name = found
    version = int(path.stat().st_mtime)
    if path.parent == WEB_DIR / "static":
        return url_for("static", filename=name, v=version)
    if path.parent == WEB_DIR / "static" / "img":
        return url_for("static", filename=f"img/{name}", v=version)
    return url_for("public_img", filename=name, v=version)



@app.route("/favicon.ico")
def favicon():
    return send_from_directory(WEB_DIR / "static", "favicon.ico", mimetype="image/vnd.microsoft.icon")

@app.route("/img/<path:filename>")
def public_img(filename: str):
    folders = (WEB_DIR / "static" / "img", WEB_DIR / "static", WEB_DIR / "img")
    for folder in folders:
        candidate = (folder / filename).resolve()
        try:
            candidate.relative_to(folder.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return send_from_directory(folder, filename)
    abort(404)


@app.context_processor
def inject_i18n() -> dict:
    lang = current_lang()
    user = session.get("user")
    access = billing.access_for(user)
    # Current Vietnam time (UTC+7)
    vn_now = datetime.now(timezone(timedelta(hours=7)))
    today_formatted = vn_now.strftime("%d/%m/%Y")
    return {
        "t": texts_for(lang),
        "today_date": today_formatted,
        "lang": lang,
        "current_user": user,
        "logo_url": logo_url(),
        "filters": query_kwargs(),
        "entitled": access["entitled"],
        "is_admin": access["is_admin"],
        "expires_date": access["expires_date"],
        "pending_tx": billing.pending_count() if access["is_admin"] else 0,
        "sub_plan": access["plan"],
        "pay_configured": vietqr.is_configured(),
        "oauth_google": bool(provider_config("google")),
        "oauth_github": bool(provider_config("github")),
        "unread_notif_count": billing.get_unread_notification_count(user) if user else 0,
        "user_notifications": billing.get_user_notifications(user, 10) if user else [],
    }


@app.before_request
def require_login():
    ip = security_guard.get_real_ip()
    if security_guard.is_ip_blacklisted(ip):
        abort(403)
    if request.endpoint in OPEN_ENDPOINTS:
        return None
    if session.get("user"):
        return None
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    error = session.pop("auth_error", "") or ""
    username = ""
    next_url = safe_next_url(request.values.get("next"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if check_credentials(username, password, load_users()):
            session["user"] = username
            session.permanent = bool(request.form.get("remember"))
            return redirect(next_url)
        error = texts_for(current_lang())["login_error"]
    return render_template(
        "auth.html",
        mode="login",
        error=error,
        next_url=next_url,
        username=username if request.method == "POST" else "",
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user"):
        return redirect(url_for("index"))
    error = session.pop("auth_error", "") or ""
    username = ""
    next_url = safe_next_url(request.values.get("next"))
    t = texts_for(current_lang())
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        users = load_users()
        if not USERNAME_RE.fullmatch(username):
            error = t["register_error_user"]
        elif user_exists(username, users):
            error = t["register_error_exists"]
        elif len(password) < MIN_PASSWORD_LEN:
            error = t["register_error_short"]
        elif not passwords_match(password, confirm):
            error = t["register_error_mismatch"]
        elif not request.form.get("terms"):
            error = t["register_error_terms"]
        else:
            users.append(
                {
                    "username": username,
                    "password": password,
                    "provider": "password",
                    "provider_id": "",
                    "email": "",
                    "name": "",
                }
            )
            save_users(users)
            session["user"] = username
            session.permanent = True
            return redirect(next_url)
    return render_template(
        "auth.html",
        mode="register",
        error=error,
        next_url=next_url,
        username=username,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def oauth_redirect_uri(provider: str) -> str:
    host = (request.host or "").split(":")[0].lower()
    if host in ("upproinfo.com", "www.upproinfo.com"):
        return f"https://{host}/auth/{provider}/callback"
    return url_for("oauth_callback", provider=provider, _external=True)


def oauth_fail_message(texts: dict, err: Exception) -> str:
    detail = str(err).lower()
    if "redirect_uri" in detail:
        return texts["oauth_redirect_mismatch"]
    return texts["oauth_error"]


@app.route("/login/<provider>")
def oauth_start(provider: str):
    provider = provider.lower()
    t = texts_for(current_lang())
    next_url = safe_next_url(request.args.get("next"))
    fail_endpoint = "register" if request.args.get("mode") == "register" else "login"
    if provider not in PROVIDERS:
        abort(404)
    cfg = provider_config(provider)
    if not cfg:
        session["auth_error"] = t["oauth_not_configured"]
        return redirect(url_for(fail_endpoint, next=next_url))
    state = new_state()
    redirect_uri = oauth_redirect_uri(provider)
    session["oauth_state"] = state
    session["oauth_provider"] = provider
    session["oauth_next"] = next_url
    session["oauth_from"] = fail_endpoint
    session["oauth_redirect_uri"] = redirect_uri
    return redirect(authorize_url(provider, cfg["client_id"], redirect_uri, state))


@app.route("/auth/<provider>/callback")
def oauth_callback(provider: str):
    provider = provider.lower()
    t = texts_for(current_lang())
    fail_endpoint = session.get("oauth_from") or "login"
    next_url = safe_next_url(session.get("oauth_next"))
    stored_redirect = session.get("oauth_redirect_uri")
    if provider not in PROVIDERS:
        abort(404)
    if request.args.get("error"):
        session["auth_error"] = t["oauth_denied"]
        return redirect(url_for(fail_endpoint, next=next_url))
    expected_state = session.get("oauth_state")
    expected_provider = session.get("oauth_provider")
    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    session.pop("oauth_state", None)
    session.pop("oauth_provider", None)
    session.pop("oauth_next", None)
    session.pop("oauth_from", None)
    session.pop("oauth_redirect_uri", None)
    if not code or not expected_state or expected_provider != provider:
        session["auth_error"] = t["oauth_error"]
        return redirect(url_for(fail_endpoint, next=next_url))
    try:
        state_ok = hmac.compare_digest(state, str(expected_state))
    except ValueError:
        state_ok = False
    if not state_ok:
        session["auth_error"] = t["oauth_error"]
        return redirect(url_for(fail_endpoint, next=next_url))
    cfg = provider_config(provider)
    if not cfg:
        session["auth_error"] = t["oauth_not_configured"]
        return redirect(url_for(fail_endpoint, next=next_url))
    redirect_uri = stored_redirect or oauth_redirect_uri(provider)
    try:
        identity = fetch_identity(provider, cfg, redirect_uri, code)
        username = login_oauth_identity(provider, identity)
    except RuntimeError as err:
        app.logger.warning("OAuth %s failed: %s", provider, err)
        session["auth_error"] = oauth_fail_message(t, err)
        return redirect(url_for(fail_endpoint, next=next_url))
    session["user"] = username
    session.permanent = True
    return redirect(next_url)


@app.route("/policy")
def policy():
    return render_template("legal.html", page=legal.page("policy", current_lang()))


@app.route("/term-of-service")
def term_of_service():
    return render_template("legal.html", page=legal.page("terms", current_lang()))


@app.route("/lang/<code>")
def set_lang(code: str):
    lang = code.lower() if code.lower() in ("vi", "en") else "vi"
    target = request.referrer or url_for("index")
    response = redirect(target)
    response.set_cookie(LANG_COOKIE, lang, max_age=365 * 24 * 60 * 60)
    return response


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skipped (
            shop_id INTEGER PRIMARY KEY,
            reason TEXT,
            skipped_at TEXT
        )
        """
    )
    detail_cols = {row[1] for row in conn.execute("PRAGMA table_info(details)")}
    if detail_cols and "payment_methods" not in detail_cols:
        conn.execute("ALTER TABLE details ADD COLUMN payment_methods TEXT")
        conn.commit()
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def parse_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed if str(item).strip()]
                if isinstance(parsed, dict):
                    return [key for key, val in parsed.items() if val]
            except json.JSONDecodeError:
                return [text]
        return [part.strip() for part in text.split(",") if part.strip()]
    return [str(value)]


def clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)
    text = re.sub(r"(?i)on\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", text)
    return text


def load_brand_json(shop_id: int) -> dict | None:
    path = BRANDS_DIR / f"{shop_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    details = conn.execute("SELECT COUNT(*) FROM details").fetchone()[0]
    try:
        skipped = conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]
    except sqlite3.OperationalError:
        skipped = 0
    cats = conn.execute(
        "SELECT COUNT(DISTINCT categories) FROM offers WHERE categories IS NOT NULL AND categories != ''"
    ).fetchone()[0]
    top = conn.execute(
        """
        SELECT categories, COUNT(*) AS n
        FROM offers
        WHERE categories IS NOT NULL AND categories != ''
        GROUP BY categories
        ORDER BY n DESC, categories
        LIMIT 1
        """
    ).fetchone()
    return {
        "total": total,
        "details": details,
        "skipped": skipped,
        "pending": max(total - details - skipped, 0),
        "categories": cats,
        "pct": round(details * 100 / total, 1) if total else 0,
        "top_category": (top["categories"] if top else "") or "—",
        "top_count": top["n"] if top else 0,
    }


def categories(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT categories, COUNT(*) AS n
        FROM offers
        WHERE categories IS NOT NULL AND categories != ''
        GROUP BY categories
        ORDER BY n DESC, categories
        """
    ).fetchall()
    return [row["categories"] for row in rows]


def free_list_filters() -> dict:
    return {
        "q": "",
        "category": "",
        "status": "",
        "review": "",
        "sort": "name",
        "min_score": "",
        "traffic": "",
        "ads": "",
        "payment": [],
    }


def query_kwargs() -> dict:
    payments = []
    for raw in request.args.getlist("payment"):
        code = str(raw or "").strip().lower()
        if code in PAYMENT_CODES and code not in payments:
            payments.append(code)
    return {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", "").strip(),
        "status": request.args.get("status", "").strip(),
        "review": request.args.get("review", "").strip(),
        "sort": request.args.get("sort", "name").strip() or "name",
        "min_score": request.args.get("min_score", "").strip(),
        "traffic": request.args.get("traffic", "").strip(),
        "ads": request.args.get("ads", "").strip(),
        "payment": payments,
    }


def offer_where(filters: dict) -> tuple[list[str], list]:
    where = ["1=1"]
    args: list = []
    if filters["q"]:
        like = f"%{filters['q']}%"
        where.append(
            "(o.name LIKE ? OR o.website LIKE ? OR o.myshopify_domain LIKE ? OR CAST(o.shop_id AS TEXT) LIKE ?)"
        )
        args.extend([like, like, like, like])
    if filters["category"]:
        where.append("o.categories = ?")
        args.append(filters["category"])
    if filters["status"] == "detail":
        where.append("d.shop_id IS NOT NULL")
    elif filters["status"] == "skipped":
        where.append("s.shop_id IS NOT NULL")
    elif filters["status"] == "list":
        where.append("d.shop_id IS NULL AND s.shop_id IS NULL")
    if filters.get("review") == "auto":
        where.append("lower(coalesce(d.application_review, '')) = 'auto'")
    elif filters.get("review") == "manual":
        where.append("lower(coalesce(d.application_review, '')) = 'manual'")
    if filters.get("min_score"):
        try:
            score = float(filters["min_score"])
            if score > 0:
                where.append("CAST(o.offer_score AS REAL) >= ?")
                args.append(score)
        except ValueError:
            pass
    if filters.get("traffic") == "5k":
        where.append("coalesce(bm.traffic_m3, 0) >= 5000")
    if filters.get("ads") == "yes":
        where.append("coalesce(bm.allows_search_ads, 0) = 1")
    elif filters.get("ads") == "no":
        where.append("coalesce(bm.allows_search_ads, 0) = 0")
    payments = [str(item).strip().lower() for item in (filters.get("payment") or []) if str(item).strip()]
    if payments:
        clauses = []
        for code in payments:
            clauses.append("instr(lower(coalesce(d.payment_methods, '')), ?) > 0")
            args.append(f"|{code}|")
        where.append("(" + " OR ".join(clauses) + ")")
    return where, args


def score_bar(value) -> int:
    try:
        number = float(str(value or "").replace("%", "").strip() or 0)
    except ValueError:
        return 0
    if number <= 0:
        return 0
    if number <= 10:
        return min(100, int(round(number * 10)))
    return min(100, int(round(number)))


def pct_label(value) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"no data yet", "n/a", "-", "—"}:
        return ""
    if text.endswith("%"):
        return text
    try:
        float(text)
    except ValueError:
        return text
    return text + "%"


def decorate_brand(row: dict | None) -> dict | None:
    if not row:
        return None
    name = str(row.get("name") or "").strip()
    row["initial"] = name[:1].upper() if name else "#"
    row["score_bar"] = score_bar(row.get("offer_score"))
    row["host"] = (
        str(row.get("website") or row.get("myshopify_domain") or "")
        .replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
    )
    extra = load_brand_json(int(row["shop_id"])) if row.get("shop_id") is not None else None
    logo = (extra or {}).get("logo") if extra else None
    row["logo"] = logo if isinstance(logo, str) and logo.startswith("http") else ""
    review = str(row.get("application_review") or "").strip().lower()
    row["review_kind"] = review if review in {"auto", "manual"} else ""
    row["approval_pct"] = pct_label(row.get("approval_rate"))
    row["payout_label"] = pct_label(row.get("payout_rate")) or str(row.get("payout_rate") or "").strip()
    return row


def pager_items(page: int, pages: int) -> list:
    if pages < 1:
        return []
    wanted = {1, pages, page, page - 1, page + 1}
    ordered = [num for num in range(1, pages + 1) if num in wanted]
    items: list = []
    last = 0
    for num in ordered:
        if last and num - last > 1:
            items.append(None)
        items.append(num)
        last = num
    return items


def page_url(page: int, **overrides) -> str:
    params = query_kwargs()
    params.update(overrides)
    params["page"] = page
    pairs: list[tuple[str, object]] = []
    for key, value in params.items():
        if value in ("", None, [], ()):
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if item not in ("", None):
                    pairs.append((key, item))
        else:
            pairs.append((key, value))
    return "/?" + urlencode(pairs)


@app.route("/")
def index():
    if not DB_PATH.exists():
        abort(500, texts_for(current_lang())["no_db"])
    entitled = billing.is_entitled(session.get("user"))
    filters = query_kwargs() if entitled else free_list_filters()
    page = 1 if not entitled else max(1, request.args.get("page", 1, type=int))
    sort_sql = SORTS.get(filters["sort"], SORTS["name"])
    where, args = offer_where(filters)

    sql = f"""
        FROM offers o
        LEFT JOIN details d ON d.shop_id = o.shop_id
        LEFT JOIN skipped s ON s.shop_id = o.shop_id
        LEFT JOIN brand_metrics bm ON bm.shop_id = o.shop_id
        WHERE {" AND ".join(where)}
    """
    conn = db()
    try:
        total = conn.execute(f"SELECT COUNT(*) {sql}", args).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        offset = (page - 1) * PAGE_SIZE
        rows = conn.execute(
            f"""
            SELECT o.*,
                   d.application_review,
                   CASE WHEN d.shop_id IS NOT NULL THEN 1 ELSE 0 END AS has_detail,
                   CASE WHEN s.shop_id IS NOT NULL THEN 1 ELSE 0 END AS is_skipped,
                   bm.traffic_m1,
                   bm.traffic_m2,
                   bm.traffic_m3,
                   bm.google_ads_running,
                   bm.google_advertisers_count,
                   bm.allows_search_ads,
                   bm.top_keywords_json
            {sql}
            ORDER BY {sort_sql}, o.shop_id
            LIMIT ? OFFSET ?
            """,
            [*args, PAGE_SIZE, offset],
        ).fetchall()
        brands = [decorate_brand(row_to_dict(row)) for row in rows]
        showing_from = 0 if total == 0 else offset + 1
        showing_to = offset + len(brands)
        return render_template(
            "index.html",
            brands=brands,
            stats=stats(conn),
            categories=categories(conn),
            payment_options=PAYMENT_METHODS,
            filters=filters,
            page=page,
            pages=pages,
            total=total,
            page_url=page_url,
            pager=pager_items(page, pages) if entitled else [],
            showing_from=showing_from,
            showing_to=showing_to,
        )
    finally:
        conn.close()


@app.route("/export.csv")
def export_brands():
    user = session.get("user")
    access = billing.access_for(user)
    if not access["is_admin"] and access["plan"] not in ("year", "quarter"):
        return redirect(url_for("billing_page"))
    if not DB_PATH.exists():
        abort(404)
    filters = query_kwargs()
    sort_sql = SORTS.get(filters["sort"], SORTS["name"])
    where, args = offer_where(filters)
    sql = f"""
        FROM offers o
        LEFT JOIN details d ON d.shop_id = o.shop_id
        LEFT JOIN skipped s ON s.shop_id = o.shop_id
        LEFT JOIN brand_metrics bm ON bm.shop_id = o.shop_id
        WHERE {" AND ".join(where)}
    """
    conn = db()
    try:
        rows = conn.execute(
            f"""
            SELECT o.name, o.website, o.categories, o.commission, o.cookie,
                   o.payout_rate, o.approval_rate, o.offer_score, o.shop_id, o.apply_url,
                   bm.traffic_m1, bm.traffic_m2, bm.traffic_m3, bm.allows_search_ads
            {sql}
            ORDER BY {sort_sql}, o.shop_id
            """,
            args,
        ).fetchall()
    finally:
        conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["name", "website", "category", "commission", "cookie", "payout", "approval", "score", "shop_id", "apply_url", "traffic_m1", "traffic_m2", "traffic_m3", "allows_search_ads"]
    )
    for row in rows:
        writer.writerow([row[key] if row[key] is not None else "" for key in row.keys()])
    payload = output.getvalue().encode("utf-8-sig")
    # private + no-store: avoid CF/browser 304 empty bodies breaking CSV downloads
    resp = Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=uppro_brands.csv",
            "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
    resp.headers.pop("ETag", None)
    resp.headers.pop("Last-Modified", None)
    return resp


def load_brand(shop_id: int) -> tuple[dict, bool] | None:
    conn = db()
    try:
        offer = row_to_dict(
            conn.execute("SELECT * FROM offers WHERE shop_id = ?", (shop_id,)).fetchone()
        )
        detail = row_to_dict(
            conn.execute("SELECT * FROM details WHERE shop_id = ?", (shop_id,)).fetchone()
        )
        metrics = row_to_dict(
            conn.execute("SELECT * FROM brand_metrics WHERE shop_id = ?", (shop_id,)).fetchone()
        )
    finally:
        conn.close()
    extra = load_brand_json(shop_id)
    if not offer and not detail and not extra:
        return None
    data = {**(offer or {}), **(detail or {}), **(metrics or {}), **(extra or {})}
    if data.get("top_keywords_json"):
        try:
            data["top_keywords"] = json.loads(data["top_keywords_json"])
        except Exception:
            data["top_keywords"] = []
    else:
        data["top_keywords"] = []
    if data.get("monthly_visits_json"):
        try:
            data["monthly_visits"] = json.loads(data["monthly_visits_json"])
        except Exception:
            data["monthly_visits"] = {}
    else:
        data["monthly_visits"] = {}
    data["apply_url"] = strip_query(data.get("apply_url"))
    data["description_html"] = clean_html(data.get("description"))
    data["hashtags_list"] = parse_list(data.get("hashtags"))
    data["channels"] = parse_list(data.get("target_audience_customer_channels"))
    data["ages"] = parse_list(data.get("target_audience_ages"))
    data["genders"] = parse_list(data.get("target_audience_genders"))
    data["locations"] = parse_list(data.get("target_audience_locations"))
    data["promos"] = parse_list(data.get("promotion_details"))
    payments = data.get("payment_support")
    if isinstance(payments, str):
        try:
            payments = json.loads(payments)
        except json.JSONDecodeError:
            payments = {}
    data["payments"] = (
        [key for key, val in (payments or {}).items() if val]
        if isinstance(payments, dict)
        else []
    )
    return data, extra is not None


@app.route("/billing")
def billing_page():
    access = billing.access_for(session.get("user"))
    site_stats = {
        "total": 0,
        "details": 0,
        "skipped": 0,
        "pending": 0,
        "pct": 0,
    }
    if DB_PATH.exists():
        conn = db()
        try:
            site_stats = stats(conn)
        finally:
            conn.close()
    return render_template(
        "billing.html",
        error=session.pop("billing_error", ""),
        pay_configured=vietqr.is_configured(),
        entitled=access["entitled"],
        is_admin=access["is_admin"],
        expires_date=access["expires_date"],
        stats=site_stats,
        page_size=PAGE_SIZE,
        plan_month_num=billing.format_vnd(billing.PLANS["month"]["amount"]),
        plan_quarter_num=billing.format_vnd(billing.PLANS["quarter"]["amount"]),
        plan_year_num=billing.format_vnd(billing.PLANS["year"]["amount"]),
    )


@app.route("/billing/checkout", methods=["POST"])
def billing_checkout():
    texts = texts_for(current_lang())
    if not vietqr.is_configured():
        session["billing_error"] = texts["billing_not_configured"]
        return redirect(url_for("billing_page"))
    plan = billing.plan_of(request.form.get("plan") or "")
    if not plan:
        session["billing_error"] = texts["billing_error"]
        return redirect(url_for("billing_page"))
    username = session.get("user") or ""
    trade_no = vietqr.new_order_id()
    order = billing.create_order(username, plan["id"], trade_no)
    pay_content = billing.pay_code(order["id"])
    return_url = url_for("billing_return", trade=trade_no, _external=True)
    try:
        created = vietqr.create_payment(int(plan["amount"]), return_url, pay_content, pay_content)
    except RuntimeError:
        session["billing_error"] = texts["billing_error"]
        return redirect(url_for("billing_page"))
    billing.update_order_checkout(
        trade_no,
        created.get("transaction_id") or created["order_id"],
        created.get("qr_link") or "",
        json.dumps(created, ensure_ascii=False),
        created["content"],
    )
    try:
        import notifier
        new_order_data = billing.get_order(trade_no) or order
        notifier.notify_new_order(new_order_data)
    except Exception as ex:
        pass
    return redirect(url_for("billing_return", trade=trade_no))


@app.route("/billing/return")
def billing_return():
    trade = (request.args.get("trade") or request.args.get("merchantTradeNo") or "").strip()
    order = billing.get_order(trade) if trade else None
    user = session.get("user")
    if not order or (order["username"] != user and not billing.is_admin(user)):
        return redirect(url_for("billing_page"))
    if order["status"] == "paid":
        return redirect(url_for("index"))
    payload = {}
    try:
        payload = json.loads(order.get("raw_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    pay_content = order.get("pay_content") or payload.get("content") or trade
    try:
        amount = int(order.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    qr_image = vietqr.branded_qr_url(amount, str(pay_content))
    return render_template(
        "billing_return.html",
        trade_no=trade,
        checkout_url=qr_image,
        qr_image=qr_image,
        qr_code="",
        pay_content=pay_content,
        pay_amount=billing.format_vnd(order.get("amount") or 0),
        bank_account=vietqr.load_config().get("bank_account") or "",
        bank_name=vietqr.account_display(),
        bank_code="MB",
        sandbox=False,
    )


@app.route("/billing/test-callback/<trade_no>", methods=["POST"])
def billing_test_callback(trade_no: str):
    order = billing.get_order(trade_no)
    user = session.get("user")
    if not order or (order["username"] != user and not billing.is_admin(user)):
        abort(404)
    if not vietqr.is_sandbox():
        abort(404)
    if order["status"] == "paid":
        return jsonify({"status": "paid", "redirect": url_for("index")})
    try:
        vietqr.simulate_payment(order.get("pay_content") or trade_no, int(order["amount"]))
    except (RuntimeError, TypeError, ValueError) as err:
        return jsonify({"status": "error", "message": str(err)}), 400
    return jsonify({"status": "pending"})


@app.route("/billing/status/<trade_no>")
def billing_status(trade_no: str):
    order = billing.get_order(trade_no)
    user = session.get("user")
    if not order or (order["username"] != user and not billing.is_admin(user)):
        abort(404)
    order = billing.get_order(trade_no) or order
    if order["status"] == "paid":
        return jsonify({"status": "paid", "redirect": url_for("index")})
    if order["status"] == "closed":
        return jsonify({"status": "closed"})
    return jsonify({"status": "pending"})


def require_admin() -> str:
    user = session.get("user") or ""
    if not billing.is_admin(user):
        abort(404)
    return user


@app.route("/admin/users")
def admin_users():
    require_admin()
    users_list = load_users()
    subs = billing.list_subscriptions()
    enriched = []
    for u in users_list:
        uname = u.get("username", "")
        sub = subs.get(uname)
        entitled = billing.is_entitled(uname)
        is_adm = billing.is_admin(uname)
        enriched.append({
            "username": uname,
            "email": u.get("email", ""),
            "name": u.get("name", ""),
            "provider": u.get("provider", "password"),
            "is_admin": is_adm,
            "entitled": entitled,
            "sub": sub,
        })
    return render_template(
        "admin_users.html",
        users=enriched,
        notice=session.pop("admin_user_notice", ""),
    )


@app.route("/admin/users/set-vip", methods=["POST"])
def admin_set_vip():
    require_admin()
    username = (request.form.get("username") or "").strip()
    try:
        days = int(request.form.get("days") or 30)
    except ValueError:
        days = 30
    if username:
        res = billing.set_subscription_days(username, days)
        session["admin_user_notice"] = f"Đã cấp VIP {days} ngày thành công cho tài khoản {username} (Hạn mới: {res[expires_at][:10]})."
    return redirect(url_for("admin_users"))


@app.route("/admin/users/change-password", methods=["POST"])
def admin_change_password():
    require_admin()
    username = (request.form.get("username") or "").strip()
    new_pass = (request.form.get("password") or "").strip()
    if not username or len(new_pass) < 6:
        session["admin_user_notice"] = "Mật khẩu mới phải từ 6 ký tự trở lên."
        return redirect(url_for("admin_users"))
    users = load_users()
    found = False
    for u in users:
        if u.get("username") == username:
            u["password"] = new_pass
            found = True
            break
    if found:
        save_users(users)
        session["admin_user_notice"] = f"Đã đổi mật khẩu thành công cho tài khoản {username}."
    else:
        session["admin_user_notice"] = f"Không tìm thấy tài khoản {username}."
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<username>/delete", methods=["POST"])
def admin_delete_user(username: str):
    require_admin()
    username = (username or "").strip()
    if billing.is_admin(username):
        session["admin_user_notice"] = "Không thể xóa tài khoản Quản trị viên!"
        return redirect(url_for("admin_users"))
    users = load_users()
    filtered = [u for u in users if u.get("username") != username]
    if len(filtered) < len(users):
        save_users(filtered)
        billing.revoke_subscription(username)
        session["admin_user_notice"] = f"Đã xóa tài khoản {username} và hủy gói đăng ký thành công."
    else:
        session["admin_user_notice"] = f"Không tìm thấy tài khoản {username}."
    return redirect(url_for("admin_users"))


@app.route("/admin/transactions")
def admin_transactions():
    require_admin()
    return render_template(
        "admin_transactions.html",
        orders=billing.list_orders(),
        notice=session.pop("tx_notice", ""),
    )


@app.route("/admin/transactions/<trade_no>/receive", methods=["POST"])
def admin_receive(trade_no: str):
    user = require_admin()
    texts = texts_for(current_lang())
    order = billing.mark_received(trade_no, user)
    session["tx_notice"] = texts["tx_done"] if order else texts["billing_error"]
    return redirect(url_for("admin_transactions"))


@app.route("/vqr/api/token_generate", methods=["POST"], strict_slashes=False)
@app.route("/api/token_generate", methods=["POST"], strict_slashes=False)
def vietqr_token():
    payload, status = vietqr.handle_token_request(request.headers.get("Authorization") or "")
    return jsonify(payload), status


@app.route("/vqr/bank/api/transaction-sync", methods=["POST"], strict_slashes=False)
@app.route("/bank/api/transaction-sync", methods=["POST"], strict_slashes=False)
def vietqr_sync():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = request.form.to_dict() if request.form else {}
    payload, status = vietqr.handle_sync_request(request.headers.get("Authorization") or "", body)
    return jsonify(payload), status




@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_internal_error(e):
    return render_template("maintenance.html"), 500

@app.route("/system/analytics/track-sync")
@app.route("/api/v1/feed-export")
def honeypot_trap():
    ip = security_guard.get_real_ip()
    ua = str(request.headers.get("User-Agent", ""))[:80]
    security_guard.blacklist_ip(ip, f"Honeypot hit (UA: {ua})", ban_seconds=86400)
    try:
        import notifier
        msg = "🚨 <b>[Security Alert]</b> Phát hiện bot cào trúng bẫy Honeypot!\n" + f"IP: <code>{ip}</code> đã bị chặn 24h.\nUA: <code>{ua}</code>"
        notifier.send_telegram_message(msg)
    except Exception:
        pass
    abort(403)

@app.route("/brand/<int:shop_id>/panel")

def brand_panel(shop_id: int):
    user = session.get("user") or "anonymous"
    ip = security_guard.get_real_ip()

    # Per-minute burst protection (max 40 detail views/min per IP)
    if not security_guard.check_rate_limit(f"brand_panel_burst:{ip}", max_requests=40, window_seconds=60):
        return Response("<p class='acc-loading text-error'>Thao tác quá nhanh, vui lòng chờ 1 phút...</p>", status=429)

    # Free tier daily quota protection (max 50 brands/day if not VIP/Admin)
    if not billing.is_entitled(user):
        if not security_guard.check_rate_limit(f"free_quota:{user}:{ip}", max_requests=50, window_seconds=86400):
            return Response(
                "<div class='p-4 bg-secondary-container/40 rounded-xl text-xs space-y-2'>"
                "<p class='font-bold text-on-surface m-0'>⚠️ Bạn đã xem hết hạn mức 50 thương hiệu/ngày của tài khoản Miễn phí.</p>"
                "<p class='text-on-surface-variant m-0'>Vui lòng nâng cấp gói VIP để xem không giới hạn toàn bộ dữ liệu đối tác và link hoa hồng.</p>"
                "<a href='/billing' class='inline-block mt-2 px-3 py-1.5 rounded-lg bg-primary text-on-primary font-semibold no-underline'>Nâng cấp VIP ngay</a>"
                "</div>",
                status=403
            )

    loaded = load_brand(shop_id)
    if not loaded:
        abort(404)
    data, has_json = loaded
    return render_template(
        "brand_panel.html",
        brand=data,
        shop_id=shop_id,
        has_json=has_json,
    )


@app.route("/brand/<int:shop_id>")
def brand(shop_id: int):
    loaded = load_brand(shop_id)
    if not loaded:
        abort(404)
    data, has_json = loaded
    return render_template(
        "brand.html",
        brand=data,
        shop_id=shop_id,
        has_json=has_json,
        filters=query_kwargs(),
    )


def main() -> None:
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
