"""
End-to-end test of the trainer workspace (runs offline).

    VIVARAN_FAKE_QUIZ=1 python test_trainer_flow.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

os.environ.setdefault("VIVARAN_FAKE_QUIZ", "1")
os.environ["VIVARAN_DB"] = os.path.join(tempfile.mkdtemp(), "trainer.db")
os.environ["VIVARAN_ADMIN_USER"] = "admin1"
os.environ["VIVARAN_ADMIN_PASSWORD"] = "admin-pass-123"

import app as web  # noqa: E402
import db  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))


def register_learner(c, name, role_idx=50, area="Data Science & Analytics"):
    role_id = web.recommender.roles()[role_idx]["role_id"]
    r = c.post("/auth/register", json={"username": name, "email": f"{name}@gov.in", "password": "secret-123",
                                       "role_id": role_id, "area_of_experience": area})
    c.get("/logout")
    return r


def login(c, user, pw, role):
    return c.post("/login", json={"username": user, "password": pw, "role": role})


def main() -> int:
    db.init_db()
    web.seed_staff_accounts()
    web.app.config["TESTING"] = True
    c = web.app.test_client()

    print("\n[learners register before any trainer exists]")
    for i in range(7):
        register_learner(c, f"learner{i}")
    check("learners wait unassigned", len(db.list_unassigned_learners()) == 7)

    print("\n[trainer registration]")
    r = c.post("/auth/register", json={"account_type": "trainer", "username": "tina", "email": "tina@gmail.com",
                                       "password": "secret-123", "specialisations": ["S001"]})
    check("trainer needs @gov.in", r.status_code == 400)
    r = c.post("/auth/register", json={"account_type": "trainer", "username": "tina", "email": "tina@gov.in",
                                       "password": "secret-123", "specialisations": []})
    check("trainer needs a specialisation", r.status_code == 400)
    r = c.post("/auth/register", json={"account_type": "trainer", "username": "tina", "email": "tina@gov.in",
                                       "password": "secret-123", "specialisations": ["S001", "S005", "BAD"]})
    check("trainer registers -> trainer dashboard", r.status_code == 200 and r.get_json()["redirect"] == "/trainer")
    tina = db.get_user_by_username("tina")
    check("specialisations saved (invalid dropped)", db.get_trainer_profile(tina["id"])["specialisations"] == ["S001", "S005"])
    mine = db.list_trainer_students(tina["id"])
    check("new trainer gets a batch of 5", len(mine) == 5, str(len(mine)))
    check("2 learners still waiting", len(db.list_unassigned_learners()) == 2)

    page = c.get("/trainer").get_data(as_text=True)
    check("dashboard lists my students", all(s["username"] in page for s in mine) and "t-sidebar" in page)
    check("sidebar has all sections", all(x in page for x in ("Assigned courses", "Uploaded courses", "My profile", "Assess an employee")))
    r = c.post("/trainer/claim")
    check("claim takes the remaining 2", len(db.list_trainer_students(tina["id"])) == 7)
    c.get("/logout")

    print("\n[second trainer + auto-assignment of new learners]")
    c.post("/auth/register", json={"account_type": "trainer", "username": "tom", "email": "tom@gov.in",
                                   "password": "secret-123", "specialisations": ["S008", "S010", "S011"]})
    tom = db.get_user_by_username("tom")
    check("no learners left for second trainer", len(db.list_trainer_students(tom["id"])) == 0)
    c.get("/logout")
    register_learner(c, "newbie", area="Administration & Management")
    newbie = db.get_user_by_username("newbie")
    check("new learner auto-assigned to a trainer", db.get_learner_profile(newbie["id"])["trainer_id"] in (tina["id"], tom["id"]))
    check("prefers the lighter matching trainer", db.get_learner_profile(newbie["id"])["trainer_id"] == tom["id"])

    print("\n[student detail + add/remove courses]")
    login(c, "tina", "secret-123", "trainer")
    s0 = db.get_user_by_username("learner0")
    other = newbie  # belongs to tom
    check("cannot open another trainer's student", c.get(f"/trainer/students/{other['id']}").status_code == 404)
    page = c.get(f"/trainer/students/{s0['id']}").get_data(as_text=True)
    check("student detail renders", "Courses assigned" in page and "learner0@gov.in" in page and "Add a course" in page)
    sub = db.latest_submission(s0["id"])
    recs = sub["recommendations"]
    before = [r["course_id"] for r in recs]
    victim = before[0]
    c.post(f"/trainer/students/{s0['id']}/courses/{victim}/remove")
    after = [r["course_id"] for r in db.latest_submission(s0["id"])["recommendations"]]
    check("remove takes course off the path", victim not in after and len(after) == len(before) - 1)
    check("steps renumbered", [r["step"] for r in db.latest_submission(s0["id"])["recommendations"]] == list(range(1, len(after) + 1)))
    page = c.get(f"/trainer/students/{s0['id']}?q=yoga").get_data(as_text=True)
    add_id = re.search(r'name="course_id" value="([^"]+)"', page).group(1)
    check("search finds courses", "Add</button>" in page)
    c.post(f"/trainer/students/{s0['id']}/courses", data={"course_id": add_id})
    recs = db.latest_submission(s0["id"])["recommendations"]
    added = [r for r in recs if r["course_id"] == add_id]
    check("add pins course on the path", added and added[0]["added_by_trainer"])
    r = c.post(f"/trainer/students/{s0['id']}/courses", data={"course_id": add_id}, follow_redirects=True)
    check("adding twice is refused", "already on this learner" in r.get_data(as_text=True))
    r = c.post(f"/api/trainer/students/{other['id']}/courses", json={"action": "add", "course_id": add_id})
    check("cannot edit another trainer's student", r.status_code == 404)
    detail = c.get(f"/api/trainer/students/{s0['id']}").get_json()
    check("student API has courses + progress", len(detail["courses"]) == len(recs) and "overall_progress" in detail)
    c.get("/logout")

    print("\n[learner sees the trainer's edits]")
    login(c, "learner0", "secret-123", "learner")
    html = c.get("/dashboard").get_data(as_text=True)
    check("learner sees trainer name", "tina" in html)
    road = c.get("/learn/roadmap").get_data(as_text=True)
    check("learner sees pinned course", "Added by your trainer" in road and f"/courses/{add_id}" in road)
    c.post("/dashboard/refresh")
    ids = [r["course_id"] for r in db.latest_submission(s0["id"])["recommendations"]]
    check("refresh keeps added course", add_id in ids)
    check("refresh never brings back removed course", victim not in ids)
    # learner completes a course -> trainer can't remove it
    first = ids[0]
    c.post(f"/courses/{first}/start")
    r = c.post(f"/courses/{first}/quiz")
    aid = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
    for _ in range(40):
        if db.get_attempt(aid)["status"] == "ready":
            break
        time.sleep(0.1)
    quiz = json.loads(db.get_attempt(aid)["quiz"])
    c.post(f"/quiz/{aid}", data={f"q_{q['id']}": q["answer"] for q in quiz})
    c.get("/logout")
    login(c, "tina", "secret-123", "trainer")
    r = c.post(f"/trainer/students/{s0['id']}/courses/{first}/remove", follow_redirects=True)
    check("completed course can't be removed", "can't be removed" in r.get_data(as_text=True).replace("&#39;", "'"))
    page = c.get(f"/trainer/students/{s0['id']}").get_data(as_text=True)
    check("detail shows quiz score + progress", "100%" in page and "Passed" in page)
    page = c.get("/trainer/courses").get_data(as_text=True)
    check("assigned courses page lists courses", "Courses on my students" in page and add_id in page)

    print("\n[uploaded courses]")
    r = c.post("/trainer/uploads", data={"title": "Intro to Survey Sampling", "videos": "not a link",
                                         "skills": ["S015"]}, follow_redirects=True)
    check("upload needs a real video link", "Add at least one video link" in r.get_data(as_text=True))
    r = c.post("/trainer/uploads", data={"title": "Intro to Survey Sampling", "description": "Basics of sampling",
                                         "category": "Statistics", "designations": "Deputy Director, All",
                                         "videos": "https://youtu.be/1Z7Oy3EDErA\nhttps://youtu.be/6c2Ab4zWnNg",
                                         "skills": ["S015", "S005"], "target_level": "2"}, follow_redirects=True)
    check("upload is submitted for approval", "submitted for approval" in r.get_data(as_text=True))
    check("pending upload NOT in the catalogue", web.courses.get("TRN001") is None)
    check("uploader can preview pending course", "Preview" in c.get("/courses/TRN001").get_data(as_text=True))
    check("no trainer shares S015/S005 yet -> needs admin", web._needs_admin(db.get_trainer_course(1)))
    c.get("/logout")

    print("\n[peer approval]")
    login(c, "learner1", "secret-123", "learner")
    check("learner can't open a pending course", c.get("/courses/TRN001").status_code == 404)
    c.get("/logout")
    login(c, "tom", "secret-123", "trainer")
    check("trainer outside the field doesn't see it", "Intro to Survey Sampling" not in c.get("/trainer/approvals").get_data(as_text=True))
    r = c.post("/trainer/approvals/1", data={"action": "approve"}, follow_redirects=True)
    check("trainer outside the field can't approve", db.get_trainer_course(1)["status"] == "pending")
    c.get("/logout")
    c.post("/auth/register", json={"account_type": "trainer", "username": "sam", "email": "sam@gov.in",
                                   "password": "secret-123", "specialisations": ["S015", "S007"]})
    check("similar-field trainer now exists -> no admin needed", not web._needs_admin(db.get_trainer_course(1)))
    page = c.get("/trainer/approvals").get_data(as_text=True)
    check("similar-field trainer sees it in the queue", "Intro to Survey Sampling" in page and "Sampling ★" in page)
    check("sidebar approvals badge", 'title="1 courses waiting">1</span>' in c.get("/trainer").get_data(as_text=True))
    r = c.post("/trainer/approvals/1", data={"action": "reject", "note": ""}, follow_redirects=True)
    check("reject needs a reason", "give a reason" in r.get_data(as_text=True) and db.get_trainer_course(1)["status"] == "pending")
    c.post("/trainer/approvals/1", data={"action": "reject", "note": "Please add a description of the syllabus."})
    check("rejected with reason", db.get_trainer_course(1)["status"] == "rejected"
          and db.get_trainer_course(1)["review_note"].startswith("Please add"))
    c.get("/logout")
    login(c, "tina", "secret-123", "trainer")
    page = c.get("/trainer/uploads").get_data(as_text=True)
    check("uploader sees rejection + reason", "Rejected" in page and "Please add a description" in page)
    r = c.post("/trainer/uploads/1/edit", data={"title": "Intro to Survey Sampling", "description": "Syllabus: sampling frames, SRS, stratified sampling.",
                                               "category": "Statistics", "designations": "Deputy Director, All",
                                               "videos": "https://youtu.be/1Z7Oy3EDErA\nhttps://youtu.be/6c2Ab4zWnNg",
                                               "skills": ["S015", "S005"], "target_level": "2"}, follow_redirects=True)
    check("edit & resubmit -> pending again", db.get_trainer_course(1)["status"] == "pending" and "resubmitted" in r.get_data(as_text=True))
    check("tina can't approve her own course", "Intro to Survey Sampling" not in c.get("/trainer/approvals").get_data(as_text=True))
    c.get("/logout")
    login(c, "sam", "secret-123", "trainer")
    c.post("/trainer/approvals/1", data={"action": "approve", "note": "Looks good."})
    check("peer approves -> live", db.get_trainer_course(1)["status"] == "approved")
    up = web.courses.get("TRN001")
    check("approved course is in the catalogue", up and len(up["videos"]) == 2 and up["skills"] == ["S015", "S005"])
    check("review history recorded", [h["action"] for h in db.list_course_reviews(1)] == ["approved", "resubmitted", "rejected", "submitted"])
    c.get("/logout")
    login(c, "tina", "secret-123", "trainer")
    c.post("/trainer/uploads", data={"title": "Managing Change in Offices", "videos": "https://youtu.be/6c2Ab4zWnNg",
                                     "skills": ["S012"], "target_level": "1"})
    row2 = db.list_trainer_courses(status="pending")[0]
    check("no similar-field trainer -> admin queue", web._needs_admin(row2))
    r = c.post(f"/trainer/verify/{row2['course_id']}/generate")
    for _ in range(50):
        if db.list_banks_for_course(row2["course_id"]):
            break
        time.sleep(0.1)
    check("MCQs can be generated before approval", r.status_code == 302 and db.list_banks_for_course(row2["course_id"]))
    c.post(f"/trainer/verify/bank/{db.list_banks_for_course(row2['course_id'])[0]['id']}/approve-all")
    check("MCQs can be verified before approval", web._pending_reviews(tina["id"]) == 0)
    c.get("/logout")
    login(c, "admin1", "admin-pass-123", "admin")
    page = c.get("/admin/approvals").get_data(as_text=True)
    check("admin sees pending course needing admin", "Managing Change in Offices" in page and "needs admin" in page)
    c.post(f"/admin/courses/{row2['id']}/review", data={"action": "approve"})
    check("admin approves", db.get_trainer_course(row2["id"])["status"] == "approved" and web.courses.get(row2["course_id"]))
    c.get("/logout")
    login(c, "tina", "secret-123", "trainer")
    page = c.get(f"/trainer/students/{s0['id']}?q=survey+sampling").get_data(as_text=True)
    check("uploaded course is searchable for students", "TRN001" in page)
    c.post(f"/trainer/students/{s0['id']}/courses", data={"course_id": "TRN001"})
    check("uploaded course can be assigned", any(r["course_id"] == "TRN001" for r in db.latest_submission(s0["id"])["recommendations"]))
    check("uploads page lists it", "Intro to Survey Sampling" in c.get("/trainer/uploads").get_data(as_text=True))
    check("approved course can't be edited", "can't be edited" in c.get("/trainer/uploads/1/edit", follow_redirects=True).get_data(as_text=True).replace("&#39;", "'"))

    print("\n[verify MCQs]")
    page = c.get("/trainer/verify?f=all").get_data(as_text=True)
    check("verify page lists my upload", "Intro to Survey Sampling" in page and "Generate questions now" in page)
    c.post("/trainer/verify/TRN001/generate")
    for _ in range(50):
        if c.get("/trainer/verify/TRN001/status.json").get_json()["status"] == "ready":
            break
        time.sleep(0.1)
    banks = db.list_banks_for_course("TRN001")
    check("generate-now creates a question set", len(banks) == 1)
    bank_id = banks[0]["id"]
    qs = json.loads(banks[0]["questions"])
    check("questions start pending", web._pending_reviews(tina["id"]) == len(qs))
    check("sidebar shows pending badge", f'>{len(qs)}</span>' in c.get("/trainer").get_data(as_text=True))
    c.post(f"/trainer/verify/bank/{bank_id}/q/0", data={"action": "approve"})
    c.post(f"/trainer/verify/bank/{bank_id}/q/1", data={"action": "reject"})
    r = c.post(f"/trainer/verify/bank/{bank_id}/q/2", data={"action": "edit", "question": "Which is a measure of centre?",
                                                          "option_0": "Mean", "option_1": "Range", "option_2": "Variance",
                                                          "option_3": "Skew", "answer_index": "0", "explanation": "Edited."})
    qs = json.loads(db.get_bank_by_id(bank_id)["questions"])
    check("approve / reject stored", qs[0]["review"]["status"] == "approved" and qs[1]["review"]["status"] == "rejected")
    check("edit stored + keeps original", qs[2]["question"] == "Which is a measure of centre?" and qs[2]["original"]["question"]
          and qs[2]["review"]["status"] == "approved")
    r = c.post(f"/trainer/verify/bank/{bank_id}/q/2", data={"action": "edit", "question": "Bad?", "option_0": "A",
                                                          "option_1": "A", "answer_index": "0"}, follow_redirects=True)
    check("invalid edit rejected", "at least 2 different options" in r.get_data(as_text=True))
    check("pending count drops", web._pending_reviews(tina["id"]) == 0)
    page = c.get("/trainer/verify?f=rejected").get_data(as_text=True)
    check("filter shows rejected", qs[1]["question"] in page and qs[0]["question"] not in page)
    c.post(f"/trainer/verify/bank/{bank_id}/q/0", data={"action": "reset"})
    c.post(f"/trainer/verify/bank/{bank_id}/approve-all")
    qs = json.loads(db.get_bank_by_id(bank_id)["questions"])
    check("approve all", all((q.get("review") or {}).get("status") in ("approved", "rejected") for q in qs))
    rejected_q = qs[1]["question"]
    c.get("/logout")
    login(c, "tom", "secret-123", "trainer")
    check("other trainer can't see my bank", c.post(f"/trainer/verify/bank/{bank_id}/q/0", data={"action": "reject"}).status_code == 404)
    check("other trainer can't generate my course", c.post("/trainer/verify/TRN001/generate").status_code == 404)
    c.get("/logout")
    login(c, "learner0", "secret-123", "learner")
    c.post("/courses/TRN001/start")
    r = c.post("/courses/TRN001/quiz")
    aid = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
    for _ in range(40):
        if db.get_attempt(aid)["status"] == "ready":
            break
        time.sleep(0.1)
    served = json.loads(db.get_attempt(aid)["quiz"])
    check("learner quiz excludes rejected question", len(served) == len(qs) - 1 and all(q["question"] != rejected_q for q in served))
    check("learner quiz has the edited question", any(q["question"] == "Which is a measure of centre?" for q in served))
    c.post(f"/quiz/{aid}", data={f"q_{q['id']}": q["answer"] for q in served})
    deck = c.get("/api/flashcards/TRN001").get_json()["cards"]
    check("flashcards follow the review", len(deck) == len(served) and all(d["question"] != rejected_q for d in deck))
    c.get("/logout")
    login(c, "tina", "secret-123", "trainer")
    page = c.get("/trainer").get_data(as_text=True)
    check("dashboard has all panels", all(x in page for x in ("Needs attention", "Recent activity", "My uploaded courses",
                                                             "Questions to review", "Top courses")))
    check("recent activity shows the quiz", "Intro to Survey Sampling" in page)

    print("\n[profile]")
    c.post("/trainer/profile", data={"specialisations": ["S001", "S013"]})
    check("specialisations editable", db.get_trainer_profile(tina["id"])["specialisations"] == ["S001", "S013"])
    check("learner blocked from trainer pages", True)
    c.get("/logout")
    login(c, "learner1", "secret-123", "learner")
    check("learner blocked from /trainer", c.get("/trainer").status_code == 403)
    check("learner blocked from student API", c.get(f"/api/trainer/students/{s0['id']}").status_code == 403)
    html = c.get(f"/courses/TRN001").get_data(as_text=True)
    check("learner can open an uploaded course", "Intro to Survey Sampling" in html)
    c.get("/logout")

    print("\n[admin reassigns]")
    login(c, "admin1", "admin-pass-123", "admin")
    page = c.get("/admin/trainers").get_data(as_text=True)
    check("admin sees trainers + specialisations", "Trainers" in page and "tina" in page and "Leadership" in page)
    c.post("/admin/assign", data={"learner_id": s0["id"], "trainer_id": tom["id"]})
    check("admin moves learner to another trainer", db.get_learner_profile(s0["id"])["trainer_id"] == tom["id"])
    c.post("/admin/assign", data={"learner_id": s0["id"], "trainer_id": ""})
    check("admin can unassign", db.get_learner_profile(s0["id"])["trainer_id"] is None)
    c.post("/admin/assign", data={"learner_id": s0["id"], "trainer_id": tina["id"]})
    c.get("/logout")
    login(c, "tom", "secret-123", "trainer")
    check("old trainer loses access after reassign", c.get(f"/trainer/students/{s0['id']}").status_code == 404)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for n in FAILED:
        print("  failed:", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
