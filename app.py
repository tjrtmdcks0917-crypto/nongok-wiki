import os
import re
import secrets
import time
import json
import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from functools import wraps
from io import BytesIO

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash
from PIL import Image, ImageOps

from db import init_db, query, execute

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 14 * 1024 * 1024

RATE = {}
RATE_WINDOW = 60
RATE_MAX = 60

MEAL_CACHE = {"expires": 0, "meals": [], "error": None}
TIMETABLE_CACHE = {}

def _masked_teacher_name(value):
    name = str(value or "").strip()
    if not name:
        return ""
    if "*" in name:
        return name
    if len(name) <= 1:
        return "*"
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" + name[-1]


def _read_url(url, encoding="utf-8", timeout=5, extra_headers=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "http://comci.net:4082/st",
        "X-Requested-With": "XMLHttpRequest",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode(encoding, errors="replace")


def _comcigan_json(text):
    cleaned = text.replace("\x00", "").strip()
    end = cleaned.rfind("}")
    if end >= 0:
        cleaned = cleaned[:end + 1]
    return json.loads(cleaned)


def _comcigan_number(value):
    raw = str(value or "0").strip()
    changed = raw.startswith(">")
    if changed:
        raw = raw[1:]
    try:
        number = int(raw)
    except ValueError:
        number = 0
    return number, changed


def _masked_teacher_name(value):
    name = str(value or "").strip()
    if not name:
        return ""
    if "*" in name:
        return name
    if len(name) <= 1:
        return "*"
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" + name[-1]


def _fetch_comcigan_current_protocol(grade, class_num, target_date):
    """Direct Comcigan request matching the current student-site protocol."""
    base = "http://comci.net:4082"
    endpoint = "36179"

    # Resolve the Comcigan school code every time from the actual school search.
    # Do not trust a hard-coded number: these codes are Comcigan-internal.
    try:
        encoded_name = "".join(f"%{b:02X}" for b in "논곡중학교".encode("euc-kr"))
        search_raw = _read_url(
            f"{base}/{endpoint}?17384l{encoded_name}",
            timeout=5,
        )
        search_data = _comcigan_json(search_raw)
    except Exception as e:
        raise RuntimeError(f"학교검색 단계 실패: {type(e).__name__}: {e}") from e

    matches = []
    for row in search_data.get("학교검색", []):
        if not isinstance(row, list) or len(row) < 4:
            continue
        if str(row[2]).strip() != "논곡중학교":
            continue
        region = str(row[1]).strip()
        if region not in ("인천", "인천광역시"):
            continue
        matches.append(row)

    if not matches:
        raise RuntimeError("학교검색 단계 실패: 인천 논곡중학교 결과 없음")

    raw_school_code = str(matches[0][3]).strip()
    school_digits = "".join(ch for ch in raw_school_code if ch.isdigit())
    if not school_digits:
        raise RuntimeError(f"학교코드 단계 실패: {raw_school_code!r}")
    school_code = school_digits

    # The current student-site flow performs a code check before timetable fetch.
    try:
        _read_url(f"{base}/{endpoint}?17384l{school_code}", timeout=5)
    except Exception as e:
        raise RuntimeError(f"학교확인 단계 실패: {type(e).__name__}: {e}") from e

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    timestamp = f"{target_date.strftime('%Y-%m-%d')} {now_kst.strftime('%H:%M:%S')}"
    payload = f"73629_{school_code}_{timestamp}_1"
    encoded = base64.b64encode(payload.encode("ascii")).decode("ascii")

    try:
        raw = _comcigan_json(_read_url(f"{base}/{endpoint}?{encoded}", timeout=5))
    except Exception as e:
        raise RuntimeError(f"시간표조회 단계 실패: {type(e).__name__}: {e}") from e

    changed_all = raw.get("자료147")
    base_all = raw.get("자료481")
    subjects = raw.get("자료492") or []
    teachers = raw.get("자료446") or []

    if not isinstance(changed_all, list) or not isinstance(base_all, list):
        raise RuntimeError("컴시간 응답에 시간표 배열이 없습니다.")

    try:
        changed_class = changed_all[grade][class_num]
        base_class = base_all[grade][class_num]
    except (IndexError, TypeError):
        raise RuntimeError("컴시간에 해당 학년/반 시간표가 없습니다.")

    weekdays = ["월", "화", "수", "목", "금"]
    result = {}

    for day_index, weekday in enumerate(weekdays, start=1):
        changed_day = changed_class[day_index] if day_index < len(changed_class) else []
        base_day = base_class[day_index] if day_index < len(base_class) else []

        # If Comcigan provides a changed-day row, use it exactly as-is.
        # An all-zero row is meaningful: it represents a holiday/no-class day.
        # Fall back to the base timetable only when the changed-day row itself is absent.
        if isinstance(changed_day, list) and len(changed_day) > 1:
            day_data = changed_day
        else:
            day_data = base_day

        lessons = []
        for period in range(1, 9):
            value = day_data[period] if isinstance(day_data, list) and period < len(day_data) else 0
            number, marked_changed = _comcigan_number(value)

            if number:
                subject_index = number // 1000
                teacher_index = number % 1000
                subject = str(subjects[subject_index]).strip() if subject_index < len(subjects) else ""
                teacher_raw = teachers[teacher_index] if teacher_index < len(teachers) else ""
                teacher = _masked_teacher_name(teacher_raw)
            else:
                subject = ""
                teacher = ""

            base_value = base_day[period] if isinstance(base_day, list) and period < len(base_day) else 0
            base_number, _ = _comcigan_number(base_value)

            lessons.append({
                "subject": subject,
                "teacher": teacher,
                "changed": marked_changed or (number != base_number),
            })

        result[weekday] = lessons

    start_text = str(raw.get("시작일") or "").strip()
    try:
        start_date = datetime.strptime(start_text[:10], "%Y-%m-%d").date()
    except ValueError:
        start_date = target_date - timedelta(days=target_date.weekday())

    return {
        "days": result,
        "times": [str(x).strip() for x in (raw.get("일과시간") or [])[:8]],
        "start_date": start_date,
        "update_date": str(raw.get("자료244") or "").strip(),
    }


def get_nongok_timetable(grade, class_num):
    """Fetch directly from Comcigan using its current student timetable request format."""
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    target = today + timedelta(days=1) if today.weekday() == 6 else today
    monday = target - timedelta(days=target.weekday())
    friday = monday + timedelta(days=4)
    week_label = f"{monday.strftime('%m/%d')} ~ {friday.strftime('%m/%d')}"

    key = ("comcigan-current-v8", grade, class_num, monday.isoformat())
    now = time.time()
    cached = TIMETABLE_CACHE.get(key)
    if cached and cached["expires"] > now:
        return cached["days"], cached["error"], cached["week_label"]

    live = None
    last_error = None
    for attempt in range(1, 4):
        try:
            live = _fetch_comcigan_current_protocol(grade, class_num, target)
            break
        except Exception as e:
            last_error = e
            app.logger.warning(
                "Current Comcigan protocol attempt %s/3 failed grade=%s class=%s: %s",
                attempt, grade, class_num, e,
            )
            if attempt < 3:
                time.sleep(0.6 * attempt)

    if live is None:
        error_text = str(last_error or "")
        if "timed out" in error_text.lower() or "timeout" in error_text.lower():
            reason = "컴시간 서버 연결 시간이 초과됐습니다."
        elif "502" in error_text:
            reason = "컴시간 서버 연결 과정에서 502 오류가 발생했습니다."
        elif "403" in error_text:
            reason = "컴시간 서버가 요청을 거부했습니다."
        else:
            reason = "컴시간 서버에서 데이터를 가져오지 못했습니다."

        TIMETABLE_CACHE[key] = {
            "expires": now + 30,
            "days": [],
            "error": "3번 조회 실패: " + reason,
            "week_label": week_label,
            "debug": f"{type(last_error).__name__}: {last_error}"[:900] if last_error else "unknown",
        }
        c = TIMETABLE_CACHE[key]
        return c["days"], c["error"], c["week_label"]

    start_date = live["start_date"]
    week_label = f"{start_date.strftime('%m/%d')} ~ {(start_date + timedelta(days=4)).strftime('%m/%d')}"
    weekdays = ["월", "화", "수", "목", "금"]
    days = []
    for index, weekday in enumerate(weekdays):
        days.append({
            "weekday": weekday,
            "date": start_date + timedelta(days=index),
            "classes": live["days"].get(weekday, [
                {"subject": "", "teacher": "", "changed": False} for _ in range(8)
            ]),
            "times": live["times"],
            "source": "컴시간알리미",
        })

    TIMETABLE_CACHE[key] = {
        "expires": now + 120,
        "days": days,
        "error": None,
        "week_label": week_label,
        "debug": {
            "source": "direct current Comcigan protocol",
            "update_date": live.get("update_date"),
        },
    }
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
        ("도움말", "논곡위키를 처음 이용하는 사용자를 위한 도움말입니다."),
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
        updated = page.get("updated_at")
        if hasattr(updated, "date"):
            lastmod = updated.date().isoformat()
            urls.append(f"<url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>")
        else:
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

@app.route("/api/timetable-debug")
def timetable_debug():
    try:
        grade = min(3, max(1, int(request.args.get("grade", "2"))))
        class_num = min(20, max(1, int(request.args.get("class", "1"))))
    except ValueError:
        grade, class_num = 2, 1

    days, error, week_label = get_nongok_timetable(grade, class_num)
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    week = 1 if today.weekday() == 6 else 0
    target = today + timedelta(days=1) if today.weekday() == 6 else today
    monday = target - timedelta(days=target.weekday())
    cache_item = TIMETABLE_CACHE.get(("comcigan-current-v8", grade, class_num, monday.isoformat()), {})

    return {
        "grade": grade,
        "class": class_num,
        "week": week_label,
        "error": error,
        "debug": cache_item.get("debug"),
        "parsed": [
            {
                "weekday": day["weekday"],
                "date": day["date"].isoformat(),
                "subjects": [x["subject"] for x in day["classes"]],
                "has_teacher": [bool(x["teacher"]) for x in day["classes"]],
            }
            for day in days
        ],
    }

@app.route("/admin/timetable-debug")
def timetable_debug_legacy():
    return redirect(url_for("timetable_debug", **request.args))

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

def _sanitize_gallery_image(data):
    """Validate and re-encode uploads so embedded EXIF/GPS metadata is removed."""
    try:
        Image.MAX_IMAGE_PIXELS = 25_000_000
        with Image.open(BytesIO(data)) as image:
            image.load()
            image = ImageOps.exif_transpose(image)
            if image.width < 1 or image.height < 1 or image.width * image.height > 25_000_000:
                return None, None

            source_format = (image.format or "").upper()
            output = BytesIO()

            if source_format in {"JPEG", "JPG"}:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(output, format="JPEG", quality=88, optimize=True)
                return "image/jpeg", output.getvalue()

            if source_format == "WEBP":
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA")
                image.save(output, format="WEBP", quality=88, method=4)
                return "image/webp", output.getvalue()

            # PNG and GIF are normalized to PNG. This also strips metadata.
            if source_format in {"PNG", "GIF"}:
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA")
                image.save(output, format="PNG", optimize=True)
                return "image/png", output.getvalue()
    except Exception:
        return None, None
    return None, None


def _gallery_time_text(value):
    if not isinstance(value, datetime):
        return str(value or "")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d %H:%M")


@app.route("/polls")
def polls():
    poll_rows = query(
        """SELECT p.id, p.question, p.is_open, p.created_at,
                  u.username AS creator
           FROM polls p
           LEFT JOIN users u ON u.id=p.created_by
           ORDER BY p.is_open DESC, p.created_at DESC, p.id DESC"""
    )
    user = current_user()
    cards = []
    for poll in poll_rows:
        options = query(
            """SELECT o.id, o.option_text, o.sort_order, COUNT(v.id) AS votes
               FROM poll_options o
               LEFT JOIN poll_votes v ON v.option_id=o.id
               WHERE o.poll_id=%s
               GROUP BY o.id, o.option_text, o.sort_order
               ORDER BY o.sort_order ASC, o.id ASC""",
            (poll["id"],),
        )
        total = sum(int(option["votes"] or 0) for option in options)
        voted_option = None
        if user:
            voted = query(
                "SELECT option_id FROM poll_votes WHERE poll_id=%s AND user_id=%s",
                (poll["id"], user["id"]),
            )
            if voted:
                voted_option = voted[0]["option_id"]
        for option in options:
            count = int(option["votes"] or 0)
            option["percent"] = round((count * 100 / total), 1) if total else 0
        poll["options"] = options
        poll["total_votes"] = total
        poll["voted_option"] = voted_option
        cards.append(poll)
    return render_template("polls.html", polls=cards)

@app.route("/polls/create", methods=["POST"])
@require_admin
def poll_create():
    check_csrf()
    question = request.form.get("question", "").strip()
    raw_options = request.form.get("options", "")
    options = [line.strip() for line in raw_options.splitlines() if line.strip()]
    options = list(dict.fromkeys(options))

    if not question or len(question) > 200:
        flash("투표 질문은 1~200자로 입력해 주세요.", "warning")
        return redirect(url_for("polls"))
    if len(options) < 2 or len(options) > 10:
        flash("선택지는 2개 이상 10개 이하로 입력해 주세요.", "warning")
        return redirect(url_for("polls"))
    if any(len(option) > 120 for option in options):
        flash("각 선택지는 120자 이하로 입력해 주세요.", "warning")
        return redirect(url_for("polls"))

    user = current_user()
    execute(
        "INSERT INTO polls(question, created_by, is_open, created_at) VALUES (%s,%s,TRUE,CURRENT_TIMESTAMP)",
        (question, user["id"]),
    )
    poll_id = query(
        "SELECT id FROM polls WHERE created_by=%s AND question=%s ORDER BY id DESC LIMIT 1",
        (user["id"], question),
    )[0]["id"]
    for index, option in enumerate(options):
        execute(
            "INSERT INTO poll_options(poll_id, option_text, sort_order) VALUES (%s,%s,%s)",
            (poll_id, option, index),
        )
    flash("투표를 만들었습니다.", "success")
    return redirect(url_for("polls"))

@app.route("/polls/<int:poll_id>/vote", methods=["POST"])
@require_login
def poll_vote(poll_id):
    check_csrf()
    poll_rows = query("SELECT id, is_open FROM polls WHERE id=%s", (poll_id,))
    if not poll_rows:
        abort(404)
    if not poll_rows[0]["is_open"]:
        flash("종료된 투표입니다.", "warning")
        return redirect(url_for("polls"))

    user = current_user()
    if query("SELECT id FROM poll_votes WHERE poll_id=%s AND user_id=%s", (poll_id, user["id"])):
        flash("이 투표에는 이미 참여했습니다.", "warning")
        return redirect(url_for("polls"))

    try:
        option_id = int(request.form.get("option_id", "0"))
    except ValueError:
        option_id = 0
    valid = query("SELECT id FROM poll_options WHERE id=%s AND poll_id=%s", (option_id, poll_id))
    if not valid:
        flash("선택지를 골라 주세요.", "warning")
        return redirect(url_for("polls"))

    execute(
        "INSERT INTO poll_votes(poll_id, option_id, user_id, created_at) VALUES (%s,%s,%s,CURRENT_TIMESTAMP)",
        (poll_id, option_id, user["id"]),
    )
    flash("투표가 반영되었습니다.", "success")
    return redirect(url_for("polls"))

@app.route("/polls/<int:poll_id>/toggle", methods=["POST"])
@require_admin
def poll_toggle(poll_id):
    check_csrf()
    rows = query("SELECT is_open FROM polls WHERE id=%s", (poll_id,))
    if not rows:
        abort(404)
    execute("UPDATE polls SET is_open=%s WHERE id=%s", (not bool(rows[0]["is_open"]), poll_id))
    return redirect(url_for("polls"))

@app.route("/gallery")
def gallery():
    posts = query(
        """SELECT p.id, p.title, p.body, p.created_at, u.username,
                  (SELECT COUNT(*) FROM gallery_images gi WHERE gi.post_id=p.id) AS image_count,
                  (SELECT COUNT(*) FROM gallery_comments gc WHERE gc.post_id=p.id AND gc.deleted=FALSE) AS comment_count,
                  (SELECT MIN(gi.id) FROM gallery_images gi WHERE gi.post_id=p.id) AS cover_image_id
           FROM gallery_posts p
           JOIN users u ON u.id=p.user_id
           WHERE p.deleted=FALSE
           ORDER BY p.created_at DESC, p.id DESC
           LIMIT 50"""
    )
    for post in posts:
        post["created_text"] = _gallery_time_text(post.get("created_at"))
    return render_template("gallery.html", posts=posts)


@app.route("/gallery/new", methods=["POST"])
@require_login
def gallery_new():
    check_csrf()
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    files = [f for f in request.files.getlist("images") if f and f.filename]

    if not title or len(title) > 100:
        flash("제목은 1~100자로 입력해 주세요.", "warning")
        return redirect(url_for("gallery"))
    if not body or len(body) > 5000:
        flash("본문은 1~5000자로 입력해 주세요.", "warning")
        return redirect(url_for("gallery"))
    if len(files) > 4:
        flash("사진은 게시물 하나에 최대 4장까지 올릴 수 있습니다.", "warning")
        return redirect(url_for("gallery"))

    images = []
    for file in files:
        data = file.read(3 * 1024 * 1024 + 1)
        if len(data) > 3 * 1024 * 1024:
            flash("사진 한 장의 최대 크기는 3MB입니다.", "warning")
            return redirect(url_for("gallery"))
        mime, clean_data = _sanitize_gallery_image(data)
        if not mime or clean_data is None:
            flash("사진은 정상적인 JPG, PNG, GIF, WebP 파일만 올릴 수 있습니다.", "warning")
            return redirect(url_for("gallery"))
        if len(clean_data) > 3 * 1024 * 1024:
            flash("사진을 안전하게 처리한 뒤에도 3MB를 넘습니다. 더 작은 사진을 올려 주세요.", "warning")
            return redirect(url_for("gallery"))
        images.append((mime, clean_data))

    user = current_user()
    execute(
        "INSERT INTO gallery_posts(user_id, title, body, deleted, created_at) VALUES (%s,%s,%s,FALSE,CURRENT_TIMESTAMP)",
        (user["id"], title, body),
    )
    post_rows = query(
        """SELECT id FROM gallery_posts
           WHERE user_id=%s AND title=%s AND body=%s AND deleted=FALSE
           ORDER BY id DESC LIMIT 1""",
        (user["id"], title, body),
    )
    if not post_rows:
        abort(500)
    post_id = post_rows[0]["id"]

    for index, (mime, data) in enumerate(images):
        execute(
            "INSERT INTO gallery_images(post_id, mime_type, image_data, sort_order, created_at) VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP)",
            (post_id, mime, data, index),
        )

    flash("논곡갤러리에 게시물을 올렸습니다.", "success")
    return redirect(url_for("gallery_post", post_id=post_id))


@app.route("/gallery/<int:post_id>")
def gallery_post(post_id):
    rows = query(
        """SELECT p.id, p.user_id, p.title, p.body, p.created_at, u.username
           FROM gallery_posts p JOIN users u ON u.id=p.user_id
           WHERE p.id=%s AND p.deleted=FALSE""",
        (post_id,),
    )
    if not rows:
        abort(404)
    post = rows[0]
    post["created_text"] = _gallery_time_text(post.get("created_at"))
    images = query(
        "SELECT id FROM gallery_images WHERE post_id=%s ORDER BY sort_order ASC, id ASC",
        (post_id,),
    )
    comments = query(
        """SELECT c.id, c.user_id, c.body, c.created_at, u.username
           FROM gallery_comments c JOIN users u ON u.id=c.user_id
           WHERE c.post_id=%s AND c.deleted=FALSE
           ORDER BY c.created_at ASC, c.id ASC""",
        (post_id,),
    )
    for comment in comments:
        comment["created_text"] = _gallery_time_text(comment.get("created_at"))
    return render_template("gallery_post.html", post=post, images=images, comments=comments)


@app.route("/gallery/image/<int:image_id>")
def gallery_image(image_id):
    rows = query(
        """SELECT gi.image_data, gi.mime_type
           FROM gallery_images gi
           JOIN gallery_posts p ON p.id=gi.post_id
           WHERE gi.id=%s AND p.deleted=FALSE""",
        (image_id,),
    )
    if not rows:
        abort(404)
    raw = rows[0]["image_data"]
    data = bytes(raw) if not isinstance(raw, bytes) else raw
    response = app.response_class(data, mimetype=rows[0]["mime_type"])
    response.headers["Cache-Control"] = "public, max-age=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/gallery/<int:post_id>/comment", methods=["POST"])
