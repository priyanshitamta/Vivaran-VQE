"""
Stage B - skill-gap calculation service.

Given an employee_id:
  1. Resolve their role and that role's required skills + levels (Dataset 2).
  2. Read the employee's current levels (Dataset 3), preferring
     quiz-verified over self-rated per the spec.
  3. gap = required_level - current_level; keep only gap > 0.
  4. Return gaps sorted largest first, with priority_weight attached.

The output objects match the ``Gap`` schema (see app/models/schemas.py).
"""

from __future__ import annotations

from typing import Optional

from app.data.repositories import Repositories


class GapService:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos

    def compute_gaps(self, employee_id: str) -> Optional[list[dict]]:
        """
        Return the sorted gap list for an employee, or ``None`` if the
        employee does not exist (the API layer maps that to a 404).

        An employee whose skills already meet/exceed all requirements yields
        an empty list - valid, not an error.
        """
        employee = self._repos.employees.get(employee_id)
        if employee is None:
            return None
        return self.compute_gaps_for_employee(employee)

    def compute_gaps_for_employee(self, employee: dict) -> list[dict]:
        """
        The shared core: compute gaps for an arbitrary employee record dict.

        This powers both ``compute_gaps`` (Dataset-3 lookup) and the
        on-the-fly compute endpoint for employees who are NOT in the dataset.
        The record only needs ``role_id``, ``_self_rated`` and
        ``_quiz_verified`` keys, in the same parsed shape the repositories
        produce - so the exact same pipeline runs for a brand-new employee
        with no Dataset-3 row.
        """
        role_id = employee["role_id"]
        gaps: list[dict] = []

        for req in self._repos.roles.get_requirements(role_id):
            skill_id: str = req["skill_id"]
            required_level: int = req["required_level"]

            current_level = self._repos.employees.current_level(employee, skill_id)
            # Defensive: a skill with no recorded level is treated as level 0,
            # i.e. the full requirement is a gap. Mirrors the spec's
            # "gap = required - current" with current absent.
            if current_level is None:
                current_level = 0

            gap = required_level - current_level
            if gap > 0:
                skill = self._repos.skills.get(skill_id)
                gaps.append(
                    {
                        "skill_id": skill_id,
                        "skill_name": skill["skill_name"] if skill else skill_id,
                        "current_level": current_level,
                        "required_level": required_level,
                        "gap": gap,
                        "priority_weight": req["priority_weight"],
                    }
                )

        # Largest gap first; ties broken by higher priority weight, then by
        # skill_id for a deterministic order.
        gaps.sort(key=lambda g: (-g["gap"], -g["priority_weight"], g["skill_id"]))
        return gaps
