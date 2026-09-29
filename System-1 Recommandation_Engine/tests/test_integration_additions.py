"""
Tests for the SYSTEM1_INTEGRATION_UPGRADE additions:
  - Addition 1: fresh skill-level reads (quiz-verified updates picked up on
    the next call, no restart)
  - Addition 2: single call + batch call are both cheap (structural checks)
  - Addition 3: GET /employees/recommendations/batch for the Admin Dashboard
  - Addition 4-7: already covered elsewhere; status field, key naming,
    /health, no-enrollment checks are asserted lightly here too.
"""

from __future__ import annotations

import shutil

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """
    An app whose Dataset-3 (synthetic) and Dataset-5 (real) are redirected to
    temp copies, so tests that WRITE skill updates never touch the real repo
    data files.
    """
    # Dataset-3 -> temp copy (must exist; used by skill-update persistence).
    d3_tmp = tmp_path / "Dataset-3_Syn_Employee_Profiles.csv"
    shutil.copy(config.DATASET_FILES["employees"], d3_tmp)
    monkeypatch.setitem(config.DATASET_FILES, "employees", d3_tmp)

    # Dataset-5 -> fresh empty temp file (grows during the test only).
    d5_tmp = tmp_path / "Dataset-5_Real_Employee_Profiles.csv"
    monkeypatch.setitem(config.DATASET_FILES, "real_employees", d5_tmp)

    app = create_app(enable_embeddings=False)
    with TestClient(app) as c:
        yield c


REGISTER_PAYLOAD = {
    "name": "Integration Tester",
    "role_id": "R001",
    "department": "NSO",
    "self_rated_skills": {"S001": 1, "S002": 1},
}


# ---------------------------------------------------------------------------
# Addition 1 - fresh skill-level reads (no stale startup snapshot)
# ---------------------------------------------------------------------------


