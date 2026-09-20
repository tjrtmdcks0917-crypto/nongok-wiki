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

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
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

# Short-lived in-process caches: avoid repeating the same database-heavy sidebar,
# visitor-stat and person-name queries on every page refresh.
PUBLIC_CONTEXT_CACHE = {"expires": 0.0, "data": None}
PERSON_NAMES_CACHE = {"expires": 0.0, "names": None}
REPEATED_TERMS_CACHE = {"expires": 0.0, "terms": None}
PUBLIC_CONTEXT_TTL = 12
PERSON_NAMES_TTL = 30
REPEATED_TERMS_TTL = 45

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

    # Count one browser once per Korea-calendar day. A session marker avoids
    # doing an INSERT ... ON CONFLICT round-trip on every page refresh.
    if (
        request.method == "GET"
        and request.endpoint not in {"static", "gallery_image"}
        and not request.path.startswith("/api/")
    ):
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        visit_date = now_kst.strftime("%Y-%m-%d")
        if session.get("daily_visit_recorded") != visit_date:
            visitor_key = session.get("daily_visitor_key")
            if not visitor_key:
                visitor_key = secrets.token_hex(16)
                session["daily_visitor_key"] = visitor_key
            execute(
                """INSERT INTO site_visits(visit_date, visitor_key, first_seen_at)
                   VALUES (%s,%s,%s)
                   ON CONFLICT(visit_date, visitor_key) DO NOTHING""",
                (visit_date, visitor_key, datetime.now(timezone.utc).replace(tzinfo=None)),
            )
            session["daily_visit_recorded"] = visit_date


@app.after_request
def cache_versioned_static_files(response):
    # CSS/logo URLs already use version query strings, so they can be cached
    # aggressively by the browser without serving stale assets after a deploy.
    if request.endpoint == "static" and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

