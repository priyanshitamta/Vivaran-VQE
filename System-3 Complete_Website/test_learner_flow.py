"""
End-to-end test of the learner platform (runs offline).

    VIVARAN_FAKE_QUIZ=1 python test_learner_flow.py

Uses a throwaway SQLite DB and Flask's test client. VIVARAN_FAKE_QUIZ=1 swaps
System 2's network/LLM pipeline for canned MCQs so the flow can be checked
without YouTube / Whisper / Groq; everything else is the real code path.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time

os.environ.setdefault("VIVARAN_FAKE_QUIZ", "1")
_tmp = tempfile.mkdtemp()
os.environ["VIVARAN_DB"] = os.path.join(_tmp, "test.db")
os.environ["VIVARAN_ADMIN_USER"] = "admin1"
os.environ["VIVARAN_ADMIN_PASSWORD"] = "admin-pass-123"
os.environ["VIVARAN_TRAINER_USER"] = "trainer1"
os.environ["VIVARAN_TRAINER_PASSWORD"] = "trainer-pass-123"

import app as web  # noqa: E402
import db  # noqa: E402

PASSED, FAILED = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))


def wait_ready(client, attempt_id: int) -> str:
    for _ in range(50):
        status = client.get(f"/quiz/{attempt_id}/status.json").get_json()["status"]
        if status != "generating":
            return status
        time.sleep(0.1)
    return "timeout"


def answer_all(client, attempt_id: int, correct: bool) -> None:
    import json
    quiz = json.loads(db.get_attempt(attempt_id)["quiz"])
    form = {}
    for q in quiz:
        wrong = [o for o in q["options"] if o != q["answer"]]
        form[f"q_{q['id']}"] = q["answer"] if correct else wrong[0]
    client.post(f"/quiz/{attempt_id}", data=form)


def main() -> int:
    db.init_db()
    web.seed_staff_accounts()
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    role_id = web.recommender.roles()[50]["role_id"]

    print("\n[registration]")
    r = c.post("/auth/register", json={"username": "rahul", "email": "rahul@gmail.com", "password": "secret-123",
                                       "role_id": role_id, "area_of_experience": "Data Science & Analytics"})
    check("rejects non-@gov.in email", r.status_code == 400 and "government" in r.get_json()["error"])
    r = c.post("/auth/register", json={"username": "rahul", "email": "rahul@gov.in", "password": "secret-123",
                                       "role_id": "", "area_of_experience": "Data Science & Analytics"})
    check("rejects missing designation", r.status_code == 400)
    r = c.post("/auth/register", json={"username": "rahul", "email": "rahul@gov.in", "password": "secret-123",
                                       "role_id": role_id, "area_of_experience": ""})
    check("rejects missing area of experience", r.status_code == 400)
    r = c.post("/auth/register", json={"username": "rahul", "email": "rahul@gov.in", "password": "secret-123",
                                       "role_id": role_id, "area_of_experience": "Data Science & Analytics"})
    check("registers learner", r.status_code == 200 and r.get_json()["redirect"] == "/dashboard", r.get_data(as_text=True))
    user = db.get_user_by_username("rahul")
    check("password is hashed (not plaintext)", user["password_hash"] != "secret-123"
          and user["password_hash"].startswith(("scrypt:", "pbkdf2:")))
    check("profile stored", (db.get_learner_profile(user["id"]) or {}).get("area_of_experience") == "Data Science & Analytics")
    r = c.post("/auth/register", json={"username": "rahul2", "email": "rahul@gov.in", "password": "secret-123",
                                       "role_id": role_id, "area_of_experience": "Data Science & Analytics"})
    check("duplicate email rejected", r.status_code == 409)

    print("\n[learner dashboard + roadmap]")
    page = c.get("/dashboard")
    check("dashboard renders", page.status_code == 200 and "Continue learning" in page.get_data(as_text=True)
          and "Up next on your roadmap" in page.get_data(as_text=True))
    for url in ("/learn/roadmap", "/learn/courses", "/learn/recommended", "/learn/flashcards",
                "/learn/results", "/learn/skills", "/learn/profile", "/courses"):
        check(f"learner page {url} renders in sidebar layout", 'aria-label="Learner navigation"' in c.get(url).get_data(as_text=True))
    html = c.get("/learn/roadmap").get_data(as_text=True)
    roadmap = c.get("/api/learner/roadmap").get_json()
    n = roadmap["count"]
    check("roadmap has multiple courses", n >= 3, str(n))
    check("roadmap is a START..COMPLETED flow", "START" in html and "COMPLETED" in html)
    first = roadmap["roadmap"][0]["course_id"]
    check("roadmap courses are links to course pages", f'/courses/{first}' in html)
    profile = c.get("/api/learner/profile").get_json()
    check("profile API has no password hash", "password_hash" not in profile and profile["email"] == "rahul@gov.in")

    print("\n[course catalogue]")
    html = c.get("/courses").get_data(as_text=True)
    check("catalogue shows 20 cards + more marker", html.count('class="cat-card"') == 20 and 'class="cat-more"' in html)
    html = c.get("/courses?skill=S015").get_data(as_text=True)
    ids = re.findall(r'<span class="subtle" style="font-size:.8rem;">([A-Z]+\d+)</span>', html)
    check("skill filter returns only matching courses", ids and all("S015" in web.courses.get(i)["skills"] for i in ids))
    n_s015 = sum(1 for x in web.courses.all_courses() if "S015" in x["skills"])
    check("skill filter count is right", f"<b>{n_s015}</b> course" in html)
    html2 = c.get("/courses?skill=S015&skill=S001").get_data(as_text=True)
    n_both = sum(1 for x in web.courses.all_courses() if {"S015", "S001"} & set(x["skills"]))
    check("multiple skills = any of them", f"<b>{n_both}</b> course" in html2)
    part = c.get("/courses?skill=S015&page=2&partial=1").get_data(as_text=True)
    check("scroll loads the next chunk", part.count('class="cat-card"') > 0 and "<html" not in part)
    html = c.get("/courses?q=yoga&level=1").get_data(as_text=True)
    check("search + level filter", 'class="cat-card"' in html and "Yoga" in html)

    print("\n[course page -> System 2 quiz]")
    page = c.get(f"/courses/{first}")
    check("course page renders", page.status_code == 200 and "Start Course" in page.get_data(as_text=True))
    c.post(f"/courses/{first}/start")
    check("start sets progress", db.get_enrollment(user["id"], first)["progress"] == 10)
    c.post(f"/courses/{first}/watched")
    check("video watched sets progress", db.get_enrollment(user["id"], first)["progress"] == 50)
    r = c.post(f"/courses/{first}/quiz")
    attempt_id = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
    check("quiz uses the course's own video", db.get_attempt(attempt_id)["source_link"] == web.courses.get(first)["videos"][0])
    check("quiz generated", wait_ready(c, attempt_id) == "ready")
    answer_all(c, attempt_id, correct=False)
    res = c.get(f"/quiz/{attempt_id}/results").get_data(as_text=True)
    check("fail result shown", "Not passed" in res and "Total questions" in res and "Wrong answers" in res)
    e = db.get_enrollment(user["id"], first)
    check("failed quiz keeps course in progress", e["status"] == "in_progress" and e["progress"] == 75)

    r = c.post(f"/courses/{first}/quiz")
    attempt2 = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
    check("retake allowed", attempt2 != attempt_id and wait_ready(c, attempt2) == "ready")
    answer_all(c, attempt2, correct=True)
    res = c.get(f"/quiz/{attempt2}/results").get_data(as_text=True)
    check("pass result shown", "Passed" in res and "100.0%" in res)
    e = db.get_enrollment(user["id"], first)
    check("passing completes course", e["status"] == "completed" and e["progress"] == 100)

    print("\n[JSON quiz API]")
    second = roadmap["roadmap"][1]["course_id"]
    c.post(f"/courses/{second}/start")
    r = c.post(f"/api/courses/{second}/generate-quiz").get_json()
    qid = r["quiz_id"]
    wait_ready(c, qid)
    import json
    quiz = json.loads(db.get_attempt(qid)["quiz"])
    answers = {str(q["id"]): q["answer"] for q in quiz}
    answers[str(quiz[0]["id"])] = "wrong"
    r = c.post(f"/api/quiz/{qid}/submit", json={"answers": answers}).get_json()
    check("submit API returns counts", r["total_questions"] == len(quiz) and r["wrong_answers"] == 1 and "%" not in str(r["percentage"]))
    results = c.get("/api/learner/results").get_json()
    check("results API lists attempts", len(results) == 3)

    print("\n[flashcards]")
    third = roadmap["roadmap"][2]["course_id"]
    r = c.get(f"/api/flashcards/{third}")
    check("deck locked before any quiz", r.status_code == 403 and r.get_json()["locked"])
    html = c.get(f"/courses/{third}/flashcards").get_data(as_text=True)
    check("locked page shown", "Take the quiz to unlock flashcards" in html)
    deck = c.get(f"/api/flashcards/{second}").get_json()
    check("deck unlocked after a quiz", deck["ok"] and len(deck["cards"]) == len(quiz))
    check("missed question comes first", deck["cards"][0]["missed"] is True
          and deck["cards"][0]["question"] == quiz[0]["question"])
    check("card back has answer", deck["cards"][0]["answer"] == quiz[0]["answer"])
    deck1 = c.get(f"/api/flashcards/{first}").get_json()["cards"]
    check("retakes deduplicated", len(deck1) == len(json.loads(db.get_attempt(attempt_id)["quiz"])))
    k0 = deck["cards"][0]["key"]
    k1 = deck["cards"][1]["key"]
    r = c.post(f"/api/flashcards/{second}/{k0}", json={"status": "know"}).get_json()
    check("mark know counts as mastered", r["ok"] and r["mastered"] == 1)
    c.post(f"/api/flashcards/{second}/{k1}", json={"status": "review"})
    order = [x["key"] for x in c.get(f"/api/flashcards/{second}").get_json()["cards"]]
    check("review-again first, known last", order[0] == k1 and order[-1] == k0)
    check("bad status rejected", c.post(f"/api/flashcards/{second}/{k0}", json={"status": "x"}).status_code == 400)
    check("unknown card rejected", c.post(f"/api/flashcards/{second}/deadbeef", json={"status": "know"}).status_code == 404)
    html = c.get("/learn/flashcards").get_data(as_text=True) + c.get("/dashboard").get_data(as_text=True)
    check("dashboard + flashcards page list decks", "My flashcard decks" in html and f"/courses/{second}/flashcards" in html and "1/" in html)
    html = c.get(f"/courses/{second}").get_data(as_text=True)
    check("course page links deck", "Study flashcards" in html)
    html = c.get(f"/courses/{second}/flashcards").get_data(as_text=True)
    check("study page renders", "fcCard" in html and "mastered" in html)

    print("\n[completed courses are not recommended again]")
    c.post("/dashboard/refresh")
    ids = [x["course_id"] for x in c.get("/api/learner/roadmap").get_json()["roadmap"]]
    check("completed course excluded after refresh", first not in ids, str(ids))

    print("\n[persistence across logout/login]")
    c.get("/logout")
    check("logged out -> dashboard redirects to login", c.get("/dashboard").status_code == 302)
    r = c.post("/login", json={"username": "rahul", "password": "nope", "role": "learner"})
    check("invalid login rejected", r.status_code == 401)
    r = c.post("/login", json={"username": "rahul", "password": "secret-123", "role": "admin"})
    check("learner cannot log in on Admin tab", r.status_code == 403)
    r = c.post("/login", json={"username": "rahul", "password": "secret-123", "role": "learner"})
    check("learner login ok", r.status_code == 200 and r.get_json()["redirect"] == "/dashboard")
    html = c.get("/learn/courses?tab=completed").get_data(as_text=True)
    title = web.courses.get(first)["title"]
    check("progress still there after re-login", "Completed" in html and title in html)
    fc = c.get(f"/api/flashcards/{second}").get_json()
    check("flashcard marks persist after re-login", fc["mastered"] == 1)

    print("\n[role-based access]")
    for url in ("/admin", "/trainer", "/recommendation", "/api/admin/overview", "/api/trainer/students"):
        check(f"learner blocked from {url}", c.get(url).status_code == 403)
    c.get("/logout")
    r = c.post("/login", json={"username": "trainer1", "password": "trainer-pass-123", "role": "trainer"})
    check("trainer login -> trainer dashboard", r.get_json()["redirect"] == "/trainer")
    check("trainer without specialisations -> onboarding", c.get("/trainer").headers.get("Location", "").endswith("/trainer/onboarding"))
    c.post("/trainer/onboarding", data={"specialisations": ["S001", "S015"]})
    page = c.get("/trainer").get_data(as_text=True)
    check("trainer gets waiting learners after onboarding", "rahul" in page and "My students" in page)
    check("trainer blocked from /admin", c.get("/admin").status_code == 403)
    check("trainer blocked from learner dashboard", c.get("/dashboard").status_code == 403)
    check("trainer blocked from flashcards", c.get(f"/api/flashcards/{first}").status_code == 403)
    check("trainer keeps original intake page", c.get("/recommendation").status_code == 200)
    c.get("/logout")
    r = c.post("/login", json={"username": "admin1", "password": "admin-pass-123", "role": "admin"})
    check("admin login -> admin dashboard", r.get_json()["redirect"] == "/admin")
    page = c.get("/admin").get_data(as_text=True)
    check("admin dashboard renders", "Admin Overview" in page and "rahul" in c.get("/admin/learners").get_data(as_text=True)
          and "trainer1" in c.get("/admin/staff").get_data(as_text=True))
    r = c.post("/admin/users", data={"username": "trainer2", "password": "trainer-pass-456", "role": "trainer"})
    check("admin creates trainer", db.get_user_by_username("trainer2")["role"] == "trainer")
    check("admin cannot open a trainer's workspace", c.get("/trainer").status_code == 403)
    c.get("/logout")
    check("anonymous API call gets 401", c.get("/api/learner/profile").status_code == 401)

    print("\n[legacy account onboarding]")
    db.create_user("olduser", web._hash_password("olduser", "oldpass"))
    r = c.post("/login", json={"username": "olduser", "password": "oldpass", "role": "learner"})
    check("legacy hash still logs in", r.status_code == 200)
    check("legacy hash upgraded", db.get_user_by_username("olduser")["password_hash"].startswith(("scrypt:", "pbkdf2:")))
    check("legacy learner sent to onboarding", c.get("/dashboard").headers.get("Location", "").endswith("/onboarding"))
    r = c.post("/onboarding", data={"email": "olduser@gov.in", "role_id": role_id,
                                    "area_of_experience": "Statistics & Surveys"})
    check("onboarding builds roadmap", r.status_code == 302 and c.get("/dashboard").status_code == 200)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print("  failed:", name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
