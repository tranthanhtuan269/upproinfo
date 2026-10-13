"""Website loc programme Awin va mua goi xem du lieu."""

from __future__ import annotations

import csv
import io
import json
import secrets
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, Response, abort, redirect, render_template, request, session, url_for

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))

from awin_storage import DB_PATH, counts, today
from awin_web import billing

import vietqr

APP_DIR = Path(__file__).resolve().parent
USERS_PATH = APP_DIR / "users.json"
SECRET_PATH = APP_DIR / "secret_key.txt"
PAGE_SIZE = 24

app = Flask(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))
if not SECRET_PATH.exists():
    SECRET_PATH.write_text(secrets.token_hex(32), encoding="utf-8")
app.secret_key = SECRET_PATH.read_text(encoding="utf-8").strip()


def load_users() -> list[dict]:
    if not USERS_PATH.exists():
        users = [{"username": "admin", "password": "awininfo"}]
        USERS_PATH.write_text(json.dumps(users, indent=2), encoding="utf-8")
        return users
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [{"username": "admin", "password": "awininfo"}]
    return data if isinstance(data, list) else [{"username": "admin", "password": "awininfo"}]


def save_users(users: list[dict]) -> None:
    USERS_PATH.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def filters_from_request() -> dict:
    return {
        "q": request.args.get("q", "").strip(),
        "membership": request.args.get("membership", "").strip(),
        "payment": request.args.get("payment", "").strip(),
        "feed": request.args.get("feed", "").strip(),
        "status": request.args.get("status", "").strip(),
        "min_commission": request.args.get("min_commission", "").strip(),
        "min_epc": request.args.get("min_epc", "").strip(),
        "sort": request.args.get("sort", "name").strip() or "name",
        "created_today": request.args.get("created_today", "").strip(),
    }


def where_sql(filters: dict) -> tuple[str, list]:
    clauses = ["1=1"]
    args: list = []
    if filters.get("q"):
        like = f"%{filters['q']}%"
        clauses.append("(p.name LIKE ? OR d.website LIKE ? OR CAST(p.advertiser_id AS TEXT) LIKE ?)")
        args.extend([like, like, like])
    if filters.get("membership"):
        clauses.append("p.membership = ?")
        args.append(filters["membership"])
    if filters.get("payment") in {"green", "amber", "red"}:
        clauses.append("p.payment_status = ?")
        args.append(filters["payment"])
    if filters.get("feed") == "yes":
        clauses.append("p.product_feed = 1")
    elif filters.get("feed") == "no":
        clauses.append("p.product_feed = 0")
    if filters.get("status") == "active":
        clauses.append("p.active = 1")
    elif filters.get("status") == "inactive":
        clauses.append("p.active = 0")
    elif filters.get("status") == "detail":
        clauses.append("d.advertiser_id IS NOT NULL AND COALESCE(d.error, '') = ''")
    if filters.get("created_today"):
        clauses.append("p.created_at = ?")
        args.append(today())
    if filters.get("min_commission"):
        try:
            clauses.append("COALESCE(d.commission_percent, 0) >= ?")
            args.append(float(filters["min_commission"]))
        except ValueError:
            pass
    if filters.get("min_epc"):
        try:
            clauses.append("COALESCE(p.epc_value, 0) >= ?")
            args.append(float(filters["min_epc"]))
        except ValueError:
            pass
    return " AND ".join(clauses), args


SORTS = {
    "name": "p.name COLLATE NOCASE",
    "commission": "COALESCE(d.commission_percent, -1) DESC",
    "epc": "COALESCE(p.epc_value, -1) DESC",
    "newest": "p.created_at DESC, p.advertiser_id DESC",
    "cookie": "COALESCE(d.cookie_days, -1) DESC",
}


def page_url(page: int, **overrides) -> str:
    params = filters_from_request()
    params.update(overrides)
    params["page"] = page
    pairs = [(key, value) for key, value in params.items() if value not in ("", None)]
    return "/?" + urlencode(pairs)


