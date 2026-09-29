"""
Unit tests for Stage B - skill-gap calculation.
"""

from __future__ import annotations

import pytest


def test_unknown_employee_returns_none(gap_service):
    assert gap_service.compute_gaps("E9999") is None


def test_gap_shape_and_ordering(gap_service, repos):
    """A typical employee's gaps must have the documented shape and be sorted
    largest-gap-first."""
    gaps = gap_service.compute_gaps("E001")
    assert gaps is not None
    assert gaps, "E001 should have at least one gap"

    for gap in gaps:
        assert set(gap) == {
            "skill_id",
            "skill_name",
            "current_level",
            "required_level",
            "gap",
            "priority_weight",
        }
        assert gap["gap"] > 0
        assert gap["required_level"] == gap["current_level"] + gap["gap"]
        # skill_id must resolve in the taxonomy
        assert repos.skills.get(gap["skill_id"]) is not None

    gap_sizes = [g["gap"] for g in gaps]
    assert gap_sizes == sorted(gap_sizes, reverse=True)


def test_quiz_verified_preferred_over_self_rated(gap_service):
    """E001: S002 self-rated=1 but quiz-verified=2 -> current level must be 2."""
    gaps = gap_service.compute_gaps("E001")
    s002 = next((g for g in gaps if g["skill_id"] == "S002"), None)
    if s002 is not None:
        assert s002["current_level"] == 2, "quiz-verified level must win over self-rating"
        assert s002["required_level"] == s002["current_level"] + s002["gap"]


def test_someone_meets_all_requirements_has_empty_gaps(gap_service, repos):
    """Stage F sanity 1 (service level): a fully-qualified employee -> empty
    gap list, not an error."""
    candidates = []
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps(employee["employee_id"])
        if not gaps:
            candidates.append(employee["employee_id"])
            break

    assert candidates, "Expected at least one employee with zero gaps"
    assert gap_service.compute_gaps(candidates[0]) == []


def test_every_employee_has_valid_role_and_skills(gap_service, repos):
    """Defensive: no employee should crash the gap calculation."""
    errors = []
    for employee in repos.employees.all():
        try:
            gap_service.compute_gaps(employee["employee_id"])
        except Exception as exc:  # noqa: BLE001
            errors.append((employee["employee_id"], repr(exc)))
    assert not errors, f"{len(errors)} employees failed gap computation: {errors[:3]}"


@pytest.mark.parametrize("employee_id", ["E001", "E100", "E500"])
def test_spot_check_known_employees(gap_service, employee_id):
    gaps = gap_service.compute_gaps(employee_id)
    assert gaps is not None
    assert isinstance(gaps, list)
