"""
End-to-end test of the admin workspace (runs offline).

    VIVARAN_FAKE_QUIZ=1 python test_admin_flow.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

os.environ.setdefault("VIVARAN_FAKE_QUIZ", "1")
os.environ["VIVARAN_DB"] = os.path.join(tempfile.mkdtemp(), "admin.db")
os.environ["VIVARAN_ADMIN_USER"] = "chief"
os.environ["VIVARAN_ADMIN_PASSWORD"] = "chief-pass-123"

import app as web  # noqa: E402
import db  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))


def login(c, user, pw, role):
    c.get("/logout")
    return c.post("/login", json={"username": user, "password": pw, "role": role})


def main() -> int:
    db.init_db()
    web.seed_staff_accounts()
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    roles = web.recommender.roles()
    chief = db.get_user_by_username("chief")
    check("env admin is the main admin", chief["is_primary"] == 1)

    # data: 2 trainers, 6 learners, one quiz each for two learners
    for name, specs in (("tina", ["S001", "S005", "S015"]), ("tom", ["S008", "S010"])):
        c.post("/auth/register", json={"account_type": "trainer", "username": name, "email": f"{name}@gov.in",
                                       "password": "secret-123", "specialisations": specs})
        c.get("/logout")
    for i in range(6):
        c.post("/auth/register", json={"username": f"lrn{i}", "email": f"lrn{i}@gov.in", "password": "secret-123",
                                       "role_id": roles[10 + i * 7]["role_id"], "area_of_experience": "Data Science & Analytics"})
        if i < 2:
            cid = db.latest_submission(db.get_user_by_username(f"lrn{i}")["id"])["recommendations"][0]["course_id"]
            c.post(f"/courses/{cid}/start")
            r = c.post(f"/courses/{cid}/quiz")
            aid = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
            for _ in range(40):
                if db.get_attempt(aid)["status"] == "ready":
                    break
                time.sleep(0.1)
            quiz = json.loads(db.get_attempt(aid)["quiz"])
            c.post(f"/quiz/{aid}", data={f"q_{q['id']}": (q["answer"] if i == 0 else "nope") for q in quiz})
        c.get("/logout")

    print("\n[access]")
    login(c, "lrn0", "secret-123", "learner")
    for url in ("/admin", "/admin/learners", "/admin/staff", "/admin/reports/learners.csv", "/admin/audit"):
        check(f"learner blocked from {url}", c.get(url).status_code == 403)
    login(c, "tina", "secret-123", "trainer")
    check("trainer blocked from /admin/trainers", c.get("/admin/trainers").status_code == 403)

    print("\n[overview + sections render]")
    r = login(c, "chief", "chief-pass-123", "admin")
    check("admin login -> /admin", r.get_json()["redirect"] == "/admin")
    page = c.get("/admin").get_data(as_text=True)
    check("overview has charts + panels", all(x in page for x in ("Registrations", "Quiz outcomes", "Learners by department",
                                                               "Quiz pass rate", "Needs action", "Recent activity")))
    check("sidebar has all 10 sections", all(x in page for x in ("Learners", "Trainers", "Course approvals", "Skill-gap analytics",
                                                              "Courses", "Quiz quality", "Reports", "Audit log", "Staff accounts")))
    for url in ("/admin/learners", "/admin/trainers", "/admin/approvals", "/admin/skill-gaps",
                "/admin/skill-gaps?segment=platform", "/admin/courses", "/admin/courses?source=in_use",
                "/admin/quality", "/admin/reports", "/admin/audit", "/admin/staff"):
        check(f"{url} renders", c.get(url).status_code == 200)

    print("\n[learners]")
    l0 = db.get_user_by_username("lrn0")
    done_page = c.get("/admin/learners?status=completed").get_data(as_text=True)
    check("filter by status", "<b>lrn0</b>" in done_page and "<b>lrn3</b>" not in done_page)
    sp = c.get("/admin/learners?q=lrn2").get_data(as_text=True)
    check("search", "<b>lrn2</b>" in sp and "<b>lrn1</b>" not in sp)
    page = c.get(f"/admin/learners/{l0['id']}").get_data(as_text=True)
    check("learner detail", "Courses" in page and "Quiz results" in page and "Reset password" in page)
    recs = db.latest_submission(l0["id"])["recommendations"]
    victim = recs[-1]["course_id"]
    c.post(f"/admin/learners/{l0['id']}/courses/{victim}/remove")
    check("admin removes a course", victim not in [r["course_id"] for r in db.latest_submission(l0["id"])["recommendations"]])
    c.post(f"/admin/learners/{l0['id']}/courses", data={"course_id": victim})
    check("admin adds a course", victim in [r["course_id"] for r in db.latest_submission(l0["id"])["recommendations"]])
    tom = db.get_user_by_username("tom")
    c.post("/admin/assign", data={"learner_id": l0["id"], "trainer_id": tom["id"], "next": f"/admin/learners/{l0['id']}"})
    check("admin changes trainer", db.get_learner_profile(l0["id"])["trainer_id"] == tom["id"])

    print("\n[trainers: balance + deactivate]")
    tina = db.get_user_by_username("tina")
    for l in db.list_learners_with_profiles():
        db.set_learner_trainer(l["id"], tina["id"])  # everyone on tina
    c.post("/admin/trainers/balance")
    loads = sorted(len(db.list_trainer_students(t)) for t in (tina["id"], tom["id"]))
    check("auto-balance evens the load", loads == [3, 3], str(loads))
    db.set_learner_trainer(l0["id"], None)
    c.post("/admin/trainers/assign-unassigned")
    check("assign unassigned", db.get_learner_profile(l0["id"])["trainer_id"] is not None)
    page = c.get(f"/admin/trainers/{tina['id']}").get_data(as_text=True)
    check("trainer detail", "Students" in page and "Uploaded courses" in page)
    tina_students = [s["id"] for s in db.list_trainer_students(tina["id"])]
    c.post(f"/admin/users/{tina['id']}/active", data={"active": "0", "next": "/admin/trainers"})
    check("trainer deactivated", db.get_user_by_id(tina["id"])["active"] == 0)
    check("their students moved to active trainers",
          all(db.get_learner_profile(s)["trainer_id"] == tom["id"] for s in tina_students))
    r = login(c, "tina", "secret-123", "trainer")
    check("deactivated trainer can't log in", r.status_code == 403 and "deactivated" in r.get_json()["error"])
    login(c, "chief", "chief-pass-123", "admin")
    c.post(f"/admin/users/{tina['id']}/active", data={"active": "1"})
    check("trainer reactivated", db.get_user_by_id(tina["id"])["active"] == 1)
    r = c.post(f"/admin/users/{chief['id']}/active", data={"active": "0"}, follow_redirects=True)
    check("admin can't deactivate self", db.get_user_by_id(chief["id"])["active"] == 1)

    print("\n[password reset + change]")
    r = c.post(f"/admin/users/{l0['id']}/reset-password", data={"next": "/admin/staff"})
    temp = re.search(r'id="tempPw">([^<]+)<', r.get_data(as_text=True)).group(1)
    check("reset shows a temporary password once", len(temp) >= 8)
    r = login(c, "lrn0", temp, "learner")
    check("temp password login -> must change", r.get_json()["redirect"] == "/account/password")
    check("other pages blocked until changed", c.get("/dashboard").headers.get("Location", "").endswith("/account/password"))
    r = c.post("/account/password", data={"current": temp, "new": "brand-new-9", "confirm": "brand-new-9"})
    check("new password set", r.status_code == 302 and c.get("/dashboard").status_code == 200)
    r = login(c, "lrn0", "brand-new-9", "learner")
    check("login with new password", r.status_code == 200)
    r = c.post("/account/password", data={"current": "wrong", "new": "x-another-1", "confirm": "x-another-1"})
    check("change needs current password", "not correct" in r.get_data(as_text=True))

    print("\n[admins by email: max 3, main admin only]")
    login(c, "chief", "chief-pass-123", "admin")
    r = c.post("/admin/admins", data={"email": "bad@gmail.com"}, follow_redirects=True)
    check("admin email must be @gov.in", "government email" in r.get_data(as_text=True))
    r = c.post("/admin/admins", data={"email": "asha@gov.in"})
    temp_a = re.search(r'id="tempPw">([^<]+)<', r.get_data(as_text=True)).group(1)
    asha = db.get_user_by_email("asha@gov.in")
    check("admin #2 created from email", asha and asha["role"] == "admin" and asha["username"] == "asha" and asha["must_change_password"])
    c.post("/admin/admins", data={"email": "ravi@gov.in"})
    r = c.post("/admin/admins", data={"email": "third@gov.in"}, follow_redirects=True)
    check("max 3 admins enforced", db.get_user_by_email("third@gov.in") is None and "maximum" in r.get_data(as_text=True))
    r = login(c, "asha", temp_a, "admin")
    check("new admin must set password", r.get_json()["redirect"] == "/account/password")
    c.post("/account/password", data={"current": temp_a, "new": "asha-pass-99", "confirm": "asha-pass-99"})
    check("new admin reaches admin workspace", c.get("/admin").status_code == 200)
    r = c.post("/admin/admins", data={"email": "x2@gov.in"}, follow_redirects=True)
    check("only main admin can add admins", "Only the main admin" in r.get_data(as_text=True))
    ravi = db.get_user_by_email("ravi@gov.in")
    c.post(f"/admin/admins/{ravi['id']}/remove")
    check("non-main admin can't remove admins", db.get_user_by_id(ravi["id"]) is not None)
    c.post(f"/admin/users/{chief['id']}/active", data={"active": "0"})
    check("main admin can't be deactivated", db.get_user_by_id(chief["id"])["active"] == 1)
    login(c, "chief", "chief-pass-123", "admin")
    c.post(f"/admin/admins/{ravi['id']}/remove")
    check("main admin removes an admin", db.get_user_by_id(ravi["id"]) is None)

    print("\n[courses: unpublish]")
    cid = db.latest_submission(db.get_user_by_username("lrn3")["id"])["recommendations"][0]["course_id"]
    c.post(f"/admin/courses/{cid}/publish", data={"publish": "0"})
    check("course unpublished", cid in db.unpublished_courses())
    login(c, "lrn3", "secret-123", "learner")
    c.post("/dashboard/refresh")
    check("unpublished course no longer recommended",
          cid not in [r["course_id"] for r in db.latest_submission(db.get_user_by_username("lrn3")["id"])["recommendations"]])
    login(c, "chief", "chief-pass-123", "admin")
    page = c.get("/admin/courses?source=unpublished").get_data(as_text=True)
    check("unpublished filter lists it", cid in page)
    c.post(f"/admin/courses/{cid}/publish", data={"publish": "1"})
    check("course published again", cid not in db.unpublished_courses())

    print("\n[skill gaps, quality, reports, audit]")
    g = web._gap_analysis("workforce")
    check("workforce gap analysis over Dataset-3", g["n"] > 700 and g["skills"][0]["people"] > 0)
    check("uncovered gap detected (Cloud Computing has no course)", any(r["skill_id"] == "S014" for r in g["uncovered"]))
    page = c.get("/admin/skill-gaps").get_data(as_text=True)
    check("skill-gap page shows heatmap + uncovered", "Gaps by department" in page and "no course in the catalogue" in page)
    page = c.get("/admin/quality").get_data(as_text=True)
    check("quality page shows pass rates + missed questions", "Pass rate by course" in page and "Most-missed questions" in page)
    for name in ("learners", "quiz_results", "enrollments", "coverage", "trainers"):
        r = c.get(f"/admin/reports/{name}.csv")
        check(f"{name}.csv downloads", r.status_code == 200 and r.mimetype == "text/csv" and len(r.get_data(as_text=True).splitlines()) >= 2)
    check("unknown report 404", c.get("/admin/reports/secret.csv").status_code == 404)
    actions = db.audit_actions()
    check("audit log records admin actions", all(a in actions for a in ("learner_reassigned", "trainers_auto_balanced",
                                                                        "account_deactivated", "password_reset", "admin_added",
                                                                        "admin_removed", "course_unpublished", "report_downloaded")))
    page = c.get("/admin/audit?action=password_reset").get_data(as_text=True)
    check("audit filter", "password reset" in page and "admin added" not in page)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for n in FAILED:
        print("  failed:", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
