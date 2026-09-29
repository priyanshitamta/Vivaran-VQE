"""
Repository layer - the single access point for all dataset lookups.

Each repository wraps one cleaned DataFrame from ``loader.load_all()`` and
exposes the lookups the services need. Parsing of packed fields (self-rated
skills, quiz-verified skills, previous trainings, course tags) happens once
here at construction time, so business logic never touches raw strings.

All lookups return ``None`` / empty containers for missing keys rather than
raising, so callers can branch on "not found" naturally. The API layer turns
those into 404s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from app.data.loader import parse_csv_list, parse_skill_levels


def _normalise_row(row: dict) -> dict:
    """
    Clean one raw CSV row dict before it enters the repository.

    Blank cells in a CSV load from pandas as ``float('nan')``. That is fine
    while the row is a DataFrame, but ``float('nan')`` then fails Pydantic's
    string validation when an employee's profile is serialised back out
    (Dataset-5 rows legitimately leave optional fields empty). Replace NaN
    with ``None`` so the parsed records are clean.
    """
    return {
        key: (None if isinstance(value, float) and pd.isna(value) else value)
        for key, value in row.items()
    }


@dataclass
class SkillRepository:
    """Dataset-1: the 16-skill taxonomy (reference table)."""

    _by_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "SkillRepository":
        return cls(_by_id={row["skill_id"]: row for row in df.to_dict("records")})

    def get(self, skill_id: str) -> Optional[dict[str, Any]]:
        return self._by_id.get(skill_id)

    def get_name(self, skill_id: str) -> Optional[str]:
        skill = self._by_id.get(skill_id)
        return skill["skill_name"] if skill else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._by_id.values())


@dataclass
class RoleRepository:
    """Dataset-2: required competency per role (R001-R060)."""

    # role_id -> list of requirement dicts, pre-joined with skill metadata.
    _requirements: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _meta: dict[str, dict[str, str]] = field(default_factory=dict)  # designation/dept

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "RoleRepository":
        requirements: dict[str, list[dict[str, Any]]] = {}
        meta: dict[str, dict[str, str]] = {}
        for row in df.to_dict("records"):
            role_id: str = row["role_id"]
            requirements.setdefault(role_id, []).append(row)
            meta[role_id] = {
                "designation": row["designation"],
                "department": row["department"],
            }
        return cls(_requirements=requirements, _meta=meta)

    def has_role(self, role_id: str) -> bool:
        return role_id in self._requirements

    def get_requirements(self, role_id: str) -> list[dict[str, Any]]:
        """All required skills for a role, each row a dict with
        skill_id / required_level / priority_weight (+ designation/dept)."""
        return self._requirements.get(role_id, [])

    def get_meta(self, role_id: str) -> Optional[dict[str, str]]:
        """designation/department for a role (used to fill in profile blanks
        when the caller does not supply them)."""
        return self._meta.get(role_id)

    def all_roles(self) -> list[str]:
        """Every known role_id, sorted - useful for role pickers on the
        frontend intake form."""
        return sorted(self._requirements)


@dataclass
class EmployeeRepository:
    """Dataset-3 + Dataset-5: synthetic + real employee profiles, kept separate."""

    _synthetic: dict[str, dict[str, Any]] = field(default_factory=dict)
    _real: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_frames(
        cls, synthetic_df: pd.DataFrame, real_df: pd.DataFrame
    ) -> "EmployeeRepository":
        synthetic = {}
        for row in synthetic_df.to_dict("records"):
            row = _normalise_row(dict(row))
            row["_self_rated"] = parse_skill_levels(row.get("self_rated_skills") or "")
            row["_quiz_verified"] = parse_skill_levels(
                row.get("quiz_verified_skills") or ""
            )
            row["_previous_trainings"] = parse_csv_list(
                row.get("previous_trainings") or ""
            )
            synthetic[row["employee_id"]] = row

        real = {}
        for row in real_df.to_dict("records"):
            row = _normalise_row(dict(row))
            row["_self_rated"] = parse_skill_levels(row.get("self_rated_skills") or "")
            row["_quiz_verified"] = parse_skill_levels(
                row.get("quiz_verified_skills") or ""
            )
            row["_previous_trainings"] = parse_csv_list(
                row.get("previous_trainings") or ""
            )
            real[row["employee_id"]] = row

        return cls(_synthetic=synthetic, _real=real)

    def get(self, employee_id: str) -> Optional[dict[str, Any]]:
        """Lookup in both stores (real takes precedence if IDs ever collide)."""
        return self._real.get(employee_id) or self._synthetic.get(employee_id)

    def all_synthetic(self) -> list[dict[str, Any]]:
        return list(self._synthetic.values())

    def all_real(self) -> list[dict[str, Any]]:
        return list(self._real.values())

    def all(self) -> list[dict[str, Any]]:
        """Combined view — real + synthetic together."""
        return list(self._real.values()) + list(self._synthetic.values())

    def current_level(self, employee: dict[str, Any], skill_id: str) -> Optional[int]:
        """Quiz-verified levels preferred over self-ratings."""
        quiz = employee.get("_quiz_verified", {})
        if skill_id in quiz:
            return quiz[skill_id]
        return employee.get("_self_rated", {}).get(skill_id)

    def add_real(self, employee: dict[str, Any]) -> None:
        """Hot-add a newly registered employee (already parsed) so it is
        immediately visible to every lookup without a restart."""
        self._real[employee["employee_id"]] = employee

    def source(self, employee_id: str) -> Optional[str]:
        """Which store owns an employee: ``"real"`` (Dataset-5) or
        ``"synthetic"`` (Dataset-3). Used when persisting updates back to the
        right CSV."""
        if employee_id in self._real:
            return "real"
        if employee_id in self._synthetic:
            return "synthetic"
        return None

    def update_skills(
        self,
        employee_id: str,
        self_rated: Optional[dict[str, int]] = None,
        quiz_verified: Optional[dict[str, int]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Hot-update an employee's skill levels in memory (no restart needed).

        ``self_rated`` / ``quiz_verified`` are merged over the existing
        levels, so System 2 can push a quiz result and the very next gap call
        picks it up. Returns the updated record, or ``None`` if the employee
        does not exist. Callers persist to disk separately (see
        ``loader.update_employee_row``).
        """
        employee = self.get(employee_id)
        if employee is None:
            return None
        if self_rated:
            employee["_self_rated"].update(self_rated)
        if quiz_verified:
            employee["_quiz_verified"].update(quiz_verified)
        return employee

    def next_id(self) -> str:
        """Next sequential employee id in the E### space. Starts at E806 once
        the 805 synthetic rows are loaded; skips any taken id automatically."""
        used = set(self._synthetic) | set(self._real)
        n = 806
        while f"E{n}" in used:
            n += 1
        return f"E{n}"


