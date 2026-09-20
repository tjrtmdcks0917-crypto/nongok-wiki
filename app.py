import os
import re
import secrets
import time
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash

from db import init_db, query, execute

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

RATE = {}
RATE_WINDOW = 60
RATE_MAX = 60

def rate_limit():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0]
    now = time.time()
    hits = [t for t in RATE.get(ip, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_MAX:
        abort(429)
    hits.append(now)
    RATE[ip] = hits

@app.before_request
def before():
    rate_limit()
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)

@app.context_processor
def inject():
    return {"current_user": current_user(), "csrf": session.get("csrf")}

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    rows = query("SELECT id, username, role FROM users WHERE id = %s", (uid,))
    return rows[0] if rows else None

def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("로그인이 필요합니다.", "warning")
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

def check_csrf():
    token = request.form.get("csrf")
    if not token or token != session.get("csrf"):
        abort(400, "CSRF 토큰이 올바르지 않습니다.")

def slugify(title):
    title = re.sub(r"\s+", " ", title.strip())
    return title[:120]

def render_wiki(text):
    safe = str(escape(text or ""))
    safe = re.sub(
        r"\[\[([^\[\]]{1,120})\]\]",
        lambda m: f'<a href="{url_for("wiki", title=m.group(1).strip())}">{m.group(1).strip()}</a>',
        safe,
    )
    return safe.replace("\n", "<br>\n")

def seed():
    existing = query("SELECT COUNT(*) AS c FROM wiki_pages")[0]["c"]
    if existing:
        return
    samples = [
        ("논곡중학교", "## 개요\n논곡중학교에 관한 공개 정보를 정리하는 문서입니다.\n\n학교의 공식 공지와 공개 자료를 우선 참고하세요."),
        ("학교 시설", "학교 시설에 관한 정보를 정리하는 문서입니다.\n\n예: 교실, 도서관, 운동장, 특별실 등."),
        ("동아리", "논곡중학교의 동아리 활동을 정리하는 문서입니다."),
        ("학생회", "학생회와 관련된 공개 정보를 정리하는 문서입니다."),
        ("학교 행사", "학교에서 진행되는 공개 행사를 정리하는 문서입니다."),
    ]
    for title, content in samples:
        execute(
            "INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)",
            (title, content),
        )

@app.route("/")
def index():
    recent = query("SELECT title, updated_at FROM wiki_pages WHERE deleted=FALSE ORDER BY updated_at DESC LIMIT 10")
    popular = query("SELECT title, views FROM wiki_pages WHERE deleted=FALSE ORDER BY views DESC, updated_at DESC LIMIT 10")
    return render_template("index.html", recent=recent, popular=popular)

@app.route("/wiki/<path:title>")
def wiki(title):
    rows = query("SELECT * FROM wiki_pages WHERE title=%s AND deleted=FALSE", (title,))
    if not rows:
        return render_template("not_found.html", title=title), 404
    page = rows[0]
    execute("UPDATE wiki_pages SET views=views+1 WHERE id=%s", (page["id"],))
    discussions = query(
        """SELECT d.*, u.username FROM discussions d
           LEFT JOIN users u ON u.id=d.user_id
           WHERE d.page_id=%s ORDER BY d.created_at DESC LIMIT 50""",
        (page["id"],),
    )
    return render_template("wiki.html", page=page, content_html=render_wiki(page["content"]), discussions=discussions)

@app.route("/edit/<path:title>", methods=["GET", "POST"])
@require_login
def edit(title):
    rows = query("SELECT * FROM wiki_pages WHERE title=%s AND deleted=FALSE", (title,))
    page = rows[0] if rows else None
    if request.method == "POST":
        check_csrf()
        content = request.form.get("content", "").strip()
        new_title = slugify(request.form.get("title", title))
        if not new_title or not content:
            flash("제목과 내용을 입력해줘.", "warning")
            return redirect(request.url)
        user = current_user()
        if page and page["protected"] and user["role"] != "admin":
            abort(403)
        if page:
            execute("INSERT INTO revisions(page_id, title, content, author_id, created_at) VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP)", (page["id"], page["title"], page["content"], user["id"]))
            execute("UPDATE wiki_pages SET title=%s, content=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (new_title, content, page["id"]))
        else:
            execute("INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)", (new_title, content, user["id"]))
        flash("문서를 저장했어.", "success")
        return redirect(url_for("wiki", title=new_title))
    return render_template("edit.html", page=page, title=title)

@app.route("/new", methods=["GET", "POST"])
@require_login
def new_page():
    if request.method == "POST":
        check_csrf()
        title = slugify(request.form.get("title", ""))
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("제목과 내용을 입력해줘.", "warning")
            return redirect(url_for("new_page"))
        if query("SELECT id FROM wiki_pages WHERE title=%s", (title,)):
            flash("이미 같은 제목의 문서가 있어.", "warning")
            return redirect(url_for("wiki", title=title))
        user = current_user()
        execute("INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)", (title, content, user["id"]))
        return redirect(url_for("wiki", title=title))
    return render_template("edit.html", page=None, title="")

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results = []
    if q:
        like = f"%{q}%"
        results = query(
            """SELECT title, content, updated_at FROM wiki_pages
               WHERE deleted=FALSE AND (title ILIKE %s OR content ILIKE %s)
               ORDER BY updated_at DESC LIMIT 50""",
            (like, like),
        )
    return render_template("search.html", q=q, results=results)

@app.route("/history/<path:title>")
def history(title):
    page_rows = query("SELECT id, title FROM wiki_pages WHERE title=%s", (title,))
    if not page_rows:
        abort(404)
    page = page_rows[0]
    revisions = query(
        """SELECT r.*, u.username FROM revisions r
           LEFT JOIN users u ON u.id=r.author_id
           WHERE r.page_id=%s ORDER BY r.created_at DESC LIMIT 100""",
        (page["id"],),
    )
    return render_template("history.html", page=page, revisions=revisions)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not re.fullmatch(r"[A-Za-z0-9가-힣_]{2,24}", username):
            flash("아이디는 2~24자의 한글/영문/숫자/밑줄만 사용할 수 있어.", "warning")
            return redirect(url_for("register"))
        if len(password) < 8:
            flash("비밀번호는 8자 이상으로 해줘.", "warning")
            return redirect(url_for("register"))
        if query("SELECT id FROM users WHERE username=%s", (username,)):
            flash("이미 사용 중인 아이디야.", "warning")
            return redirect(url_for("register"))
        execute("INSERT INTO users(username,password_hash,role,created_at) VALUES (%s,%s,'user',CURRENT_TIMESTAMP)", (username, generate_password_hash(password)))
        flash("회원가입 완료! 로그인해줘.", "success")
        return redirect(url_for("login"))
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        rows = query("SELECT * FROM users WHERE username=%s", (username,))
        if rows and check_password_hash(rows[0]["password_hash"], password):
            session["user_id"] = rows[0]["id"]
            flash("로그인했어.", "success")
            return redirect(request.args.get("next") or url_for("index"))
        flash("아이디 또는 비밀번호가 맞지 않아.", "warning")
    return render_template("auth.html", mode="login")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/discuss/<int:page_id>", methods=["POST"])
@require_login
def discuss(page_id):
    check_csrf()
    body = request.form.get("body", "").strip()
    if not body or len(body) > 2000:
        flash("토론 내용은 1~2000자로 입력해줘.", "warning")
        return redirect(request.referrer or url_for("index"))
    execute("INSERT INTO discussions(page_id,user_id,body,created_at) VALUES (%s,%s,%s,CURRENT_TIMESTAMP)", (page_id, current_user()["id"], body))
    return redirect(request.referrer or url_for("index"))

@app.route("/report", methods=["POST"])
@require_login
def report():
    check_csrf()
    page_id = request.form.get("page_id")
    reason = request.form.get("reason", "").strip()
    if not reason or len(reason) > 1000:
        flash("신고 사유를 입력해줘.", "warning")
        return redirect(request.referrer or url_for("index"))
    execute("INSERT INTO reports(page_id,user_id,reason,status,created_at) VALUES (%s,%s,%s,'open',CURRENT_TIMESTAMP)", (page_id, current_user()["id"], reason))
    flash("신고가 접수됐어.", "success")
    return redirect(request.referrer or url_for("index"))

@app.route("/admin")
@require_admin
def admin():
    reports = query(
        """SELECT r.*, w.title, u.username FROM reports r
           LEFT JOIN wiki_pages w ON w.id=r.page_id
           LEFT JOIN users u ON u.id=r.user_id
           ORDER BY r.created_at DESC LIMIT 100"""
    )
    users = query("SELECT id, username, role, created_at FROM users ORDER BY created_at DESC LIMIT 100")
    return render_template("admin.html", reports=reports, users=users)

@app.route("/admin/report/<int:report_id>/<status>", methods=["POST"])
@require_admin
def report_status(report_id, status):
    check_csrf()
    if status not in {"open", "resolved", "dismissed"}:
        abort(400)
    execute("UPDATE reports SET status=%s WHERE id=%s", (status, report_id))
    return redirect(url_for("admin"))

@app.route("/admin/protect/<int:page_id>", methods=["POST"])
@require_admin
def protect(page_id):
    check_csrf()
    execute("UPDATE wiki_pages SET protected=NOT protected WHERE id=%s", (page_id,))
    return redirect(request.referrer or url_for("admin"))

@app.route("/admin/delete/<int:page_id>", methods=["POST"])
@require_admin
def delete_page(page_id):
    check_csrf()
    execute("UPDATE wiki_pages SET deleted=TRUE, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (page_id,))
    return redirect(request.referrer or url_for("admin"))

@app.errorhandler(429)
def too_many(_):
    return render_template("error.html", code=429, message="요청이 너무 많아. 잠시 후 다시 시도해줘."), 429

@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, message="이 작업을 할 권한이 없어."), 403

@app.errorhandler(500)
def server_error(_):
    return render_template("error.html", code=500, message="서버 오류가 발생했어. 관리자에게 알려줘."), 500

with app.app_context():
    init_db()
    from db import ensure_admin
    ensure_admin()
    seed()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
