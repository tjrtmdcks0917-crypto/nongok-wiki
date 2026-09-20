import os
import re
import secrets
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import urlopen, Request
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

MEAL_CACHE = {"expires": 0, "meals": [], "error": None}
TIMETABLE_CACHE = {}\nTEACHER_CACHE = {}

def get_masked_comcigan_teachers(grade, class_num, target):
    """Best-effort masked teacher labels; failure never breaks the NEIS timetable."""
    monday = target - timedelta(days=target.weekday())
    key = (grade, class_num, monday.isoformat())
    now = time.time()
    cached = TEACHER_CACHE.get(key)
    if cached and cached["expires"] > now:
        return cached["teachers"]
    teachers = {}
    try:
        from comci import search_schools, get_timetable
        schools = search_schools("논곡중학교") or []
        exact = [x for x in schools if x.get("school_name") == "논곡중학교"]
        school = next((x for x in exact if x.get("region") == "인천"), exact[0] if exact else None)
        if not school:
            raise RuntimeError("컴시간 학교 검색 실패")
        digits = "".join(ch for ch in str(school["school_code"]) if ch.isdigit())
        if not digits:
            raise RuntimeError("컴시간 학교 코드 해석 실패")
        table = get_timetable(int(digits), grade=grade, class_num=class_num, on=target) or {}
        for weekday in ["월", "화", "수", "목", "금"]:
            teachers[weekday] = []
            for item in (table.get(weekday) or []):
                teacher = item.get("teacher", "") if isinstance(item, dict) else ""
                # Keep only names already masked by Comcigan; never reconstruct hidden letters.
                teachers[weekday].append(teacher if "*" in teacher else "")
    except Exception:
        app.logger.warning("Comcigan teacher enrichment failed", exc_info=True)
        teachers = {}
    TEACHER_CACHE[key] = {"expires": now + (300 if teachers else 60), "teachers": teachers}
    return teachers