def _as_utc_datetime(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _public_context_data():
    now = time.time()
    cached = PUBLIC_CONTEXT_CACHE.get("data")
    if cached is not None and now < PUBLIC_CONTEXT_CACHE.get("expires", 0):
        return cached

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
    gallery_popular = query("""
        SELECT id, title, views
        FROM gallery_posts
        WHERE deleted=FALSE
        ORDER BY views DESC, created_at DESC, id DESC
        LIMIT 10
    """)
    latest_gallery = int(query(
        "SELECT COALESCE(MAX(id),0) AS max_id FROM gallery_posts WHERE deleted=FALSE"
    )[0]["max_id"] or 0)

    notice_rows = query(
        "SELECT content, updated_at FROM homepage_sections WHERE section_key=%s",
        ("news",),
    )
    notice_content = notice_rows[0]["content"].strip() if notice_rows else ""
    notice_updated_at = notice_rows[0]["updated_at"] if notice_rows else None
    notice_dt = _as_utc_datetime(notice_updated_at)
    site_notice_active = bool(
        notice_content
        and notice_dt
        and datetime.now(timezone.utc) - notice_dt <= timedelta(hours=48)
    )
    site_notice = notice_content if site_notice_active else ""
    site_notice_html = _link_document_mentions(str(escape(site_notice))).replace("\n", "<br>") if site_notice else ""

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_key = now_kst.strftime("%Y-%m-%d")
    visit_rows = query(
        "SELECT first_seen_at FROM site_visits WHERE visit_date=%s ORDER BY first_seen_at ASC",
        (today_key,),
    )
    first_seen_hours = []
    for visit in visit_rows:
        visit_dt = _as_utc_datetime(visit.get("first_seen_at"))
        if visit_dt:
            first_seen_hours.append(visit_dt.astimezone(ZoneInfo("Asia/Seoul")).hour)

    total_visitors = len(first_seen_hours)
    daily_visit_stats = []
    for hour in range(25):
        count = total_visitors if hour == 24 else sum(1 for seen_hour in first_seen_hours if seen_hour <= hour)
        daily_visit_stats.append({
            "hour": hour,
            "count": count,
            "future": hour > now_kst.hour and hour < 24,
            "height": 8 if total_visitors == 0 else max(8, round((count / total_visitors) * 100)),
        })

    data = {
        "global_recent": recent,
        "global_popular": popular,
        "global_daily": daily,
        "global_gallery_popular": gallery_popular,
        "latest_gallery": latest_gallery,
        "site_notice": site_notice,
        "site_notice_html": site_notice_html,
        "site_notice_active": site_notice_active,
        "site_notice_new": site_notice_active,
        "site_notice_updated_at": notice_updated_at,
        "daily_visit_stats": daily_visit_stats,
        "daily_visit_total": total_visitors,
        "daily_visit_date": today_key,
    }
    PUBLIC_CONTEXT_CACHE["data"] = data
    PUBLIC_CONTEXT_CACHE["expires"] = now + PUBLIC_CONTEXT_TTL
    return data


@app.context_processor
def inject():
    public = _public_context_data()
    user = current_user()
    latest_gallery = public["latest_gallery"]

    if user:
        seen_rows = query(
            "SELECT last_seen_post_id FROM gallery_reads WHERE user_id=%s",
            (user["id"],),
        )
        seen_gallery = int(seen_rows[0]["last_seen_post_id"] or 0) if seen_rows else 0
    else:
        seen_gallery = int(session.get("gallery_seen_post_id", 0) or 0)

    if latest_gallery > seen_gallery:
        gallery_unread = int(query(
            "SELECT COUNT(*) AS c FROM gallery_posts WHERE deleted=FALSE AND id>%s",
            (seen_gallery,),
        )[0]["c"] or 0)
    else:
        gallery_unread = 0

    return {
        "current_user": user,
        "csrf": session.get("csrf"),
        "can_create_school_posts": bool(user and user.get("school_name") == "논곡중학교"),
        "can_open_admin": role_at_least(user, "moderator"),
        "can_manage_documents": role_at_least(user, "teacher"),
        "can_edit_notice": role_at_least(user, "teacher"),
        "can_moderate_gallery": role_at_least(user, "moderator"),
        "global_recent": public["global_recent"],
        "global_popular": public["global_popular"],
        "global_daily": public["global_daily"],
        "global_gallery_popular": public["global_gallery_popular"],
        "gallery_unread": gallery_unread,
        "site_notice": public["site_notice"],
        "site_notice_html": public["site_notice_html"],
        "site_notice_active": public["site_notice_active"],
        "site_notice_new": public["site_notice_new"],
        "site_notice_updated_at": public["site_notice_updated_at"],
        "daily_visit_stats": public["daily_visit_stats"],
        "daily_visit_total": public["daily_visit_total"],
        "daily_visit_date": public["daily_visit_date"],
    }


def current_user():
    if hasattr(g, "current_user_value"):
        return g.current_user_value
    uid = session.get("user_id")
    if not uid:
        g.current_user_value = None
        return None
    rows = query(
        "SELECT id, username, real_name, student_no, school_name, profile_name, profile_bio, "
        "profile_status, profile_color, profile_emoji, role, account_status FROM users WHERE id = %s",
        (uid,),
    )
    g.current_user_value = rows[0] if rows else None
    return g.current_user_value

def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("로그인이 필요합니다.", "warning")
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

ROLE_LEVELS = {"user": 0, "moderator": 1, "teacher": 2, "admin": 3}
ROLE_LABELS = {
    "user": "일반학생",
    "moderator": "학생관리자",
    "teacher": "교사",
    "admin": "최고관리자",
}


def role_at_least(user, minimum_role):
    if not user:
        return False
    return ROLE_LEVELS.get(user.get("role", "user"), 0) >= ROLE_LEVELS.get(minimum_role, 99)


def require_staff(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not role_at_least(current_user(), "moderator"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def require_teacher(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not role_at_least(current_user(), "teacher"):
            abort(403)
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


def can_manage_member(actor, target):
    if not actor or not target:
        return False
    if actor.get("role") == "admin":
        return target.get("role") != "admin"
    if actor.get("role") == "teacher":
        return target.get("role") in {"user", "moderator"}
    return False


def log_admin_action(action, target_type="", target_id=None, detail=""):
    actor = current_user()
    if not actor or not role_at_least(actor, "moderator"):
        return
    execute(
        """INSERT INTO admin_activity_logs(actor_id, action, target_type, target_id, detail, created_at)
           VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)""",
        (
            actor["id"],
            str(action or "")[:80],
            str(target_type or "")[:40],
            str(target_id)[:64] if target_id is not None else None,
            str(detail or "")[:500],
        ),
    )


def _admin_activity_logs():
    return query(
        """SELECT l.id, l.action, l.target_type, l.target_id, l.detail, l.created_at,
                  u.username AS actor_username, u.real_name AS actor_real_name
           FROM admin_activity_logs l
           LEFT JOIN users u ON u.id=l.actor_id
           ORDER BY l.created_at DESC, l.id DESC
           LIMIT 100"""
    )


def _pending_document_edits():
    return query(
        """SELECT p.id, p.page_id, p.proposed_content, p.status, p.created_at,
                  w.title,
                  u.username AS submitter_username,
                  u.real_name AS submitter_real_name
           FROM pending_document_edits p
           JOIN wiki_pages w ON w.id=p.page_id
           LEFT JOIN users u ON u.id=p.submitter_id
           WHERE p.status='pending' AND w.deleted=FALSE
           ORDER BY p.created_at ASC, p.id ASC
           LIMIT 100"""
    )

def check_csrf():
    token = request.form.get("csrf")
    if not token or token != session.get("csrf"):
        abort(400, "CSRF 토큰이 올바르지 않습니다.")

def slugify(title):
    title = re.sub(r"\s+", " ", title.strip())
    return title[:120]

PERSON_ROLE_WORDS = (
    "학생", "선생님", "선생", "교사", "교장", "교감",
    "회장", "부회장", "운영자", "개발자", "디자이너",
)
PERSON_NAME_STOPWORDS = {
    "인물", "학생", "학교", "중학교", "논곡", "교장", "교감", "교사", "선생님",
    "회장", "부회장", "운영자", "개발자", "디자이너", "도움말", "연습장", "시간표",
    "동아리", "학교생활", "공지사항", "편집지침", "운영방침", "개인정보",
}
HOME_OPERATOR_NAMES = {"석승찬", "김현우"}

# Words that would create noisy links rather than useful topic groupings.
REPEATED_TERM_STOPWORDS = {
    "논곡", "논곡중학교", "학교", "중학교", "문서", "문서입니다", "내용", "관련", "정보",
    "학생", "교직원", "개인", "개인정보", "작성", "작성하지", "주세요", "있습니다", "있어",
    "있고", "있는", "대한", "관한", "위한", "통해", "함께", "공개", "경우", "수업",
    "학교생활", "공간", "장소", "기억", "확인", "자유롭게", "정리", "기록", "시설",
    "사용", "이용", "해당", "현재", "등은", "등을", "등의", "에서", "으로", "에게",
    "그리고", "하지만", "또한", "때문", "대한", "이름", "사람", "페이지", "위키",
    "nongok", "wiki", "https", "http",
}


def _known_person_names():
    """Find names explicitly used as people in wiki documents or on the homepage."""
    now = time.time()
    cached = PERSON_NAMES_CACHE.get("names")
    if cached is not None and now < PERSON_NAMES_CACHE.get("expires", 0):
        return cached

    rows = query("SELECT title, content FROM wiki_pages WHERE deleted=FALSE")
    homepage_rows = query("SELECT content FROM homepage_sections")
    names = set(HOME_OPERATOR_NAMES)
    role_pattern = "|".join(map(re.escape, PERSON_ROLE_WORDS))

    for row in rows:
        title = str(row.get("title") or "")
        content = str(row.get("content") or "")

        # Person-profile titles such as '인물/석승찬', '김현우 선생님', '교장 김철수'.
        if any(key in title for key in ("인물", "교장", "교감", "선생님")):
            for candidate in re.findall(r"(?<![가-힣])[가-힣]{2,4}(?![가-힣])", title):
                if candidate not in PERSON_NAME_STOPWORDS:
                    names.add(candidate)

        # Also learn names that are directly paired with a person-role in document text.
        patterns = (
            rf"(?<![가-힣])([가-힣]{{2,4}})(?![가-힣])\s*(?:{role_pattern})",
            rf"(?:{role_pattern})\s*(?<![가-힣])([가-힣]{{2,4}})(?![가-힣])",
        )
        for pattern in patterns:
            for candidate in re.findall(pattern, content):
                if candidate not in PERSON_NAME_STOPWORDS:
                    names.add(candidate)

    # Editable homepage sections can also contain person mentions.
    for row in homepage_rows:
        content = str(row.get("content") or "")
        patterns = (
            rf"(?<![가-힣])([가-힣]{{2,4}})(?![가-힣])\s*(?:{role_pattern})",
            rf"(?:{role_pattern})\s*(?<![가-힣])([가-힣]{{2,4}})(?![가-힣])",
        )
        for pattern in patterns:
            for candidate in re.findall(pattern, content):
                if candidate not in PERSON_NAME_STOPWORDS:
                    names.add(candidate)

    result = sorted(names, key=lambda value: (-len(value), value))
    PERSON_NAMES_CACHE["names"] = result
    PERSON_NAMES_CACHE["expires"] = now + PERSON_NAMES_TTL
    return result


def _link_person_names(safe_text):
    names = _known_person_names()
    if not names:
        return safe_text

    # Protect links first so names inside an existing link do not become nested links.
    placeholders = []

    def protect(html):
        token = f"@@NONGOK_LINK_{len(placeholders)}@@"
        placeholders.append(html)
        return token

    safe_text = re.sub(
        r"\[\[([^\[\]]{1,120})\]\]",
        lambda m: protect(
            f'<a href="{url_for("wiki", title=m.group(1).strip())}">{m.group(1).strip()}</a>'
        ),
        safe_text,
    )
    safe_text = re.sub(
        r"\[([^\[\]\n]{1,200})\]\((https?://[^\s<>]+)\)",
        lambda m: protect(
            f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>'
        ),
        safe_text,
    )

    pattern = re.compile(
        r"(?<![가-힣A-Za-z0-9])(" + "|".join(map(re.escape, names)) + r")(?![가-힣A-Za-z0-9])"
    )
    safe_text = pattern.sub(
        lambda m: f'<a class="person-mention" href="{url_for("person_mentions", name=m.group(1))}">{m.group(1)}</a>',
        safe_text,
    )

    for index, html in enumerate(placeholders):
        safe_text = safe_text.replace(f"@@NONGOK_LINK_{index}@@", html)
    return safe_text


def _known_repeated_terms():
    """Return useful words that appear in at least two different public pages."""
    now = time.time()
    cached = REPEATED_TERMS_CACHE.get("terms")
    if cached is not None and now < REPEATED_TERMS_CACHE.get("expires", 0):
        return cached

    rows = query("SELECT title, content FROM wiki_pages WHERE deleted=FALSE")
    homepage_rows = query("SELECT content FROM homepage_sections")
    doc_counts = {}
    person_names = set(_known_person_names())

    sources = [
        f"{row.get('title') or ''}\n{row.get('content') or ''}"
        for row in rows
    ]
    # Treat the editable homepage text as one additional source.
    home_text = "\n".join(str(row.get("content") or "") for row in homepage_rows)
    if home_text.strip():
        sources.append(home_text)

    token_pattern = re.compile(r"[가-힣]{2,12}|[A-Za-z][A-Za-z0-9_-]{2,23}")
    for source in sources:
        source_terms = set()
        for raw in token_pattern.findall(source):
            term = raw.lower() if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", raw) else raw
            if term in REPEATED_TERM_STOPWORDS or term in person_names:
                continue
            if term.isdigit():
                continue
            # Filter common Korean endings that otherwise dominate auto-linking.
            if len(term) <= 2 and term.endswith(("은", "는", "이", "가", "을", "를", "에", "의")):
                continue
            source_terms.add(term)
        for term in source_terms:
            doc_counts[term] = doc_counts.get(term, 0) + 1

    # Cap the linker set so a large wiki does not create an enormous regex.
    terms = [
        term for term, count in sorted(
            doc_counts.items(),
            key=lambda item: (-item[1], -len(item[0]), item[0]),
        )
        if count >= 2
    ][:180]

    REPEATED_TERMS_CACHE["terms"] = terms
    REPEATED_TERMS_CACHE["expires"] = now + REPEATED_TERMS_TTL
    return terms


def _link_repeated_terms(html_text):
    terms = _known_repeated_terms()
    if not terms:
        return html_text

    placeholders = []

    # Protect every already-generated link, including person links and wiki links.
    def protect_link(match):
        token = f"@@NONGOK_EXISTING_LINK_{len(placeholders)}@@"
        placeholders.append(match.group(0))
        return token

    protected = re.sub(
        r"<a\b[^>]*>.*?</a>",
        protect_link,
        html_text,
        flags=re.IGNORECASE,
    )

    # Longest terms first prevents a short term from splitting a more useful phrase.
    ordered = sorted(terms, key=lambda value: (-len(value), value))
    pattern = re.compile(
        r"(?<![가-힣A-Za-z0-9])(" + "|".join(map(re.escape, ordered)) + r")(?![가-힣A-Za-z0-9])",
        re.IGNORECASE,
    )
    protected = pattern.sub(
        lambda m: f'<a class="term-mention" href="{url_for("term_mentions", term=m.group(1))}">{m.group(1)}</a>',
        protected,
    )

    for index, html in enumerate(placeholders):
        protected = protected.replace(f"@@NONGOK_EXISTING_LINK_{index}@@", html)
    return protected


def _link_document_mentions(text):
    linked = _link_person_names(text)
    return _link_repeated_terms(linked)


def render_wiki(text):
    safe = str(escape(text or ""))
    safe = _link_document_mentions(safe)

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
        ("학교 시설", """== 학교 시설 ==
논곡중학교의 주요 시설을 정리한 문서입니다.

논곡중학교 홈페이지의 학교현황에 따르면 교사 부지 7,140.5㎡, 운동장 4,385.7㎡로 대지면적은 총 11,526.2㎡이며, 건물면적은 8,514.2㎡입니다.

=== 시설 문서 ===
 * [[학교 시설/교실 및 교과교실]]
 * [[학교 시설/과학·창의활동시설]]
 * [[학교 시설/예체능·체육시설]]
 * [[학교 시설/학생지원시설]]
 * [[학교 시설/운영·행정시설]]
 * [[학교 시설/야외시설]]

=== 출처 ===
[논곡중학교 학교현황](https://nongok.icems.kr/sub/info.do?m=0104&s=nongok)
"""),
        ("학교 시설/교실 및 교과교실", """== 교실 및 교과교실 ==
논곡중학교의 일반교실과 교과 수업 공간을 정리한 문서입니다.

=== 일반교실 ===
 * 일반교실: 13실

=== 교과교실 ===
 * 수학교과교실: 2실
 * 영어교과교실: 2실
 * 국어교과교실: 1실
 * 사회교과교실: 1실
 * 한국어교실: 2실
 * 교과준비실: 4실

=== 관련 활동 공간 ===
 * 동아리활동실: 2실
 * 진로활동실: 1실

=== 출처 ===
[논곡중학교 학교현황](https://nongok.icems.kr/sub/info.do?m=0104&s=nongok)
"""),
        ("학교 시설/과학·창의활동시설", """== 과학·창의활동시설 ==
과학 실험과 창의·기술 활동에 활용되는 시설을 정리한 문서입니다.

 * 과학실험실: 2실
 * 창의공작실: 1실
 * 기술실: 1실
 * 가사실: 1실
 * 미래교실: 1실

과학실험실과 창의공작실 등은 실험·제작·창의활동에 활용되는 공간입니다.

=== 출처 ===
[논곡중학교 학교현황](https://nongok.icems.kr/sub/info.do?m=0104&s=nongok)
"""),
        ("학교 시설/예체능·체육시설", """== 예체능·체육시설 ==
체육 및 예술 활동과 관련된 시설을 정리한 문서입니다.

=== 체육 시설 ===
 * 강당: 1실
 * 건강체력교실: 1실
 * 당구교실: 1실
 * 다목적실: 2실

=== 예술 시설 ===
 * 음악실: 1실
 * 미술실: 1실

학교알리미에도 논곡중학교의 체육집회공간이 1실로 공시되어 있습니다.

=== 출처 ===
[논곡중학교 학교현황](https://nongok.icems.kr/sub/info.do?m=0104&s=nongok)
[학교알리미 논곡중학교](https://www.schoolinfo.go.kr)
"""),
        ("학교 시설/학생지원시설", """== 학생지원시설 ==
학생들의 학습·상담·건강·자치 활동을 지원하는 시설을 정리한 문서입니다.

 * 도서관: 1실
 * 식당(급식실): 1실
 * 통합교육지원실: 2실
 * 보건실: 1실
 * Wee Class: 1실
 * 선도 교육실: 1실
 * 방송실: 1실
 * 학습자료실: 2실
 * 자치활동실: 1실
 * 탈의실: 6실(남 3실, 여 3실)

=== 출처 ===
[논곡중학교 학교현황](https://nongok.icems.kr/sub/info.do?m=0104&s=nongok)
"""),
        ("학교 시설/운영·행정시설", """== 운영·행정시설 ==
학교 운영과 교직원 업무를 위한 시설을 정리한 문서입니다.

 * 교장실: 1실
 * 소회의실: 1실
 * 교원연구실: 7실
 * 행정실: 1실
 * 교사 커뮤니티실: 1실
 * 학부모 커뮤니티실: 1실
 * 시설관리실: 1실
 * 교직원 휴게실: 2실(남·여)
 * 당직실: 1실

=== 출처 ===
[논곡중학교 학교현황](https://nongok.icems.kr/sub/info.do?m=0104&s=nongok)
"""),
        ("학교 시설/야외시설", """== 야외시설 ==
논곡중학교의 야외 공간을 정리한 문서입니다.

=== 운동장 ===
논곡중학교 홈페이지 학교현황에 따르면 운동장 면적은 4,385.7㎡입니다.

=== 교사 부지 ===
교사 부지 면적은 7,140.5㎡이며, 운동장을 포함한 전체 대지면적은 11,526.2㎡입니다.

학교 개인정보처리방침의 CCTV 설치 안내에는 정문, 교사동 출입문, 주차장, 학교 외곽 등이 학교 시설로 안내되어 있습니다.

=== 출처 ===
[논곡중학교 학교현황](https://nongok.icems.kr/sub/info.do?m=0104&s=nongok)
[논곡중학교 개인정보처리방침](https://nongok.icems.kr/priPrivacy.do?s=nongok)
"""),
        ("학교 시설/4층/1학년 교실", """== 1학년 교실 ==
논곡중학교 본관 4층의 1학년 교실을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
이 장소에서 있었던 학교생활의 추억이나 기억을 자유롭게 적어 주세요.

다만 특정 학생이나 교직원의 실명·학번·연락처 등 개인정보, 누군가를 비방하거나 곤란하게 할 내용은 적지 말아 주세요.
"""),
        ("학교 시설/4층/1학년 교무실", """== 1학년 교무실 ==
논곡중학교 본관 4층의 1학년 교무실을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
이 장소와 관련된 학교생활의 기억을 자유롭게 적어 주세요.

개인의 사생활이나 개인정보, 확인되지 않은 소문은 작성하지 말아 주세요.
"""),
        ("학교 시설/4층/민주 관련 부서", """== 민주 관련 부서 ==
논곡중학교 본관 4층의 관련 부서 공간을 기록하는 문서입니다.

정확한 부서 명칭은 확인 후 문서 제목과 내용을 수정해 주세요.

=== 이곳에서의 기억 ===
이 장소와 함께한 학교생활의 기억을 자유롭게 적어 주세요.

개인의 사생활이나 개인정보, 확인되지 않은 소문은 작성하지 말아 주세요.
"""),
        ("학교 시설/4층/음악실", """== 음악실 ==
논곡중학교 본관 4층 음악실에 관한 문서입니다.

=== 이곳에서의 기억 ===
음악 수업, 연습, 행사 등 이 장소와 함께한 기억을 자유롭게 적어 주세요.

다른 사람의 개인정보나 당사자가 원하지 않을 이야기는 적지 말아 주세요.
"""),
        ("학교 시설/3층/2학년 교무실", """== 2학년 교무실 ==
논곡중학교 본관 3층의 2학년 교무실을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
이 장소와 관련된 학교생활의 기억을 자유롭게 적어 주세요.

교직원 개인의 사생활이나 연락처 등 개인정보는 작성하지 말아 주세요.
"""),
        ("학교 시설/3층/2학년 교실", """== 2학년 교실 ==
논곡중학교 본관 3층의 2학년 교실을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
수업, 반 활동, 학교생활 등 이 장소에서의 추억을 자유롭게 적어 주세요.

특정 학생을 놀리거나 개인정보가 드러나는 내용은 작성하지 말아 주세요.
"""),
        ("학교 시설/3층/학생자치실", """== 학생자치실 ==
논곡중학교 본관 3층 학생자치실에 관한 문서입니다.

=== 이곳에서의 기억 ===
학생자치 활동과 이 공간에서의 기억을 자유롭게 적어 주세요.

공개되지 않은 회의 내용이나 개인을 특정하는 민감한 내용은 올리지 말아 주세요.
"""),
        ("학교 시설/2층/교장실", """== 교장실 ==
논곡중학교 본관 2층 교장실에 관한 문서입니다.

=== 이곳에서의 기억 ===
이 장소와 관련해 공개적으로 공유할 수 있는 학교생활의 기억을 적어 주세요.

개인의 사생활이나 확인되지 않은 이야기는 작성하지 말아 주세요.
"""),
        ("학교 시설/2층/본교무실", """== 본교무실 ==
논곡중학교 본관 2층 본교무실에 관한 문서입니다.

=== 이곳에서의 기억 ===
학교생활 중 이 장소와 함께한 기억을 자유롭게 적어 주세요.

교직원 개인의 연락처나 사생활 등 개인정보는 작성하지 말아 주세요.
"""),
        ("학교 시설/2층/3학년 교실", """== 3학년 교실 ==
논곡중학교 본관 2층의 3학년 교실을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
수업, 반 활동, 학교생활 등 이 장소에서의 추억을 자유롭게 적어 주세요.

특정 학생을 놀리거나 개인정보가 드러나는 내용은 작성하지 말아 주세요.
"""),
        ("학교 시설/2층/체육 교무실", """== 체육 교무실 ==
논곡중학교 본관 2층의 체육 관련 교무실을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
체육 활동이나 학교생활 중 이 장소와 관련된 기억을 자유롭게 적어 주세요.

교직원 개인의 사생활이나 개인정보는 작성하지 말아 주세요.
"""),
        ("학교 시설/2층/정보실", """== 정보실 ==
논곡중학교 본관 2층 정보실에 관한 문서입니다.

=== 이곳에서의 기억 ===
정보 수업이나 이 장소에서 있었던 학교생활의 기억을 자유롭게 적어 주세요.

계정·비밀번호 등 보안정보나 다른 사람의 개인정보는 절대 작성하지 말아 주세요.
"""),
        ("학교 시설/1층/보건실", """== 보건실 ==
논곡중학교 본관 1층 보건실에 관한 문서입니다.

=== 이곳에서의 기억 ===
이 장소와 관련된 학교생활의 기억을 자유롭게 적을 수 있습니다.

본인이나 다른 사람의 질병, 치료 내용 등 건강정보는 민감한 개인정보이므로 작성하지 말아 주세요.
"""),
        ("학교 시설/1층/Wee Class", """== Wee Class ==
논곡중학교 본관 1층 Wee Class에 관한 문서입니다.

=== 이곳에서의 기억 ===
공간 자체와 관련된 일반적인 학교생활의 기억만 적어 주세요.

상담 여부나 상담 내용은 매우 사적인 정보이므로 본인과 다른 사람 모두에 대해 작성하지 말아 주세요.
"""),
        ("학교 시설/1층/도움반 교실", """== 도움반 교실 ==
논곡중학교 본관 1층 도움반 교실에 관한 문서입니다.

=== 이곳에서의 기억 ===
이 장소와 함께한 긍정적인 학교생활의 기억을 자유롭게 적어 주세요.

특정 학생의 장애·건강·지원 여부처럼 민감한 개인정보를 드러내는 내용은 작성하지 말아 주세요.
"""),
        ("학교 시설/논곡관/강당", """== 논곡관 강당 ==
논곡중학교 논곡관의 강당을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
학교 행사, 체육 활동 등 강당에서 있었던 추억을 자유롭게 적어 주세요.

다른 사람의 얼굴이 나온 사진이나 개인정보는 당사자의 동의 없이 올리지 말아 주세요.
"""),
        ("학교 시설/논곡관/급식실", """== 논곡관 급식실 ==
논곡중학교 논곡관의 급식실을 기록하는 문서입니다.

=== 이곳에서의 기억 ===
점심시간이나 급식과 관련해 이 장소에서 있었던 학교생활의 기억을 자유롭게 적어 주세요.

특정 학생이나 교직원을 놀리거나 개인정보가 드러나는 내용은 작성하지 말아 주세요.
"""),
    ]
    existing_rows = query("SELECT id, title, protected FROM wiki_pages WHERE deleted=FALSE")
    existing = {row["title"]: row for row in existing_rows}

    for title, content in pages:
        if title not in existing:
            execute(
                "INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) "
                "VALUES (%s,%s,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)",
                (title, content),
            )

    guideline = existing.get("편집지침")
    if not guideline:
        execute(
            "INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,TRUE,FALSE)",
            ("편집지침", "논곡위키의 공식 편집지침입니다."),
        )
    elif not guideline["protected"]:
        execute("UPDATE wiki_pages SET protected=TRUE WHERE id=%s", (guideline["id"],))

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
@app.route("/robots.txt")
def robots_txt():
    body = "User-agent: *\nAllow: /\nSitemap: https://nongok-wiki.onrender.com/sitemap.xml\n"
    return app.response_class(body, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap_xml():
    pages = query("SELECT title, updated_at FROM wiki_pages WHERE deleted=FALSE ORDER BY updated_at DESC")
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    urls = [
        f"<url><loc>https://nongok-wiki.onrender.com/</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>1.0</priority></url>",
        "<url><loc>https://nongok-wiki.onrender.com/all-pages</loc><changefreq>daily</changefreq><priority>0.8</priority></url>",
    ]
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
    defaults = {
        "notice": "다른 사람의 연락처, 주소 등 사적인 개인정보는 보호해 주세요.\n친구를 공격하거나 괴롭히는 내용은 작성하지 말아 주세요.\n학교생활, 추억, 정보 등 다양한 내용을 자유롭게 작성해 주세요.",
        "news": "논곡위키 공개 베타 운영 중입니다.\n문서 편집과 토론 기능을 사용할 수 있습니다.",
        "feedback": "오류나 개선할 점은 문서 토론 또는 관리자에게 알려주세요.",
        "supporters": "아직 등록된 후원자가 없습니다.",
    }
    rows = query("SELECT section_key, content FROM homepage_sections")
    sections = defaults.copy()
    sections.update({row["section_key"]: row["content"] for row in rows})
    sections_html = {
        key: _link_document_mentions(str(escape(value))).replace("\n", "<br>")
        for key, value in sections.items()
    }

    homepage_pages = query(
        "SELECT title FROM wiki_pages WHERE deleted=FALSE ORDER BY title ASC"
    )
    titles = [row["title"] for row in homepage_pages]

    category_titles = {
        "공지": {"도움말", "편집지침", "운영방침", "개인정보처리방침"},
        "학교 시설 목록": {"학교 시설"},
        "생활 관련 문서 목록": {"학교생활", "급식", "시간표", "학교 행사", "학생회"},
        "동아리 목록": {"동아리"},
    }

    used = set()
    document_groups = []
    for label, wanted in category_titles.items():
        items = [title for title in titles if title in wanted]
        used.update(items)
        document_groups.append({"label": label, "items": items})

    # Titles that explicitly look like person-profile pages are grouped separately.
    facility_subpages = [title for title in titles if title.startswith("학교 시설/")]
    used.update(facility_subpages)

    person_items = [
        title for title in titles
        if title not in used
        and not title.startswith("학교 시설/")
        and any(key in title for key in ("인물", "교장", "교감", "선생님"))
    ]
    used.update(person_items)
    document_groups.append({"label": "인물 문서 목록", "items": person_items})

    other_items = [
        title for title in titles
        if title not in used and title != "논곡위키:대문"
    ]
    document_groups.append({"label": "기타 문서 목록", "items": other_items})

    return render_template(
        "index.html",
        sections=sections,
        sections_html=sections_html,
        document_groups=document_groups,
        facility_floors=[
            {
                "floor": "4층",
                "rooms": [
                    {"label": "1학년 교실", "title": "학교 시설/4층/1학년 교실"},
                    {"label": "1학년 교무실", "title": "학교 시설/4층/1학년 교무실"},
                    {"label": "민주 관련 부서", "title": "학교 시설/4층/민주 관련 부서"},
                    {"label": "음악실", "title": "학교 시설/4층/음악실"},
                ],
            },
            {
                "floor": "3층",
                "rooms": [
                    {"label": "2학년 교무실", "title": "학교 시설/3층/2학년 교무실"},
                    {"label": "2학년 교실", "title": "학교 시설/3층/2학년 교실"},
                    {"label": "학생자치실", "title": "학교 시설/3층/학생자치실"},
                ],
            },
            {
                "floor": "2층",
                "rooms": [
                    {"label": "교장실", "title": "학교 시설/2층/교장실"},
                    {"label": "본교무실", "title": "학교 시설/2층/본교무실"},
                    {"label": "3학년 교실", "title": "학교 시설/2층/3학년 교실"},
                    {"label": "체육 교무실", "title": "학교 시설/2층/체육 교무실"},
                    {"label": "정보실", "title": "학교 시설/2층/정보실"},
                ],
            },
            {
                "floor": "1층",
                "rooms": [
                    {"label": "보건실", "title": "학교 시설/1층/보건실"},
                    {"label": "Wee Class", "title": "학교 시설/1층/Wee Class"},
                    {"label": "도움반 교실", "title": "학교 시설/1층/도움반 교실"},
                ],
            },
        ],
        facility_wings=[
            {
                "area": "논곡관",
                "rooms": [
                    {"label": "강당", "title": "학교 시설/논곡관/강당"},
                    {"label": "급식실", "title": "학교 시설/논곡관/급식실"},
                ],
                "note": "별동",
            },
            {
                "area": "야외 시설",
                "rooms": [
                    {"label": "운동장", "title": "학교 시설/야외시설"},
                ],
                "note": "야외",
            },
        ],
        all_document_titles=[title for title in titles if title != "논곡위키:대문"],
    )

@app.route("/admin/homepage/<section_key>", methods=["GET", "POST"])
@require_teacher
def edit_homepage_section(section_key):
    labels = {"notice": "유의사항", "news": "공지사항", "feedback": "피드백", "supporters": "후원자"}
    if section_key not in labels:
        abort(404)
    if current_user()["role"] != "admin" and section_key != "news":
        abort(403)
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
        log_admin_action("대문/공지 수정", "homepage_section", section_key, labels[section_key])
        flash(f'{labels[section_key]} 내용을 저장했습니다.', "success")
        return redirect(url_for("index"))
    return render_template("homepage_edit.html", section_key=section_key, section_label=labels[section_key], content=content)

@app.route("/term/<term>")
def term_mentions(term):
    term = re.sub(r"\s+", " ", str(term or "").strip())[:40]
    if not re.fullmatch(r"[가-힣A-Za-z][가-힣A-Za-z0-9 _.-]{1,39}", term):
        abort(404)

    known_terms = _known_repeated_terms()
    canonical = next((item for item in known_terms if item.lower() == term.lower()), None)
    if not canonical:
        abort(404)
    term = canonical

    like = f"%{term}%"
    rows = query(
        """SELECT title, content, updated_at, views
           FROM wiki_pages
           WHERE deleted=FALSE AND (title ILIKE %s OR content ILIKE %s)
           ORDER BY
             CASE WHEN title ILIKE %s THEN 0 ELSE 1 END,
             updated_at DESC,
             title ASC
           LIMIT 100""",
        (like, like, like),
    )

    pages = []
    for row in rows:
        compact = re.sub(r"\s+", " ", str(row.get("content") or ""))
        pos = compact.lower().find(term.lower())
        if pos >= 0:
            start = max(0, pos - 65)
            end = min(len(compact), pos + len(term) + 95)
            snippet = compact[start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(compact):
                snippet += "…"
        else:
            snippet = "문서 제목에 이 단어가 포함되어 있습니다."
        row["snippet"] = snippet
        row["url"] = url_for("wiki", title=row["title"])
        row["is_home"] = False
        pages.append(row)

    homepage_rows = query("SELECT content FROM homepage_sections ORDER BY section_key")
    home_text = " ".join(str(row.get("content") or "") for row in homepage_rows)
    home_compact = re.sub(r"\s+", " ", home_text)
    home_pos = home_compact.lower().find(term.lower())
    if home_pos >= 0:
        start = max(0, home_pos - 65)
        end = min(len(home_compact), home_pos + len(term) + 95)
        snippet = home_compact[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(home_compact):
            snippet += "…"
        pages.insert(0, {
            "title": "논곡위키:대문",
            "snippet": snippet,
            "views": 0,
            "url": url_for("index"),
            "is_home": True,
        })

    return render_template(
        "term_mentions.html",
        term=term,
        pages=pages,
    )


@app.route("/person/<name>")
def person_mentions(name):
    name = re.sub(r"\s+", " ", str(name or "").strip())[:30]
    if not re.fullmatch(r"[가-힣A-Za-z][가-힣A-Za-z .·'-]{1,29}", name):
        abort(404)

    like = f"%{name}%"
    rows = query(
        """SELECT title, content, updated_at, views
           FROM wiki_pages
           WHERE deleted=FALSE AND (title ILIKE %s OR content ILIKE %s)
           ORDER BY
             CASE WHEN title ILIKE %s THEN 0 ELSE 1 END,
             updated_at DESC,
             title ASC
           LIMIT 100""",
        (like, like, like),
    )

    pages = []
    for row in rows:
        content = str(row.get("content") or "")
        compact = re.sub(r"\s+", " ", content)
        pos = compact.lower().find(name.lower())
        if pos >= 0:
            start = max(0, pos - 65)
            end = min(len(compact), pos + len(name) + 95)
            snippet = compact[start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(compact):
                snippet += "…"
        else:
            snippet = "문서 제목에 이 이름이 포함되어 있습니다."
        row["snippet"] = snippet
        row["url"] = url_for("wiki", title=row["title"])
        row["is_home"] = False
        pages.append(row)

    homepage_rows = query("SELECT content FROM homepage_sections ORDER BY section_key")
    homepage_text = " ".join(str(row.get("content") or "") for row in homepage_rows)
    if name in HOME_OPERATOR_NAMES:
        homepage_text = (homepage_text + " 논곡위키 운영자 " + " ".join(sorted(HOME_OPERATOR_NAMES))).strip()

    home_compact = re.sub(r"\s+", " ", homepage_text)
    home_pos = home_compact.lower().find(name.lower())
    if home_pos >= 0:
        start = max(0, home_pos - 65)
        end = min(len(home_compact), home_pos + len(name) + 95)
        home_snippet = home_compact[start:end]
        if start > 0:
            home_snippet = "…" + home_snippet
        if end < len(home_compact):
            home_snippet += "…"
        pages.insert(0, {
            "title": "논곡위키:대문",
            "snippet": home_snippet,
            "views": 0,
            "url": url_for("index"),
            "is_home": True,
        })

    return render_template(
        "person_mentions.html",
        person_name=name,
        pages=pages,
    )


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
        if page and page["protected"] and not role_at_least(user, "teacher"):
            existing = query(
                """SELECT id FROM pending_document_edits
                   WHERE page_id=%s AND submitter_id=%s AND status='pending'
                   ORDER BY id DESC LIMIT 1""",
                (page["id"], user["id"]),
            )
            if existing:
                execute(
                    """UPDATE pending_document_edits
                       SET proposed_content=%s, created_at=CURRENT_TIMESTAMP
                       WHERE id=%s""",
                    (content, existing[0]["id"]),
                )
            else:
                execute(
                    """INSERT INTO pending_document_edits
                       (page_id, proposed_content, submitter_id, status, created_at)
                       VALUES (%s,%s,%s,'pending',CURRENT_TIMESTAMP)""",
                    (page["id"], content, user["id"]),
                )
            flash("공식 문서 수정안이 검토 대기 상태로 제출되었습니다.", "success")
            return redirect(url_for("wiki", title=page["title"]))
        if page:
            execute("INSERT INTO revisions(page_id, title, content, author_id, created_at) VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP)", (page["id"], page["title"], page["content"], user["id"]))
            execute("UPDATE wiki_pages SET title=%s, content=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (new_title, content, page["id"]))
        else:
            execute("INSERT INTO wiki_pages(title, content, author_id, created_at, updated_at, protected, deleted) VALUES (%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,FALSE,FALSE)", (new_title, content, user["id"]))
        flash("문서를 저장했습니다.", "success")
        return redirect(url_for("wiki", title=new_title))
    return render_template(
        "edit.html",
        page=page,
        title=title,
        official_review=bool(page and page["protected"] and not role_at_least(current_user(), "teacher")),
    )

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
            source_format = (image.format or "").upper()
            image.load()
            image = ImageOps.exif_transpose(image)
            if image.width < 1 or image.height < 1 or image.width * image.height > 25_000_000:
                return None, None

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


@app.route("/members")
@require_login
def members():
    rows = query(
        """SELECT u.id, u.username, u.profile_name, u.profile_bio, u.profile_status,
                  u.profile_color, u.profile_emoji, u.role,
                  (SELECT COUNT(*) FROM follows f WHERE f.following_id=u.id) AS follower_count,
                  (SELECT COUNT(*) FROM follows f WHERE f.follower_id=u.id) AS following_count
           FROM users u
           ORDER BY COALESCE(NULLIF(u.profile_name,''), u.username) ASC
           LIMIT 200"""
    )
    viewer = current_user()
    viewer_following = {
        row["following_id"] for row in query(
            "SELECT following_id FROM follows WHERE follower_id=%s",
            (viewer["id"],),
        )
    }
    return render_template("members.html", members=rows, viewer_following=viewer_following)


@app.route("/profile/<username>")
@require_login
def user_profile(username):
    rows = query(
        """SELECT id, username, profile_name, profile_bio, profile_status,
                  profile_color, profile_emoji, role
           FROM users WHERE username=%s""",
        (username,),
    )
    if not rows:
        abort(404)
    profile = rows[0]
    viewer = current_user()
    follower_count = int(query(
        "SELECT COUNT(*) AS c FROM follows WHERE following_id=%s",
        (profile["id"],),
    )[0]["c"] or 0)
    following_count = int(query(
        "SELECT COUNT(*) AS c FROM follows WHERE follower_id=%s",
        (profile["id"],),
    )[0]["c"] or 0)
    is_following = False
    if viewer["id"] != profile["id"]:
        is_following = bool(query(
            "SELECT follower_id FROM follows WHERE follower_id=%s AND following_id=%s",
            (viewer["id"], profile["id"]),
        ))
    gallery_posts = query(
        """SELECT id, title, created_at FROM gallery_posts
           WHERE user_id=%s AND deleted=FALSE
           ORDER BY created_at DESC, id DESC LIMIT 6""",
        (profile["id"],),
    )
    for post in gallery_posts:
        post["created_text"] = _gallery_time_text(post.get("created_at"))
    return render_template(
        "profile.html",
        profile=profile,
        follower_count=follower_count,
        following_count=following_count,
        is_following=is_following,
        gallery_posts=gallery_posts,
    )


@app.route("/profile/<username>/<kind>")
@require_login
def profile_connections(username, kind):
    if kind not in {"followers", "following"}:
        abort(404)
    owner_rows = query(
        "SELECT id, username, profile_name, profile_color, profile_emoji FROM users WHERE username=%s",
        (username,),
    )
    if not owner_rows:
        abort(404)
    owner = owner_rows[0]

    if kind == "followers":
        people = query(
            """SELECT u.id, u.username, u.profile_name, u.profile_status,
                      u.profile_color, u.profile_emoji
               FROM follows f JOIN users u ON u.id=f.follower_id
               WHERE f.following_id=%s
               ORDER BY COALESCE(NULLIF(u.profile_name,''), u.username) ASC""",
            (owner["id"],),
        )
    else:
        people = query(
            """SELECT u.id, u.username, u.profile_name, u.profile_status,
                      u.profile_color, u.profile_emoji
               FROM follows f JOIN users u ON u.id=f.following_id
               WHERE f.follower_id=%s
               ORDER BY COALESCE(NULLIF(u.profile_name,''), u.username) ASC""",
            (owner["id"],),
        )

    viewer = current_user()
    viewer_following = {
        row["following_id"] for row in query(
            "SELECT following_id FROM follows WHERE follower_id=%s",
            (viewer["id"],),
        )
    }
    return render_template(
        "connections.html",
        owner=owner,
        people=people,
        kind=kind,
        viewer_following=viewer_following,
    )


@app.route("/profile/edit", methods=["GET", "POST"])
@require_login
def profile_edit():
    user = current_user()
    if request.method == "POST":
        check_csrf()
        profile_name = request.form.get("profile_name", "").strip()
        profile_status = request.form.get("profile_status", "").strip()
        profile_bio = request.form.get("profile_bio", "").strip()
        profile_emoji = request.form.get("profile_emoji", "").strip()
        profile_color = request.form.get("profile_color", "#87aa43").strip().lower()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        new_password_confirm = request.form.get("new_password_confirm", "")

        if len(profile_name) > 30:
            flash("프로필 이름은 30자 이하로 입력해 주세요.", "warning")
            return redirect(url_for("profile_edit"))
        if len(profile_status) > 80:
            flash("상태 메시지는 80자 이하로 입력해 주세요.", "warning")
            return redirect(url_for("profile_edit"))
        if len(profile_bio) > 300:
            flash("자기소개는 300자 이하로 입력해 주세요.", "warning")
            return redirect(url_for("profile_edit"))
        if len(profile_emoji) > 8 or any(ch in profile_emoji for ch in "<>"):
            flash("프로필 이모지는 짧게 입력해 주세요.", "warning")
            return redirect(url_for("profile_edit"))
        if not re.fullmatch(r"#[0-9a-f]{6}", profile_color):
            profile_color = "#87aa43"

        password_changed = False
        if current_password or new_password or new_password_confirm:
            password_rows = query("SELECT password_hash FROM users WHERE id=%s", (user["id"],))
            if not password_rows or not check_password_hash(password_rows[0]["password_hash"], current_password):
                flash("현재 비밀번호가 올바르지 않습니다.", "warning")
                return redirect(url_for("profile_edit"))
            if len(new_password) < 6:
                flash("새 비밀번호는 6자 이상으로 입력해 주세요.", "warning")
                return redirect(url_for("profile_edit"))
            if new_password != new_password_confirm:
                flash("새 비밀번호 확인이 일치하지 않습니다.", "warning")
                return redirect(url_for("profile_edit"))
            password_changed = True

        execute(
            """UPDATE users
               SET profile_name=%s, profile_status=%s, profile_bio=%s,
                   profile_color=%s, profile_emoji=%s
               WHERE id=%s""",
            (profile_name, profile_status, profile_bio, profile_color, profile_emoji, user["id"]),
        )
        if password_changed:
            execute(
                "UPDATE users SET password_hash=%s WHERE id=%s",
                (generate_password_hash(new_password), user["id"]),
            )
            flash("프로필과 비밀번호를 저장했습니다.", "success")
        else:
            flash("프로필을 저장했습니다.", "success")
        return redirect(url_for("user_profile", username=user["username"]))

    return render_template("profile_edit.html", profile=user)


@app.route("/profile/<username>/follow", methods=["POST"])
@require_login
def profile_follow(username):
    check_csrf()
    viewer = current_user()
    rows = query("SELECT id FROM users WHERE username=%s", (username,))
    if not rows:
        abort(404)
    target_id = rows[0]["id"]
    if target_id == viewer["id"]:
        return redirect(url_for("user_profile", username=username))
    existing = query(
        "SELECT follower_id FROM follows WHERE follower_id=%s AND following_id=%s",
        (viewer["id"], target_id),
    )
    if existing:
        execute(
            "DELETE FROM follows WHERE follower_id=%s AND following_id=%s",
            (viewer["id"], target_id),
        )
    else:
        execute(
            "INSERT INTO follows(follower_id,following_id,created_at) VALUES (%s,%s,CURRENT_TIMESTAMP)",
            (viewer["id"], target_id),
        )
    return redirect(url_for("user_profile", username=username))


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
    new_state = not bool(rows[0]["is_open"])
    execute("UPDATE polls SET is_open=%s WHERE id=%s", (new_state, poll_id))
    log_admin_action("투표 상태 변경", "poll", poll_id, "진행" if new_state else "마감")
    return redirect(url_for("polls"))

@app.route("/notices")
def notices():
    rows = query(
        "SELECT content, updated_at FROM homepage_sections WHERE section_key=%s",
        ("news",),
    )
    raw_content = rows[0]["content"].strip() if rows else ""
    updated_at = rows[0]["updated_at"] if rows else None
    updated_dt = _as_utc_datetime(updated_at)
    is_active = bool(
        raw_content
        and updated_dt
        and datetime.now(timezone.utc) - updated_dt <= timedelta(hours=48)
    )
    user = current_user()
    admin_view = role_at_least(user, "teacher")
    content = raw_content if (is_active or admin_view) else ""
    return render_template(
        "notices.html",
        notice_content=content,
        notice_updated_at=updated_at,
        notice_is_new=is_active,
        notice_is_active=is_active,
        notice_admin_view=admin_view,
    )


@app.route("/gallery")
def gallery():
    latest_row = query(
        "SELECT COALESCE(MAX(id),0) AS max_id FROM gallery_posts WHERE deleted=FALSE"
    )[0]
    latest_id = int(latest_row["max_id"] or 0)
    user = current_user()
    if user:
        read_rows = query(
            "SELECT user_id FROM gallery_reads WHERE user_id=%s",
            (user["id"],),
        )
        if read_rows:
            execute(
                "UPDATE gallery_reads SET last_seen_post_id=%s, updated_at=CURRENT_TIMESTAMP WHERE user_id=%s",
                (latest_id, user["id"]),
            )
        else:
            execute(
                "INSERT INTO gallery_reads(user_id,last_seen_post_id,updated_at) VALUES (%s,%s,CURRENT_TIMESTAMP)",
                (user["id"], latest_id),
            )
    else:
        session["gallery_seen_post_id"] = latest_id

    posts = query(
        """SELECT p.id, p.title, p.body, p.created_at, p.views, u.username,
                  COALESCE(NULLIF(u.real_name, ''), u.username) AS display_name,
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
    user = current_user()
    if user.get("school_name") != "논곡중학교":
        flash("논곡갤러리 게시물 작성은 논곡중학교 재학생만 할 수 있습니다.", "warning")
        return redirect(url_for("gallery"))

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
        """SELECT p.id, p.user_id, p.title, p.body, p.created_at, p.views, u.username,
                  COALESCE(NULLIF(u.real_name, ''), u.username) AS display_name
           FROM gallery_posts p JOIN users u ON u.id=p.user_id
           WHERE p.id=%s AND p.deleted=FALSE""",
        (post_id,),
    )
    if not rows:
        abort(404)
    post = rows[0]
    execute("UPDATE gallery_posts SET views=views+1 WHERE id=%s", (post_id,))
    post["views"] = int(post.get("views") or 0) + 1
    post["created_text"] = _gallery_time_text(post.get("created_at"))
    images = query(
        "SELECT id FROM gallery_images WHERE post_id=%s ORDER BY sort_order ASC, id ASC",
        (post_id,),
    )
    comments = query(
        """SELECT c.id, c.user_id, c.body, c.created_at, u.username,
                  COALESCE(NULLIF(u.real_name, ''), u.username) AS display_name
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
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, noimageindex"
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
    if rows[0]["user_id"] != user["id"] and not role_at_least(user, "moderator"):
        abort(403)
    moderated = rows[0]["user_id"] != user["id"]
    execute("UPDATE gallery_posts SET deleted=TRUE WHERE id=%s", (post_id,))
    if moderated:
        log_admin_action("갤러리 게시물 삭제", "gallery_post", post_id, f"게시물 #{post_id}")
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
    if rows[0]["user_id"] != user["id"] and not role_at_least(user, "moderator"):
        abort(403)
    moderated = rows[0]["user_id"] != user["id"]
    execute("UPDATE gallery_comments SET deleted=TRUE WHERE id=%s", (comment_id,))
    if moderated:
        log_admin_action("갤러리 댓글 삭제", "gallery_comment", comment_id, f"게시물 #{rows[0]['post_id']}의 댓글")
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
    gallery_results = []
    if q:
        like = f"%{q}%"
        results = query(
            """SELECT title, content, updated_at FROM wiki_pages
               WHERE deleted=FALSE AND (title ILIKE %s OR content ILIKE %s)
               ORDER BY updated_at DESC LIMIT 50""",
            (like, like),
        )
        gallery_results = query(
            """SELECT p.id, p.title, p.body, p.created_at, p.views,
                      COALESCE(NULLIF(u.real_name, ''), u.username) AS display_name
               FROM gallery_posts p
               JOIN users u ON u.id=p.user_id
               WHERE p.deleted=FALSE AND (p.title ILIKE %s OR p.body ILIKE %s)
               ORDER BY p.created_at DESC, p.id DESC
               LIMIT 50""",
            (like, like),
        )
    return render_template(
        "search.html",
        q=q,
        results=results,
        gallery_results=gallery_results,
    )

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
        real_name = request.form.get("real_name", "").strip()
        student_no = request.form.get("student_no", "").strip()
        school_name = request.form.get("school_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not re.fullmatch(r"[A-Za-z가-힣·ㆍ' -]{2,30}", real_name):
            flash("이름은 2~30자의 한글/영문 이름으로 입력해 주세요.", "warning")
            return redirect(url_for("register"))
        if not re.fullmatch(r"[1-3](0[1-4])(0[1-9]|1[0-9]|2[0-9])", student_no):
            flash("학번 형식이 올바르지 않습니다. 예: 10101 = 1학년 1반 1번", "warning")
            return redirect(url_for("register"))
        if len(school_name) < 2 or len(school_name) > 80:
            flash("현재 재학 중인 학교 이름을 정확히 입력해 주세요.", "warning")
            return redirect(url_for("register"))
        if not re.fullmatch(r"[A-Za-z0-9가-힣_]{2,24}", username):
            flash("아이디는 2~24자의 한글/영문/숫자/밑줄만 사용할 수 있습니다.", "warning")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("비밀번호는 6자 이상으로 입력해 주세요.", "warning")
            return redirect(url_for("register"))
        if query("SELECT id FROM users WHERE username=%s", (username,)):
            flash("이미 사용 중인 아이디입니다.", "warning")
            return redirect(url_for("register"))
        legacy_student_no = student_no[0] + "0" + student_no[1:]
        if query(
            "SELECT id FROM users WHERE student_no IN (%s,%s)",
            (student_no, legacy_student_no),
        ):
            flash("이미 가입에 사용된 학번입니다.", "warning")
            return redirect(url_for("register"))

        execute(
            """INSERT INTO users(username,password_hash,real_name,student_no,school_name,role,account_status,created_at)
               VALUES (%s,%s,%s,%s,%s,'user','pending',CURRENT_TIMESTAMP)""",
            (username, generate_password_hash(password), real_name, student_no, school_name),
        )
        flash("가입 신청이 완료되었습니다. 관리자 승인 후 로그인할 수 있습니다.", "success")
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
            account = rows[0]
            status = account.get("account_status", "approved")
            if account["role"] != "admin" and status != "approved":
                if status == "pending":
                    flash("가입 승인 대기 중입니다. 관리자가 승인한 뒤 로그인할 수 있습니다.", "warning")
                else:
                    flash("가입 승인이 거절되었거나 비활성화된 계정입니다. 관리자에게 문의해 주세요.", "warning")
            else:
                session["user_id"] = account["id"]
                flash("로그인되었습니다.", "success")
                if account["role"] != "admin" and (
                    not account.get("real_name")
                    or not account.get("student_no")
                    or not account.get("school_name")
                ):
                    return redirect(url_for("identity_setup"))
                return redirect(request.form.get("next") or request.args.get("next") or url_for("index"))
        else:
            flash("아이디 또는 비밀번호가 맞지 않습니다.", "warning")
    return render_template(
        "auth.html",
        mode="login",
        next_url=request.args.get("next", ""),
        entered_username=request.form.get("username", ""),
    )

@app.route("/account/identity", methods=["GET", "POST"])
@require_login
def identity_setup():
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("index"))
    if user.get("real_name") and user.get("student_no") and user.get("school_name"):
        return redirect(url_for("index"))

    if request.method == "POST":
        check_csrf()
        real_name = request.form.get("real_name", "").strip()
        student_no = request.form.get("student_no", "").strip()
        school_name = request.form.get("school_name", "").strip()

        if not re.fullmatch(r"[A-Za-z가-힣·ㆍ' -]{2,30}", real_name):
            flash("이름은 2~30자의 한글/영문 이름으로 입력해 주세요.", "warning")
            return redirect(url_for("identity_setup"))
        if not re.fullmatch(r"[1-3](0[1-4])(0[1-9]|1[0-9]|2[0-9])", student_no):
            flash("학번 형식이 올바르지 않습니다. 예: 10101 = 1학년 1반 1번", "warning")
            return redirect(url_for("identity_setup"))
        if len(school_name) < 2 or len(school_name) > 80:
            flash("현재 재학 중인 학교 이름을 정확히 입력해 주세요.", "warning")
            return redirect(url_for("identity_setup"))
        legacy_student_no = student_no[0] + "0" + student_no[1:]
        if query(
            "SELECT id FROM users WHERE student_no IN (%s,%s) AND id<>%s",
            (student_no, legacy_student_no, user["id"]),
        ):
            flash("이미 다른 계정에 등록된 학번입니다.", "warning")
            return redirect(url_for("identity_setup"))

        execute(
            "UPDATE users SET real_name=%s, student_no=%s, school_name=%s WHERE id=%s",
            (real_name, student_no, school_name, user["id"]),
        )
        flash("이름, 학번, 학교 정보가 등록되었습니다.", "success")
        return redirect(url_for("index"))

    return render_template("identity.html")


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

def _admin_page_data(member_q=""):
    reports = query(
        """SELECT r.*, w.title, u.username FROM reports r
           LEFT JOIN wiki_pages w ON w.id=r.page_id
           LEFT JOIN users u ON u.id=r.user_id
           ORDER BY r.created_at DESC LIMIT 100"""
    )
    if member_q:
        users = query(
            """SELECT id, username, real_name, student_no, school_name, role, account_status, created_at
               FROM users
               WHERE COALESCE(real_name, '') ILIKE %s
               ORDER BY CASE account_status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                        real_name ASC, created_at DESC
               LIMIT 100""",
            (f"%{member_q}%",),
        )
    else:
        users = query(
            "SELECT id, username, real_name, student_no, school_name, role, account_status, created_at "
            "FROM users ORDER BY CASE account_status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, "
            "created_at DESC LIMIT 100"
        )
    return reports, users


@app.route("/admin")
@require_staff
def admin():
    actor = current_user()
    can_manage_members = role_at_least(actor, "teacher")
    member_q = request.args.get("member_q", "").strip()[:30] if can_manage_members else ""
    reports, users = _admin_page_data(member_q) if can_manage_members else (_admin_page_data("")[0], [])
    for member in users:
        member["can_manage"] = can_manage_member(actor, member)
    can_review_official = role_at_least(actor, "teacher")
    return render_template(
        "admin.html",
        reports=reports,
        users=users,
        logs=_admin_activity_logs(),
        pending_edits=_pending_document_edits() if can_review_official else [],
        reset_result=None,
        member_q=member_q,
        role_labels=ROLE_LABELS,
        can_manage_members=can_manage_members,
        can_change_roles=actor["role"] == "admin",
        can_review_official=can_review_official,
    )


@app.route("/admin/user/<int:user_id>/approval/<status>", methods=["POST"])
@require_teacher
def admin_user_approval(user_id, status):
    check_csrf()
    if status not in {"approved", "rejected"}:
        abort(400)

    rows = query("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not rows:
        abort(404)
    target = rows[0]
    if not can_manage_member(current_user(), target):
        abort(403)

    execute("UPDATE users SET account_status=%s WHERE id=%s", (status, user_id))
    log_admin_action(
        "가입 승인" if status == "approved" else "가입 승인 거절",
        "user",
        user_id,
        f"@{target['username']}",
    )
    if status == "approved":
        flash(f"@{target['username']} 가입을 승인했습니다.", "success")
    else:
        flash(f"@{target['username']} 가입 승인을 거절했습니다.", "success")

    member_q = request.form.get("member_q", "").strip()[:30]
    return redirect(url_for("admin", member_q=member_q) if member_q else url_for("admin"))


@app.route("/admin/user/<int:user_id>/reset-password", methods=["POST"])
@require_teacher
def admin_reset_password(user_id):
    check_csrf()
    rows = query("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not rows:
        abort(404)
    target = rows[0]
    if not can_manage_member(current_user(), target):
        abort(403)

    temporary_password = "pw12345"
    execute(
        "UPDATE users SET password_hash=%s WHERE id=%s",
        (generate_password_hash(temporary_password), user_id),
    )
    log_admin_action("비밀번호 초기화", "user", user_id, f"@{target['username']}")

    member_q = request.form.get("member_q", "").strip()[:30]
    reports, users = _admin_page_data(member_q)
    actor = current_user()
    for member in users:
        member["can_manage"] = can_manage_member(actor, member)
    return render_template(
        "admin.html",
        reports=reports,
        users=users,
        logs=_admin_activity_logs(),
        pending_edits=_pending_document_edits(),
        reset_result={
            "username": target["username"],
            "temporary_password": temporary_password,
        },
        member_q=member_q,
        role_labels=ROLE_LABELS,
        can_manage_members=True,
        can_change_roles=actor["role"] == "admin",
        can_review_official=True,
    )


@app.route("/admin/user/<int:user_id>/role", methods=["POST"])
@require_admin
def admin_user_role(user_id):
    check_csrf()
    new_role = request.form.get("role", "").strip()
    if new_role not in {"user", "moderator", "teacher"}:
        abort(400)
    rows = query("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not rows:
        abort(404)
    target = rows[0]
    if target["role"] == "admin":
        abort(403)
    old_role = target["role"]
    execute("UPDATE users SET role=%s WHERE id=%s", (new_role, user_id))
    log_admin_action(
        "회원 권한 변경",
        "user",
        user_id,
        f"@{target['username']}: {ROLE_LABELS.get(old_role, old_role)} → {ROLE_LABELS[new_role]}",
    )
    flash(f"@{target['username']} 권한을 {ROLE_LABELS[new_role]}(으)로 변경했습니다.", "success")
    member_q = request.form.get("member_q", "").strip()[:30]
    return redirect(url_for("admin", member_q=member_q) if member_q else url_for("admin"))


@app.route("/admin/document-review/<int:review_id>/<status>", methods=["POST"])
@require_teacher
def admin_document_review(review_id, status):
    check_csrf()
    if status not in {"approved", "rejected"}:
        abort(400)

    rows = query(
        """SELECT p.id, p.page_id, p.proposed_content, p.submitter_id, p.status,
                  w.title, w.content, w.protected
           FROM pending_document_edits p
           JOIN wiki_pages w ON w.id=p.page_id
           WHERE p.id=%s AND w.deleted=FALSE""",
        (review_id,),
    )
    if not rows:
        abort(404)
    review = rows[0]
    if review["status"] != "pending":
        flash("이미 처리된 수정안입니다.", "warning")
        return redirect(url_for("admin"))

    reviewer = current_user()
    if status == "approved":
        execute(
            """INSERT INTO revisions(page_id, title, content, author_id, created_at)
               VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP)""",
            (review["page_id"], review["title"], review["content"], review["submitter_id"]),
        )
        execute(
            "UPDATE wiki_pages SET content=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (review["proposed_content"], review["page_id"]),
        )

    execute(
        """UPDATE pending_document_edits
           SET status=%s, reviewer_id=%s, reviewed_at=CURRENT_TIMESTAMP
           WHERE id=%s""",
        (status, reviewer["id"], review_id),
    )
    log_admin_action(
        "공식 문서 수정 승인" if status == "approved" else "공식 문서 수정 거절",
        "document_review",
        review_id,
        review["title"],
    )
    flash(
        f"{review['title']} 수정안을 {'승인하여 반영했습니다.' if status == 'approved' else '거절했습니다.'}",
        "success",
    )
    return redirect(url_for("admin"))


@app.route("/admin/report/<int:report_id>/<status>", methods=["POST"])
@require_staff
def report_status(report_id, status):
    check_csrf()
    if status not in {"open", "resolved", "dismissed"}:
        abort(400)
    execute("UPDATE reports SET status=%s WHERE id=%s", (status, report_id))
    log_admin_action("신고 처리", "report", report_id, f"상태 → {status}")
    return redirect(url_for("admin"))

@app.route("/admin/protect/<int:page_id>", methods=["POST"])
@require_teacher
def protect(page_id):
    check_csrf()
    rows = query("SELECT title, protected FROM wiki_pages WHERE id=%s AND deleted=FALSE", (page_id,))
    if not rows:
        abort(404)
    if rows[0]["title"] == "논곡위키:대문" and current_user()["role"] != "admin":
        abort(403)
    new_protected = not bool(rows[0]["protected"])
    execute("UPDATE wiki_pages SET protected=%s WHERE id=%s", (new_protected, page_id))
    log_admin_action(
        "문서 보호 변경",
        "wiki_page",
        page_id,
        f"{rows[0]['title']} → {'보호' if new_protected else '보호 해제'}",
    )
    return redirect(request.referrer or url_for("admin"))

@app.route("/admin/delete/<int:page_id>", methods=["POST"])
@require_teacher
def delete_page(page_id):
    check_csrf()
    rows = query("SELECT title FROM wiki_pages WHERE id=%s AND deleted=FALSE", (page_id,))
    if not rows:
        abort(404)
    if rows[0]["title"] == "논곡위키:대문" and current_user()["role"] != "admin":
        abort(403)
    execute("UPDATE wiki_pages SET deleted=TRUE, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (page_id,))
    log_admin_action("문서 삭제", "wiki_page", page_id, rows[0]["title"])
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
