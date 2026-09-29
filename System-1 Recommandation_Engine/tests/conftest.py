"""
Shared test fixtures.

The real datasets are loaded once per test session (session-scoped) and shared
across tests. ``client`` is a FastAPI ``TestClient`` over the assembled app
with Tier 2 embeddings force-disabled (embeddings need the heavy
sentence-transformers install and are tested separately when available).

Helper fixtures scan the dataset for the edge-case employees the Stage-F
sanity checks need (an already-qualified employee; an employee gapped on the
intentionally-uncovered skill S014). These are computed lazily so a test run
does not pay for them unless a test asks.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.data.loader import load_all
from app.data.repositories import Repositories
from app.main import create_app


@pytest.fixture(scope="session")
def repos() -> Repositories:
    return Repositories.build(load_all())


@pytest.fixture(scope="session")
def client() -> TestClient:
    app = create_app(enable_embeddings=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def gap_service(repos):
    from app.services.gap_service import GapService

    return GapService(repos)


@pytest.fixture(scope="session")
def employee_with_no_gaps(repos, gap_service) -> str:
    """Any employee whose skills already meet/exceed every requirement."""
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if not gaps:
            return employee["employee_id"]
    pytest.skip("No employee with zero gaps exists in the dataset")


@pytest.fixture(scope="session")
def employee_with_s014_gap(repos, gap_service) -> str:
    """An employee with a gap in S014 (Cloud Computing - zero course coverage)."""
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if any(g["skill_id"] == "S014" for g in gaps):
            return employee["employee_id"]
    pytest.skip("No employee gapped on S014 exists in the dataset")


@pytest.fixture(scope="session")
def typical_employee(repos, gap_service) -> str:
    """An employee with several gaps and at least one covered skill."""
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps(employee["employee_id"])
        covered = [g for g in gaps if repos.courses.has_skill(g["skill_id"])]
        if len(gaps) >= 2 and covered:
            return employee["employee_id"]
    pytest.skip("No suitable typical employee found in the dataset")
