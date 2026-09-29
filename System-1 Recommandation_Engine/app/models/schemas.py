"""
Pydantic response schemas - the stable JSON contract served by the API.

These shapes are the integration contract for the frontend: once deployed,
clients build against them, so field names and types here should be treated
as public API. They follow the exact output shapes defined in the spec
(Stage B gap objects, Stage D unmatched-gap objects, course recommendations).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app import config


class Gap(BaseModel):
    """One positive skill gap (Stage B output)."""

    skill_id: str
    skill_name: str
    current_level: int
    required_level: int
    gap: int = Field(ge=1)  # only positive gaps reach this model
    priority_weight: int = Field(ge=1, le=3)


class UnmatchedGap(BaseModel):
    """
    A gap skill with zero course coverage (Stage D - required behaviour).

    Returned alongside recommendations instead of being silently dropped.
    """

    skill_id: str
    skill_name: str
    status: str = "no_courses_available"
    message: str


class Recommendation(BaseModel):
    """One recommended course (Stage C output)."""

    course_id: str
    course_title: str
    description: str
    target_level: int
    duration_minutes: int
    mode: str
    provider: str
    language: str
    matched_skills: list[str] = Field(
        description="Gap skills this course covers, in descending priority order"
    )
    score: float = Field(description="Composite ranking score (higher = better)")


class RecommendationResponse(BaseModel):
    """
    Top-level shape for ``/recommendations``.

    ``recommendations`` holds the ranked courses; ``unmatched_gaps`` reports
    every gap skill with no course coverage (never silently omitted).
    """

    employee_id: str
    recommendations: list[Recommendation]
    unmatched_gaps: list[UnmatchedGap]


class EmployeeProfile(BaseModel):
    """Raw employee profile for frontend display (Stage E)."""

    employee_id: str
    name: str
    role_id: str
    designation: str
    department: str
    current_assignment: Optional[str] = None
    educational_qualifications: Optional[str] = None
    work_experience_years: Optional[int] = None
    previous_trainings: list[str] = Field(default_factory=list)
    self_rated_skills: dict[str, int] = Field(default_factory=dict)
    quiz_verified_skills: dict[str, int] = Field(default_factory=dict)


class NewEmployeeInput(BaseModel):
    """
    Full employee information accepted by the on-the-fly compute endpoint.

    Approach A for new-employee intake during full-stack integration: no row
    in Dataset-3 is required. The caller supplies the person's role and their
    current skill levels (self-rated, plus quiz-verified where a quiz
    exists), and the same Stage B/C/D pipeline runs directly on this payload.
    """

    employee_id: Optional[str] = None  # host-system id, if one exists yet
    name: Optional[str] = None
    role_id: str  # R001-R060; must exist in Dataset-2
    designation: Optional[str] = None  # falls back to the role's designation
    department: Optional[str] = None  # falls back to the role's department
    current_assignment: Optional[str] = None
    educational_qualifications: Optional[str] = None
    work_experience_years: Optional[int] = Field(default=None, ge=0, le=80)
    previous_trainings: list[str] = Field(default_factory=list)
    self_rated_skills: dict[str, int] = Field(default_factory=dict)
    quiz_verified_skills: dict[str, int] = Field(default_factory=dict)

    @field_validator("self_rated_skills", "quiz_verified_skills")
    @classmethod
    def _validate_skill_levels(cls, skills: dict[str, int]) -> dict[str, int]:
        for skill_id, level in skills.items():
            if not (1 <= level <= config.MAX_LEVEL):
                raise ValueError(
                    f"{skill_id}: level must be 1..{config.MAX_LEVEL}, got {level}"
                )
        return skills


class ComputeResponse(BaseModel):
    """
    The complete on-the-fly result (POST /employees/compute).

    Echoes the submitted profile back - the frontend sees exactly what the
    system computed from - and returns the Stage B gaps, Stage C ranked
    recommendations, and Stage D unmatched-gap report. Everything a
    full-stack result screen needs in one round trip.
    """

    employee: EmployeeProfile
    gaps: list[Gap] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    unmatched_gaps: list[UnmatchedGap] = Field(default_factory=list)


class SkillUpdateInput(BaseModel):
    """
    System-2 integration: updated skill levels for an existing employee.

    System 2 (the quiz generator) verifies skills by quiz; the result must
    raise an employee's ``quiz_verified_skills`` so the next gap call reflects
    it - no restart, no stale startup snapshot. At least one of the two
    fields must be present; levels are validated to 1..MAX_LEVEL.
    """

    self_rated_skills: Optional[dict[str, int]] = None
    quiz_verified_skills: Optional[dict[str, int]] = None

    @field_validator("self_rated_skills", "quiz_verified_skills")
    @classmethod
    def _validate_skill_levels(cls, skills) -> Optional[dict[str, int]]:
        if not skills:
            return skills
        for skill_id, level in skills.items():
            if not (1 <= level <= config.MAX_LEVEL):
                raise ValueError(
                    f"{skill_id}: level must be 1..{config.MAX_LEVEL}, got {level}"
                )
        return skills


class BatchRecommendationEntry(BaseModel):
    """
    One row of the admin batch rollup (GET /employees/recommendations/batch).

    System 3's Admin Dashboard calls this once instead of looping ~805
    individual requests. Mirrors what the per-employee endpoints return, so
    the frontend renders a batch row exactly like a single-employee result.
    """

    employee_id: str
    gaps: list[Gap] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    unmatched_gaps: list[UnmatchedGap] = Field(default_factory=list)
