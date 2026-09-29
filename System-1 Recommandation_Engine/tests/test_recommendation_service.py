"""
Unit tests for Stage C/D - course recommendation + unmatched-gap handling.
"""

from __future__ import annotations

import pytest

from app.services.recommendation_service import RecommendationService


@pytest.fixture(scope="session")
def rec_service(repos):
    return RecommendationService(repos, embeddings=None)


def test_unknown_employee_returns_none(rec_service):
    assert rec_service.recommend("E9999") is None


def test_recommendations_respect_top_n(rec_service, repos, gap_service):
    employee_id = repos.employees.all()[0]["employee_id"]
    if not gap_service.compute_gaps(employee_id):
        pytest.skip("first employee has no gaps; pick another")

    recs, unmatched = rec_service.recommend(employee_id, top_n=3)
    assert len(recs) <= 3
    for r in recs:
        assert set(r) == {
            "course_id",
            "course_title",
            "description",
            "target_level",
            "duration_minutes",
            "mode",
            "provider",
            "language",
            "matched_skills",
            "score",
        }


def test_recommendations_only_cover_gap_skills(rec_service, repos, gap_service):
    """Every recommended course must be tagged with at least one of the
    employee's gap skills (Tier 1 candidate set is tag-based)."""
    for employee in repos.employees.all()[:50]:
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if not gaps:
            continue
        gap_skills = {g["skill_id"] for g in gaps}
        recs, _ = rec_service.recommend(employee["employee_id"])
        for r in recs:
            assert set(r["matched_skills"]) & gap_skills, (
                f"course {r['course_id']} recommended for {employee['employee_id']} "
                f"but tags {r['matched_skills']} don't intersect gaps {gap_skills}"
            )


def test_no_courses_for_skill_gives_empty_recs(rec_service, repos, gap_service):
    """An employee whose ONLY gaps are uncovered skills gets no fabricated
    courses - just the Stage D report."""
    found = None
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if gaps and all(not repos.courses.has_skill(g["skill_id"]) for g in gaps):
            found = employee["employee_id"]
            break
    if found is None:
        pytest.skip("No employee with exclusively-uncovered gaps exists")

    recs, unmatched = rec_service.recommend(found)
    assert recs == []
    assert unmatched, "must report the uncovered gaps, not silently omit them"


def test_s014_gap_produces_unmatched_entry(rec_service, repos, gap_service):
    """Stage D: any S014 gap must surface the explicit no_courses_available
    block with the exact status string."""
    employee_id = None
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if any(g["skill_id"] == "S014" for g in gaps):
            employee_id = employee["employee_id"]
            break
    assert employee_id is not None, "expected at least one S014-gapped employee"

    recs, unmatched = rec_service.recommend(employee_id)
    s014 = next((u for u in unmatched if u["skill_id"] == "S014"), None)
    assert s014 is not None
    assert s014["status"] == "no_courses_available"
    assert "Cloud Computing" in s014["message"]


def test_no_gaps_means_no_recommendations(rec_service, repos, gap_service):
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if not gaps:
            recs, unmatched = rec_service.recommend(employee["employee_id"])
            assert recs == []
            assert unmatched == []
            return
    pytest.skip("No employee with zero gaps exists in the dataset")


def test_scores_descending(rec_service, repos, gap_service):
    for employee in repos.employees.all()[:30]:
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if not gaps:
            continue
        recs, _ = rec_service.recommend(employee["employee_id"], top_n=10)
        scores = [r["score"] for r in recs]
        assert scores == sorted(scores, reverse=True), (
            f"recommendations for {employee['employee_id']} not ranked by score"
        )
