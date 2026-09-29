"""
Vivaran-VQE (System 3) - learner progress: streaks, XP, levels, badges.

Everything is DERIVED from what is already stored (enrollments, graded quiz
attempts, flashcard marks, the learner_activity log), so history counts and
nothing can drift out of sync.

Streak rule (decided with the team): a day counts when the learner
**finished a video lecture** or **took a quiz** that day. Days use the
platform's time zone (VIVARAN_TZ, default Asia/Kolkata). A streak is still
"alive" today until midnight even if today has no activity yet.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import db
import settings

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("VIVARAN_TZ", "Asia/Kolkata"))
except Exception:  # noqa: BLE001 - fall back to IST offset if tzdata is missing
    TZ = timezone(timedelta(hours=5, minutes=30))

XP_RULES = {
    "start": 10,          # start a course
    "video": 20,          # watch the course video
    "quiz": 10,           # take a quiz (max 3 per course count)
    "pass": 50,           # first pass of a course's quiz
    "perfect": 25,        # first 100% on a course's quiz (bonus)
    "complete": 100,      # complete a course
    "card": 5,            # each flashcard mastered
}

LEVELS = [(0, "Beginner"), (100, "Learner"), (300, "Explorer"), (600, "Achiever"),
          (1000, "Expert"), (1600, "Master"), (2500, "Champion")]

BADGES = [
    ("first_step", "First Step", "Start your first course", "🚀"),
    ("first_pass", "First Pass", "Pass your first quiz", "✅"),
    ("perfect", "Perfect Score", "Score 100% on a quiz", "💯"),
    ("streak_3", "On a Roll", "Reach a 3-day streak", "🔥"),
    ("streak_7", "Week Warrior", "Reach a 7-day streak", "⚡"),
    ("streak_30", "Unstoppable", "Reach a 30-day streak", "🏆"),
    ("courses_5", "Fast Learner", "Complete 5 courses", "🎓"),
    ("flash_master", "Flashcard Master", "Master every card in a deck", "🃏"),
    ("roadmap", "Roadmap Champion", "Finish a whole learning roadmap", "🗺️"),
]


def _day(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).date()


def today() -> date:
    return datetime.now(TZ).date()


def _streak_events(user_id: int, enrollments: list[dict], graded: list[dict]) -> list[tuple[date, str, str]]:
    """(day, kind, course_id) for every streak-counting action."""
    events = []
    logged_video = set()
    for a in db.list_activity(user_id):
        if a["kind"] == "video":
            logged_video.add(a["course_id"])
            d = _day(a["created_at"])
            if d:
                events.append((d, "video", a["course_id"]))
    for e in enrollments:  # videos watched before the activity log existed
        if e.get("video_watched") and e["course_id"] not in logged_video:
            d = _day(e.get("updated_at"))
            if d:
                events.append((d, "video", e["course_id"]))
    for a in graded:
        d = _day(a.get("finished_at"))
        if d:
            events.append((d, "quiz", a["course_id"]))
    return events


def _streaks(days: set[date], now: date) -> tuple[int, int, dict[int, date]]:
    """(current, longest, {n: first date a streak of n was reached})."""
    longest, run, prev = 0, 0, None
    reached: dict[int, date] = {}
    for d in sorted(days):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        prev = d
        longest = max(longest, run)
        for n in (3, 7, 30):
            if run >= n and n not in reached:
                reached[n] = d
    current = 0
    start = now if now in days else now - timedelta(days=1)
    while start in days:
        current += 1
        start -= timedelta(days=1)
    return current, longest, reached


def level_for(xp: int) -> dict:
    idx = max(i for i, (need, _) in enumerate(LEVELS) if xp >= need)
    need, name = LEVELS[idx]
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    pct = 100 if not nxt else round(100 * (xp - need) / (nxt[0] - need))
    return {"number": idx + 1, "name": name, "next_name": nxt[1] if nxt else None,
            "next_xp": nxt[0] if nxt else None, "to_next": (nxt[0] - xp) if nxt else 0, "pct": pct}


def summary(user_id: int) -> dict:
    """Full progress picture for one learner."""
    enrollments = db.list_enrollments(user_id)
    attempts = db.list_attempts_for_user(user_id)
    graded = [a for a in attempts if a["status"] == "graded"]
    passed = [a for a in graded if (a.get("accuracy") or 0) >= settings.PASS_THRESHOLD]
    perfect = [a for a in graded if (a.get("accuracy") or 0) >= 100]
    completed = [e for e in enrollments if e["status"] == "completed"]
    marks = db.list_flashcard_marks(user_id)
    known = [m for m in marks if m["status"] == "know"]
    now = today()

    # ---- streaks + calendar ------------------------------------------------
    events = _streak_events(user_id, enrollments, graded)
    per_day: dict[date, int] = {}
    for d, _k, _c in events:
        per_day[d] = per_day.get(d, 0) + 1
    current, longest, reached = _streaks(set(per_day), now)
    first_monday = now - timedelta(days=now.weekday() + 7 * 11)  # 12 weeks incl. this one
    weeks = []
    for w in range(12):
        col = []
        for i in range(7):
            d = first_monday + timedelta(days=7 * w + i)
            n = per_day.get(d, 0)
            col.append({"date": d.isoformat(), "n": n, "future": d > now,
                        "lvl": 0 if n == 0 else 1 if n == 1 else 2 if n == 2 else 3 if n <= 4 else 4})
        weeks.append(col)
    active_days = sum(1 for d in per_day if (now - d).days < 84)

    # ---- XP ---------------------------------------------------------------
    by_course: dict[str, list[dict]] = {}
    for a in graded:
        by_course.setdefault(a["course_id"], []).append(a)
    xp_parts = {
        "Courses started": XP_RULES["start"] * len(enrollments),
        "Videos watched": XP_RULES["video"] * sum(1 for e in enrollments if e.get("video_watched")),
        "Quizzes taken": XP_RULES["quiz"] * sum(min(3, len(v)) for v in by_course.values()),
        "Quizzes passed": XP_RULES["pass"] * len({a["course_id"] for a in passed}),
        "Perfect scores": XP_RULES["perfect"] * len({a["course_id"] for a in perfect}),
        "Courses completed": XP_RULES["complete"] * len(completed),
        "Flashcards mastered": XP_RULES["card"] * len(known),
    }
    xp = sum(xp_parts.values())

    # ---- badges -----------------------------------------------------------
    earned: dict[str, date | None] = {}
    if enrollments:
        earned["first_step"] = min(filter(None, (_day(e["started_at"]) for e in enrollments)), default=None)
    if passed:
        earned["first_pass"] = min(filter(None, (_day(a["finished_at"]) for a in passed)), default=None)
    if perfect:
        earned["perfect"] = min(filter(None, (_day(a["finished_at"]) for a in perfect)), default=None)
    for n in (3, 7, 30):
        if n in reached:
            earned[f"streak_{n}"] = reached[n]
    if len(completed) >= 5:
        done_days = sorted(filter(None, (_day(e.get("completed_at")) for e in completed)))
        earned["courses_5"] = done_days[4] if len(done_days) >= 5 else None
    decks: dict[str, list[dict]] = {}
    for m in marks:
        decks.setdefault(m["course_id"], []).append(m)
    for cid, ms in decks.items():
        total = _deck_size(user_id, cid)
        if total and sum(1 for m in ms if m["status"] == "know") >= total:
            d = max(filter(None, (_day(m["updated_at"]) for m in ms)), default=None)
            if "flash_master" not in earned or (d and earned["flash_master"] and d < earned["flash_master"]):
                earned["flash_master"] = d
    done_ids = {e["course_id"]: e for e in completed}
    for sub in db.list_submissions(user_id):
        ids = [r["course_id"] for r in sub.get("recommendations") or []]
        if ids and all(i in done_ids for i in ids):
            d = max(filter(None, (_day(done_ids[i].get("completed_at")) for i in ids)), default=None)
            if "roadmap" not in earned or (d and earned["roadmap"] and d < earned["roadmap"]):
                earned["roadmap"] = d
    badges = [{"key": k, "name": n, "desc": ds, "icon": ic, "earned": k in earned,
               "date": earned.get(k).isoformat() if earned.get(k) else None} for k, n, ds, ic in BADGES]

    # ---- timeline ---------------------------------------------------------
    timeline = [{"date": b["date"], "icon": b["icon"], "text": f"Earned the “{b['name']}” badge"}
                for b in badges if b["earned"] and b["date"]]
    for e in completed:
        d = _day(e.get("completed_at"))
        if d:
            timeline.append({"date": d.isoformat(), "icon": "🎓", "text": f"Completed “{e['course_title']}”"})
    timeline.sort(key=lambda t: t["date"], reverse=True)

    avg = round(sum(a["accuracy"] or 0 for a in graded) / len(graded)) if graded else None
    return {
        "streak": current, "longest": longest, "active_today": now in per_day,
        "weeks": weeks, "active_days": active_days, "total_days": len(per_day),
        "xp": xp, "xp_parts": xp_parts, "level": level_for(xp),
        "badges": badges, "badges_earned": sum(1 for b in badges if b["earned"]),
        "timeline": timeline[:12],
        "stats": {"completed": len(completed), "passed": len({a["course_id"] for a in passed}),
                  "quizzes": len(graded), "perfect": len(perfect), "avg": avg,
                  "cards": len(known), "videos": sum(1 for e in enrollments if e.get("video_watched"))},
    }


def _deck_size(user_id: int, course_id: str) -> int:
    """Distinct questions the learner has seen in graded quizzes for a course."""
    seen = set()
    for a in db.list_attempts_for_user_course(user_id, course_id):
        if a["status"] == "graded" and a.get("quiz"):
            for q in json.loads(a["quiz"]):
                seen.add(" ".join((q.get("question") or "").lower().split()))
    return len(seen)


def brief(user_id: int) -> dict:
    """Streak + XP only (for trainer/admin tables and the dashboard banner)."""
    s = summary(user_id)
    return {"streak": s["streak"], "longest": s["longest"], "xp": s["xp"], "level": s["level"],
            "badges": s["badges_earned"]}
