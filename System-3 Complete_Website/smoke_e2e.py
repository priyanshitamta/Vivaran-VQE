"""
Offline end-to-end smoke test for System 3.

Runs the full user journey (register -> login -> intake -> recommendations ->
quiz start -> take -> results -> profile) against a Flask test client with
System 1's client functions stubbed and quiz generation canned
(VIVARAN_FAKE_QUIZ=1). No servers, network, or LLM needed.

Run with the System 2 venv python:
    ".venv/Scripts/python.exe" smoke_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# ---- env must be set before importing app/db/settings --------------------
TMP = tempfile.mkdtemp(prefix="vivaran_smoke_")
os.environ["VIVARAN_DB"] = os.path.join(TMP, "vivaran.db")
os.environ["VIVARAN_FAKE_QUIZ"] = "1"
os.environ["VIVARAN_SECRET"] = "smoke-test-secret"

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import s1_client  # noqa: E402
from app import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


# ---------------------------------------------------------------------------
# Stub System 1 (we only exercise S3's own logic here)
# ---------------------------------------------------------------------------
def _meta_roles():
    return [
        {"role_id": "R001", "designation": "Junior Statistical Officer", "department": "Ministry of Statistics"},
        {"role_id": "R002", "designation": "Data Analyst", "department": "Ministry of Statistics"},
    ]


def _meta_skills():
    return [
        {"skill_id": "SK001", "skill_name": "Statistical Methods"},
        {"skill_id": "SK002", "skill_name": "Data Analysis"},
    ]


def _meta_courses():
    return [{"course_id": "C001", "course_title": "Intro to Statistics"},
            {"course_id": "C002", "course_title": "Python for Data"}]


def _meta_unique():
    return {
        "current_assignment": ["Census 2021", "NAD"],
        "educational_qualifications": ["M.Sc Statistics", "B.E."],
    }


def _register_employee(payload, top_n=5):
    # Echo the payload like System 1 does, plus fixed gaps/recommendations.
    employee = {
        "employee_id": "E999",
        "name": payload["name"],
        "role_id": payload["role_id"],
        "designation": payload["designation"],
        "department": payload["department"],
        "current_assignment": payload.get("current_assignment"),
        "educational_qualifications": payload.get("educational_qualifications"),
        "work_experience_years": payload.get("work_experience_years"),
        "previous_trainings": payload.get("previous_trainings") or [],
        "self_rated_skills": payload.get("self_rated_skills") or {},
        "quiz_verified_skills": {},
    }
    recommendations = [
        {
            "course_id": "C001", "course_title": "Intro to Statistics",
            "description": "Foundations of applied statistics.", "target_level": 3,
            "duration_minutes": 120, "mode": "Self-paced", "provider": "iGOT",
            "language": "English", "matched_skills": ["SK001"], "score": 9.5,
        },
        {
            "course_id": "C002", "course_title": "Python for Data",
            "description": "Hands-on Python for analysis.", "target_level": 2,
            "duration_minutes": 180, "mode": "Self-paced", "provider": "iGOT",
            "language": "English", "matched_skills": ["SK002"], "score": 8.0,
        },
    ]
    return {"employee": employee, "gaps": [], "recommendations": recommendations, "unmatched_gaps": []}


s1_client.meta_roles = _meta_roles
s1_client.meta_skills = _meta_skills
s1_client.meta_courses_light = _meta_courses
s1_client.meta_unique_values = _meta_unique
s1_client.register_employee = _register_employee


def main() -> int:
    global PASS, FAIL
    db.init_db()
    client = app.test_client()

    # 1. Landing -------------------------------------------------------------
    r = client.get("/")
    check("landing serves Vivaran-VQE copy", r.status_code == 200 and "Vivaran-VQE" in r.get_data(as_text=True))

    # 2. Register ------------------------------------------------------------
    r = client.post("/api/register", json={"username": "demo", "password": "secret"})
    check("register returns ok", r.status_code == 200 and r.get_json().get("ok"))
    r = client.post("/api/register", json={"username": "demo", "password": "x"})
    check("duplicate register rejected", r.status_code == 409)

    # 3. Login (bad then good) -----------------------------------------------
    r = client.post("/login", json={"username": "demo", "password": "wrong"})
    check("bad password rejected", r.status_code == 401
          and "Incorrect password. Please try again." in r.get_json()["error"])
    r = client.post("/login", json={"username": "demo", "password": "secret"})
    check("good login redirects", r.status_code == 200 and r.get_json().get("redirect") == "/recommendation")

    # 4. Intake page ---------------------------------------------------------
    r = client.get("/recommendation")
    html = r.get_data(as_text=True)
    check("intake page renders", r.status_code == 200 and "iGOT Recommendation" in html)
    check("intake embeds metadata", "Statistical Methods" in html)

    # 5. Register an employee (relayed -> S3 saves a snapshot) --------------
    payload = {
        "name": "Aarav", "role_id": "R001",
        "designation": "Junior Statistical Officer", "department": "Ministry of Statistics",
        "current_assignment": "Census 2021", "educational_qualifications": "M.Sc Statistics",
        "work_experience_years": 6, "previous_trainings": ["C002"],
        "self_rated_skills": {"SK001": 1},
    }
    r = client.post("/recommendation", json=payload)
    data = r.get_json()
    check("employee registered", r.status_code == 200 and data.get("ok"))
    results_url = data["redirect"]
    submission_id = int(results_url.rstrip("/").split("/")[-1])

    # 6. Results page --------------------------------------------------------
    r = client.get(results_url)
    html = r.get_data(as_text=True)
    check("results show employee id", "E999" in html)
    check("results list top-5 courses", "Intro to Statistics" in html and "Python for Data" in html)
    check("results show Take a quiz", "Take a quiz" in html)
    check("results page has no back button", "nav-back" not in html)

    # 7. Start quiz (fake pipeline ready in background) -----------------------
    r = client.post("/quiz/start", data={"submission_id": submission_id, "course_id": "C001"})
    check("quiz start redirects", r.status_code == 302 and "/quiz/" in r.headers.get("Location", ""))
    attempt_id = int(r.headers["Location"].split("/")[-1])

    for _ in range(40):
        st = client.get(f"/quiz/{attempt_id}/status.json").get_json()
        if st["status"] == "ready":
            break
        time.sleep(0.05)
    else:
        st = {"status": "timeout"}
    check("quiz becomes ready", st["status"] == "ready")

    # 8. Take the quiz -------------------------------------------------------
    attempt = db.get_attempt(attempt_id)
    quiz = json.loads(attempt["quiz"])
    answers = {f"q_{q['id']}": q["answer"] for q in quiz}
    r = client.get(f"/quiz/{attempt_id}")
    check("quiz page renders questions", r.status_code == 200
          and f"Q1." in r.get_data(as_text=True))
    r = client.post(f"/quiz/{attempt_id}", data=answers)
    check("quiz graded -> redirect to results", r.status_code == 302 and "results" in r.headers["Location"])

    # 9. Results page --------------------------------------------------------
    r = client.get(r.headers["Location"])
    html = r.get_data(as_text=True)
    check("quiz results show full score", f"{len(quiz)}/{len(quiz)}" in html)
    check("one-attempt notice shown", "can only be attempted once per" in html)

    # 10. Profile ------------------------------------------------------------
    r = client.get("/profile")
    html = r.get_data(as_text=True)
    check("profile greets user", "Hi, demo!" in html)
    check("profile lists employee", "Aarav" in html)
    check("profile shows quiz score", "Quiz:" in html and f"{len(quiz)}/{len(quiz)}" in html)
    # New: an un-attempted course (C002) offers "Take a quiz" from the profile,
    # while the attempted course (C001) shows its score instead.
    check("profile offers Take a quiz for un-attempted course", "Take a quiz" in html)
    # New: the back button renders on the profile only.
    check("profile shows back button", "nav-back" in html)

    # 11. Logout --------------------------------------------------------------
    r = client.get("/logout")
    check("logout returns to landing", r.status_code == 302 and r.headers.get("Location", "").endswith("/"))

    print(f"\n{'-'*46}\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