@app.context_processor
def inject() -> dict:
    user = session.get("user")
    access = billing.access_for(user)
    return {
        "current_user": user,
        "entitled": access["entitled"],
        "sub_plan": access["plan"],
        "is_admin": access["is_admin"],
        "today_date": today(),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        for user in load_users():
            if user.get("username") == username and user.get("password") == password:
                session["user"] = username
                return redirect(url_for("index"))
        error = "Sai tên đăng nhập hoặc mật khẩu."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if len(username) < 3 or len(password) < 6:
            error = "Tên đăng nhập từ 3 ký tự, mật khẩu từ 6 ký tự."
        else:
            users = load_users()
            if any(item.get("username") == username for item in users):
                error = "Tên đăng nhập đã có."
            else:
                users.append({"username": username, "password": password})
                save_users(users)
                session["user"] = username
                return redirect(url_for("billing_page"))
    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not DB_PATH.exists():
        abort(500, "Chưa có dữ liệu. Chạy python awin_daily_sync.py trước.")
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    entitled = billing.is_entitled(user)
    filters = filters_from_request() if entitled else {
        "q": "", "membership": "", "payment": "", "feed": "", "status": "active",
        "min_commission": "", "min_epc": "", "sort": "name", "created_today": "",
    }
    page = 1 if not entitled else max(1, request.args.get("page", 1, type=int))
    where, args = where_sql(filters)
    order = SORTS.get(filters["sort"], SORTS["name"])
    conn = db()
    try:
        total = conn.execute(
            f"""
            SELECT COUNT(*) FROM programmes p
            LEFT JOIN details d ON d.advertiser_id = p.advertiser_id
            WHERE {where}
            """,
            args,
        ).fetchone()[0]
        if not entitled:
            pages = 1
            page = 1
            limit = PAGE_SIZE
            offset = 0
        else:
            pages = max(1, (int(total) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, pages)
            limit = PAGE_SIZE
            offset = (page - 1) * PAGE_SIZE
            total_shown = int(total)
        rows = conn.execute(
            f"""
            SELECT p.*, d.summary, d.website, d.logo, d.cookie_days, d.commission_percent,
                   d.regions, d.commission_text,
                   CASE WHEN d.advertiser_id IS NOT NULL AND COALESCE(d.error, '') = '' THEN 1 ELSE 0 END AS has_detail
            FROM programmes p
            LEFT JOIN details d ON d.advertiser_id = p.advertiser_id
            WHERE {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            [*args, limit, offset],
        ).fetchall()
        programmes = [dict(row) for row in rows]
        return render_template(
            "index.html",
            programmes=programmes,
            stats=counts(),
            filters=filters,
            page=page,
            pages=pages,
            total=int(total),
            page_url=page_url,
            pager=list(range(max(1, page - 2), min(pages, page + 2) + 1)) if entitled else [],
        )
    finally:
        conn.close()


@app.route("/programme/<int:advertiser_id>")
def programme(advertiser_id: int):
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    if not billing.is_entitled(user):
        return redirect(url_for("billing_page"))
    conn = db()
    try:
        row = conn.execute(
            """
            SELECT p.*, d.summary, d.description, d.website, d.logo, d.cookie_text, d.cookie_days,
                   d.validation_text, d.commission_text, d.commission_percent, d.commission_groups,
                   d.regions, d.emails, d.mobile_optimised, d.app_tracking, d.payment_note,
                   d.date_joined, d.profile_membership, d.detail_text, d.fetched_at, d.error
            FROM programmes p
            LEFT JOIN details d ON d.advertiser_id = p.advertiser_id
            WHERE p.advertiser_id = ?
            """,
            (advertiser_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    item = dict(row)
    try:
        item["groups"] = json.loads(item.get("commission_groups") or "[]")
    except json.JSONDecodeError:
        item["groups"] = []
    if not isinstance(item["groups"], list):
        item["groups"] = []
    return render_template("programme.html", item=item)


@app.route("/export.csv")
def export_csv():
    user = session.get("user")
    access = billing.access_for(user)
    if not access["is_admin"] and access["plan"] not in {"year", "quarter"}:
        return redirect(url_for("billing_page"))
    filters = filters_from_request()
    where, args = where_sql(filters)
    conn = db()
    try:
        rows = conn.execute(
            f"""
            SELECT p.advertiser_id, p.name, p.membership, p.conversion_rate, p.approval_rate,
                   p.epc, p.payment_status, p.avg_payment_text, p.product_feed, p.launch_date,
                   p.link_status, p.active, p.created_at, d.website, d.cookie_days,
                   d.commission_percent, d.commission_text, d.regions, d.summary
            FROM programmes p
            LEFT JOIN details d ON d.advertiser_id = p.advertiser_id
            WHERE {where}
            ORDER BY p.name COLLATE NOCASE
            """,
            args,
        ).fetchall()
    finally:
        conn.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "advertiser_id", "name", "membership", "conversion_rate", "approval_rate", "epc",
        "payment_status", "avg_payment", "product_feed", "launch_date", "link_status",
        "active", "created_at", "website", "cookie_days", "commission_percent",
        "commission_text", "regions", "summary",
    ])
    for row in rows:
        writer.writerow([row[key] for key in row.keys()])
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=awin-programmes.csv"},
    )


@app.route("/billing")
def billing_page():
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    access = billing.access_for(user)
    return render_template(
        "billing.html",
        plans=billing.PLANS,
        access=access,
        pay_ready=vietqr.is_configured(),
        error=session.pop("billing_error", ""),
    )


@app.route("/billing/checkout", methods=["POST"])
def billing_checkout():
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    if not vietqr.is_configured():
        session["billing_error"] = "Chưa cấu hình VietQR."
        return redirect(url_for("billing_page"))
    plan = billing.plan_of(request.form.get("plan") or "")
    if not plan:
        session["billing_error"] = "Gói không hợp lệ."
        return redirect(url_for("billing_page"))
    trade_no = vietqr.new_order_id()
    order = billing.create_order(user, plan["id"], trade_no)
    return_url = url_for("billing_return", trade=trade_no, _external=True)
    try:
        created = vietqr.create_payment(
            int(plan["amount"]),
            return_url,
            order["pay_content"],
            order["pay_content"],
        )
    except RuntimeError:
        session["billing_error"] = "Không tạo được mã thanh toán."
        return redirect(url_for("billing_page"))
    billing.update_checkout(
        trade_no,
        created.get("transaction_id") or "",
        created.get("qr_link") or "",
        json.dumps({k: v for k, v in created.items() if k != "qr_image"}, ensure_ascii=False),
    )
    session["awin_qr"] = created.get("qr_image") or ""
    return redirect(url_for("billing_return", trade=trade_no))


@app.route("/billing/return")
def billing_return():
    user = session.get("user")
    trade = (request.args.get("trade") or "").strip()
    order = billing.get_order(trade) if trade else None
    if not order or order["username"] != user:
        return redirect(url_for("billing_page"))
    if order["status"] == "paid":
        return redirect(url_for("index"))
    plan = billing.plan_of(order["plan"]) or {}
    qr_image = session.get("awin_qr") or ""
    if not qr_image and order.get("pay_content"):
        try:
            qr_image = vietqr.branded_qr_url(int(order["amount"]), str(order["pay_content"]))
        except Exception:
            qr_image = ""
    return render_template(
        "billing_return.html",
        order=order,
        plan=plan,
        qr_image=qr_image,
        amount_text=billing.format_vnd(order["amount"]),
    )


def main() -> None:
    app.run(host="127.0.0.1", port=5051, debug=False)


if __name__ == "__main__":
    main()