def get_nongok_timetable(grade, class_num):
    """Fetch weekly timetable from NEIS using the same HTTP pattern as meals."""
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    target = today + timedelta(days=1) if today.weekday() == 6 else today
    monday = target - timedelta(days=target.weekday())
    friday = monday + timedelta(days=4)
    label = f"{monday.strftime('%m/%d')} ~ {friday.strftime('%m/%d')}"
    key = (grade, class_num, monday.isoformat())
    now = time.time()
    cached = TIMETABLE_CACHE.get(key)
    if cached and cached["expires"] > now:
        return cached["days"], cached["error"], cached["week_label"]
    params = {
        "KEY": os.environ.get("NEIS_API_KEY", ""),
        "Type": "json", "pIndex": 1, "pSize": 100,
        "ATPT_OFCDC_SC_CODE": "E10",
        "SD_SCHUL_CODE": os.environ.get("NEIS_SCHOOL_CODE", "7341070"),
        "AY": str(target.year),
        "SEM": "1" if target.month <= 7 else "2",
        "GRADE": str(grade),
        "CLASS_NM": str(class_num),
        "TI_FROM_YMD": monday.strftime("%Y%m%d"),
        "TI_TO_YMD": friday.strftime("%Y%m%d"),
    }
    try:
        if not params["KEY"]:
            params.pop("KEY")
        url = "https://open.neis.go.kr/hub/misTimetable?" + urlencode(params)
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        rows = []
        for part in data.get("misTimetable", []):
            if isinstance(part, dict) and "row" in part:
                rows = part["row"]
                break
        # With no API key NEIS may cap results at five rows. Retry day-by-day,
        # exactly like the meal API, so a full week still works.
        if len(rows) <= 5:
            rows = []
            for n in range(5):
                d = monday + timedelta(days=n)
                day_params = dict(params)
                day_params.pop("TI_FROM_YMD", None)
                day_params.pop("TI_TO_YMD", None)
                day_params["ALL_TI_YMD"] = d.strftime("%Y%m%d")
                day_url = "https://open.neis.go.kr/hub/misTimetable?" + urlencode(day_params)
                with urlopen(Request(day_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=8) as response:
                    day_data = json.loads(response.read().decode("utf-8"))
                for part in day_data.get("misTimetable", []):
                    if isinstance(part, dict) and "row" in part:
                        rows.extend(part["row"])
                        break
        by_date = {}
        for row in rows:
            ymd = row.get("ALL_TI_YMD", "")
            if not ymd:
                continue
            perio = int(row.get("PERIO", 0) or 0)
            subject = row.get("ITRT_CNTNT", "").strip()
            by_date.setdefault(ymd, {})[perio] = subject
        weekdays = ["월", "화", "수", "목", "금"]
        days = []
        for n, weekday in enumerate(weekdays):
            d = monday + timedelta(days=n)
            periods = by_date.get(d.strftime("%Y%m%d"), {})
            classes = [periods.get(p, "") for p in range(1, max(periods.keys(), default=0) + 1)]
            days.append({"weekday": weekday, "date": d, "classes": classes})
        if not any(day["classes"] for day in days):
            app.logger.warning("NEIS timetable returned no rows: %s", data)
            raise RuntimeError("NEIS에 해당 학년/반 시간표가 없습니다.")
        TIMETABLE_CACHE[key] = {"expires": now + 1800, "days": days, "error": None, "week_label": label, "debug": None}
    except Exception as e:
        app.logger.exception("NEIS timetable fetch failed grade=%s class=%s", grade, class_num)
        TIMETABLE_CACHE[key] = {"expires": now + 60, "days": [], "error": "시간표 정보를 불러오지 못했습니다.", "week_label": label, "debug": f"{type(e).__name__}: {e}"[:700]}
    c = TIMETABLE_CACHE[key]
    return c["days"], c["error"], c["week_label"]

def get_nongok_meals():
    """Fetch this week's Nongok Middle School lunches from NEIS; cache for 30 min."""
    now = time.time()
    if MEAL_CACHE["expires"] > now:
        return MEAL_CACHE["meals"], MEAL_CACHE["error"]
    # Render runs in UTC, so always calculate the school date in Korea time.
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    # Mon-Sat: keep showing the current week's Mon-Fri meals.
    # Sun: switch ahead and show the coming week's Mon-Fri meals.
    if today.weekday() == 6:
        monday = today + timedelta(days=1)
    else:
        monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    params = {
        "KEY": os.environ.get("NEIS_API_KEY", ""),
        "Type": "json", "pIndex": 1, "pSize": 5,
        "ATPT_OFCDC_SC_CODE": "E10",
        # Exact NEIS school code for Nongok Middle School can be set in Render.
        # Keeping the school name as well prevents similarly named schools from matching.
        "SD_SCHUL_CODE": os.environ.get("NEIS_SCHOOL_CODE", "7341070"),
        "SCHUL_NM": "논곡중학교",
        "MLSV_FROM_YMD": monday.strftime("%Y%m%d"),
        "MLSV_TO_YMD": friday.strftime("%Y%m%d"),
    }
    try:
        # Do not send an empty school-code parameter.
        if not params["SD_SCHUL_CODE"]:
            params.pop("SD_SCHUL_CODE")
        # NEIS allows unauthenticated JSON queries for this public dataset.
        # Omitting KEY is more reliable than using the restricted demo/sample key.
        if not params["KEY"]:
            params.pop("KEY")
        url = "https://open.neis.go.kr/hub/mealServiceDietInfo?" + urlencode(params)
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        rows = []
        # NEIS may return either {"mealServiceDietInfo":[...]} or an error/result object.
        for block in data.get("mealServiceDietInfo", []):
            if isinstance(block, dict) and "row" in block:
                rows = block["row"]
                break
        if not rows:
            app.logger.warning("NEIS returned no meal rows: %s", data)
        meals = []
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        for row in rows:
            date = datetime.strptime(row["MLSV_YMD"], "%Y%m%d").date()
            dishes = re.sub(r"<br\s*/?>", "\n", row.get("DDISH_NM", ""), flags=re.I)
            meals.append({"date": date, "weekday": weekdays[date.weekday()], "dishes": dishes,
                          "calories": row.get("CAL_INFO", ""), "today": date == today})
        MEAL_CACHE.update({"expires": now + 1800, "meals": meals, "error": None})
    except Exception as e:
        # Keep the page usable, while exposing a short diagnostic to admins.
        MEAL_CACHE.update({"expires": now + 60, "meals": [], "error": "급식 정보를 불러오지 못했습니다."})
        app.logger.warning("NEIS meal fetch failed: %s", e)
    return MEAL_CACHE["meals"], MEAL_CACHE["error"]


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
    recent = query("""
        SELECT w.title, w.updated_at, w.views,
               CASE
                 WHEN (SELECT MIN(viewed_at) FROM page_views) IS NULL
                   OR (SELECT MIN(viewed_at) FROM page_views) >= CURRENT_TIMESTAMP - INTERVAL '12 hours'
                 THEN w.views
                 ELSE (SELECT COUNT(*) FROM page_views v WHERE v.page_id=w.id AND v.viewed_at >= CURRENT_TIMESTAMP - INTERVAL '12 hours')
               END AS hourly_views
        FROM wiki_pages w WHERE w.deleted=FALSE ORDER BY w.updated_at DESC LIMIT 10
    """)
    popular = query("""
        SELECT w.title, w.views,
               CASE
                 WHEN (SELECT MIN(viewed_at) FROM page_views) IS NULL
                   OR (SELECT MIN(viewed_at) FROM page_views) >= CURRENT_TIMESTAMP - INTERVAL '12 hours'
                 THEN w.views
                 ELSE (SELECT COUNT(*) FROM page_views v WHERE v.page_id=w.id AND v.viewed_at >= CURRENT_TIMESTAMP - INTERVAL '12 hours')
               END AS hourly_views
        FROM wiki_pages w WHERE w.deleted=FALSE ORDER BY hourly_views DESC, w.views DESC LIMIT 10
    """)
    daily = query("""
        SELECT w.title, w.views,
               CASE
                 WHEN (SELECT MIN(viewed_at) FROM page_views) IS NULL
                   OR (SELECT MIN(viewed_at) FROM page_views) >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
                 THEN w.views
                 ELSE COUNT(v.id)
               END AS daily_views
        FROM wiki_pages w LEFT JOIN page_views v
          ON v.page_id=w.id AND v.viewed_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        WHERE w.deleted=FALSE
        GROUP BY w.id, w.title, w.views
        ORDER BY daily_views DESC, w.views DESC LIMIT 10
    """)
    return {"current_user": current_user(), "csrf": session.get("csrf"),
            "global_recent": recent, "global_popular": popular, "global_daily": daily}

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

    # Internal links: [[문서명]]
    safe = re.sub(
        r"\[\[([^\[\]]{1,120})\]\]",
        lambda m: f'<a href="{url_for("wiki", title=m.group(1).strip())}">{m.group(1).strip()}</a>',
        safe,
    )

    # External links: [표시할 글](https://example.com)
    safe = re.sub(
        r"\[([^\[\]\n]{1,200})\]\((https?://[^\s<>]+)\)",
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        safe,
    )

    # NamuWiki-style headings with automatic section numbering.
    # == 큰 제목 ==  -> 1. 큰 제목
    # === 작은 제목 === -> 1.1. 작은 제목
    major = 0
    minor = 0
    out = []
    for line in safe.splitlines():
        sub = re.fullmatch(r"===\s*(.+?)\s*===", line.strip())
        top = re.fullmatch(r"==\s*(.+?)\s*==", line.strip())
        if sub:
            if major == 0:
                major = 1
            minor += 1
            title = sub.group(1)
            anchor = f"section-{major}-{minor}"
            out.append(f'<h3 id="{anchor}" class="wiki-heading wiki-heading-sub"><span class="wiki-section-number">{major}.{minor}.</span> {title}</h3>')
        elif top:
            major += 1
            minor = 0
            title = top.group(1)
            anchor = f"section-{major}"
            out.append(f'<h2 id="{anchor}" class="wiki-heading"><span class="wiki-section-number">{major}.</span> {title}</h2>')
        else:
            out.append(line)
    return "<br>\n".join(out)

def ensure_schoollife_pages():
    """Ensure core School Life documents exist without overwriting user edits."""
    pages = [
        ("학교생활", "== 학교생활 ==\n논곡중학교의 학교생활 정보를 정리하는 문서입니다.\n\n=== 급식 ===\n[[급식]]\n\n=== 시간표 ===\n[[시간표]]"),
        ("급식", "논곡중학교 급식 정보를 확인하는 문서입니다."),
        ("시간표", "== 시간표 ==\n논곡중학교 시간표 정보를 정리하는 문서입니다.\n\n학년과 반별 시간표를 확인할 수 있도록 내용을 추가해 주세요."),
    ]
    for title, content in pages:
        if not query("SELECT id FROM wiki_pages WHERE title=%s AND deleted=FALSE", (title,)):
            execute("INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)", (title, content))

def seed():
    existing = query("SELECT COUNT(*) AS c FROM wiki_pages")[0]["c"]
    if existing:
        return
    samples = [
        ("논곡중학교", "## 개요\n논곡중학교에 관한 정보를 자유롭게 정리하는 문서입니다.\n\n학교생활과 관련된 다양한 내용을 함께 기록해 주세요."),
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
    ensure_schoollife_pages()

@app.route("/robots.txt")
def robots_txt():
    body = "User-agent: *\nAllow: /\nSitemap: https://nongok-wiki.onrender.com/sitemap.xml\n"
    return app.response_class(body, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap_xml():
    pages = query("SELECT title, updated_at FROM wiki_pages WHERE deleted=FALSE ORDER BY updated_at DESC")
    urls = ['<url><loc>https://nongok-wiki.onrender.com/</loc></url>']
    from urllib.parse import quote
    for page in pages:
        loc = "https://nongok-wiki.onrender.com/wiki/" + quote(page["title"], safe="")
        urls.append(f"<url><loc>{loc}</loc></url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(urls) + "</urlset>"
    return app.response_class(xml, mimetype="application/xml")

@app.route("/")
def index():
    recent = query("SELECT title, updated_at FROM wiki_pages WHERE deleted=FALSE ORDER BY updated_at DESC LIMIT 10")
    popular = query("SELECT title, views FROM wiki_pages WHERE deleted=FALSE ORDER BY views DESC, updated_at DESC LIMIT 10")
    defaults = {
        "notice": "다른 사람의 연락처, 주소 등 사적인 개인정보는 보호해 주세요.\n친구를 공격하거나 괴롭히는 내용은 작성하지 말아 주세요.\n학교생활, 추억, 정보 등 다양한 내용을 자유롭게 작성해 주세요.",
        "news": "논곡위키 공개 베타 운영 중입니다.\n문서 편집과 토론 기능을 사용할 수 있습니다.",
        "feedback": "오류나 개선할 점은 문서 토론 또는 관리자에게 알려주세요.",
        "supporters": "아직 등록된 후원자가 없습니다.",
    }
    rows = query("SELECT section_key, content FROM homepage_sections")
    sections = defaults.copy()
    sections.update({row["section_key"]: row["content"] for row in rows})
    return render_template("index.html", recent=recent, popular=popular, sections=sections)

@app.route("/admin/homepage/<section_key>", methods=["GET", "POST"])
@require_admin
def edit_homepage_section(section_key):
    labels = {"notice": "유의사항", "news": "공지사항", "feedback": "피드백", "supporters": "후원자"}
    if section_key not in labels:
        abort(404)
    rows = query("SELECT content FROM homepage_sections WHERE section_key=%s", (section_key,))
    content = rows[0]["content"] if rows else ""
    if request.method == "POST":
        check_csrf()
        content = request.form.get("content", "").strip()
        existing = query("SELECT section_key FROM homepage_sections WHERE section_key=%s", (section_key,))
        if existing:
            execute("UPDATE homepage_sections SET content=%s, updated_at=CURRENT_TIMESTAMP WHERE section_key=%s", (content, section_key))
        else:
            execute("INSERT INTO homepage_sections(section_key, content, updated_at) VALUES (%s,%s,CURRENT_TIMESTAMP)", (section_key, content))
        flash(f'{labels[section_key]} 내용을 저장했습니다.', "success")
        return redirect(url_for("index"))
    return render_template("homepage_edit.html", section_key=section_key, section_label=labels[section_key], content=content)

@app.route("/wiki/<path:title>")
def wiki(title):
    rows = query("SELECT * FROM wiki_pages WHERE title=%s AND deleted=FALSE", (title,))
    if not rows:
        return render_template("not_found.html", title=title), 404
    page = rows[0]
    execute("UPDATE wiki_pages SET views=views+1 WHERE id=%s", (page["id"],))
    execute("INSERT INTO page_views(page_id, viewed_at) VALUES (%s, CURRENT_TIMESTAMP)", (page["id"],))
    discussions = query(
        """SELECT d.*, u.username FROM discussions d
           LEFT JOIN users u ON u.id=d.user_id
           WHERE d.page_id=%s ORDER BY d.created_at DESC LIMIT 50""",
        (page["id"],),
    )
    meals, meal_error = ([], None)
    if page["title"] in ("급식", "학교생활"):
        meals, meal_error = get_nongok_meals()
    timetable, timetable_error, timetable_week = ([], None, "")
    timetable_grade, timetable_class = (2, 1)
    if page["title"] == "시간표":
        try:
            timetable_grade = min(3, max(1, int(request.args.get("grade", "2"))))
            timetable_class = min(20, max(1, int(request.args.get("class", "1"))))
        except ValueError:
            timetable_grade, timetable_class = (2, 1)
        timetable, timetable_error, timetable_week = get_nongok_timetable(timetable_grade, timetable_class)
    return render_template("wiki.html", page=page, content_html=render_wiki(page["content"]), discussions=discussions,
                           meals=meals, meal_error=meal_error, timetable=timetable,
                           timetable_error=timetable_error, timetable_week=timetable_week,
                           timetable_grade=timetable_grade, timetable_class=timetable_class)

@app.route("/admin/timetable-debug")
@require_admin
def timetable_debug():
    get_nongok_timetable(1, 1)
    items = []
    for key, value in TIMETABLE_CACHE.items():
        items.append({"key": str(key), "error": value.get("error"), "debug": value.get("debug"), "week": value.get("week_label")})
    return {"cache": items}

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
            flash("제목과 내용을 입력해 주세요.", "warning")
            return redirect(request.url)
        user = current_user()
        if new_title == "논곡위키:대문" and user["role"] != "admin":
            abort(403)
        if page and page["title"] == "논곡위키:대문" and user["role"] != "admin":
            abort(403)
        if page and page["protected"] and user["role"] != "admin":
            abort(403)
        if page:
            execute("INSERT INTO revisions(page_id, title, content, author_id, created_at) VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP)", (page["id"], page["title"], page["content"], user["id"]))
            execute("UPDATE wiki_pages SET title=%s, content=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (new_title, content, page["id"]))
        else:
            execute("INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)", (new_title, content, user["id"]))
        flash("문서를 저장했습니다.", "success")
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
            flash("이미 같은 제목의 문서가 있습니다.", "warning")
            return redirect(url_for("wiki", title=title))
        user = current_user()
        execute("INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)", (title, content, user["id"]))
        return redirect(url_for("wiki", title=title))
    return render_template("edit.html", page=None, title="")

@app.route("/all-pages")
def all_pages():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 10
    total = query("SELECT COUNT(*) AS c FROM wiki_pages WHERE deleted=FALSE")[0]["c"]
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    pages = query(
        "SELECT title, updated_at, views FROM wiki_pages WHERE deleted=FALSE ORDER BY title ASC LIMIT %s OFFSET %s",
        (per_page, offset),
    )
    return render_template("all_pages.html", pages=pages, page=page, total_pages=total_pages, total=total)

@app.route("/chat")
def chat():
    return render_template("chat.html")

@app.route("/api/chat/messages")
def chat_messages():
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        after = 0
    rows = query("""SELECT c.id, c.body, c.created_at, u.username
                    FROM chat_messages c JOIN users u ON u.id=c.user_id
                    WHERE c.id>%s ORDER BY c.id ASC LIMIT 100""", (after,))
    for row in rows:
        if hasattr(row["created_at"], "isoformat"):
            row["created_at"] = row["created_at"].isoformat()
    return {"messages": rows}

@app.route("/api/chat/send", methods=["POST"])
@require_login
def chat_send():
    check_csrf()
    body = request.form.get("body", "").strip()
    if not body or len(body) > 500:
        return {"ok": False, "error": "메시지는 1~500자로 작성해 주세요."}, 400
    user = current_user()
    execute("INSERT INTO chat_messages(user_id, body, created_at) VALUES (%s,%s,CURRENT_TIMESTAMP)", (user["id"], body))
    return {"ok": True}

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
            flash("아이디는 2~24자의 한글/영문/숫자/밑줄만 사용할 수 있습니다.", "warning")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("비밀번호는 6자 이상으로 입력해 주세요.", "warning")
            return redirect(url_for("register"))
        if query("SELECT id FROM users WHERE username=%s", (username,)):
            flash("이미 사용 중인 아이디입니다.", "warning")
            return redirect(url_for("register"))
        execute("INSERT INTO users(username,password_hash,role,created_at) VALUES (%s,%s,'user',CURRENT_TIMESTAMP)", (username, generate_password_hash(password)))
        flash("회원가입이 완료되었습니다. 로그인해 주세요.", "success")
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
            flash("로그인되었습니다.", "success")
            return redirect(request.form.get("next") or request.args.get("next") or url_for("index"))
        flash("아이디 또는 비밀번호가 맞지 않습니다.", "warning")
    return render_template("auth.html", mode="login", next_url=request.args.get("next", ""), entered_username=request.form.get("username", ""))

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
        flash("토론 내용은 1~2000자로 입력해 주세요.", "warning")
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
        flash("신고 사유를 입력해 주세요.", "warning")
        return redirect(request.referrer or url_for("index"))
    execute("INSERT INTO reports(page_id,user_id,reason,status,created_at) VALUES (%s,%s,%s,'open',CURRENT_TIMESTAMP)", (page_id, current_user()["id"], reason))
    flash("신고가 접수되었습니다.", "success")
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
    return render_template("error.html", code=429, message="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."), 429

@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, message="이 작업을 할 권한이 없습니다."), 403

@app.errorhandler(500)
def server_error(_):
    return render_template("error.html", code=500, message="서버 오류가 발생했습니다. 관리자에게 알려 주세요."), 500

with app.app_context():
    init_db()
    from db import ensure_admin
    ensure_admin()
    seed()
    ensure_schoollife_pages()
    # Keep the default school-facilities page structured with numbered sections.
    facility = query("SELECT id, content FROM wiki_pages WHERE title=%s AND deleted=FALSE", ("학교 시설",))
    if facility and facility[0]["content"].strip() == "학교 본관은 앞뒤를 기준으로 '전관'과 '후관'으로 나뉩니다.":
        execute(
            "UPDATE wiki_pages SET content=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            ("== 학교 시설 ==\n\n학교 본관은 앞뒤를 기준으로 '전관'과 '후관'으로 나뉩니다.\n\n=== 전관 ===\n교장실\n교무실\n1~3학년 교실\nwee클래스\n보건실\n\n=== 후관 ===\n과학실\n기술실\n음악실\n정보실\n학생자치실", facility[0]["id"]),
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