@dataclass
class CourseRepository:
    """Dataset-4: course catalogue (C001-C784)."""

    _by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    # skill_id -> list of course rows that carry that skill tag.
    _by_skill: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "CourseRepository":
        by_id: dict[str, dict[str, Any]] = {}
        by_skill: dict[str, list[dict[str, Any]]] = {}
        for row in df.to_dict("records"):
            row = dict(row)
            row["_tags"] = parse_csv_list(row.get("skill_tags") or "")
            by_id[row["course_id"]] = row
            for tag in row["_tags"]:
                by_skill.setdefault(tag, []).append(row)
        return cls(_by_id=by_id, _by_skill=by_skill)

    def get(self, course_id: str) -> Optional[dict[str, Any]]:
        return self._by_id.get(course_id)

    def has_skill(self, skill_id: str) -> bool:
        """True when at least one course carries this skill tag."""
        return skill_id in self._by_skill

    def get_courses_for_skill(self, skill_id: str) -> list[dict[str, Any]]:
        """All courses tagged with the given skill (empty list if none)."""
        return self._by_skill.get(skill_id, [])

    def all(self) -> list[dict[str, Any]]:
        return list(self._by_id.values())


@dataclass
class Repositories:
    """Container bundling all four repositories (built once at startup)."""

    skills: SkillRepository
    roles: RoleRepository
    employees: EmployeeRepository
    courses: CourseRepository

    @classmethod
    def build(cls, loaded: dict[str, pd.DataFrame]) -> "Repositories":
        return cls(
            skills=SkillRepository.from_frame(loaded["skills"]),
            roles=RoleRepository.from_frame(loaded["required_competency"]),
            employees=EmployeeRepository.from_frames(
                loaded["employees"],          # Dataset-3: synthetic (805)
                loaded["real_employees"],     # Dataset-5: real (grows)
            ),
            courses=CourseRepository.from_frame(loaded["courses"]),
        )