@require_login
def gallery_comment(post_id):
    check_csrf()
    if not query("SELECT id FROM gallery_posts WHERE id=%s AND deleted=FALSE", (post_id,)):
        abort(404)
    body = request.form.get("body", "").strip()
    if not body or len(body) > 1000:
        flash("댓글은 1~1000자로 입력해 주세요.", "warning")
        return redirect(url_for("gallery_post", post_id=post_id))
    execute(
        "INSERT INTO gallery_comments(post_id, user_id, body, deleted, created_at) VALUES (%s,%s,%s,FALSE,CURRENT_TIMESTAMP)",
        (post_id, current_user()["id"], body),
    )
    return redirect(url_for("gallery_post", post_id=post_id) + "#comments")


@app.route("/gallery/<int:post_id>/delete", methods=["POST"])
@require_login
def gallery_delete_post(post_id):
    check_csrf()
    rows = query("SELECT user_id FROM gallery_posts WHERE id=%s AND deleted=FALSE", (post_id,))
    if not rows:
        abort(404)
    user = current_user()
    if rows[0]["user_id"] != user["id"] and user["role"] != "admin":
        abort(403)
    execute("UPDATE gallery_posts SET deleted=TRUE WHERE id=%s", (post_id,))
    flash("게시물을 삭제했습니다.", "success")
    return redirect(url_for("gallery"))


