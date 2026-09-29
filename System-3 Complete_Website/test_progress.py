"""
Tests for streaks / XP / badges (runs offline).

    VIVARAN_FAKE_QUIZ=1 python test_progress.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

os.environ.setdefault("VIVARAN_FAKE_QUIZ", "1")
os.environ["VIVARAN_DB"] = os.path.join(tempfile.mkdtemp(), "progress.db")
os.environ["VIVARAN_ADMIN_USER"] = "chief"
os.environ["VIVARAN_ADMIN_PASSWORD"] = "chief-pass-123"

import app as web  # noqa: E402
import db  # noqa: E402
import progress  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))


def at(days_ago: int) -> str:
    """ISO timestamp for noon (IST) N days ago."""
    d = progress.today() - timedelta(days=days_ago)
    local = datetime(d.year, d.month, d.day, 12, 0, tzinfo=progress.TZ)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    db.init_db()
    web.seed_staff_accounts()
    web.app.config["TESTING"] = True
    c = web.app.test_client()

    print("\n[streak maths]")
    t = progress.today()
    cur, longest, reached = progress._streaks({t, t - timedelta(1), t - timedelta(2)}, t)
    check("3 days ending today", cur == 3 and longest == 3 and 3 in reached)
    cur, _, _ = progress._streaks({t - timedelta(1), t - timedelta(2)}, t)
    check("streak still alive today if yesterday was active", cur == 2)
    cur, longest, _ = progress._streaks({t - timedelta(2), t - timedelta(5), t - timedelta(6), t - timedelta(7)}, t)
    check("gap breaks the current streak, longest kept", cur == 0 and longest == 3)
    check("levels", progress.level_for(0)["name"] == "Beginner" and progress.level_for(350)["number"] == 3)

    print("\n[learner flow]")
    c.post("/auth/register", json={"account_type": "trainer", "username": "tina", "email": "tina@gov.in",
                                   "password": "secret-123", "specialisations": ["S001", "S005", "S015"]})
    c.get("/logout")
    role_id = web.recommender.roles()[50]["role_id"]
    c.post("/auth/register", json={"username": "aarav", "email": "aarav@gov.in", "password": "secret-123",
                                   "role_id": role_id, "area_of_experience": "Data Science & Analytics"})
    uid = db.get_user_by_username("aarav")["id"]
    s0 = progress.summary(uid)
    check("new learner starts at 0", s0["streak"] == 0 and s0["xp"] == 0 and s0["badges_earned"] == 0)
    html = c.get("/learn/progress").get_data(as_text=True)
    check("progress page renders", "day streak" in html and "Badges" in html and "Activity" in html)
    check("sidebar has My progress", "My progress" in html)

    cid = db.latest_submission(uid)["recommendations"][0]["course_id"]
    c.post(f"/courses/{cid}/start")
    s1 = progress.summary(uid)
    check("starting a course gives XP but no streak", s1["xp"] == 10 and s1["streak"] == 0)
    check("First Step badge", any(b["key"] == "first_step" and b["earned"] for b in s1["badges"]))
    c.post(f"/courses/{cid}/watched")
    s2 = progress.summary(uid)
    check("finishing a video starts a streak", s2["streak"] == 1 and s2["active_today"] and s2["xp"] == 30)
    c.post(f"/courses/{cid}/watched")
    check("watching again doesn't double-log", len([a for a in db.list_activity(uid) if a["kind"] == "video"]) == 1)
    r = c.post(f"/courses/{cid}/quiz")
    aid = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
    for _ in range(40):
        if db.get_attempt(aid)["status"] == "ready":
            break
        time.sleep(0.1)
    quiz = json.loads(db.get_attempt(aid)["quiz"])
    c.post(f"/quiz/{aid}", data={f"q_{q['id']}": q["answer"] for q in quiz})
    s3 = progress.summary(uid)
    check("quiz same day keeps streak at 1", s3["streak"] == 1)
    check("XP: start+video+quiz+pass+perfect+complete", s3["xp"] == 10 + 20 + 10 + 50 + 25 + 100, str(s3["xp_parts"]))
    earned = {b["key"] for b in s3["badges"] if b["earned"]}
    check("First Pass + Perfect badges", {"first_step", "first_pass", "perfect"} <= earned)

    print("\n[multi-day streak from history]")
    with closing(db.connect()) as conn:
        for d in (1, 2, 3, 4, 5, 6):
            conn.execute("INSERT INTO learner_activity (user_id, kind, course_id, created_at) VALUES (?, 'video', 'X', ?)", (uid, at(d)))
        conn.execute("INSERT INTO learner_activity (user_id, kind, course_id, created_at) VALUES (?, 'video', 'X', ?)", (uid, at(20)))
        conn.commit()
    s4 = progress.summary(uid)
    check("7-day streak counted", s4["streak"] == 7 and s4["longest"] == 7)
    earned = {b["key"] for b in s4["badges"] if b["earned"]}
    check("streak badges 3 and 7", {"streak_3", "streak_7"} <= earned and "streak_30" not in earned)
    check("calendar has 12 weeks x 7 days", len(s4["weeks"]) == 12 and all(len(w) == 7 for w in s4["weeks"]))
    check("calendar marks active days", sum(1 for w in s4["weeks"] for d in w if d["n"]) >= 8)
    html = c.get("/dashboard").get_data(as_text=True)
    check("dashboard banner shows streak + XP", "7-day streak" in html and "XP" in html)

    print("\n[flashcard XP + master badge]")
    for card in c.get(f"/api/flashcards/{cid}").get_json()["cards"]:
        c.post(f"/api/flashcards/{cid}/{card['key']}", json={"status": "know"})
    s5 = progress.summary(uid)
    check("mastered cards give XP", s5["xp_parts"]["Flashcards mastered"] == 5 * len(quiz))
    check("Flashcard Master badge", any(b["key"] == "flash_master" and b["earned"] for b in s5["badges"]))

    print("\n[trainer + admin can see it]")
    c.get("/logout")
    c.post("/login", json={"username": "tina", "password": "secret-123", "role": "trainer"})
    check("trainer dashboard shows streak", "🔥 7" in c.get("/trainer").get_data(as_text=True))
    page = c.get(f"/trainer/students/{uid}").get_data(as_text=True)
    check("trainer student page shows streak, XP, badges", "Current streak" in page and "XP" in page and "Week Warrior" in page)
    c.get("/logout")
    c.post("/login", json={"username": "chief", "password": "chief-pass-123", "role": "admin"})
    check("admin learners list shows streak", "🔥 7" in c.get("/admin/learners").get_data(as_text=True))
    check("admin learner page shows streak", "Longest streak" in c.get(f"/admin/learners/{uid}").get_data(as_text=True))
    check("admin can't open learner progress page", c.get("/learn/progress").status_code == 403)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for n in FAILED:
        print("  failed:", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