def test_quiz_verified_update_reflected_on_next_call(isolated_app):
    """System 2 pushes a quiz-verified level -> the very next gaps call
    reflects it, without a restart."""
    resp = isolated_app.post("/employees/register", json=REGISTER_PAYLOAD)
    emp_id = resp.json()["employee"]["employee_id"]

    # Before the update, S002 (Communication, required 3) is a gap of 2
    # (current 1 from self-rating).
    before = isolated_app.get(f"/employees/{emp_id}/gaps").json()
    before_s002 = next(g for g in before if g["skill_id"] == "S002")
    assert before_s002["current_level"] == 1

    # System 2 verifies S002 by quiz at level 3.
    upd = isolated_app.post(
        f"/employees/{emp_id}/skills",
        json={"quiz_verified_skills": {"S002": 3}},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["quiz_verified_skills"]["S002"] == 3

    # Next call: no longer a gap (level 3 meets the requirement).
    after = isolated_app.get(f"/employees/{emp_id}/gaps").json()
    assert all(g["skill_id"] != "S002" for g in after)


def test_skill_update_persists_to_disk(isolated_app):
    """The update must survive a restart: re-load the app and the level is
    still there (persisted to Dataset-5, not just memory)."""
    emp_id = isolated_app.post("/employees/register", json=REGISTER_PAYLOAD).json()[
        "employee"
    ]["employee_id"]

    isolated_app.post(
        f"/employees/{emp_id}/skills",
        json={"quiz_verified_skills": {"S001": 3}},
    )

    # Fresh app built from the same (temp) files. Assert on the stored
    # profile rather than /gaps: raising a skill to level 3 can close the gap
    # entirely, so it may legitimately vanish from the gaps list.
    with TestClient(create_app(enable_embeddings=False)) as c2:
        profile = c2.get(f"/employees/{emp_id}/profile").json()
    assert profile["quiz_verified_skills"]["S001"] == 3  # not the original 1


def test_skill_update_synthetic_employee_persists(isolated_app):
    """Dataset-3 synthetic employees can be updated too, and the write goes
    to the (temp) Dataset-3 file."""
    # E001 is a real synthetic employee.
    resp = isolated_app.post(
        "/employees/E001/skills",
        json={"quiz_verified_skills": {"S001": 3}},
    )
    assert resp.status_code == 200, resp.text

    # The temp Dataset-3 copy now carries the packed update.
    df = pd.read_csv(config.DATASET_FILES["employees"], dtype=str)
    row = df[df["employee_id"] == "E001"].iloc[0]
    assert "S001:3" in row["quiz_verified_skills"]


def test_skill_update_validation(isolated_app):
    assert (
        isolated_app.post("/employees/E001/skills", json={}).status_code == 422
    )
    assert (
        isolated_app.post(
            "/employees/E001/skills", json={"quiz_verified_skills": {"S001": 5}}
        ).status_code
        == 422
    )
    assert (
        isolated_app.post(
            "/employees/NOPE/skills",
            json={"quiz_verified_skills": {"S001": 3}},
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Addition 3 - batch endpoint for the Admin Dashboard
# ---------------------------------------------------------------------------


def test_batch_returns_all_employees(isolated_app):
    isolated_app.post("/employees/register", json=REGISTER_PAYLOAD)
    resp = isolated_app.get("/employees/recommendations/batch")
    assert resp.status_code == 200
    batch = resp.json()
    # 805 synthetic + the one just registered.
    assert len(batch) == 806
    ids = {entry["employee_id"] for entry in batch}
    assert "E001" in ids
    assert any(i.startswith("E80") for i in ids)


def test_batch_entry_matches_single_employee_endpoint(isolated_app):
    """Each batch row must equal what the per-employee endpoints return."""
    batch = isolated_app.get("/employees/recommendations/batch").json()
    sample = next(entry for entry in batch if entry["employee_id"] == "E001")

    gaps = isolated_app.get("/employees/E001/gaps").json()
    recs = isolated_app.get("/employees/E001/recommendations").json()

    assert sample["gaps"] == gaps
    assert sample["recommendations"] == recs["recommendations"]
    assert sample["unmatched_gaps"] == recs["unmatched_gaps"]


def test_batch_top_n_respected(isolated_app):
    batch = isolated_app.get(
        "/employees/recommendations/batch?top_n=2"
    ).json()
    for entry in batch:
        assert len(entry["recommendations"]) <= 2


def test_batch_response_keys_match_datasets(isolated_app):
    """Addition 5 - JSON keys must match the CSV column names exactly."""
    entry = isolated_app.get("/employees/recommendations/batch").json()[0]
    assert "employee_id" in entry and "gaps" in entry and "recommendations" in entry

    gap_keys = {"skill_id", "skill_name", "current_level", "required_level",
                "gap", "priority_weight"}
    for gap in entry["gaps"]:
        assert set(gap) == gap_keys

    rec_keys = {"course_id", "course_title", "description", "target_level",
                "duration_minutes", "mode", "provider", "language",
                "matched_skills", "score"}
    for rec in entry["recommendations"]:
        assert set(rec) == rec_keys


def test_unmatched_gap_has_machine_readable_status(isolated_app):
    """Addition 4 - zero-coverage gaps carry a status field, not just text."""
    # Find any employee gapped on S014 (Cloud Computing, zero course coverage).
    batch = isolated_app.get("/employees/recommendations/batch").json()
    s014_entries = [e for e in batch if any(
        g["skill_id"] == "S014" for g in e["gaps"]
    )]
    assert s014_entries, "No employee gapped on S014 in the dataset"

    unmatched = s014_entries[0]["unmatched_gaps"]
    s014_unmatched = [u for u in unmatched if u["skill_id"] == "S014"]
    assert s014_unmatched
    assert s014_unmatched[0]["status"] == "no_courses_available"
    assert "message" in s014_unmatched[0]


# ---------------------------------------------------------------------------
# Addition 2 & 6 & 7 - health + performance smoke checks
# ---------------------------------------------------------------------------


def test_health_fast_and_informative(isolated_app):
    import time

    start = time.perf_counter()
    health = isolated_app.get("/health").json()
    elapsed = time.perf_counter() - start
    assert health["status"] == "ok"
    assert elapsed < 1.0  # trivial endpoint, must stay fast


def test_no_enrollment_quiz_auth_fields(isolated_app):
    """Addition 7 - we must NOT expose enrollment/quiz/auth state of our own."""
    batch = isolated_app.get("/employees/recommendations/batch").json()[0]
    assert "enrolled" not in batch["recommendations"][0]
    assert "auth" not in batch and "enrollment" not in batch
