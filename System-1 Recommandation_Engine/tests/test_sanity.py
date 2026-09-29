"""
Stage F - the three sanity checks, exercised through the HTTP API.

  1. An employee who already meets/exceeds all requirements -> gaps empty or
     very short, recommendations empty/minimal, no error.
  2. An employee with a large gap in an uncovered skill (S014) -> explicit
     no_courses_available response, not a silent failure.
  3. A typical mixed-profile employee -> plausible recommendations (courses
     actually tagged with their gap skills rank near the top).
"""

from __future__ import annotations


def test_sanity_1_already_qualified_employee(client, employee_with_no_gaps):
    employee_id = employee_with_no_gaps

    gaps_resp = client.get(f"/employees/{employee_id}/gaps")
    assert gaps_resp.status_code == 200
    gaps = gaps_resp.json()
    assert isinstance(gaps, list)
    assert len(gaps) <= 1  # empty or at most a single small gap

    recs_resp = client.get(f"/employees/{employee_id}/recommendations")
    assert recs_resp.status_code == 200
    body = recs_resp.json()
    assert body["employee_id"] == employee_id
    assert isinstance(body["recommendations"], list)
    assert len(body["recommendations"]) <= 1  # empty or minimal
    assert isinstance(body["unmatched_gaps"], list)


def test_sanity_2_s014_gap_employee(client, employee_with_s014_gap):
    employee_id = employee_with_s014_gap

    resp = client.get(f"/employees/{employee_id}/recommendations")
    assert resp.status_code == 200
    body = resp.json()

    # The S014 gap must appear in unmatched_gaps with the exact protocol.
    s014_entries = [u for u in body["unmatched_gaps"] if u["skill_id"] == "S014"]
    assert s014_entries, "S014 gap must be reported, not silently dropped"
    entry = s014_entries[0]
    assert entry["status"] == "no_courses_available"
    assert entry["skill_name"] == "Cloud Computing"
    assert "training team" in entry["message"]

    # And no course in the response may pretend to cover S014.
    for rec in body["recommendations"]:
        assert "S014" not in rec["matched_skills"]


def test_sanity_3_typical_employee(client, typical_employee, repos, gap_service):
    employee_id = typical_employee

    gaps = gap_service.compute_gaps(employee_id)
    gap_skills = {g["skill_id"] for g in gaps}

    resp = client.get(f"/employees/{employee_id}/recommendations?top_n=5")
    assert resp.status_code == 200
    body = resp.json()

    recs = body["recommendations"]
    assert recs, "typical employee should get recommendations"

    # Every recommendation must be tagged with a real gap skill...
    for rec in recs:
        assert set(rec["matched_skills"]) & gap_skills, (
            f"recommended course {rec['course_id']} matches no gap skill"
        )
        # ...and every matched skill must actually be tagged on the course.
        course = repos.courses.get(rec["course_id"])
        assert course is not None
        course_tags = set(course["_tags"])
        assert set(rec["matched_skills"]) <= course_tags

    # Top recommendation should address the highest-priority gap (plausibility).
    top = recs[0]
    top_gap = max(gaps, key=lambda g: (g["gap"], g["priority_weight"]))
    assert top_gap["skill_id"] in top["matched_skills"] or any(
        top_gap["skill_id"] in r["matched_skills"] for r in recs[:2]
    ), "top-ranked courses should address the largest/most-priority gap"

    # Plausibility: the top-N should not be dominated by irrelevant courses.
    assert len(recs) <= 5


def test_unknown_employee_is_404(client):
    assert client.get("/employees/E9999/gaps").status_code == 404
    assert client.get("/employees/E9999/recommendations").status_code == 404
    assert client.get("/employees/E9999/profile").status_code == 404


def test_profile_endpoint(client, repos):
    employee_id = repos.employees.all()[0]["employee_id"]
    resp = client.get(f"/employees/{employee_id}/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["employee_id"] == employee_id
    assert body["name"]
    assert isinstance(body["self_rated_skills"], dict)
    assert isinstance(body["previous_trainings"], list)


def test_top_n_query_validation(client, repos):
    employee_id = repos.employees.all()[0]["employee_id"]
    assert client.get(f"/employees/{employee_id}/recommendations?top_n=0").status_code == 422
    assert client.get(f"/employees/{employee_id}/recommendations?top_n=500").status_code == 422


def test_health_endpoint(client, repos):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # The 805-person synthetic baseline never changes; Dataset-5 real
    # registrations grow the total, so do not hardcode a fixed final count.
    assert body["employees_synthetic"] == 805
    assert body["employees_real"] == len(repos.employees.all_real())
    assert body["employees_loaded"] == (
        body["employees_synthetic"] + body["employees_real"]
    )
    assert body["courses_loaded"] == 784