@app.route("/gallery/comment/<int:comment_id>/delete", methods=["POST"])
@require_login
def gallery_delete_comment(comment_id):
    check_csrf()
    rows = query(
        "SELECT post_id, user_id FROM gallery_comments WHERE id=%s AND deleted=FALSE",
        (comment_id,),
    )
    if not rows:
        abort(404)
    user = current_user()
    if rows[0]["user_id"] != user["id"] and user["role"] != "admin":
        abort(403)
    execute("UPDATE gallery_comments SET deleted=TRUE WHERE id=%s", (comment_id,))
    return redirect(url_for("gallery_post", post_id=rows[0]["post_id"]) + "#comments")


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
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            # DB timestamps are stored as UTC without a timezone. Attach UTC,
            # then return an explicit Korea-time ISO timestamp.
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            row["created_at"] = created_at.astimezone(
                ZoneInfo("Asia/Seoul")
            ).isoformat()
    return {"messages": rows}

@app.route("/api/chat/send", methods=["POST"])
@require_login
def chat_send():
    check_csrf()
    body = request.form.get("body", "").strip()
    if not body or len(body) > 500:
        return {"ok": False, "error": "메시지는 1~500자로 작성해 주세요."}, 400
    user = current_user()
    # Store chat timestamps consistently as naive UTC in both PostgreSQL and SQLite.
    created_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    execute(
        "INSERT INTO chat_messages(user_id, body, created_at) VALUES (%s,%s,%s)",
        (user["id"], body, created_utc),
    )
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
