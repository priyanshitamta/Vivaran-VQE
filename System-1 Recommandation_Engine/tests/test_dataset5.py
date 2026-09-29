"""
Tests for Dataset-5 persistence + dual-segment admin analytics.

The real Dataset-5 path is monkeypatched to a per-test temp file, so tests
never write to the repo. Each test builds a fresh app so in-memory state
(reset per create_app) and the file (reset per tmp_path) stay in sync.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config
from app.data.loader import load_all
from app.data.repositories import Repositories
from app.main import create_app


@pytest.fixture
def dataset5_path(tmp_path, monkeypatch):
    path = tmp_path / "Dataset-5_Real_Employee_Profiles.csv"
    monkeypatch.setitem(config.DATASET_FILES, "real_employees", path)
    return path


@pytest.fixture
def app_client(dataset5_path):
    app = create_app(enable_embeddings=False)
    with TestClient(app) as c:
        yield c


PAYLOAD = {
    "employee_id": "E-NEW-01",  # caller-supplied id must be honoured
    "name": "Real Person",
    "role_id": "R001",
    "department": "NSO",
    "work_experience_years": 6,
    "self_rated_skills": {"S001": 2, "S002": 1},
    "quiz_verified_skills": {"S001": 2},
}


def test_register_persists_to_dataset5(app_client, dataset5_path):
    resp = app_client.post("/employees/register", json=PAYLOAD)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # Assigned id + profile echo
    assert body["employee"]["employee_id"] == "E-NEW-01"
    assert body["employee"]["name"] == "Real Person"
    assert body["employee"]["department"] == "NSO"
    assert isinstance(body["gaps"], list)
    assert isinstance(body["recommendations"], list)

    # File created on disk with header + the row
    assert dataset5_path.exists()
    df = pd.read_csv(dataset5_path, dtype=str)
    assert list(df.columns)[0] == "employee_id"
    assert len(df) == 1
    assert df.iloc[0]["employee_id"] == "E-NEW-01"
    assert df.iloc[0]["self_rated_skills"] == "S001:2;S002:1"


def test_registered_employee_immediately_queryable(app_client):
    resp = app_client.post("/employees/register", json=PAYLOAD)
    assert resp.status_code == 201
    emp_id = resp.json()["employee"]["employee_id"]

    # Hot-added: GET lookup works without a restart.
    gaps = app_client.get(f"/employees/{emp_id}/gaps")
    assert gaps.status_code == 200
    profile = app_client.get(f"/employees/{emp_id}/profile")
    assert profile.status_code == 200
    assert profile.json()["name"] == "Real Person"


def test_auto_id_assignment(app_client):
    """No employee_id supplied -> next in the E### sequence (E806)."""
    payload = {k: v for k, v in PAYLOAD.items() if k != "employee_id"}
    resp = app_client.post("/employees/register", json=payload)
    assert resp.status_code == 201
    assert resp.json()["employee"]["employee_id"] == "E806"


def test_duplicate_employee_id_is_409(app_client):
    assert app_client.post("/employees/register", json=PAYLOAD).status_code == 201
    resp = app_client.post("/employees/register", json=PAYLOAD)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_analytics_segments_stay_separate(app_client):
    # Baseline: synthetic 805, real 0, combined 805.
    assert app_client.get("/analytics/synthetic").json()["count"] == 805
    assert app_client.get("/analytics/real").json()["count"] == 0
    assert app_client.get("/analytics/combined").json()["count"] == 805

    app_client.post("/employees/register", json=PAYLOAD)

    # After registration: synthetic unchanged, real = 1, combined = 806.
    synth = app_client.get("/analytics/synthetic").json()
    real = app_client.get("/analytics/real").json()
    combined = app_client.get("/analytics/combined").json()
    assert synth["count"] == 805
    assert real["count"] == 1
    assert combined["count"] == 806

    # The real segment exposes the same shape (segment label differs).
    assert real["segment"] == "real"
    assert "by_department" in real and "by_role" in real


def test_registrations_log(app_client):
    app_client.post("/employees/register", json=PAYLOAD)
    log = app_client.get("/analytics/registrations").json()
    assert len(log) == 1
    entry = log[0]
    assert entry["employee_id"] == "E-NEW-01"
    assert entry["name"] == "Real Person"
    assert entry["role_id"] == "R001"
    assert entry["registered_at"]  # timestamp present


def test_health_reflects_real_count(app_client):
    assert app_client.get("/health").json()["employees_real"] == 0
    app_client.post("/employees/register", json=PAYLOAD)
    health = app_client.get("/health").json()
    assert health["employees_synthetic"] == 805
    assert health["employees_real"] == 1
    assert health["employees_loaded"] == 806


def test_register_matches_stored_employee_path(app_client):
    """Same inputs -> identical gaps as the compute/GET path."""
    resp = app_client.post("/employees/register", json=PAYLOAD)
    body = resp.json()

    repo_gaps = None
    repos = Repositories.build(load_all())
    emp = repos.employees.get("E-NEW-01")
    if emp:
        from app.services.gap_service import GapService

        repo_gaps = GapService(repos).compute_gaps_for_employee(emp)
    assert body["gaps"] == repo_gaps
