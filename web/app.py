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
from datetime import timedelta
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
import binance_pay

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
    "billing_webhook",
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
    return {
        "t": texts_for(lang),
        "lang": lang,
        "current_user": user,
        "logo_url": logo_url(),
        "filters": query_kwargs(),
        "entitled": access["entitled"],
        "is_admin": access["is_admin"],
        "expires_date": access["expires_date"],
        "sub_plan": access["plan"],
        "pay_configured": binance_pay.is_configured(),
    }


@app.before_request
def require_login():
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
    session["oauth_state"] = state
    session["oauth_provider"] = provider
    session["oauth_next"] = next_url
    session["oauth_from"] = fail_endpoint
    redirect_uri = url_for("oauth_callback", provider=provider, _external=True)
    return redirect(authorize_url(provider, cfg["client_id"], redirect_uri, state))


@app.route("/auth/<provider>/callback")
def oauth_callback(provider: str):
    provider = provider.lower()
    t = texts_for(current_lang())
    fail_endpoint = session.get("oauth_from") or "login"
    next_url = safe_next_url(session.get("oauth_next"))
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
    redirect_uri = url_for("oauth_callback", provider=provider, _external=True)
    try:
        identity = fetch_identity(provider, cfg, redirect_uri, code)
        username = login_oauth_identity(provider, identity)
    except RuntimeError:
        session["auth_error"] = t["oauth_error"]
        return redirect(url_for(fail_endpoint, next=next_url))
    session["user"] = username
    session.permanent = True
    return redirect(next_url)


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
    }


def query_kwargs() -> dict:
    return {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", "").strip(),
        "status": request.args.get("status", "").strip(),
        "review": request.args.get("review", "").strip(),
        "sort": request.args.get("sort", "name").strip() or "name",
        "min_score": request.args.get("min_score", "").strip(),
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
    clean = {key: value for key, value in params.items() if value not in ("", None)}
    return "/?" + urlencode(clean)


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
                   CASE WHEN s.shop_id IS NOT NULL THEN 1 ELSE 0 END AS is_skipped
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
    if not billing.is_entitled(session.get("user")):
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
        WHERE {" AND ".join(where)}
    """
    conn = db()
    try:
        rows = conn.execute(
            f"""
            SELECT o.name, o.website, o.categories, o.commission, o.cookie,
                   o.payout_rate, o.approval_rate, o.offer_score, o.shop_id, o.apply_url
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
        ["name", "website", "category", "commission", "cookie", "payout", "approval", "score", "shop_id", "apply_url"]
    )
    for row in rows:
        writer.writerow([row[key] if row[key] is not None else "" for key in row.keys()])
    payload = output.getvalue().encode("utf-8-sig")
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=uppro_brands.csv"},
    )


def load_brand(shop_id: int) -> tuple[dict, bool] | None:
    conn = db()
    try:
        offer = row_to_dict(
            conn.execute("SELECT * FROM offers WHERE shop_id = ?", (shop_id,)).fetchone()
        )
        detail = row_to_dict(
            conn.execute("SELECT * FROM details WHERE shop_id = ?", (shop_id,)).fetchone()
        )
    finally:
        conn.close()
    extra = load_brand_json(shop_id)
    if not offer and not detail and not extra:
        return None
    data = {**(offer or {}), **(detail or {}), **(extra or {})}
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
        pay_configured=binance_pay.is_configured(),
        entitled=access["entitled"],
        is_admin=access["is_admin"],
        expires_date=access["expires_date"],
        stats=site_stats,
        page_size=PAGE_SIZE,
    )


@app.route("/billing/checkout", methods=["POST"])
def billing_checkout():
    texts = texts_for(current_lang())
    if not binance_pay.is_configured():
        session["billing_error"] = texts["billing_not_configured"]
        return redirect(url_for("billing_page"))
    plan = billing.plan_of(request.form.get("plan") or "")
    if not plan:
        session["billing_error"] = texts["billing_error"]
        return redirect(url_for("billing_page"))
    username = session.get("user") or ""
    trade_no = f"u{int(time.time())}{secrets.token_hex(6)}"[:32]
    billing.create_order(username, plan["id"], trade_no)
    return_url = url_for("billing_return", trade=trade_no, _external=True)
    cancel_url = url_for("billing_page", _external=True)
    goods_name = texts["billing_month_name"] if plan["id"] == "month" else texts["billing_year_name"]
    try:
        created = binance_pay.create_order(
            merchant_trade_no=trade_no,
            amount=plan["amount"],
            goods_id=plan["goods_id"],
            goods_name=goods_name,
            description=f"UpproInfo {plan['id']}",
            return_url=return_url,
            cancel_url=cancel_url,
        )
    except RuntimeError:
        session["billing_error"] = texts["billing_error"]
        return redirect(url_for("billing_page"))
    billing.update_order_checkout(
        trade_no,
        created["prepay_id"],
        created["checkout_url"],
        json.dumps(created.get("raw") or {}, ensure_ascii=False),
    )
    if created.get("checkout_url"):
        return redirect(created["checkout_url"])
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
    return render_template(
        "billing_return.html",
        trade_no=trade,
        checkout_url=order.get("checkout_url") or "",
    )


@app.route("/billing/status/<trade_no>")
def billing_status(trade_no: str):
    order = billing.get_order(trade_no)
    user = session.get("user")
    if not order or (order["username"] != user and not billing.is_admin(user)):
        abort(404)
    if order["status"] == "paid":
        return jsonify({"status": "paid", "redirect": url_for("index")})
    if order["status"] == "closed":
        return jsonify({"status": "closed"})
    if not binance_pay.is_configured():
        return jsonify({"status": "pending"})
    try:
        result = binance_pay.query_order(trade_no)
    except RuntimeError:
        return jsonify({"status": "pending"})
    raw = json.dumps(result.get("raw") or {}, ensure_ascii=False)
    if result["paid"]:
        billing.fulfill_paid_order(trade_no, raw)
        return jsonify({"status": "paid", "redirect": url_for("index")})
    if result["closed"]:
        billing.mark_order_closed(trade_no, raw)
        return jsonify({"status": "closed"})
    return jsonify({"status": "pending"})


@app.route("/billing/binance/webhook", methods=["POST"])
def billing_webhook():
    raw = request.get_data(as_text=True) or ""
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    trade = binance_pay.parse_webhook_trade_no(body)
    if trade and binance_pay.is_configured():
        try:
            result = binance_pay.query_order(trade)
            dumped = json.dumps(result.get("raw") or body, ensure_ascii=False)
            if result["paid"]:
                billing.fulfill_paid_order(trade, dumped)
            elif result["closed"] or binance_pay.webhook_biz_status(body) == "PAY_CLOSED":
                billing.mark_order_closed(trade, dumped)
        except RuntimeError:
            pass
    return jsonify({"returnCode": "SUCCESS", "returnMessage": None})


@app.route("/brand/<int:shop_id>/panel")
def brand_panel(shop_id: int):
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
