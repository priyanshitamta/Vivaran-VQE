"""
Tests for the on-the-fly compute endpoint (Approach A new-employee intake).

POST /employees/compute takes a full employee profile that need not exist
in Dataset-3, runs the same Stage B/C/D pipeline as the lookup endpoints,
and returns the profile echo + gaps + recommendations + unmatched gaps.

The key guarantee: for the same inputs, the on-the-fly path produces
byte-identical output to the stored-employee path.
"""

from __future__ import annotations


def _role_with_skill(repos, skill_id: str) -> str:
    for role_id in repos.roles.all_roles():
        reqs = repos.roles.get_requirements(role_id)
        if any(r["skill_id"] == skill_id for r in reqs):
            return role_id
    raise AssertionError(f"no role requires {skill_id}")


def test_compute_echoes_full_profile(client, repos):
    role_id = repos.roles.all_roles()[0]
    payload = {
        "employee_id": "E-NEW-001",
        "name": "Aarti Sharma",
        "role_id": role_id,
        "self_rated_skills": {"S001": 2, "S002": 3},
        "quiz_verified_skills": {"S001": 2},
        "previous_trainings": ["C001"],
    }
    resp = client.post("/employees/compute", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    emp = body["employee"]
    assert emp["employee_id"] == "E-NEW-001"
    assert emp["name"] == "Aarti Sharma"
    assert emp["role_id"] == role_id
    assert emp["self_rated_skills"] == {"S001": 2, "S002": 3}
    assert emp["quiz_verified_skills"] == {"S001": 2}
    assert emp["previous_trainings"] == ["C001"]

    # designation/department fall back to the role's own values.
    meta = repos.roles.get_meta(role_id)
    assert emp["designation"] == meta["designation"]
    assert emp["department"] == meta["department"]

    assert isinstance(body["gaps"], list)
    assert isinstance(body["recommendations"], list)
    assert isinstance(body["unmatched_gaps"], list)


def test_compute_employee_id_defaults_when_absent(client, repos):
    role_id = repos.roles.all_roles()[0]
    resp = client.post(
        "/employees/compute",
        json={"role_id": role_id, "self_rated_skills": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["employee"]["employee_id"] == f"NEW-{role_id}"


def test_compute_already_qualified_no_gaps_no_recs(client, repos):
    role_id = repos.roles.all_roles()[0]
    reqs = repos.roles.get_requirements(role_id)
    payload = {
        "name": "Fully Qualified",
        "role_id": role_id,
        "self_rated_skills": {r["skill_id"]: r["required_level"] for r in reqs},
    }
    resp = client.post("/employees/compute", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["gaps"] == []
    assert body["recommendations"] == []
    assert body["unmatched_gaps"] == []


def test_compute_quiz_verified_precedence(client, repos):
    role_id = repos.roles.all_roles()[0]
    reqs = repos.roles.get_requirements(role_id)
    candidates = [r for r in reqs if r["required_level"] >= 2]
    assert candidates, f"role {role_id} has no required_level >= 2"
    target = candidates[0]

    payload = {
        "name": "Quiz vs Self",
        "role_id": role_id,
        "self_rated_skills": {target["skill_id"]: target["required_level"]},
        "quiz_verified_skills": {target["skill_id"]: 1},
    }
    resp = client.post("/employees/compute", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    gap = next(g for g in body["gaps"] if g["skill_id"] == target["skill_id"])
    assert gap["current_level"] == 1  # quiz beats self-rating
    assert gap["required_level"] == target["required_level"]
    assert gap["gap"] == target["required_level"] - 1


def test_compute_s014_unmatched_gap(client, repos):
    """Stage D behaviour on the on-the-fly path: an S014 gap on a new
    employee must surface the explicit no_courses_available report."""
    role_id = _role_with_skill(repos, "S014")
    payload = {
        "name": "Cloud Novice",
        "role_id": role_id,
        "self_rated_skills": {},  # no levels -> every requirement is a gap
    }
    resp = client.post("/employees/compute", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    s014 = [u for u in body["unmatched_gaps"] if u["skill_id"] == "S014"]
    assert s014
    assert s014[0]["status"] == "no_courses_available"
    assert "Cloud Computing" in s014[0]["message"]

    # No course may pretend to cover S014.
    for rec in body["recommendations"]:
        assert "S014" not in rec["matched_skills"]


def test_compute_matches_stored_employee(client, repos, gap_service):
    """Same inputs -> identical output to the lookup endpoints."""
    employee = repos.employees.all()[0]
    emp_id = employee["employee_id"]
    payload = {
        "employee_id": emp_id,
        "name": employee["name"],
        "role_id": employee["role_id"],
        "self_rated_skills": employee["_self_rated"],
        "quiz_verified_skills": employee["_quiz_verified"],
        "previous_trainings": employee["_previous_trainings"],
    }
    resp = client.post("/employees/compute", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    expected_gaps = gap_service.compute_gaps(emp_id) or []
    assert body["gaps"] == expected_gaps

    stored = client.get(f"/employees/{emp_id}/recommendations").json()
    assert body["recommendations"] == stored["recommendations"]
    assert body["unmatched_gaps"] == stored["unmatched_gaps"]


def test_compute_respects_top_n(client, repos):
    role_id = repos.roles.all_roles()[0]
    reqs = repos.roles.get_requirements(role_id)
    payload = {
        "role_id": role_id,
        "self_rated_skills": {r["skill_id"]: 1 for r in reqs},
    }
    resp = client.post("/employees/compute?top_n=2", json=payload)
    assert resp.status_code == 200
    assert len(resp.json()["recommendations"]) <= 2


def test_compute_unknown_role_is_422(client):
    resp = client.post("/employees/compute", json={"role_id": "R999"})
    assert resp.status_code == 422


def test_compute_unknown_skill_is_422(client):
    resp = client.post(
        "/employees/compute",
        json={"role_id": "R001", "self_rated_skills": {"S099": 2}},
    )
    assert resp.status_code == 422
    assert "S099" in resp.json()["detail"]


def test_compute_out_of_range_level_is_422(client):
    resp = client.post(
        "/employees/compute",
        json={"role_id": "R001", "self_rated_skills": {"S001": 4}},
    )
    assert resp.status_code == 422
